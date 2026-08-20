from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.api.agent as agent_api
import app.models as model_module
from app.api.agent import edit_user_message
from app.api.agent import handoff_session
from app.context.memory import MemoryManager
from app.context.provenance import (
    append_provenance_edges,
    canonical_digest,
    ensure_provenance_node,
)
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.context import ContextAssembler
from app.models import (
    CalendarEvent,
    AgentRun,
    ChatMessage,
    ContextSnapshot,
    LearningEvent,
    Memory,
    Notification,
    Plan,
    Quiz,
    ReviewSchedule,
    Session,
    SessionPlanLink,
    SessionSummary,
    Stage,
    Task,
)
from app.schemas import MessageEdit, SessionHandoffCreate


def _require_setup(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"H5 context setup failed: {message}")


class _ChunkCompletions:
    def __init__(self, *, fail_on_call: int | None = None):
        self.fail_on_call = fail_on_call
        self.prompts: list[str] = []

    async def create(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        self.prompts.append(prompt)
        if self.fail_on_call == len(self.prompts):
            raise RuntimeError("injected second summary chunk failure")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"summary-chunk-{len(self.prompts)}")
                )
            ]
        )


class _ChunkClient:
    def __init__(self, *, fail_on_call: int | None = None):
        self.completions = _ChunkCompletions(fail_on_call=fail_on_call)
        self.chat = SimpleNamespace(completions=self.completions)


class _BlockingCompletions:
    def __init__(self, content: str = "blocking summary"):
        self.content = content
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, **_kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class _BlockingClient:
    def __init__(self, content: str = "blocking summary"):
        self.completions = _BlockingCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)


async def _seed_chunked_session(*, title: str, message_count: int = 72) -> tuple[str, list[int], list[str]]:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title=title, summary="stable-before-compression")
        db.add(session)
        await db.flush()
        messages = []
        markers = []
        for index in range(message_count):
            marker = f"H5_CHUNK_{index:03d}_BEGIN|"
            markers.append(marker)
            content = marker + (chr(65 + index % 26) * 6_000) + f"|H5_CHUNK_{index:03d}_END"
            messages.append(
                ChatMessage(
                    session_id=session.id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=content,
                    version=1,
                    content_hash=canonical_digest(content),
                )
            )
        db.add_all(messages)
        await db.flush()
        session_id = session.id
        message_ids = [message.id for message in messages]
        await db.commit()
    return session_id, message_ids, markers


@pytest.mark.asyncio
@pytest.mark.parametrize("relation_type", ["discussed", "created", "focused"])
async def test_global_session_link_relation_never_unlocks_plan_private_blocks(
    relation_type: str,
) -> None:
    now = datetime.now(timezone.utc)
    marker_prefix = f"H5_LINK_{relation_type.upper()}"
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title=f"Global {relation_type} navigation")
        plan = Plan(
            owner_id="local",
            title=f"Allowed compact index {relation_type}",
            goal="Private goal",
            status="active",
        )
        stage = Stage(plan=plan, title="Private stage")
        task = Task(stage=stage, title=f"{marker_prefix}_TASK", status="active")
        db.add_all([session, plan, stage, task])
        await db.flush()
        db.add(
            SessionPlanLink(
                owner_id="local",
                session_id=session.id,
                plan_id=plan.id,
                relation_type=relation_type,
            )
        )
        db.add_all(
            [
                LearningEvent(
                    owner_id="local",
                    plan_id=plan.id,
                    event_type="private.link.fixture",
                    summary=f"{marker_prefix}_EVENT",
                ),
                ReviewSchedule(
                    owner_id="local",
                    plan_id=plan.id,
                    task_id=task.id,
                    due_at=now + timedelta(hours=1),
                    status="scheduled",
                ),
                Quiz(
                    owner_id="local",
                    plan_id=plan.id,
                    task_id=task.id,
                    prompt=f"{marker_prefix}_QUIZ",
                    status="open",
                ),
                Notification(
                    owner_id="local",
                    plan_id=plan.id,
                    title=f"{marker_prefix}_NOTICE_TITLE",
                    body=f"{marker_prefix}_NOTICE_BODY",
                    status="sent",
                ),
                CalendarEvent(
                    owner_id="local",
                    plan_id=plan.id,
                    task_id=task.id,
                    title=f"{marker_prefix}_CALENDAR",
                    starts_at=now + timedelta(days=1),
                    status="scheduled",
                ),
            ]
        )
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            session_id=session.id,
            plan_id=None,
            objective="List my plans without opening private details",
        )

    forbidden_markers = {
        f"{marker_prefix}_TASK",
        f"{marker_prefix}_EVENT",
        f"{marker_prefix}_QUIZ",
        f"{marker_prefix}_NOTICE_TITLE",
        f"{marker_prefix}_NOTICE_BODY",
        f"{marker_prefix}_CALENDAR",
    }
    leaked = sorted(marker for marker in forbidden_markers if marker in snapshot.markdown)
    private_types = {
        "learning_event",
        "review",
        "quiz",
        "notification",
        "calendar_event",
    }
    leaked_manifest = [
        item
        for item in snapshot.source_manifest
        if str(item.get("type")) in private_types
    ]

    assert leaked == [] and leaked_manifest == []


@pytest.mark.asyncio
async def test_multisource_edit_closure_is_recursive_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Multi-source closure", status="active")
        session = Session(owner_id="local", title="Multi-source closure session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        first_run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            trigger="user_message",
            objective="first source",
            status="completed",
            phase="terminal",
        )
        second_run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            trigger="user_message",
            objective="second source",
            status="completed",
            phase="terminal",
        )
        db.add_all([first_run, second_run])
        await db.flush()
        first_content = "H5_MULTI_SOURCE_A"
        second_content = "H5_MULTI_SOURCE_B"
        first_message = ChatMessage(
            session_id=session.id,
            run_id=first_run.id,
            role="user",
            content=first_content,
            version=1,
            content_hash=canonical_digest(first_content),
        )
        second_message = ChatMessage(
            session_id=session.id,
            run_id=second_run.id,
            role="assistant",
            content=second_content,
            version=1,
            content_hash=canonical_digest(second_content),
        )
        db.add_all([first_message, second_message])
        await db.flush()
        source_manifest = [
            {
                "id": first_message.id,
                "version": first_message.version,
                "role": first_message.role,
                "content_digest": first_message.content_hash,
                "source_bytes": len(first_message.content.encode("utf-8")),
            },
            {
                "id": second_message.id,
                "version": second_message.version,
                "role": second_message.role,
                "content_digest": second_message.content_hash,
                "source_bytes": len(second_message.content.encode("utf-8")),
            },
        ]
        summary_content = "Derived jointly from H5_MULTI_SOURCE_A and H5_MULTI_SOURCE_B"
        summary = SessionSummary(
            owner_id="local",
            session_id=session.id,
            version=1,
            content=summary_content,
            coverage_start_message_id=first_message.id,
            covered_through_message_id=second_message.id,
            coverage_count=2,
            source_message_ids=[first_message.id, second_message.id],
            method="model",
            source_digest=canonical_digest(source_manifest),
            content_hash=canonical_digest(summary_content),
            algorithm_version="h5-test-exact-v1",
            validity_state="building",
        )
        db.add(summary)
        await db.flush()
        message_nodes = [
            await ensure_provenance_node(
                db,
                owner_id="local",
                plan_id=plan.id,
                session_id=session.id,
                kind="message",
                entity_key=message.id,
                entity_version=message.version,
                content_digest=message.content_hash,
            )
            for message in (first_message, second_message)
        ]
        summary_node = await ensure_provenance_node(
            db,
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            kind="session_summary",
            entity_key=summary.id,
            entity_version=summary.version,
            content_digest=summary.content_hash,
        )
        await append_provenance_edges(
            db,
            [
                {
                    "owner_id": "local",
                    "source_node_id": node.id,
                    "target_node_id": summary_node.id,
                    "relation": "summary_source",
                    "ordinal": ordinal,
                    "source_bytes": source_manifest[ordinal]["source_bytes"],
                    "read_bytes": source_manifest[ordinal]["source_bytes"],
                    "chunk_count": 1,
                }
                for ordinal, node in enumerate(message_nodes)
            ],
        )
        summary.provenance_node_id = summary_node.id
        summary.validity_state = "valid"
        await db.flush()
        manager = MemoryManager(db)
        memory, reused = await manager.propose(
            "local",
            scope="plan",
            scope_id=str(plan.id),
            layer="semantic",
            content="Memory derived from H5_MULTI_SOURCE_A H5_MULTI_SOURCE_B joint summary",
            source_type="session_summary",
            source_id=str(summary.id),
        )
        _require_setup(not reused, "multi-source Memory unexpectedly deduplicated")
        memory = await manager.confirm("local", memory.id)
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            plan_id=plan.id,
            session_id=session.id,
            objective="H5_MULTI_SOURCE_A H5_MULTI_SOURCE_B joint summary memory",
        )
        retained_keys = {
            (str(item.get("type")), str(item.get("id")))
            for item in snapshot.source_manifest
        }
        _require_setup(
            ("session_summary", str(summary.id)) in retained_keys
            and ("memory", str(memory.id)) in retained_keys,
            "verified multi-source Summary and Memory were not retained in Context",
        )
        await db.commit()
        first_message_id = first_message.id
        second_message_id = second_message.id
        summary_id = summary.id
        memory_id = memory.id
        snapshot_id = snapshot.id

        await edit_user_message(
            first_message_id,
            MessageEdit(content="H5_MULTI_SOURCE_A_CORRECTED"),
            db,
        )

    async with AsyncSessionLocal() as reopened:
        stored_summary = await reopened.get(SessionSummary, summary_id)
        stored_memory = await reopened.get(Memory, memory_id)
        stored_snapshot = await reopened.get(ContextSnapshot, snapshot_id)
        lifecycle_model = getattr(model_module, "MemoryLifecycleEvent", None)
        lifecycle_rows = [] if lifecycle_model is None else list(
            (await reopened.execute(select(lifecycle_model))).scalars()
        )
        edge_model = getattr(model_module, "ProvenanceEdge", None)
        edge_rows = [] if edge_model is None else list(
            (await reopened.execute(select(edge_model))).scalars()
        )

    _require_setup(
        stored_summary is not None and stored_memory is not None and stored_snapshot is not None,
        "audit rows were deleted instead of retained",
    )

    def is_invalid(value) -> bool:
        return (
            getattr(value, "validity_state", None) in {"stale", "invalid", "invalidated", "needs_review"}
            and getattr(value, "invalidated_at", None) is not None
            and bool(getattr(value, "invalidation_reason", ""))
        )

    memory_events = [
        row
        for row in lifecycle_rows
        if str(getattr(row, "memory_id", "")) == str(memory_id)
        and getattr(row, "event_kind", getattr(row, "event_type", None))
        in {"invalidated", "needs_review"}
    ]
    failures: list[str] = []
    if not is_invalid(stored_summary):
        failures.append("derived_summary_not_invalidated")
    if not is_invalid(stored_memory):
        failures.append("derived_memory_not_invalidated")
    if not is_invalid(stored_snapshot):
        failures.append("derived_snapshot_not_invalidated")
    if edge_model is None or not edge_rows:
        failures.append("normalized_multisource_graph_missing")
    if lifecycle_model is None:
        failures.append("memory_lifecycle_audit_missing")
    elif len(memory_events) != 1:
        failures.append(f"memory_invalidation_not_exactly_once:{len(memory_events)}")
    if list(stored_summary.source_message_ids) != [first_message_id, second_message_id]:
        failures.append("immutable_multisource_identity_changed")

    assert failures == []


@pytest.mark.asyncio
async def test_context_budget_selects_whole_typed_blocks_with_exact_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_CONTEXT_TOKEN_BUDGET", 2_000)
    async with AsyncSessionLocal() as db:
        source_run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="Provide verified sources for typed Context budget blocks",
            status="completed",
            phase="terminal",
        )
        db.add(source_run)
        await db.flush()
        manager = MemoryManager(db)
        memories = []
        markers: dict[int, tuple[str, str]] = {}
        for index in range(12):
            begin = f"H5_TYPED_BLOCK_{index:02d}_BEGIN|"
            end = f"|H5_TYPED_BLOCK_{index:02d}_END"
            memory, reused = await manager.propose(
                "local",
                scope="global",
                scope_id=None,
                layer="semantic" if index % 2 == 0 else "episodic",
                content=begin + "budget|" + (chr(65 + index) * 700) + end,
                source_type="agent_run",
                source_id=source_run.id,
            )
            _require_setup(not reused, "typed budget Memory unexpectedly deduplicated")
            memory = await manager.confirm("local", memory.id)
            memories.append(memory)
        await db.flush()
        markers = {
            memory.id: (
                f"H5_TYPED_BLOCK_{index:02d}_BEGIN|",
                f"|H5_TYPED_BLOCK_{index:02d}_END",
            )
            for index, memory in enumerate(memories)
        }
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            objective="H5 typed block budget partition",
        )

    actual_retained = {
        memory_id
        for memory_id, (begin, end) in markers.items()
        if begin in snapshot.markdown and end in snapshot.markdown
    }
    partial = {
        memory_id
        for memory_id, (begin, end) in markers.items()
        if (begin in snapshot.markdown) != (end in snapshot.markdown)
    }
    actual_dropped = set(markers) - actual_retained
    retained_entries = [
        item
        for item in snapshot.source_manifest
        if item.get("type") == "memory" and int(item.get("id")) in markers
    ]
    dropped_entries = [
        item
        for item in (getattr(snapshot, "dropped_source_manifest", None) or [])
        if item.get("type") == "memory" and int(item.get("id")) in markers
    ]
    reported_retained = {int(item["id"]) for item in retained_entries}
    reported_dropped = {int(item["id"]) for item in dropped_entries}
    required_block_fields = {"block_id", "block_type", "estimated_tokens", "priority"}

    _require_setup(bool(actual_retained), "fixture retained no complete Memory block")
    _require_setup(bool(actual_dropped), "fixture dropped no Memory block")
    failures: list[str] = []
    if partial:
        failures.append(f"sources_cut_mid_block:{sorted(partial)}")
    if reported_retained != actual_retained:
        failures.append("retained_manifest_differs_from_rendered_blocks")
    if reported_dropped != actual_dropped:
        failures.append("dropped_manifest_differs_from_omitted_blocks")
    if reported_retained & reported_dropped:
        failures.append("retained_and_dropped_overlap")
    if reported_retained | reported_dropped != set(markers):
        failures.append("candidate_blocks_are_not_fully_partitioned")
    if any(required_block_fields - set(item) for item in retained_entries + dropped_entries):
        failures.append("manifest_entry_is_not_a_typed_budget_block")
    if any(not str(item.get("reason_code") or "").strip() for item in dropped_entries):
        failures.append("dropped_block_has_no_reason_code")
    breakdown = getattr(snapshot, "budget_breakdown", None)
    if not isinstance(breakdown, dict) or not breakdown:
        failures.append("snapshot_has_no_budget_breakdown")
    elif int(breakdown.get("context_tokens") or -1) != snapshot.estimated_tokens:
        failures.append("budget_breakdown_disagrees_with_retained_context")

    # The serialized manifests themselves must remain deterministic and JSON-safe.
    _require_setup(
        bool(json.dumps([*retained_entries, *dropped_entries], sort_keys=True)),
        "typed manifests are not serializable",
    )
    assert failures == []


@pytest.mark.asyncio
async def test_multichunk_failure_rolls_back_summary_and_all_coverage() -> None:
    session_id, message_ids, _ = await _seed_chunked_session(title="Atomic chunk failure")
    client = _ChunkClient(fail_on_call=2)

    compression_raised = False
    compressed = False
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        _require_setup(session is not None, "seeded Session was not durable")
        try:
            compressed = await MemoryManager(db).compress_session(session, client)
            await db.commit()
        except RuntimeError as exc:
            if "injected second summary chunk failure" not in str(exc):
                raise
            compression_raised = True
            await db.rollback()

    async with AsyncSessionLocal() as reopened:
        stored_session = await reopened.get(Session, session_id)
        summaries = list(
            (
                await reopened.execute(
                    select(SessionSummary).where(SessionSummary.session_id == session_id)
                )
            ).scalars()
        )
        messages = list(
            (
                await reopened.execute(
                    select(ChatMessage)
                    .where(ChatMessage.id.in_(message_ids))
                    .order_by(ChatMessage.id)
                )
            ).scalars()
        )

    _require_setup(stored_session is not None, "Session disappeared across reopen")
    _require_setup(len(messages) == len(message_ids), "source messages disappeared")
    failures: list[str] = []
    if len(client.completions.prompts) < 2:
        failures.append("compressor_never_reached_a_second_chunk")
    if not compression_raised and compressed is not False:
        failures.append("mid_chunk_failure_reported_success")
    if summaries:
        failures.append("mid_chunk_failure_committed_summary")
    if stored_session.summary != "stable-before-compression":
        failures.append("mid_chunk_failure_replaced_active_summary")
    if any((message.message_metadata or {}).get("included_in_summary") for message in messages):
        failures.append("mid_chunk_failure_advanced_message_coverage")

    assert failures == []


@pytest.mark.asyncio
async def test_multichunk_success_has_continuous_exact_coverage() -> None:
    session_id, message_ids, markers = await _seed_chunked_session(
        title="Continuous chunk coverage",
        message_count=68,
    )
    client = _ChunkClient()

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        _require_setup(session is not None, "seeded Session was not durable")
        compressed = await MemoryManager(db).compress_session(session, client)
        await db.commit()

    async with AsyncSessionLocal() as reopened:
        summary = (
            await reopened.execute(
                select(SessionSummary).where(SessionSummary.session_id == session_id)
            )
        ).scalars().one_or_none()

    _require_setup(compressed is True, "compression did not run")
    _require_setup(summary is not None, "compression created no durable summary")
    keep = settings.AGENT_RECENT_MESSAGE_LIMIT
    expected_ids = message_ids[:-keep]
    expected_markers = markers[:-keep]
    combined_prompts = "\n".join(client.completions.prompts)
    expected_digest = hashlib.sha256(
        "\n".join(str(message_id) for message_id in expected_ids).encode("utf-8")
    ).hexdigest()

    failures: list[str] = []
    if len(client.completions.prompts) < 2:
        failures.append("history_was_not_read_as_multiple_bounded_chunks")
    missing_markers = [marker for marker in expected_markers if marker not in combined_prompts]
    if missing_markers:
        failures.append(f"covered_messages_not_read:{len(missing_markers)}")
    if list(summary.source_message_ids) != expected_ids:
        failures.append("source_message_ids_are_not_the_exact_continuous_prefix")
    if len(summary.source_message_ids) != len(set(summary.source_message_ids)):
        failures.append("coverage_contains_duplicate_message_ids")
    if getattr(summary, "coverage_start_message_id", None) != expected_ids[0]:
        failures.append("coverage_start_is_not_persisted")
    if summary.covered_through_message_id != expected_ids[-1]:
        failures.append("coverage_end_is_not_the_last_read_message")
    if getattr(summary, "coverage_count", None) != len(expected_ids):
        failures.append("coverage_count_is_not_persisted")
    source_digest = getattr(summary, "source_digest", None)
    if not isinstance(source_digest, str) or len(source_digest) != len(expected_digest):
        failures.append("summary_has_no_source_digest")

    assert failures == []


@pytest.mark.asyncio
async def test_concurrent_compressors_claim_once_and_commit_one_summary() -> None:
    baseline_session_id, baseline_message_ids, _ = await _seed_chunked_session(
        title="Concurrent compression claim baseline",
        message_count=40,
    )
    baseline_client = _ChunkClient()
    async with AsyncSessionLocal() as baseline_db:
        baseline_session = await baseline_db.get(Session, baseline_session_id)
        _require_setup(baseline_session is not None, "baseline Session disappeared")
        _require_setup(
            await MemoryManager(baseline_db).compress_session(
                baseline_session,
                baseline_client,
            )
            is True,
            "single-worker baseline compression did not run",
        )
        await baseline_db.commit()
    baseline_calls = len(baseline_client.completions.prompts)
    _require_setup(baseline_calls >= 1, "single-worker baseline never called the model")

    session_id, message_ids, _ = await _seed_chunked_session(
        title="Concurrent compression claim",
        message_count=40,
    )
    client = _BlockingClient("one claimed summary")

    async def compress_worker() -> dict:
        async with AsyncSessionLocal() as db:
            session = await db.get(Session, session_id)
            _require_setup(session is not None, "worker could not load Session")
            try:
                result = await MemoryManager(db).compress_session(session, client)
                await db.commit()
                return {"result": result, "error": None}
            except Exception as exc:  # an uncontrolled uniqueness/lock race is a domain failure
                await db.rollback()
                return {"result": None, "error": type(exc).__name__}

    first = asyncio.create_task(compress_worker())
    await asyncio.wait_for(client.completions.started.wait(), timeout=5)
    second = asyncio.create_task(compress_worker())
    await asyncio.sleep(0.1)
    client.completions.release.set()
    results = await asyncio.gather(first, second)

    async with AsyncSessionLocal() as reopened:
        summaries = list(
            (
                await reopened.execute(
                    select(SessionSummary).where(SessionSummary.session_id == session_id)
                )
            ).scalars()
        )
        compression_model = getattr(model_module, "SessionCompressionState", None)
        state = None if compression_model is None else await reopened.get(
            compression_model,
            session_id,
        )

    expected_end = message_ids[-settings.AGENT_RECENT_MESSAGE_LIMIT - 1]
    failures: list[str] = []
    if client.completions.calls != baseline_calls:
        failures.append(
            "model_call_chain_was_duplicated:"
            f"baseline={baseline_calls},concurrent={client.completions.calls}"
        )
    if any(result["error"] for result in results):
        failures.append(f"concurrent_worker_leaked_storage_error:{results}")
    if len(summaries) != 1:
        failures.append(f"concurrent_summary_count:{len(summaries)}")
    elif list(summaries[0].source_message_ids) != message_ids[
        :-settings.AGENT_RECENT_MESSAGE_LIMIT
    ]:
        failures.append("concurrent_summary_coverage_is_not_exact")
    if state is None:
        failures.append("durable_compression_state_missing")
    else:
        if getattr(state, "claim_token", None) is not None:
            failures.append("terminal_compression_left_live_claim")
        if getattr(state, "covered_through_message_id", None) != expected_end:
            failures.append("compression_cursor_did_not_advance_exactly_once")
        if int(getattr(state, "generation", 0)) != 1:
            failures.append(f"compression_generation_not_one:{getattr(state, 'generation', None)}")

    assert failures == []


@pytest.mark.asyncio
async def test_message_edit_invalidates_inflight_compression_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    session_id, message_ids, _ = await _seed_chunked_session(
        title="Edit versus compression CAS",
        message_count=40,
    )
    client = _BlockingClient("STALE_PROVIDER_SUMMARY_MUST_NOT_COMMIT")

    async def pending_compression() -> str | None:
        async with AsyncSessionLocal() as db:
            session = await db.get(Session, session_id)
            _require_setup(session is not None, "compressor could not load Session")
            try:
                await MemoryManager(db).compress_session(session, client)
                await db.commit()
                return None
            except Exception as exc:
                await db.rollback()
                return type(exc).__name__

    compression_task = asyncio.create_task(pending_compression())
    await asyncio.wait_for(client.completions.started.wait(), timeout=5)
    async with AsyncSessionLocal() as edit_db:
        await edit_user_message(
            message_ids[0],
            MessageEdit(content="EDITED_WHILE_PROVIDER_WAITED"),
            edit_db,
        )
    client.completions.release.set()
    worker_error = await compression_task

    async with AsyncSessionLocal() as reopened:
        summaries = list(
            (
                await reopened.execute(
                    select(SessionSummary).where(SessionSummary.session_id == session_id)
                )
            ).scalars()
        )
        target = await reopened.get(ChatMessage, message_ids[0])
        compression_model = getattr(model_module, "SessionCompressionState", None)
        state = None if compression_model is None else await reopened.get(
            compression_model,
            session_id,
        )

    _require_setup(target is not None, "edited source message disappeared")
    failures: list[str] = []
    if any(summary.content == "STALE_PROVIDER_SUMMARY_MUST_NOT_COMMIT" for summary in summaries):
        failures.append("stale_provider_summary_committed_after_edit")
    if worker_error not in {None, "CompressionClaimLost", "CompressionSourceChanged"}:
        failures.append(f"edit_CAS_leaked_untyped_error:{worker_error}")
    if target.content != "EDITED_WHILE_PROVIDER_WAITED":
        failures.append("edit_was_lost")
    if state is None:
        failures.append("compression_state_missing_after_edit_race")
    elif getattr(state, "claim_token", None) is not None:
        failures.append("edit_race_left_stale_compression_claim")

    assert failures == []


@pytest.mark.asyncio
async def test_concurrent_handoff_creates_one_child_and_one_immutable_fact() -> None:
    async with AsyncSessionLocal() as setup_db:
        plan = Plan(owner_id="local", title="Concurrent handoff", status="active")
        source = Session(owner_id="local", title="Concurrent handoff source")
        setup_db.add_all([plan, source])
        await setup_db.flush()
        source_content = "HANDOFF_CONCURRENT_SOURCE"
        setup_db.add(
            ChatMessage(
                session_id=source.id,
                role="user",
                content=source_content,
                version=1,
                content_hash=canonical_digest(source_content),
            )
        )
        await setup_db.commit()
        source_id = source.id
        plan_id = plan.id

    async def handoff_worker() -> dict:
        async with AsyncSessionLocal() as db:
            try:
                result = await handoff_session(
                    source_id,
                    SessionHandoffCreate(plan_id=plan_id),
                    db,
                )
                return {"child_id": result["id"], "error": None}
            except Exception as exc:
                await db.rollback()
                return {"child_id": None, "error": type(exc).__name__}

    results = await asyncio.gather(handoff_worker(), handoff_worker())
    async with AsyncSessionLocal() as reopened:
        children = list(
            (
                await reopened.execute(
                    select(Session).where(
                        Session.owner_id == "local",
                        Session.parent_session_id == source_id,
                        Session.plan_id == plan_id,
                    )
                )
            ).scalars()
        )
        handoff_model = getattr(model_module, "SessionHandoff", None)
        handoffs = [] if handoff_model is None else list(
            (await reopened.execute(select(handoff_model))).scalars()
        )

    failures: list[str] = []
    if any(result["error"] for result in results):
        failures.append(f"concurrent_handoff_leaked_storage_error:{results}")
    if len(children) != 1:
        failures.append(f"child_session_count:{len(children)}")
    if len({result["child_id"] for result in results}) != 1:
        failures.append("concurrent_requests_returned_different_child_identity")
    if handoff_model is None or len(handoffs) != 1:
        failures.append(f"immutable_handoff_fact_count:{len(handoffs)}")
    elif getattr(handoffs[0], "target_session_id", None) != children[0].id:
        failures.append("handoff_fact_does_not_target_idempotent_child")

    assert failures == []


@pytest.mark.asyncio
async def test_source_edit_marks_handoff_stale_without_rewriting_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Handoff stale", status="active")
        source = Session(owner_id="local", title="Handoff stale source")
        db.add_all([plan, source])
        await db.flush()
        source_content = "HANDOFF_SOURCE_BEFORE_EDIT"
        source_message = ChatMessage(
            session_id=source.id,
            role="user",
            content=source_content,
            version=1,
            content_hash=canonical_digest(source_content),
        )
        db.add(source_message)
        await db.commit()
        child = await handoff_session(
            source.id,
            SessionHandoffCreate(plan_id=plan.id),
            db,
        )
        frozen_text = child["handoff_summary"]
        source_message_id = source_message.id
        child_id = child["id"]

        await edit_user_message(
            source_message_id,
            MessageEdit(content="HANDOFF_SOURCE_AFTER_EDIT"),
            db,
        )

    async with AsyncSessionLocal() as reopened:
        stored_child = await reopened.get(Session, child_id)
        handoff_model = getattr(model_module, "SessionHandoff", None)
        handoffs = [] if handoff_model is None else list(
            (await reopened.execute(select(handoff_model))).scalars()
        )

    _require_setup(stored_child is not None, "handoff child disappeared")
    failures: list[str] = []
    if stored_child.handoff_summary != frozen_text:
        failures.append("legacy_handoff_text_was_rewritten")
    if len(handoffs) != 1:
        failures.append(f"immutable_handoff_fact_count:{len(handoffs)}")
    else:
        handoff = handoffs[0]
        if getattr(handoff, "content", getattr(handoff, "handoff_summary", None)) != frozen_text:
            failures.append("immutable_handoff_content_changed")
        if getattr(handoff, "validity_state", None) not in {"stale", "invalid"}:
            failures.append("source_edit_did_not_mark_handoff_stale")
        if getattr(handoff, "invalidated_at", None) is None:
            failures.append("stale_handoff_has_no_invalidation_time")

    assert failures == []
