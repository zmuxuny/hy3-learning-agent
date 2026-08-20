from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

import app.api.agent as agent_api
import app.context.assembler as context_assembler_module
from app.api.agent import edit_user_message, handoff_session
from app.context import ContextAssembler
from app.context.memory import MemoryManager
from app.context.provenance import canonical_digest
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.uow import commit as commit_uow
from app.models import (
    AgentRun,
    CalendarEvent,
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
)
from app.schemas import MessageEdit, SessionHandoffCreate
from app.services.sessions import ensure_session_handoff


class _RecordingCompletions:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.prompts: list[str] = []

    async def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1]["content"])
        if self.fail:
            raise RuntimeError("deterministic summary failure")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="deterministic summary"))]
        )


class _RecordingClient:
    def __init__(self, *, fail: bool = False):
        self.completions = _RecordingCompletions(fail=fail)
        self.chat = SimpleNamespace(completions=self.completions)


def _require_fixture(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _verified_message_values(
    *, session_id: str, role: str, content: str
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "role": role,
        "content": content,
        "version": 1,
        "content_hash": canonical_digest(content),
        "message_metadata": {},
    }


def _verified_message(*, session_id: str, role: str, content: str) -> ChatMessage:
    return ChatMessage(**_verified_message_values(
        session_id=session_id,
        role=role,
        content=content,
    ))


@pytest.fixture(autouse=True)
def _isolate_context_projection(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """ContextAssembler writes readable projections; keep every test in tmp_path."""

    monkeypatch.setattr(context_assembler_module, "PROJECT_ROOT", tmp_path)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_global_session_links_do_not_unlock_private_plan_blocks():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="global navigation")
        plans = [
            Plan(owner_id="local", title="Linked plan A"),
            Plan(owner_id="local", title="Linked plan B"),
        ]
        db.add_all([session, *plans])
        await db.flush()

        private_markers: set[str] = set()
        private_source_keys: set[tuple[str, int]] = set()
        now = datetime.now(timezone.utc)
        for index, plan in enumerate(plans, start=1):
            db.add(
                SessionPlanLink(
                    owner_id="local",
                    session_id=session.id,
                    plan_id=plan.id,
                    relation_type="discussed",
                )
            )
            event = LearningEvent(
                owner_id="local",
                plan_id=plan.id,
                event_type="private.plan.fact",
                summary=f"PRIVATE_EVENT_{index}",
            )
            quiz = Quiz(
                owner_id="local",
                plan_id=plan.id,
                prompt=f"PRIVATE_QUIZ_{index}",
            )
            notification = Notification(
                owner_id="local",
                plan_id=plan.id,
                title=f"PRIVATE_NOTIFICATION_{index}",
                body=f"PRIVATE_NOTIFICATION_BODY_{index}",
            )
            review = ReviewSchedule(
                owner_id="local",
                plan_id=plan.id,
                due_at=now + timedelta(days=index),
            )
            calendar = CalendarEvent(
                owner_id="local",
                plan_id=plan.id,
                title=f"PRIVATE_CALENDAR_{index}",
                starts_at=now + timedelta(hours=index),
            )
            db.add_all([event, quiz, notification, review, calendar])
            await db.flush()
            private_markers.update(
                {
                    event.summary,
                    quiz.prompt,
                    notification.title,
                    notification.body,
                    calendar.title,
                    f"review:{review.id}",
                }
            )
            private_source_keys.update(
                {
                    ("learning_event", event.id),
                    ("quiz", quiz.id),
                    ("notification", notification.id),
                    ("review", review.id),
                    ("calendar_event", calendar.id),
                }
            )
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            session_id=session.id,
            objective="show only my compact plan index",
        )
        active_manifest_keys = {
            (str(item.get("type")), int(item["id"]))
            for item in snapshot.source_manifest
            if isinstance(item.get("id"), int)
        }

        failures = [marker for marker in private_markers if marker in snapshot.markdown]
        leaked_sources = sorted(private_source_keys.intersection(active_manifest_keys))
        assert failures == []
        assert leaked_sources == []


@pytest.mark.asyncio
async def test_10000_message_coverage_contains_only_messages_seen_by_summarizer():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="ten thousand turns")
        db.add(session)
        await db.flush()
        await db.execute(
            insert(ChatMessage),
            [
                _verified_message_values(
                    session_id=session.id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"MSG_{index:05d}|" + ("x" * 20),
                )
                for index in range(10_000)
            ],
        )
        await db.commit()
        messages = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session.id)
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        marker_by_id = {
            message.id: f"MSG_{index:05d}|" for index, message in enumerate(messages)
        }
        client = _RecordingClient()

        compressed = await MemoryManager(db).compress_session(session, client)
        summary = (
            await db.execute(
                select(SessionSummary).where(SessionSummary.session_id == session.id)
            )
        ).scalars().one()
        summarizer_inputs = "\n".join(client.completions.prompts)
        coverage_observations: list[dict[str, int | str]] = []
        for message_id in summary.source_message_ids:
            marker = marker_by_id.get(message_id)
            if marker is None:
                coverage_observations.append(
                    {
                        "message_id": message_id,
                        "violation": "coverage_references_unknown_message",
                    }
                )
            elif marker not in summarizer_inputs:
                coverage_observations.append(
                    {
                        "message_id": message_id,
                        "violation": "covered_message_not_seen_by_summarizer",
                    }
                )
        expected_prefix = [message.id for message in messages[:-settings.AGENT_RECENT_MESSAGE_LIMIT]]

        _require_fixture(compressed is True, "10k compression fixture did not run")
        assert coverage_observations == []
        assert summary.source_message_ids == expected_prefix
        assert len(summary.source_message_ids) == len(set(summary.source_message_ids))
        assert summary.covered_through_message_id == expected_prefix[-1]


@pytest.mark.asyncio
async def test_long_message_prefix_is_read_before_message_is_marked_covered():
    begin_marker = "LONG_MESSAGE_BEGIN_SENTINEL"
    end_marker = "LONG_MESSAGE_END_SENTINEL"
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="long source message")
        db.add(session)
        await db.flush()
        db.add(
            _verified_message(
                session_id=session.id,
                role="user",
                content=begin_marker + ("x" * 45_000) + end_marker,
            )
        )
        db.add_all(
            [
                _verified_message(
                    session_id=session.id,
                    role="assistant",
                    content=f"recent-{index}",
                )
                for index in range(settings.AGENT_RECENT_MESSAGE_LIMIT)
            ]
        )
        await db.commit()
        client = _RecordingClient()

        compressed = await MemoryManager(db).compress_session(session, client)
        summary = (
            await db.execute(
                select(SessionSummary).where(SessionSummary.session_id == session.id)
            )
        ).scalars().one()
        summarizer_inputs = "\n".join(client.completions.prompts)

        _require_fixture(compressed is True, "long-message compression fixture did not run")
        assert len(summary.source_message_ids) == 1
        assert begin_marker in summarizer_inputs
        assert end_marker in summarizer_inputs


@pytest.mark.asyncio
async def test_compression_failure_does_not_advance_coverage():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="failed compression", summary="stable summary")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                _verified_message(
                    session_id=session.id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"turn-{index}",
                )
                for index in range(40)
            ]
        )
        await db.commit()
        before_summary = session.summary
        before_rows = list(
            (
                await db.execute(
                    select(SessionSummary).where(SessionSummary.session_id == session.id)
                )
            ).scalars()
        )

        compressed = await MemoryManager(db).compress_session(
            session, _RecordingClient(fail=True)
        )
        after_rows = list(
            (
                await db.execute(
                    select(SessionSummary).where(SessionSummary.session_id == session.id)
                )
            ).scalars()
        )
        messages = list(
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.session_id == session.id)
                )
            ).scalars()
        )

        assert compressed is False
        assert [row.id for row in after_rows] == [row.id for row in before_rows]
        assert session.summary == before_summary
        assert all(not message.message_metadata.get("included_in_summary") for message in messages)


@pytest.mark.asyncio
async def test_handoff_is_frozen_when_plan_session_is_created():
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Frozen handoff plan")
        source = Session(owner_id="local", title="Source conversation")
        db.add_all([plan, source])
        await db.flush()
        db.add(
            ChatMessage(
                session_id=source.id,
                role="user",
                content="DECISION_AT_HANDOFF",
            )
        )
        await db.commit()

        first = await handoff_session(
            source.id, SessionHandoffCreate(plan_id=plan.id), db
        )
        frozen_handoff = first["handoff_summary"]
        frozen_digest = hashlib.sha256(frozen_handoff.encode("utf-8")).hexdigest()
        db.add(
            ChatMessage(
                session_id=source.id,
                role="user",
                content="LATE_SOURCE_MESSAGE_MUST_NOT_DRIFT_HANDOFF",
            )
        )
        await db.commit()

        second = await handoff_session(
            source.id, SessionHandoffCreate(plan_id=plan.id), db
        )

        assert second["id"] == first["id"]
        assert second["handoff_summary"] == frozen_handoff
        assert (
            hashlib.sha256(second["handoff_summary"].encode("utf-8")).hexdigest()
            == frozen_digest
        )
        assert "LATE_SOURCE_MESSAGE_MUST_NOT_DRIFT_HANDOFF" not in second["handoff_summary"]


@pytest.mark.asyncio
async def test_message_edit_invalidates_all_derived_memory_scopes_and_sources(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Edit invalidation plan")
        session = Session(owner_id="local", plan_id=plan.id, title="Editable session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        original_run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="user_message",
            objective="obsolete premise",
            status="completed",
        )
        downstream_run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="user_message",
            objective="derived follow-up",
            status="completed",
        )
        db.add_all([original_run, downstream_run])
        await db.flush()
        target = ChatMessage(
            session_id=session.id,
            run_id=original_run.id,
            role="user",
            content="OBSOLETE_SOURCE_FACT",
        )
        downstream = ChatMessage(
            session_id=session.id,
            run_id=downstream_run.id,
            role="assistant",
            content="OBSOLETE_DERIVED_ANSWER",
        )
        db.add_all([target, downstream])
        await db.flush()
        memories = [
            Memory(
                owner_id="local",
                scope="session",
                scope_id=session.id,
                layer="semantic",
                content="obsolete original-run memory",
                source_type="agent_run",
                source_id=original_run.id,
                status="confirmed",
            ),
            Memory(
                owner_id="local",
                scope="session",
                scope_id=session.id,
                layer="semantic",
                content="obsolete downstream session memory",
                source_type="agent_run",
                source_id=downstream_run.id,
                status="confirmed",
            ),
            Memory(
                owner_id="local",
                scope="plan",
                scope_id=str(plan.id),
                layer="semantic",
                content="obsolete downstream plan memory",
                source_type="agent_run",
                source_id=downstream_run.id,
                status="confirmed",
            ),
            Memory(
                owner_id="local",
                scope="global",
                layer="semantic",
                content="obsolete downstream global memory",
                source_type="agent_run",
                source_id=downstream_run.id,
                status="confirmed",
            ),
            Memory(
                owner_id="local",
                scope="session",
                scope_id=session.id,
                layer="semantic",
                content="obsolete direct-message memory",
                source_type="message",
                source_id=str(target.id),
                status="confirmed",
            ),
        ]
        db.add_all(memories)
        await db.commit()

        await edit_user_message(
            target.id,
            MessageEdit(content="corrected premise"),
            db,
        )
        for memory in memories:
            await db.refresh(memory)

        assert all(memory.status not in {"proposed", "confirmed"} for memory in memories)
        assert all(memory.archived_reason for memory in memories)
        retrieved = await MemoryManager(db).retrieve(
            "local",
            plan_id=plan.id,
            session_id=session.id,
            query="obsolete memory",
            limit=20,
        )
        assert {memory.id for memory in memories}.isdisjoint(
            {memory.id for memory in retrieved}
        )


@pytest.mark.asyncio
async def test_message_edit_preserves_audit_and_marks_summary_and_snapshot_invalid(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Source validity", summary="OLD_SUMMARY_FACT")
        db.add(session)
        await db.flush()
        source_run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="OLD_SOURCE_FACT",
            status="completed",
        )
        db.add(source_run)
        await db.flush()
        source_message = ChatMessage(
            session_id=session.id,
            run_id=source_run.id,
            role="user",
            content="OLD_SOURCE_FACT",
        )
        db.add(source_message)
        await db.flush()
        summary = SessionSummary(
            owner_id="local",
            session_id=session.id,
            version=1,
            content="OLD_SUMMARY_FACT",
            covered_through_message_id=source_message.id,
            source_message_ids=[source_message.id],
            method="fixture",
        )
        db.add(summary)
        await db.flush()
        # H2 requires ContextAssembler to start from a caller-owned clean UoW;
        # this fixture setup is durable before the snapshot under test.
        await db.commit()
        snapshot = await ContextAssembler(db).build(
            "local",
            session_id=session.id,
            run_id=source_run.id,
            objective="OLD_SOURCE_FACT",
        )
        await db.commit()
        old_summary_content = summary.content
        old_snapshot_markdown = snapshot.markdown

        await edit_user_message(
            source_message.id,
            MessageEdit(content="NEW_CORRECTED_FACT"),
            db,
        )
        stored_summary = await db.get(SessionSummary, summary.id)
        stored_snapshot = await db.get(ContextSnapshot, snapshot.id)

        assert stored_summary is not None
        assert stored_snapshot is not None
        assert stored_summary.content == old_summary_content
        assert stored_snapshot.markdown == old_snapshot_markdown
        assert getattr(stored_summary, "invalidated_at", None) is not None
        assert getattr(stored_summary, "invalidation_reason", "") == "source_message_edited"
        assert getattr(stored_snapshot, "invalidated_at", None) is not None
        assert getattr(stored_snapshot, "invalidation_reason", "") == "source_message_edited"


@pytest.mark.asyncio
async def test_budget_manifest_separates_retained_and_dropped_sources(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "AGENT_CONTEXT_TOKEN_BUDGET", 2_000)
    monkeypatch.setattr(settings, "AGENT_RECENT_MESSAGE_LIMIT", 10)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Budget manifest")
        source_run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="Provide verified budget-manifest Memory sources",
            status="completed",
            phase="terminal",
        )
        db.add_all([session, source_run])
        await db.flush()
        manager = MemoryManager(db)
        memories = []
        for index in range(10):
            memory, reused = await manager.propose(
                "local",
                scope="global",
                scope_id=None,
                layer="semantic",
                content=f"CTX008_MEMORY_{index:02d}|budget|" + ("m" * 600),
                source_type="agent_run",
                source_id=source_run.id,
            )
            _require_fixture(not reused, "budget Memory unexpectedly deduplicated")
            memories.append(await manager.confirm("local", memory.id))
        event = LearningEvent(
            owner_id="local",
            event_type="budget.middle",
            summary="CTX008_EVENT|",
        )
        db.add(event)
        messages = [
            ChatMessage(
                session_id=session.id,
                role="user",
                content=f"CTX008_MESSAGE_{index:02d}|" + ("t" * 600),
            )
            for index in range(10)
        ]
        db.add_all(messages)
        await db.flush()
        candidate_marker_by_key = {
            **{
                ("memory", str(memory.id)): f"CTX008_MEMORY_{index:02d}|"
                for index, memory in enumerate(memories)
            },
            ("learning_event", str(event.id)): "CTX008_EVENT|",
            **{
                ("message", str(message.id)): f"CTX008_MESSAGE_{index:02d}|"
                for index, message in enumerate(messages)
            },
        }
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            session_id=session.id,
            objective="CTX008 MEMORY budget",
        )
        candidate_keys = set(candidate_marker_by_key)
        actually_retained = {
            key
            for key, marker in candidate_marker_by_key.items()
            if marker in snapshot.markdown
        }
        actually_dropped = candidate_keys - actually_retained
        active_manifest_entries = [
            (str(item.get("type")), str(item.get("id")))
            for item in snapshot.source_manifest
            if (str(item.get("type")), str(item.get("id"))) in candidate_keys
        ]
        dropped_manifest = getattr(snapshot, "dropped_source_manifest", [])
        dropped_manifest_entries = [
            (str(item.get("type")), str(item.get("id")))
            for item in dropped_manifest
            if (str(item.get("type")), str(item.get("id"))) in candidate_keys
        ]
        reported_retained = set(active_manifest_entries)
        reported_dropped = set(dropped_manifest_entries)
        reason_by_dropped_key = {
            (str(item.get("type")), str(item.get("id"))): item.get("reason_code")
            for item in dropped_manifest
            if (str(item.get("type")), str(item.get("id"))) in candidate_keys
        }

        partition_observations: list[str] = []
        if reported_retained != actually_retained:
            partition_observations.append("retained_manifest_differs_from_rendered_sources")
        if reported_dropped != actually_dropped:
            partition_observations.append("dropped_manifest_differs_from_omitted_sources")
        if reported_retained.intersection(reported_dropped):
            partition_observations.append("retained_and_dropped_manifests_overlap")
        if reported_retained.union(reported_dropped) != candidate_keys:
            partition_observations.append("candidate_sources_are_not_fully_partitioned")
        if len(active_manifest_entries) != len(reported_retained):
            partition_observations.append("retained_manifest_contains_duplicate_sources")
        if len(dropped_manifest_entries) != len(reported_dropped):
            partition_observations.append("dropped_manifest_contains_duplicate_sources")
        if any(
            not isinstance(reason_by_dropped_key.get(key), str)
            or not reason_by_dropped_key[key].strip()
            for key in actually_dropped
        ):
            partition_observations.append("dropped_source_has_no_reason_code")

        _require_fixture(bool(actually_retained), "budget fixture retained no candidate sources")
        _require_fixture(bool(actually_dropped), "budget fixture dropped no candidate sources")
        assert partition_observations == []


@pytest.mark.asyncio
async def test_context_budget_accounts_for_system_tools_and_output_reserve(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 80_000)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_TOKEN_BUDGET", 12_000)
    async with AsyncSessionLocal() as db:
        source_run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="Provide verified full-envelope budget sources",
            status="completed",
            phase="terminal",
        )
        db.add(source_run)
        await db.flush()
        manager = MemoryManager(db)
        for index in range(24):
            memory, reused = await manager.propose(
                "local",
                scope="global",
                scope_id=None,
                layer="semantic",
                content=f"full-budget-source-{index}-" + ("b" * 4_000),
                source_type="agent_run",
                source_id=source_run.id,
            )
            _require_fixture(not reused, "full-budget Memory unexpectedly deduplicated")
            await manager.confirm("local", memory.id)
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            objective="full budget source",
        )
        budget_observations: list[str] = []
        breakdown = getattr(snapshot, "budget_breakdown", None)
        required_numeric_fields = {
            "model_context_window",
            "context_token_budget",
            "system_prompt_tokens",
            "tool_schema_tokens",
            "context_tokens",
            "output_reserve_tokens",
            "tool_result_reserve_tokens",
            "total_tokens",
        }

        def observe_budget_arithmetic(
            observed_snapshot: ContextSnapshot,
            observed_breakdown: object,
            *,
            label: str,
        ) -> None:
            if not isinstance(observed_breakdown, dict):
                budget_observations.append(f"{label}_snapshot_has_no_budget_breakdown")
                return
            missing_fields = sorted(required_numeric_fields.difference(observed_breakdown))
            if missing_fields:
                budget_observations.append(
                    f"{label}_budget_breakdown_missing_fields:{','.join(missing_fields)}"
                )
            invalid_fields = []
            for field in sorted(required_numeric_fields.intersection(observed_breakdown)):
                value = observed_breakdown[field]
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    invalid_fields.append(field)
            for field in invalid_fields:
                budget_observations.append(
                    f"{label}_budget_field_is_not_positive_integer:{field}"
                )
            if missing_fields or invalid_fields:
                return

            component_total = sum(
                observed_breakdown[field]
                for field in (
                    "system_prompt_tokens",
                    "tool_schema_tokens",
                    "context_tokens",
                    "output_reserve_tokens",
                    "tool_result_reserve_tokens",
                )
            )
            if observed_breakdown["total_tokens"] != component_total:
                budget_observations.append(f"{label}_budget_total_differs_from_component_sum")
            if observed_breakdown["context_tokens"] != observed_snapshot.estimated_tokens:
                budget_observations.append(
                    f"{label}_context_component_differs_from_snapshot_estimate"
                )
            if observed_breakdown["context_tokens"] > observed_breakdown["context_token_budget"]:
                budget_observations.append(f"{label}_context_component_exceeds_context_budget")
            if observed_breakdown["total_tokens"] > observed_breakdown["model_context_window"]:
                budget_observations.append(f"{label}_complete_request_exceeds_model_window")

        reserve_overrides: dict[str, int] = {}
        observe_budget_arithmetic(snapshot, breakdown, label="initial")
        if isinstance(breakdown, dict):
            config_sources = breakdown.get("config_sources")
            configured_fields = (
                "model_context_window",
                "context_token_budget",
                "output_reserve_tokens",
                "tool_result_reserve_tokens",
            )
            if not isinstance(config_sources, dict):
                budget_observations.append("budget_breakdown_has_no_config_sources")
            else:
                for field in configured_fields:
                    setting_name = config_sources.get(field)
                    if not isinstance(setting_name, str) or not setting_name:
                        budget_observations.append(f"budget_field_has_no_config_source:{field}")
                        continue
                    configured_value = getattr(settings, setting_name, None)
                    if not isinstance(configured_value, int) or isinstance(configured_value, bool):
                        budget_observations.append(f"budget_config_source_is_invalid:{field}")
                        continue
                    if breakdown.get(field) != configured_value:
                        budget_observations.append(f"budget_field_differs_from_config:{field}")
                    if field in {"output_reserve_tokens", "tool_result_reserve_tokens"}:
                        override = configured_value + (137 if field == "output_reserve_tokens" else 251)
                        monkeypatch.setattr(settings, setting_name, override)
                        reserve_overrides[field] = override

        if len(reserve_overrides) == 2:
            await commit_uow(db)
            configured_snapshot = await ContextAssembler(db).build(
                "local",
                objective="full budget source after reserve override",
            )
            configured_breakdown = getattr(configured_snapshot, "budget_breakdown", None)
            observe_budget_arithmetic(
                configured_snapshot,
                configured_breakdown,
                label="configured",
            )
            if isinstance(configured_breakdown, dict):
                if configured_breakdown.get("config_sources") != breakdown.get("config_sources"):
                    budget_observations.append("configured_budget_lost_config_provenance")
                for field, override in reserve_overrides.items():
                    if configured_breakdown.get(field) != override:
                        budget_observations.append(f"budget_does_not_follow_config_override:{field}")

        assert budget_observations == []


@pytest.mark.asyncio
async def test_memory_retrieval_enforces_relevance_threshold_and_layer_quota():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Retrieval quotas")
        db.add(session)
        await db.flush()
        manager = MemoryManager(db)

        async def verified_memory(**values):
            memory, reused = await manager.propose(
                "local",
                source_type="user",
                **values,
            )
            assert reused is False
            return await manager.confirm("local", memory.id)

        unrelated = await verified_memory(
            scope="global",
            scope_id=None,
            layer="long_term",
            content="garden tomatoes watering schedule",
        )
        for index in range(30):
            await verified_memory(
                scope="session",
                scope_id=session.id,
                layer="long_term",
                content=f"asyncio timeout session {index}",
                confidence=1.0,
            )
        core_global = await verified_memory(
            scope="global",
            scope_id=None,
            layer="long_term",
            content="asyncio timeout",
            confidence=1.0,
        )
        await db.commit()

        unrelated_query = await manager.retrieve(
            "local",
            plan_id=None,
            session_id=session.id,
            query="Rust borrow checker ownership",
            limit=40,
        )
        quota_query = await manager.retrieve(
            "local",
            plan_id=None,
            session_id=session.id,
            query="asyncio timeout",
            limit=24,
        )
        failures = []
        if unrelated.id in {memory.id for memory in unrelated_query}:
            failures.append("irrelevant_memory_returned")
        if core_global.id not in {memory.id for memory in quota_query}:
            failures.append("core_global_memory_evicted")

        assert failures == []


@pytest.mark.asyncio
async def test_manual_archive_restore_preserves_future_expiry():
    future_expiry = datetime.now(timezone.utc) + timedelta(days=30)
    async with AsyncSessionLocal() as db:
        manager = MemoryManager(db)
        memory, reused = await manager.propose(
            "local",
            scope="global",
            scope_id=None,
            layer="long_term",
            content="temporary preference with a future expiry",
            source_type="user",
            expires_at=future_expiry,
        )
        assert reused is False
        memory = await manager.confirm("local", memory.id)
        await db.commit()

        archived = await manager.archive("local", memory.id)
        _require_fixture(archived.status == "archived", "memory archive fixture failed")
        restored = await manager.restore("local", memory.id)

        _require_fixture(restored.status == "confirmed", "memory restore fixture did not reactivate")
        assert restored.expires_at is not None
        assert _as_utc(restored.expires_at) == _as_utc(future_expiry)


@pytest.mark.asyncio
async def test_context_manifest_traces_active_summary_and_frozen_handoff():
    handoff_text = "HANDOFF_SOURCE_SENTINEL"
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Manifest provenance plan")
        source_session = Session(
            owner_id="local",
            plan_id=None,
            title="Manifest provenance source",
        )
        session = Session(
            owner_id="local",
            plan_id=None,
            title="Manifest provenance target",
            handoff_summary=handoff_text,
        )
        db.add_all([plan, source_session, session])
        await db.flush()
        source_session.plan_id = plan.id
        session.plan_id = plan.id
        source_message = ChatMessage(
            session_id=source_session.id,
            role="user",
            content="durable handoff source",
            version=1,
            content_hash=canonical_digest("durable handoff source"),
        )
        target_messages = [
            ChatMessage(
                session_id=session.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"target summary source {index}",
                version=1,
                content_hash=canonical_digest(f"target summary source {index}"),
            )
            for index in range(20)
        ]
        db.add_all([source_message, *target_messages])
        await db.commit()

        compressed = await MemoryManager(db).compress_session(session)
        _require_fixture(compressed, "summary fixture did not use compression service")
        summary = (
            await db.execute(
                select(SessionSummary)
                .where(
                    SessionSummary.session_id == session.id,
                    SessionSummary.validity_state == "valid",
                )
                .order_by(SessionSummary.version.desc())
                .limit(1)
            )
        ).scalars().one()
        handoff = await ensure_session_handoff(
            db,
            source=source_session,
            target=session,
            plan=plan,
            content=handoff_text,
        )
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local",
            session_id=session.id,
            objective="continue from the handoff",
        )
        summary_entry = next(
            (
                item
                for item in snapshot.source_manifest
                if item.get("type") == "session_summary"
                and item.get("id") == summary.id
            ),
            None,
        )
        handoff_entry = next(
            (
                item
                for item in snapshot.source_manifest
                if item.get("type") == "session_handoff"
                and item.get("id") == handoff.id
            ),
            None,
        )

        _require_fixture(summary.content in snapshot.markdown, "summary fixture was not rendered")
        _require_fixture(handoff.content in snapshot.markdown, "handoff fixture was not rendered")
        assert summary_entry is not None
        assert summary_entry.get("version") == summary.version
        assert handoff_entry is not None
        assert handoff_entry.get("content_hash") == handoff.content_hash
