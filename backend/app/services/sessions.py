from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.provenance import (
    append_provenance_edge,
    bump_context_generation,
    canonical_digest,
    ensure_provenance_node,
    provenance_descendant_node_ids,
    read_context_generation,
    transition_memory,
)
from app.db.uow import flush as flush_uow
from app.models import (
    AgentRun,
    ChatMessage,
    ContextSnapshot,
    Memory,
    Plan,
    PlanProposal,
    PlanningIntake,
    ProvenanceNode,
    Session,
    SessionCompressionState,
    SessionHandoff,
    SessionPlanLink,
    SessionSummary,
)


class SessionHandoffConflict(RuntimeError):
    """The durable source/target handoff identity is ambiguous or incomplete."""


async def link_session_plan(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str,
    plan_id: int,
    relation_type: str,
    source_run_id: str | None = None,
) -> SessionPlanLink:
    link = (await db.execute(
        select(SessionPlanLink).where(
            SessionPlanLink.session_id == session_id,
            SessionPlanLink.plan_id == plan_id,
            SessionPlanLink.relation_type == relation_type,
        )
    )).scalars().one_or_none()
    if link is None:
        link = SessionPlanLink(
            owner_id=owner_id,
            session_id=session_id,
            plan_id=plan_id,
            relation_type=relation_type,
            source_run_id=source_run_id,
        )
        db.add(link)
        await flush_uow(db)
    elif source_run_id and link.source_run_id is None:
        link.source_run_id = source_run_id
    return link


async def build_handoff_summary(db: AsyncSession, session: Session) -> str:
    messages = list((await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )).scalars())
    messages = [
        message for message in messages
        if message.validity_state == "active"
        and not message.message_metadata.get("superseded_by_edit")
    ][:6]
    current_summary = (await db.execute(
        select(SessionSummary)
        .where(
            SessionSummary.session_id == session.id,
            SessionSummary.validity_state == "valid",
        )
        .order_by(SessionSummary.version.desc())
        .limit(1)
    )).scalars().one_or_none()
    excerpts = [
        f"{message.role}: {' '.join(message.content.split())[:360]}"
        for message in reversed(messages)
    ]
    parts = [
        f"由对话《{session.title}》转入。",
        current_summary.content.strip() if current_summary is not None else "",
        "最近上下文：\n" + "\n".join(excerpts) if excerpts else "",
    ]
    return "\n".join(part for part in parts if part)[:5000]


def _timestamp_version(value: datetime | None) -> int:
    if value is None:
        return 1
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(1, int(value.timestamp() * 1_000_000))


async def _handoff_source_nodes(
    db: AsyncSession,
    *,
    source: Session,
    plan: Plan,
) -> list[ProvenanceNode]:
    """Materialize exact, owner-scoped sources used by one frozen handoff."""

    nodes: dict[str, ProvenanceNode] = {}
    messages = list((await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.session_id == source.id,
            ChatMessage.validity_state == "active",
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )).scalars())
    messages = [
        message
        for message in messages
        if not (message.message_metadata or {}).get("superseded_by_edit")
    ][:6]
    for message in reversed(messages):
        digest = message.content_hash or canonical_digest(message.content)
        if not message.content_hash:
            message.content_hash = digest
            await flush_uow(db)
        node = await ensure_provenance_node(
            db,
            owner_id=source.owner_id,
            plan_id=source.plan_id,
            session_id=source.id,
            kind="message",
            entity_key=message.id,
            entity_version=message.version,
            content_digest=digest,
        )
        nodes[node.id] = node

    summary = (await db.execute(
        select(SessionSummary)
        .where(
            SessionSummary.owner_id == source.owner_id,
            SessionSummary.session_id == source.id,
            SessionSummary.validity_state == "valid",
        )
        .order_by(SessionSummary.version.desc())
        .limit(1)
    )).scalars().one_or_none()
    if summary is not None:
        node = await ensure_provenance_node(
            db,
            owner_id=source.owner_id,
            plan_id=source.plan_id,
            session_id=source.id,
            kind="session_summary",
            entity_key=summary.id,
            entity_version=summary.version,
            content_digest=canonical_digest(summary.content),
        )
        nodes[node.id] = node

    intake = await db.get(PlanningIntake, source.id)
    if intake is not None and intake.owner_id == source.owner_id:
        node = await ensure_provenance_node(
            db,
            owner_id=source.owner_id,
            plan_id=source.plan_id,
            session_id=source.id,
            kind="planning_intake",
            entity_key=intake.session_id,
            entity_version=_timestamp_version(intake.updated_at),
            content_digest=canonical_digest({
                "goal": intake.goal,
                "confirmed_facts": intake.confirmed_facts,
                "open_questions": intake.open_questions,
                "readiness": intake.readiness,
                "readiness_confidence": intake.readiness_confidence,
                "rationale": intake.rationale,
                "source_run_id": intake.source_run_id,
            }),
        )
        nodes[node.id] = node

    proposal = (await db.execute(
        select(PlanProposal)
        .where(
            PlanProposal.owner_id == source.owner_id,
            PlanProposal.session_id == source.id,
            PlanProposal.plan_id == plan.id,
            PlanProposal.status == "accepted",
        )
        .order_by(PlanProposal.decided_at.desc(), PlanProposal.created_at.desc())
        .limit(1)
    )).scalars().one_or_none()
    if proposal is not None:
        node = await ensure_provenance_node(
            db,
            owner_id=source.owner_id,
            plan_id=plan.id,
            session_id=source.id,
            kind="plan_proposal",
            entity_key=proposal.id,
            entity_version=_timestamp_version(proposal.updated_at),
            content_digest=canonical_digest({
                "title": proposal.title,
                "rationale": proposal.rationale,
                "plan_payload": proposal.plan_payload,
                "specialist_reports": proposal.specialist_reports,
                "status": proposal.status,
                "plan_id": proposal.plan_id,
            }),
        )
        nodes[node.id] = node

    run_ids = {
        message.run_id for message in messages if message.run_id is not None
    }
    if intake is not None and intake.source_run_id is not None:
        run_ids.add(intake.source_run_id)
    if proposal is not None and proposal.source_run_id is not None:
        run_ids.add(proposal.source_run_id)
    if run_ids:
        runs = list((await db.execute(
            select(AgentRun).where(
                AgentRun.id.in_(run_ids),
                AgentRun.owner_id == source.owner_id,
                AgentRun.session_id == source.id,
            )
        )).scalars())
        for run in runs:
            if run.plan_id not in {None, plan.id}:
                continue
            node = await ensure_provenance_node(
                db,
                owner_id=source.owner_id,
                plan_id=run.plan_id,
                session_id=source.id,
                kind="agent_run",
                entity_key=run.id,
                entity_version=1,
                content_digest=canonical_digest({
                    "id": run.id,
                    "trigger": run.trigger,
                    "objective": run.objective,
                    "created_at": run.created_at,
                }),
            )
            nodes[node.id] = node
    return sorted(nodes.values(), key=lambda value: (value.kind, value.entity_key, value.entity_version))


async def ensure_session_handoff(
    db: AsyncSession,
    *,
    source: Session,
    target: Session,
    plan: Plan,
    content: str,
) -> SessionHandoff:
    """Create one immutable verified handoff, or return its exact replay."""

    existing = list((await db.execute(
        select(SessionHandoff)
        .where(SessionHandoff.target_session_id == target.id)
        .order_by(SessionHandoff.version)
    )).scalars())
    if len(existing) > 1:
        raise SessionHandoffConflict("multiple handoff facts target the same active Session")
    if existing:
        handoff = existing[0]
        if (
            handoff.owner_id != source.owner_id
            or handoff.source_session_id != source.id
            or handoff.plan_id != plan.id
            or handoff.content != content
            or handoff.content_hash != canonical_digest(content)
            or handoff.validity_state != "valid"
        ):
            raise SessionHandoffConflict("existing handoff identity does not match the Session")
        return handoff

    generation = await read_context_generation(db, source.owner_id)
    handoff = SessionHandoff(
        owner_id=source.owner_id,
        source_session_id=source.id,
        target_session_id=target.id,
        plan_id=plan.id,
        version=1,
        content=content,
        content_hash=canonical_digest(content),
        source_context_generation=generation,
        provenance_state="verified",
        validity_state="building",
    )
    db.add(handoff)
    await flush_uow(db)
    target_node = await ensure_provenance_node(
        db,
        owner_id=source.owner_id,
        plan_id=plan.id,
        session_id=target.id,
        kind="session_handoff",
        entity_key=handoff.id,
        entity_version=handoff.version,
        content_digest=handoff.content_hash,
    )
    sources = await _handoff_source_nodes(db, source=source, plan=plan)
    if not sources:
        raise SessionHandoffConflict("handoff source has no durable provenance")
    for ordinal, source_node in enumerate(sources):
        await append_provenance_edge(
            db,
            owner_id=source.owner_id,
            source_node_id=source_node.id,
            target_node_id=target_node.id,
            relation="handoff_source",
            ordinal=ordinal,
        )
    handoff.provenance_node_id = target_node.id
    handoff.validity_state = "valid"
    await flush_uow(db)
    return handoff


async def invalidate_message_edit_derivations(
    db: AsyncSession,
    *,
    owner_id: str,
    session: Session,
    message: ChatMessage,
    previous_version: int,
    previous_content_hash: str,
    previous_run_id: str | None,
    downstream_messages: Sequence[ChatMessage],
    changed_at: datetime,
) -> int:
    """Invalidate the exact transitive closure of one edited message.

    Legacy scalar/manifest references may cause conservative invalidation but
    are never promoted into verified graph state by the edit path.
    """

    generation = await bump_context_generation(db, owner_id)
    message_nodes: dict[int, ProvenanceNode] = {}
    old_message_node = await ensure_provenance_node(
        db,
        owner_id=owner_id,
        plan_id=session.plan_id,
        session_id=session.id,
        kind="message",
        entity_key=message.id,
        entity_version=previous_version,
        content_digest=previous_content_hash,
    )
    message_nodes[message.id] = old_message_node
    impacted_message_ids = {message.id}
    for downstream in downstream_messages:
        impacted_message_ids.add(downstream.id)
        digest = downstream.content_hash or canonical_digest(downstream.content)
        if not downstream.content_hash:
            downstream.content_hash = digest
            await flush_uow(db)
        node = await ensure_provenance_node(
            db,
            owner_id=owner_id,
            plan_id=session.plan_id,
            session_id=session.id,
            kind="message",
            entity_key=downstream.id,
            entity_version=downstream.version,
            content_digest=digest,
        )
        message_nodes[downstream.id] = node

    all_runs = list((await db.execute(
        select(AgentRun).where(AgentRun.owner_id == owner_id)
    )).scalars())
    run_by_id = {run.id: run for run in all_runs}
    by_parent: dict[str, list[AgentRun]] = {}
    for run in all_runs:
        if run.parent_run_id is not None:
            by_parent.setdefault(run.parent_run_id, []).append(run)
    seed_run_ids = {
        run_id
        for run_id in [previous_run_id, *(item.run_id for item in downstream_messages)]
        if run_id is not None
    }
    previous_run = run_by_id.get(previous_run_id) if previous_run_id else None
    if previous_run is not None:
        seed_run_ids.update(
            run.id
            for run in all_runs
            if run.session_id == session.id
            and run.parent_run_id is None
            and run.created_at >= previous_run.created_at
        )
    impacted_run_ids = set(seed_run_ids)
    frontier = list(seed_run_ids)
    while frontier:
        for child in by_parent.get(frontier.pop(), []):
            if child.id not in impacted_run_ids:
                impacted_run_ids.add(child.id)
                frontier.append(child.id)

    run_nodes: dict[str, ProvenanceNode] = {}
    for run_id in sorted(impacted_run_ids):
        run = run_by_id.get(run_id)
        if run is None:
            continue
        node = await ensure_provenance_node(
            db,
            owner_id=owner_id,
            plan_id=run.plan_id,
            session_id=run.session_id,
            kind="agent_run",
            entity_key=run.id,
            entity_version=1,
            content_digest=canonical_digest({
                "id": run.id,
                "trigger": run.trigger,
                "objective": run.objective,
                "created_at": run.created_at,
            }),
        )
        run_nodes[run.id] = node

    summaries = list((await db.execute(
        select(SessionSummary).where(
            SessionSummary.owner_id == owner_id,
            SessionSummary.session_id == session.id,
        )
    )).scalars())
    directly_impacted_summary_ids: set[int] = set()
    for summary in summaries:
        source_ids: list[int] = []
        for value in summary.source_message_ids or []:
            try:
                source_id = int(value)
            except (TypeError, ValueError):
                continue
            if source_id not in source_ids:
                source_ids.append(source_id)
        if impacted_message_ids.intersection(source_ids):
            directly_impacted_summary_ids.add(summary.id)

    memories = list((await db.execute(
        select(Memory).where(Memory.owner_id == owner_id)
    )).scalars())
    directly_impacted_memory_ids: set[int] = set()
    for memory in memories:
        if (
            memory.source_type in {"agent_run", "run"}
            and memory.source_id in impacted_run_ids
        ):
            directly_impacted_memory_ids.add(memory.id)
        elif memory.source_type in {"message", "chat_message"}:
            try:
                source_message_id = int(memory.source_id or "")
            except (TypeError, ValueError):
                source_message_id = -1
            if source_message_id in impacted_message_ids:
                directly_impacted_memory_ids.add(memory.id)
        elif memory.source_type == "session_summary":
            try:
                source_summary_id = int(memory.source_id or "")
            except (TypeError, ValueError):
                source_summary_id = -1
            if source_summary_id in directly_impacted_summary_ids:
                directly_impacted_memory_ids.add(memory.id)

    snapshots = list((await db.execute(
        select(ContextSnapshot).where(ContextSnapshot.owner_id == owner_id)
    )).scalars())
    directly_impacted_snapshot_ids: set[int] = set()
    for snapshot in snapshots:
        if snapshot.provenance_node_id is not None:
            continue
        # Legacy JSON is never promoted into verified graph state during an
        # edit. Exact explicit references may still conservatively invalidate
        # the legacy fact; ranges and inferred containment are forbidden.
        for item in [
            *(snapshot.source_manifest or []),
            *(snapshot.dropped_source_manifest or []),
        ]:
            if not isinstance(item, dict):
                continue
            source_type = str(item.get("type") or "")
            source_key = item.get("id")
            try:
                numeric_key = int(source_key)
            except (TypeError, ValueError):
                numeric_key = -1
            directly_impacted = (
                source_type in {"message", "chat_message"}
                and numeric_key in impacted_message_ids
            ) or (
                source_type == "session_summary"
                and numeric_key in directly_impacted_summary_ids
            ) or (
                source_type == "memory" and numeric_key in directly_impacted_memory_ids
            ) or (
                source_type in {"agent_run", "run"}
                and source_key is not None
                and str(source_key) in impacted_run_ids
            )
            if directly_impacted:
                directly_impacted_snapshot_ids.add(snapshot.id)
                break

    seed_node_ids = {
        *(
            node.id
            for message_id, node in message_nodes.items()
            if message_id in impacted_message_ids
        ),
        *(node.id for node in run_nodes.values()),
    }
    descendant_ids = await provenance_descendant_node_ids(
        db,
        owner_id=owner_id,
        source_node_ids=sorted(seed_node_ids),
    )
    descendant_nodes = (
        list((await db.execute(
            select(ProvenanceNode).where(ProvenanceNode.id.in_(descendant_ids))
        )).scalars())
        if descendant_ids
        else []
    )
    entity_ids_by_kind: dict[str, set[str]] = {}
    for node in descendant_nodes:
        entity_ids_by_kind.setdefault(node.kind, set()).add(node.entity_key)

    invalid_reason = "source_message_edited"
    for summary in summaries:
        if (
            str(summary.id) in entity_ids_by_kind.get("session_summary", set())
            or summary.id in directly_impacted_summary_ids
        ) and summary.validity_state in {"valid", "legacy_unverified"}:
            summary.validity_state = "invalid"
            summary.invalidated_at = changed_at
            summary.invalidation_reason = invalid_reason

    impacted_memory_ids = {
        int(value) for value in entity_ids_by_kind.get("memory", set())
    } | directly_impacted_memory_ids
    for memory in memories:
        if memory.id not in impacted_memory_ids:
            continue
        if memory.validity_state in {"invalid", "review_required"}:
            continue
        source_scope_matches = (
            memory.scope == "session" and memory.scope_id == session.id
        ) or (
            memory.scope == "plan"
            and session.plan_id is not None
            and memory.scope_id == str(session.plan_id)
        )
        await transition_memory(
            db,
            owner_id=owner_id,
            memory_id=memory.id,
            expected_version=memory.lifecycle_version,
            event_type="invalidated",
            action_key=f"message-edit:{message.id}:v{previous_version + 1}:memory:{memory.id}",
            to_status="archived",
            to_validity_state="invalid",
            reason_code=invalid_reason,
            archived_from_status=memory.status,
            archived_reason="来源消息已被用户修订",
            source_run_id=previous_run_id if source_scope_matches else None,
            source_message_id=message.id if source_scope_matches else None,
            changed_at=changed_at,
        )

    for snapshot in snapshots:
        if (
            (
                str(snapshot.id) in entity_ids_by_kind.get("context_snapshot", set())
                or snapshot.id in directly_impacted_snapshot_ids
            )
            and snapshot.validity_state in {"valid", "legacy_unverified"}
        ):
            snapshot.validity_state = "invalid"
            snapshot.invalidated_at = changed_at
            snapshot.invalidation_reason = invalid_reason

    handoffs = list((await db.execute(
        select(SessionHandoff).where(SessionHandoff.owner_id == owner_id)
    )).scalars())
    for handoff in handoffs:
        if (
            (
                str(handoff.id) in entity_ids_by_kind.get("session_handoff", set())
                or (
                    handoff.validity_state == "legacy_unverified"
                    and handoff.source_session_id == session.id
                )
            )
            and handoff.validity_state in {"valid", "legacy_unverified"}
        ):
            handoff.validity_state = "invalid"
            handoff.invalidated_at = changed_at
            handoff.invalidation_reason = invalid_reason

    compression = await db.get(SessionCompressionState, session.id)
    if compression is not None:
        compression.covered_through_message_id = None
        compression.covered_through_message_version = None
        compression.generation += 1
        compression.claim_token = None
        compression.claim_owner = None
        compression.claim_generation = None
        compression.claim_start_message_id = None
        compression.claim_end_message_id = None
        compression.claim_base_summary_id = None
        compression.claim_source_manifest = None
        compression.claim_source_digest = None
        compression.claim_algorithm_version = None
        compression.claim_started_at = None
        compression.claim_expires_at = None
        compression.updated_at = changed_at
    await flush_uow(db)
    return generation
