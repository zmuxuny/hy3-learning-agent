from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.api.agent as agent_api
from app.api.agent import edit_user_message
from app.context.assembler import ContextAssembler
from app.context.provenance import canonical_digest
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    ChatMessage,
    ChatMessageRevision,
    ContextSnapshot,
    Intervention,
    Memory,
    Notification,
    Plan,
    ProvenanceNode,
    Session,
    SessionSummary,
)
from app.notifications.conversation import materialize_intervention_message
from app.schemas import MessageEdit


@pytest.mark.asyncio
async def test_first_same_content_edit_is_explicit_noop_without_version_guard_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    content = "unchanged canonical user message"
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="same-content edit")
        db.add(session)
        await db.flush()
        message = ChatMessage(
            session_id=session.id,
            role="user",
            content=content,
            version=1,
            content_hash=canonical_digest(content),
        )
        db.add(message)
        await db.commit()
        message_id = message.id
        session_id = session.id

        with pytest.raises(HTTPException) as exc_info:
            await edit_user_message(message_id, MessageEdit(content=content), db)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Edited content is unchanged"

    async with AsyncSessionLocal() as db:
        stored = await db.get(ChatMessage, message_id)
        revision_count = await db.scalar(
            select(func.count(ChatMessageRevision.id)).where(
                ChatMessageRevision.message_id == message_id
            )
        )
        run_count = await db.scalar(
            select(func.count(AgentRun.id)).where(AgentRun.session_id == session_id)
        )
        assert stored is not None
        assert stored.content == content
        assert stored.version == 1
        assert stored.content_hash == canonical_digest(content)
        assert revision_count == 0
        assert run_count == 0


@pytest.mark.asyncio
async def test_legacy_unverified_memory_is_never_materialized_as_context_provenance() -> None:
    marker = "LEGACY_UNVERIFIED_CONTEXT_SENTINEL"
    async with AsyncSessionLocal() as db:
        memory = Memory(
            owner_id="local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content=marker,
            source_type="user",
            status="confirmed",
            validity_state="legacy_unverified",
        )
        db.add(memory)
        await db.commit()
        memory_id = memory.id

        snapshot = await ContextAssembler(db).build(
            "local",
            objective=marker,
            prompt_system="fixture system",
            prompt_tools=[],
        )
        await db.commit()

    assert marker not in snapshot.markdown
    assert all(
        not (
            item.get("type") == "memory"
            and str(item.get("id")) == str(memory_id)
        )
        for item in [*snapshot.source_manifest, *snapshot.dropped_source_manifest]
    )
    async with AsyncSessionLocal() as db:
        node = await db.scalar(
            select(ProvenanceNode).where(
                ProvenanceNode.owner_id == "local",
                ProvenanceNode.kind == "memory",
                ProvenanceNode.entity_key == str(memory_id),
            )
        )
        assert node is None


@pytest.mark.asyncio
async def test_context_limits_logical_interventions_before_delivery_rows() -> None:
    shared_created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="multi-channel interventions")
        db.add(session)
        await db.flush()
        interventions: list[Intervention] = []
        for index in range(12):
            title = f"LOGICAL_INTERVENTION_{index:02d}"
            body = f"logical body {index}"
            intervention = Intervention(
                owner_id="local",
                session_id=session.id,
                plan_id=None,
                title=title,
                body=body,
                content_digest=canonical_digest({"title": title, "body": body}),
                reason_code="fixture",
                state="building",
                created_at=shared_created_at,
            )
            db.add(intervention)
            await db.flush()
            await materialize_intervention_message(
                db,
                session=session,
                intervention=intervention,
            )
            for channel in ("in_app", "browser", "email"):
                db.add(
                    Notification(
                        owner_id="local",
                        intervention_id=intervention.id,
                        delivery_generation=1,
                        legacy_unlinked=False,
                        session_id=session.id,
                        plan_id=None,
                        channel=channel,
                        title=title,
                        body=body,
                        status="sent",
                        reply_token=intervention.reply_token,
                        created_at=shared_created_at,
                    )
                )
            interventions.append(intervention)
        await db.commit()
        expected_ids = {
            item.id for item in sorted(interventions, key=lambda item: item.id, reverse=True)[:10]
        }

        snapshot = await ContextAssembler(db).build(
            "local",
            objective="logical notification limit",
            prompt_system="fixture system",
            prompt_tools=[],
        )
        await db.commit()

    intervention_entries = [
        item for item in snapshot.source_manifest if item.get("type") == "intervention"
    ]
    assert len(intervention_entries) == 10
    assert {str(item["id"]) for item in intervention_entries} == expected_ids
    assert "[email/" not in snapshot.markdown
    assert "[browser/" not in snapshot.markdown
    assert "[in_app/" not in snapshot.markdown


@pytest.mark.asyncio
async def test_edit_invalidates_exact_legacy_facts_without_promoting_json_to_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="private legacy source")
        db.add(plan)
        await db.flush()
        session = Session(
            owner_id="local",
            plan_id=plan.id,
            title="legacy edit boundary",
        )
        db.add(session)
        await db.flush()
        source_run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="user_message",
            objective="legacy source",
            status="completed",
            phase="terminal",
        )
        db.add(source_run)
        await db.flush()
        before = ChatMessage(
            session_id=session.id,
            role="assistant",
            content="before",
            version=1,
            content_hash=canonical_digest("before"),
        )
        edited = ChatMessage(
            session_id=session.id,
            run_id=source_run.id,
            role="user",
            content="legacy source",
            version=1,
            content_hash=canonical_digest("legacy source"),
        )
        after = ChatMessage(
            session_id=session.id,
            role="assistant",
            content="after",
            version=1,
            content_hash=canonical_digest("after"),
        )
        db.add_all([before, edited, after])
        await db.flush()
        exact_summary = SessionSummary(
            owner_id="local",
            session_id=session.id,
            version=1,
            content="exact legacy summary",
            coverage_start_message_id=edited.id,
            covered_through_message_id=edited.id,
            coverage_count=1,
            source_message_ids=[edited.id],
            method="legacy",
        )
        range_only_summary = SessionSummary(
            owner_id="local",
            session_id=session.id,
            version=2,
            content="range-only legacy summary",
            coverage_start_message_id=before.id,
            covered_through_message_id=after.id,
            coverage_count=1,
            source_message_ids=[before.id],
            method="legacy",
        )
        legacy_memory = Memory(
            owner_id="local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="legacy global fact from a plan/session-private message",
            source_type="message",
            source_id=str(edited.id),
            status="confirmed",
        )
        db.add_all([exact_summary, range_only_summary, legacy_memory])
        await db.flush()
        exact_snapshot = ContextSnapshot(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            markdown="legacy exact snapshot",
            source_manifest=[{"type": "session_summary", "id": exact_summary.id}],
            estimated_tokens=4,
        )
        unrelated_snapshot = ContextSnapshot(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            markdown="legacy unrelated snapshot",
            source_manifest=[{"type": "session_summary", "id": range_only_summary.id}],
            estimated_tokens=4,
        )
        db.add_all([exact_snapshot, unrelated_snapshot])
        await db.commit()
        edited_id = edited.id
        exact_summary_id = exact_summary.id
        range_summary_id = range_only_summary.id
        exact_snapshot_id = exact_snapshot.id
        unrelated_snapshot_id = unrelated_snapshot.id
        memory_id = legacy_memory.id

        await edit_user_message(
            edited_id,
            MessageEdit(content="corrected source"),
            db,
        )

    async with AsyncSessionLocal() as db:
        exact_summary = await db.get(SessionSummary, exact_summary_id)
        range_summary = await db.get(SessionSummary, range_summary_id)
        exact_snapshot = await db.get(ContextSnapshot, exact_snapshot_id)
        unrelated_snapshot = await db.get(ContextSnapshot, unrelated_snapshot_id)
        memory = await db.get(Memory, memory_id)
        asserted_legacy_keys = {
            ("session_summary", str(exact_summary_id)),
            ("session_summary", str(range_summary_id)),
            ("context_snapshot", str(exact_snapshot_id)),
            ("context_snapshot", str(unrelated_snapshot_id)),
            ("memory", str(memory_id)),
        }
        promoted = list(
            (
                await db.execute(
                    select(ProvenanceNode).where(
                        ProvenanceNode.owner_id == "local",
                        ProvenanceNode.kind.in_(
                            ["session_summary", "context_snapshot", "memory"]
                        ),
                    )
                )
            ).scalars()
        )

        assert exact_summary is not None and exact_summary.validity_state == "invalid"
        assert exact_summary.provenance_node_id is None
        assert range_summary is not None and range_summary.validity_state == "legacy_unverified"
        assert range_summary.provenance_node_id is None
        assert exact_snapshot is not None and exact_snapshot.validity_state == "invalid"
        assert exact_snapshot.provenance_node_id is None
        assert unrelated_snapshot is not None
        assert unrelated_snapshot.validity_state == "legacy_unverified"
        assert unrelated_snapshot.provenance_node_id is None
        assert memory is not None and memory.validity_state == "invalid"
        assert memory.provenance_node_id is None
        assert {
            (node.kind, node.entity_key) for node in promoted
        }.isdisjoint(asserted_legacy_keys)
