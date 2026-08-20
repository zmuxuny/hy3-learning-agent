from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.context.memory as memory_module
from app.context.memory import MemoryManager
from app.context.provenance import (
    MemoryLifecycleConflict,
    canonical_digest,
    read_context_generation,
    transition_memory,
)
from app.db.database import AsyncSessionLocal
from app.main import app
from app.models import (
    AgentRun,
    ChatMessage,
    ContextState,
    Memory,
    MemoryLifecycleEvent,
    Plan,
    ProvenanceEdge,
    ProvenanceNode,
    Session,
    SessionCompressionState,
    SessionSummary,
)


class _Summaries:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1]["content"])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"summary-{len(self.prompts)}")
                )
            ]
        )


class _Client:
    def __init__(self) -> None:
        self.completions = _Summaries()
        self.chat = SimpleNamespace(completions=self.completions)


def _chat_message(*, session_id: int, role: str, content: str, **kwargs) -> ChatMessage:
    return ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        version=1,
        content_hash=canonical_digest(content),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_context_generation_read_establishes_durable_zero_row() -> None:
    async with AsyncSessionLocal() as db:
        assert await read_context_generation(db, "local") == 0
        await db.commit()
    async with AsyncSessionLocal() as reopened:
        state = await reopened.get(ContextState, "local")
        assert state is not None
        assert state.generation == 0


@pytest.mark.asyncio
async def test_memory_transition_replay_requires_every_semantic_argument() -> None:
    async with AsyncSessionLocal() as db:
        manager = MemoryManager(db)
        memory, _ = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="stable transition identity",
            source_type="user",
        )
        memory = await manager.confirm("local", memory.id)
        await db.commit()

        action_key = f"memory:{memory.id}:archive:v3"
        first = await transition_memory(
            db,
            owner_id="local",
            memory_id=memory.id,
            expected_version=2,
            event_type="archived",
            action_key=action_key,
            to_status="archived",
            to_validity_state="valid",
            reason_code="manual_archive",
            archived_from_status="confirmed",
            archived_reason="用户归档",
        )
        await db.commit()
        replay = await transition_memory(
            db,
            owner_id="local",
            memory_id=memory.id,
            expected_version=2,
            event_type="archived",
            action_key=action_key,
            to_status="archived",
            to_validity_state="valid",
            reason_code="manual_archive",
            archived_from_status="confirmed",
            archived_reason="用户归档",
        )
        assert replay.id == first.id
        with pytest.raises(MemoryLifecycleConflict):
            await transition_memory(
                db,
                owner_id="local",
                memory_id=memory.id,
                expected_version=2,
                event_type="archived",
                action_key=action_key,
                to_status="archived",
                to_validity_state="valid",
                reason_code="manual_archive",
                archived_from_status="confirmed",
                archived_reason="different payload",
            )
        with pytest.raises(MemoryLifecycleConflict):
            await transition_memory(
                db,
                owner_id="local",
                memory_id=memory.id,
                expected_version=2,
                event_type="archived",
                action_key=action_key,
                to_status="archived",
                to_validity_state="valid",
                reason_code="manual_archive",
                expires_at=None,
                archived_from_status="confirmed",
                archived_reason="用户归档",
            )
        rows = list(
            (
                await db.execute(
                    select(MemoryLifecycleEvent).where(
                        MemoryLifecycleEvent.action_key == action_key
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].request_digest is not None


@pytest.mark.asyncio
async def test_compression_cursor_uses_created_at_then_id_not_raw_id() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="backdated cursor")
        db.add(session)
        await db.flush()
        base = datetime(2030, 1, 1, tzinfo=timezone.utc)
        messages = [
            _chat_message(
                session_id=session.id,
                role="user",
                content=f"ordered-{index}",
                created_at=base + timedelta(minutes=index + 2),
            )
            for index in range(40)
        ]
        db.add_all(messages)
        await db.flush()
        # id=2 is the durable cursor but sorts before id=1. id=1 must remain
        # eligible even though its numeric id is lower than the cursor id.
        messages[1].created_at = base
        messages[0].created_at = base + timedelta(minutes=1)
        db.add(
            SessionCompressionState(
                session_id=session.id,
                owner_id="local",
                covered_through_message_id=messages[1].id,
                covered_through_message_version=messages[1].version,
                generation=0,
            )
        )
        await db.commit()
        session_id = session.id
        expected_first = messages[0].id

    client = _Client()
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        assert session is not None
        assert await MemoryManager(db).compress_session(session, client) is True
    async with AsyncSessionLocal() as reopened:
        summary = (
            await reopened.execute(
                select(SessionSummary).where(SessionSummary.session_id == session_id)
            )
        ).scalars().one()
        assert summary.source_message_ids[0] == expected_first


@pytest.mark.asyncio
async def test_second_summary_links_verified_base_for_transitive_invalidation() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="summary ancestry")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                _chat_message(session_id=session.id, role="user", content=f"first-{index}")
                for index in range(40)
            ]
        )
        await db.commit()
        session_id = session.id

    client = _Client()
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        assert session is not None
        assert await MemoryManager(db).compress_session(session, client) is True
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        assert session is not None
        db.add_all(
            [
                _chat_message(
                    session_id=session.id,
                    role="assistant",
                    content=f"second-{index}",
                )
                for index in range(24)
            ]
        )
        await db.commit()
        assert await MemoryManager(db).compress_session(session, client) is True

    async with AsyncSessionLocal() as reopened:
        summaries = list(
            (
                await reopened.execute(
                    select(SessionSummary)
                    .where(SessionSummary.session_id == session_id)
                    .order_by(SessionSummary.version)
                )
            ).scalars()
        )
        assert len(summaries) == 2
        base_edge = (
            await reopened.execute(
                select(ProvenanceEdge).where(
                    ProvenanceEdge.source_node_id == summaries[0].provenance_node_id,
                    ProvenanceEdge.target_node_id == summaries[1].provenance_node_id,
                    ProvenanceEdge.relation == "summary_base",
                )
            )
        ).scalar_one_or_none()
        assert base_edge is not None


@pytest.mark.asyncio
async def test_utf8_compression_chunks_cover_both_ends_with_bounded_envelopes() -> None:
    begin = "多字节开头标记"
    end = "多字节结尾标记"
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="UTF8 chunk coverage")
        db.add(session)
        await db.flush()
        db.add(
            _chat_message(
                session_id=session.id,
                role="user",
                content=begin + ("界" * 60_000) + end,
            )
        )
        db.add_all(
            [
                _chat_message(
                    session_id=session.id,
                    role="assistant",
                    content=f"recent-{i}",
                )
                for i in range(16)
            ]
        )
        await db.commit()
        session_id = session.id

    client = _Client()
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        assert session is not None
        assert await MemoryManager(db).compress_session(session, client) is True
    prompts = "\n".join(client.completions.prompts)
    assert begin in prompts
    assert end in prompts
    assert len(client.completions.prompts) >= 2


@pytest.mark.asyncio
async def test_new_memory_accepts_only_verified_summary_provenance() -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="verified summary source")
        db.add(plan)
        await db.flush()
        session = Session(owner_id="local", plan_id=plan.id, title="verified source")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                _chat_message(session_id=session.id, role="user", content=f"source-{index}")
                for index in range(40)
            ]
        )
        await db.commit()
        plan_id = plan.id
        session_id = session.id

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        assert session is not None
        assert await MemoryManager(db).compress_session(session, _Client()) is True
    async with AsyncSessionLocal() as db:
        summary = (
            await db.execute(
                select(SessionSummary).where(SessionSummary.session_id == session_id)
            )
        ).scalars().one()
        memory, reused = await MemoryManager(db).propose(
            "local",
            scope="plan",
            scope_id=str(plan_id),
            layer="semantic",
            content="verified derived fact",
            source_type="session_summary",
            source_id=str(summary.id),
        )
        assert reused is False
        edge = (
            await db.execute(
                select(ProvenanceEdge).where(
                    ProvenanceEdge.source_node_id == summary.provenance_node_id,
                    ProvenanceEdge.target_node_id == memory.provenance_node_id,
                    ProvenanceEdge.relation == "memory_source",
                )
            )
        ).scalar_one_or_none()
        assert edge is not None


@pytest.mark.asyncio
async def test_new_memory_rejects_legacy_or_plan_private_global_source() -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="private source plan")
        db.add(plan)
        await db.flush()
        session = Session(owner_id="local", plan_id=plan.id, title="private source")
        db.add(session)
        await db.flush()
        message = _chat_message(
            session_id=session.id,
            role="user",
            content="private fact",
        )
        legacy_summary = SessionSummary(
            owner_id="local",
            session_id=session.id,
            version=1,
            content="unverified legacy claim",
            source_message_ids=[],
            method="legacy",
        )
        db.add_all([message, legacy_summary])
        await db.commit()
        plan_id = plan.id
        message_id = message.id
        summary_id = legacy_summary.id

    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="Summary is not verified"):
            await MemoryManager(db).propose(
                "local",
                scope="plan",
                scope_id=str(plan_id),
                layer="semantic",
                content="must not upgrade legacy",
                source_type="session_summary",
                source_id=str(summary_id),
            )
        await db.rollback()

    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="Plan-private source"):
            await MemoryManager(db).propose(
                "local",
                scope="global",
                scope_id=None,
                layer="semantic",
                content="must not leak between plans",
                source_type="message",
                source_id=str(message_id),
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_global_session_run_can_source_global_memory_without_plan_leak() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="global source session")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="remember an owner-wide preference",
            status="completed",
            phase="terminal",
        )
        db.add(run)
        await db.flush()
        memory, reused = await MemoryManager(db).propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="owner-wide preference",
            source_type="agent_run",
            source_id=run.id,
        )
        assert reused is False
        edge = (
            await db.execute(
                select(ProvenanceEdge).where(
                    ProvenanceEdge.target_node_id == memory.provenance_node_id,
                    ProvenanceEdge.relation == "memory_source",
                )
            )
        ).scalar_one()
        assert edge is not None


@pytest.mark.asyncio
async def test_reinforcement_atomically_versions_pointer_and_multisource_graph() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="reinforcement sources")
        db.add(session)
        await db.flush()
        runs = [
            AgentRun(
                owner_id="local",
                session_id=session.id,
                trigger="user_message",
                objective=f"source {index}",
                status="completed",
                phase="terminal",
            )
            for index in range(2)
        ]
        db.add_all(runs)
        await db.flush()
        manager = MemoryManager(db)
        memory, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="same durable fact",
            source_type="agent_run",
            source_id=runs[0].id,
        )
        assert reused is False
        memory = await manager.confirm("local", memory.id)
        memory, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="same durable fact",
            source_type="agent_run",
            source_id=runs[1].id,
        )
        assert reused is True
        await db.commit()
        memory_id = memory.id

    async with AsyncSessionLocal() as db:
        stored = await db.get(Memory, memory_id)
        assert stored is not None
        assert stored.lifecycle_version == 3
        pointer = await db.get(ProvenanceNode, stored.provenance_node_id)
        assert pointer is not None
        assert pointer.entity_version == stored.lifecycle_version
        source_ids = list(
            await db.scalars(
                select(ProvenanceEdge.source_node_id)
                .where(
                    ProvenanceEdge.target_node_id == pointer.id,
                    ProvenanceEdge.relation == "memory_source",
                )
                .order_by(ProvenanceEdge.ordinal)
            )
        )
        assert len(source_ids) == 2
        assert len(set(source_ids)) == 2
        assert stored.provenance_digest == canonical_digest(source_ids)


@pytest.mark.asyncio
async def test_retrieval_revalidates_expiry_and_records_no_lost_candidate_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_wait = datetime(2035, 1, 1, tzinfo=timezone.utc)
    after_wait = before_wait + timedelta(seconds=2)
    async with AsyncSessionLocal() as db:
        memory, reused = await MemoryManager(db).propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="expiry fence sentinel",
            source_type="user",
            expires_at=before_wait + timedelta(seconds=1),
        )
        assert reused is False
        memory = await MemoryManager(db).confirm("local", memory.id)
        await db.commit()
        memory_id = memory.id

    instants = iter([before_wait, after_wait])
    monkeypatch.setattr(memory_module, "utc_now", lambda: next(instants))
    async with AsyncSessionLocal() as db:
        found = await MemoryManager(db).retrieve(
            "local",
            plan_id=None,
            query="expiry fence sentinel",
            limit=3,
        )
        assert found == []

    async with AsyncSessionLocal() as db:
        stored = await db.get(Memory, memory_id)
        assert stored is not None
        assert stored.access_count == 0
        assert stored.last_accessed_at is None


@pytest.mark.asyncio
async def test_unverified_legacy_memory_is_not_retrieved_or_counted() -> None:
    async with AsyncSessionLocal() as db:
        legacy = Memory(
            owner_id="local",
            scope="global",
            layer="semantic",
            content="legacy unverified sentinel",
            status="confirmed",
            validity_state="legacy_unverified",
        )
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id

    async with AsyncSessionLocal() as db:
        found = await MemoryManager(db).retrieve(
            "local",
            plan_id=None,
            query="legacy unverified sentinel",
            limit=3,
        )
        assert found == []

    async with AsyncSessionLocal() as db:
        stored = await db.get(Memory, legacy_id)
        assert stored is not None
        assert stored.access_count == 0
        assert stored.last_accessed_at is None


@pytest.mark.asyncio
async def test_valid_memory_without_provenance_cannot_enter_lifecycle() -> None:
    async with AsyncSessionLocal() as db:
        inconsistent = Memory(
            owner_id="local",
            scope="global",
            layer="semantic",
            content="pointerless valid row",
            status="confirmed",
            validity_state="valid",
        )
        expired_inconsistent = Memory(
            owner_id="local",
            scope="global",
            layer="short_term",
            content="expired pointerless valid row",
            status="confirmed",
            validity_state="valid",
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        db.add_all([inconsistent, expired_inconsistent])
        await db.commit()
        inconsistent_id = inconsistent.id
        expired_id = expired_inconsistent.id

        found = await MemoryManager(db).retrieve(
            "local",
            plan_id=None,
            query="pointerless valid row",
            limit=5,
        )
        assert found == []
        await db.refresh(inconsistent)
        assert inconsistent.access_count == 0
        assert inconsistent.last_accessed_at is None

        replacement, reused = await MemoryManager(db).propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="pointerless valid row",
            source_type="user",
        )
        assert reused is False
        assert replacement.id != inconsistent_id
        assert replacement.provenance_node_id is not None
        await db.commit()

        maintained = await MemoryManager(db).maintain("local")
        assert maintained["expired"] == 0
        await db.refresh(expired_inconsistent)
        assert expired_inconsistent.id == expired_id
        assert expired_inconsistent.status == "confirmed"
        assert expired_inconsistent.embedding is None

        with pytest.raises(MemoryLifecycleConflict, match="missing its provenance pointer"):
            await MemoryManager(db).archive("local", inconsistent.id)


@pytest.mark.asyncio
async def test_repeated_noop_maintenance_does_not_advance_context_generation() -> None:
    async with AsyncSessionLocal() as db:
        db.add(Plan(owner_id="local", title="stable maintenance summary"))
        await db.commit()
        manager = MemoryManager(db)
        first = await manager.maintain("local")
        await db.commit()
        generation_after_change = await read_context_generation(db, "local")
        await db.commit()
        second = await manager.maintain("local")
        await db.commit()
        generation_after_noop = await read_context_generation(db, "local")

        assert first["plans_refreshed"] == 1
        assert second["plans_refreshed"] == 0
        assert generation_after_noop == generation_after_change


@pytest.mark.asyncio
async def test_retrieval_enforces_hard_scope_and_layer_caps() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="hard retrieval quotas")
        db.add(session)
        await db.flush()
        manager = MemoryManager(db)
        core, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="long_term",
            content="quota sentinel",
            source_type="user",
        )
        assert reused is False
        core = await manager.confirm("local", core.id)
        for index in range(30):
            session_memory, reused = await manager.propose(
                "local",
                scope="session",
                scope_id=session.id,
                layer="long_term",
                content=f"quota sentinel session {index}",
                source_type="user",
            )
            assert reused is False
            await manager.confirm("local", session_memory.id)
        await db.commit()
        core_id = core.id
        session_id = session.id

    async with AsyncSessionLocal() as db:
        found = await MemoryManager(db).retrieve(
            "local",
            plan_id=None,
            session_id=session_id,
            query="quota sentinel",
            limit=24,
        )
        assert core_id in {memory.id for memory in found}
        assert sum(memory.scope == "session" for memory in found) <= 15


@pytest.mark.asyncio
async def test_retrieval_never_commits_unrelated_caller_writes() -> None:
    async with AsyncSessionLocal() as db:
        db.add(Plan(owner_id="local", title="must remain uncommitted"))
        with pytest.raises(RuntimeError, match="requires a clean session"):
            await MemoryManager(db).retrieve(
                "local",
                plan_id=None,
                query="anything",
                limit=3,
            )
        await db.rollback()

    async with AsyncSessionLocal() as db:
        leaked = (
            await db.execute(
                select(Plan).where(Plan.title == "must remain uncommitted")
            )
        ).scalar_one_or_none()
        assert leaked is None


@pytest.mark.asyncio
async def test_restore_authorization_uses_stable_reason_code_not_free_text() -> None:
    future = datetime.now(timezone.utc) + timedelta(days=30)
    async with AsyncSessionLocal() as db:
        manager = MemoryManager(db)
        allowed, _ = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="allowed archive",
            source_type="user",
            expires_at=future,
        )
        denied, _ = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="denied archive",
            source_type="user",
            expires_at=future,
        )
        allowed = await manager.confirm("local", allowed.id)
        denied = await manager.confirm("local", denied.id)
        allowed = await manager.archive("local", allowed.id)
        denied = await transition_memory(
            db,
            owner_id="local",
            memory_id=denied.id,
            expected_version=denied.lifecycle_version,
            event_type="archived",
            action_key=f"memory:{denied.id}:missing-scope:v3",
            to_status="archived",
            to_validity_state="valid",
            reason_code="missing_plan_scope",
            archived_from_status="confirmed",
            archived_reason="用户归档",
        )

        restored = await manager.restore("local", allowed.id)
        assert restored.status == "confirmed"
        assert restored.expires_at is not None
        with pytest.raises(ValueError, match="cannot be restored"):
            await manager.restore("local", denied.id)


@pytest.mark.asyncio
async def test_restore_replay_after_reopen_is_an_exact_noop() -> None:
    async with AsyncSessionLocal() as db:
        manager = MemoryManager(db)
        memory, _ = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="restore replay",
            source_type="user",
        )
        memory = await manager.confirm("local", memory.id)
        memory = await manager.archive("local", memory.id)
        await db.commit()
        memory_id = memory.id

    async with AsyncSessionLocal() as db:
        restored = await MemoryManager(db).restore("local", memory_id)
        await db.commit()
        restored_version = restored.lifecycle_version
        generation = await read_context_generation(db, "local")
        event_count = len(
            list(
                (
                    await db.execute(
                        select(MemoryLifecycleEvent).where(
                            MemoryLifecycleEvent.memory_id == memory_id
                        )
                    )
                ).scalars()
            )
        )

    async with AsyncSessionLocal() as db:
        replay = await MemoryManager(db).restore("local", memory_id)
        await db.commit()
        assert replay.lifecycle_version == restored_version
        assert await read_context_generation(db, "local") == generation
        replay_event_count = len(
            list(
                (
                    await db.execute(
                        select(MemoryLifecycleEvent).where(
                            MemoryLifecycleEvent.memory_id == memory_id
                        )
                    )
                ).scalars()
            )
        )
        assert replay_event_count == event_count


@pytest.mark.asyncio
async def test_confirm_replay_after_reopen_is_an_exact_noop() -> None:
    async with AsyncSessionLocal() as db:
        manager = MemoryManager(db)
        memory, _ = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="confirm replay",
            source_type="user",
        )
        confirmed = await manager.confirm("local", memory.id)
        await db.commit()
        memory_id = confirmed.id
        confirmed_version = confirmed.lifecycle_version
        generation = await read_context_generation(db, "local")
        event_count = len(
            list(
                (
                    await db.execute(
                        select(MemoryLifecycleEvent).where(
                            MemoryLifecycleEvent.memory_id == memory_id
                        )
                    )
                ).scalars()
            )
        )

    async with AsyncSessionLocal() as db:
        replay = await MemoryManager(db).confirm("local", memory_id)
        await db.commit()
        assert replay.lifecycle_version == confirmed_version
        assert await read_context_generation(db, "local") == generation
        replay_event_count = len(
            list(
                (
                    await db.execute(
                        select(MemoryLifecycleEvent).where(
                            MemoryLifecycleEvent.memory_id == memory_id
                        )
                    )
                ).scalars()
            )
        )
        assert replay_event_count == event_count


@pytest.mark.asyncio
async def test_http_memory_proposal_rejects_forged_source_and_normalizes_user_source() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        forged = await client.post(
            "/api/v1/memories/proposals",
            json={
                "scope": "global",
                "layer": "semantic",
                "content": "forged source must fail",
                "source_type": "test",
                "source_id": "arbitrary-existing-id",
            },
        )
        assert forged.status_code == 422
        accepted = await client.post(
            "/api/v1/memories/proposals",
            json={
                "scope": "global",
                "layer": "semantic",
                "content": "normalized user-authored memory",
            },
        )
        assert accepted.status_code == 201
        memory_id = int(accepted.json()["id"])

    async with AsyncSessionLocal() as db:
        stored = await db.get(Memory, memory_id)
        assert stored is not None
        assert stored.source_type == "user"
        assert stored.source_id is None
        assert stored.validity_state == "valid"


@pytest.mark.asyncio
async def test_concurrent_http_archive_and_restore_have_one_lifecycle_winner() -> None:
    async with AsyncSessionLocal() as db:
        manager = MemoryManager(db)
        memory, _ = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="concurrent lifecycle",
            source_type="user",
        )
        memory = await manager.confirm("local", memory.id)
        await db.commit()
        memory_id = memory.id

    async def request(method: str, path: str) -> tuple[int, dict]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.request(method, path)
            return response.status_code, response.json()

    archive_results = await asyncio.gather(
        request("DELETE", f"/api/v1/memories/{memory_id}"),
        request("DELETE", f"/api/v1/memories/{memory_id}"),
    )
    assert [status for status, _ in archive_results] == [200, 200]
    assert {payload["lifecycle_version"] for _, payload in archive_results} == {3}

    restore_results = await asyncio.gather(
        request("POST", f"/api/v1/memories/{memory_id}/restore"),
        request("POST", f"/api/v1/memories/{memory_id}/restore"),
    )
    assert [status for status, _ in restore_results] == [200, 200]
    assert {payload["lifecycle_version"] for _, payload in restore_results} == {4}

    async with AsyncSessionLocal() as db:
        events = list(
            (
                await db.execute(
                    select(MemoryLifecycleEvent).where(
                        MemoryLifecycleEvent.memory_id == memory_id
                    )
                )
            ).scalars()
        )
        assert sum(event.event_type == "archived" for event in events) == 1
        assert sum(event.event_type == "restored" for event in events) == 1
