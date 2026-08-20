from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select

import pytest

from app.db.database import AsyncSessionLocal, Base
import app.models as model_module
from app.core.config import settings
from app.models import AgentRun, ChatMessage, Intervention, Notification, Plan, ReviewSchedule, Session
from app.notifications.conversation import open_notification_in_conversation
from app.notifications.service import NotificationService
from app.runtime.scheduler import ProactiveScheduler
from app.runtime.interventions import finalize_run_intervention
from app.runtime.proactive import (
    ProactiveDecisionConflictError,
    capture_proactive_candidate,
    create_proactive_decision,
    finalize_proactive_decision,
)
from app.schemas import (
    AgentRunRead,
    NotificationOpenResult,
    QueuedMessageCreate,
    QueuedMessageRead,
)
from app.tools.base import ToolContext
from app.tools.registry import execute_tool


def _require_setup(condition: bool, message: str) -> None:
    """Do not let fixture drift satisfy a strict-xfail contract."""

    if not condition:
        raise RuntimeError(f"H5 intervention setup failed: {message}")


def test_intervention_schema_can_persist_identity_before_behavior() -> None:
    tables = Base.metadata.tables
    failures: list[str] = []
    intervention = tables.get("interventions")
    if intervention is None:
        failures.append("interventions_table_missing")
    else:
        columns = set(intervention.columns.keys())
        required = {
            "id",
            "owner_id",
            "source_run_id",
            "plan_id",
            "session_id",
            "canonical_message_id",
            "reason_code",
            "state",
            "outcome",
        }
        failures.extend(
            f"interventions_column_missing:{name}" for name in sorted(required - columns)
        )

    notification = tables.get("notifications")
    _require_setup(notification is not None, "notifications table is absent")
    if "intervention_id" not in notification.columns:
        failures.append("notification_delivery_has_no_intervention_fk")

    queued = tables.get("queued_messages")
    _require_setup(queued is not None, "queued_messages table is absent")
    if "reply_to_intervention_id" not in queued.columns:
        failures.append("queued_reply_target_has_no_typed_fk")

    messages = tables.get("chat_messages")
    _require_setup(messages is not None, "chat_messages table is absent")
    if "reply_to_intervention_id" not in messages.columns:
        failures.append("message_reply_target_has_no_typed_fk")

    if "reply_to_intervention_id" not in QueuedMessageCreate.model_fields:
        failures.append("queue_create_contract_omits_intervention_target")
    if "reply_to_intervention_id" not in QueuedMessageRead.model_fields:
        failures.append("queue_read_contract_omits_intervention_target")
    if "reply_to_intervention_id" not in AgentRunRead.model_fields:
        failures.append("run_read_contract_omits_intervention_target")
    if "intervention_id" not in NotificationOpenResult.model_fields:
        failures.append("notification_open_contract_omits_intervention_identity")

    assert failures == []


def test_proactive_decision_schema_records_terminal_outcome_and_retry_time() -> None:
    tables = Base.metadata.tables
    failures: list[str] = []
    decisions = tables.get("proactive_decisions")
    if decisions is None:
        failures.append("proactive_decisions_table_missing")
    else:
        columns = set(decisions.columns.keys())
        required = {
            "id",
            "owner_id",
            "source_run_id",
            "outcome",
            "next_eligible_at",
        }
        failures.extend(
            f"proactive_decision_column_missing:{name}"
            for name in sorted(required - columns)
        )

    runs = tables.get("agent_runs")
    _require_setup(runs is not None, "agent_runs table is absent")
    candidate_fields = {
        "proactive_candidate_key",
        "proactive_candidate_kind",
        "proactive_candidate_payload",
        "proactive_candidate_digest",
        "proactive_source_watermark",
        "proactive_source_projection_digest",
        "proactive_detected_at",
    }
    missing_candidate_fields = candidate_fields - set(runs.columns.keys())
    failures.extend(
        f"agent_run_candidate_field_missing:{name}"
        for name in sorted(missing_candidate_fields)
    )
    public_candidate_fields = {
        "proactive_candidate_state",
        "proactive_candidate_key",
        "proactive_candidate_kind",
        "proactive_candidate_digest",
        "proactive_source_watermark",
        "proactive_source_projection_digest",
        "proactive_detected_at",
    }
    failures.extend(
        f"agent_run_read_candidate_field_missing:{name}"
        for name in sorted(public_candidate_fields - set(AgentRunRead.model_fields))
    )

    assert failures == []


@pytest.mark.asyncio
async def test_failed_proactive_decision_retries_after_short_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "AGENT_CANDIDATE_COOLDOWN_MINUTES", 7 * 24 * 60)
    async with AsyncSessionLocal() as db:
        plan = Plan(
            owner_id="local",
            title="Failed proactive retry",
            goal="Retry failed decision after short backoff",
            status="active",
            created_at=now - timedelta(days=10),
        )
        db.add(plan)
        await db.flush()
        review = ReviewSchedule(
            owner_id="local",
            plan_id=plan.id,
            due_at=now - timedelta(hours=1),
            status="scheduled",
        )
        failed_run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            trigger="heartbeat",
            objective="Provider failed before a terminal decision",
            status="failed",
            created_at=now - timedelta(minutes=30),
            completed_at=now - timedelta(minutes=29),
        )
        db.add_all([review, failed_run])
        await db.flush()

        decision_model = getattr(model_module, "ProactiveDecision", None)
        if decision_model is not None:
            available_columns = set(decision_model.__table__.columns.keys())
            candidate_payload = {
                "plan_id": plan.id,
                "review_id": review.id,
                "kind": "due_review",
            }
            decision_payload = {"failure": "provider_failure", "retryable": True}
            candidate_digest = hashlib.sha256(
                json.dumps(
                    candidate_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            decision_digest = hashlib.sha256(
                json.dumps(
                    decision_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            candidate_values = {
                "owner_id": "local",
                "plan_id": plan.id,
                "source_run_id": failed_run.id,
                "candidate_key": f"review:{review.id}",
                "candidate_kind": "due_review",
                "candidate_payload": candidate_payload,
                "candidate_digest": candidate_digest,
                "policy_version": "h5-test-policy-v1",
                "status": "terminal",
                "outcome": "model_failed",
                "reason_code": "provider_failure",
                "next_eligible_at": now - timedelta(seconds=1),
                "decision_payload": decision_payload,
                "decision_digest": decision_digest,
                "decided_at": now - timedelta(seconds=2),
            }
            db.add(
                decision_model(
                    **{
                        key: value
                        for key, value in candidate_values.items()
                        if key in available_columns
                    }
                )
            )
        await db.commit()
        plan_id = plan.id

    candidate = await ProactiveScheduler()._next_candidate()
    failures: list[str] = []
    if decision_model is None:
        failures.append("failed_attempt_has_no_proactive_decision_identity")
    if candidate is None:
        failures.append("failed_attempt_consumed_success_cooldown")
    elif candidate.get("plan_id") != plan_id or candidate.get("reason") != "due_review":
        failures.append(f"retry_selected_wrong_candidate:{candidate}")

    assert failures == []


@pytest.mark.asyncio
async def test_proactive_decision_create_is_concurrent_and_identity_fenced() -> None:
    async with AsyncSessionLocal() as setup_db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="Concurrent proactive decision",
        )
        capture_proactive_candidate(
            run,
            {
                "candidate_key": "plan:concurrent:progress",
                "candidate_kind": "progress_checkin_due",
                "candidate_payload": {"plan_id": None, "watermark": 7},
                "source_watermark": 7,
                "source_projection_digest": "a" * 64,
            },
        )
        setup_db.add(run)
        await setup_db.commit()
        run_id = run.id

    async def create_once() -> str:
        async with AsyncSessionLocal() as db:
            stored_run = await db.get(AgentRun, run_id)
            _require_setup(stored_run is not None, "captured Run disappeared")
            decision = await create_proactive_decision(db, stored_run)
            _require_setup(decision is not None, "captured Run produced no Decision")
            await db.commit()
            return decision.id

    first_id, second_id = await asyncio.gather(create_once(), create_once())
    assert first_id == second_id

    async with AsyncSessionLocal() as db:
        stored_run = await db.get(AgentRun, run_id)
        _require_setup(stored_run is not None, "captured Run disappeared after race")
        with pytest.raises(
            ProactiveDecisionConflictError,
            match="source_invocation_id",
        ):
            await create_proactive_decision(
                db,
                stored_run,
                source_invocation_id=987654321,
            )


@pytest.mark.asyncio
async def test_terminal_proactive_decision_replay_must_match_exact_outcome() -> None:
    decided_at = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="Exact terminal replay",
        )
        capture_proactive_candidate(
            run,
            {
                "candidate_key": "plan:replay:progress",
                "candidate_kind": "progress_checkin_due",
                "candidate_payload": {"plan_id": None},
                "source_projection_digest": "b" * 64,
            },
        )
        db.add(run)
        await db.flush()
        first = await finalize_proactive_decision(
            db,
            run,
            outcome="model_failed",
            reason_code="provider_timeout",
            decision_payload={"provider": "offline-test"},
            decided_at=decided_at,
        )
        replay = await finalize_proactive_decision(
            db,
            run,
            outcome="model_failed",
            reason_code="provider_timeout",
            decision_payload={"provider": "offline-test"},
            decided_at=decided_at,
        )
        assert first is not None and replay is not None and replay.id == first.id

        with pytest.raises(ProactiveDecisionConflictError, match="does not match"):
            await finalize_proactive_decision(
                db,
                run,
                outcome="runtime_failed",
                reason_code="executor_crash",
                decision_payload={"provider": "offline-test"},
                decided_at=decided_at,
            )


@pytest.mark.asyncio
async def test_three_deliveries_share_one_intervention_and_one_canonical_message() -> None:
    marker = "H5-INT-002-ONE-LOGICAL-REMINDER"
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Intervention delivery plan", status="active")
        session = Session(owner_id="local", title="Intervention delivery session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="manual_heartbeat",
            objective="Create one reminder over three channels",
            status="completed",
        )
        db.add(run)
        await db.commit()

        result = await NotificationService(db).send(
            owner_id="local",
            run_id=run.id,
            session_id=session.id,
            trigger="manual_heartbeat",
            title="One logical reminder",
            body=marker,
            plan_id=plan.id,
            channels=["email", "browser"],
        )
        await db.commit()

        deliveries = list(
            (
                await db.execute(
                    select(Notification)
                    .where(Notification.run_id == run.id, Notification.body == marker)
                    .order_by(Notification.id)
                )
            ).scalars()
        )
        messages = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.session_id == session.id,
                        ChatMessage.content == marker,
                    )
                )
            ).scalars()
        )

        _require_setup(result.get("blocked") is False, "notification Guard blocked the fixture")
        _require_setup(
            [delivery.channel for delivery in deliveries] == ["in_app", "email", "browser"],
            "service did not create the requested three delivery rows",
        )
        _require_setup(len(messages) == 1, "legacy service did not materialize its visible message")

        intervention_ids = {
            getattr(delivery, "intervention_id", None) for delivery in deliveries
        }
        failures: list[str] = []
        if None in intervention_ids or len(intervention_ids) != 1:
            failures.append("deliveries_do_not_share_one_persisted_intervention")
        intervention_model = getattr(model_module, "Intervention", None)
        intervention = None
        if intervention_model is not None and None not in intervention_ids and len(intervention_ids) == 1:
            intervention = await db.get(intervention_model, next(iter(intervention_ids)))
        if intervention is None:
            failures.append("authoritative_intervention_row_missing")
        elif getattr(intervention, "canonical_message_id", None) != messages[0].id:
            failures.append("canonical_message_is_not_mapped_to_intervention")
        elif {delivery.reply_token for delivery in deliveries} != {
            intervention.reply_token
        }:
            failures.append("delivery_exposes_non_authoritative_reply_token")
        if result.get("intervention_id") not in intervention_ids:
            failures.append("service_result_omits_stable_intervention_identity")

        assert failures == []


@pytest.mark.asyncio
async def test_two_identical_interventions_never_share_canonical_message() -> None:
    marker = "H5-INT-003-INTENTIONALLY-IDENTICAL"
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Identical intervention plan", status="active")
        session = Session(owner_id="local", title="Identical intervention session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="manual_heartbeat",
            objective="Issue two semantically separate reminders",
            status="completed",
        )
        db.add(run)
        await db.commit()

        results = []
        for _ in range(2):
            results.append(
                await NotificationService(db).send(
                    owner_id="local",
                    run_id=run.id,
                    session_id=session.id,
                    trigger="manual_heartbeat",
                    title="Identical visible title",
                    body=marker,
                    plan_id=plan.id,
                    channels=["in_app"],
                )
            )
            await db.commit()

        deliveries = list(
            (
                await db.execute(
                    select(Notification)
                    .where(Notification.run_id == run.id, Notification.body == marker)
                    .order_by(Notification.id)
                )
            ).scalars()
        )
        _require_setup(len(deliveries) == 2, "two notification calls did not create two deliveries")
        first_session, first_message, _ = await open_notification_in_conversation(db, deliveries[0])
        second_session, second_message, _ = await open_notification_in_conversation(db, deliveries[1])
        await db.commit()
        _require_setup(first_session.id == second_session.id == session.id, "conversation scope drifted")

        intervention_ids = [getattr(delivery, "intervention_id", None) for delivery in deliveries]
        result_ids = [result.get("intervention_id") for result in results]
        failures: list[str] = []
        if any(identity is None for identity in intervention_ids):
            failures.append("delivery_has_no_intervention_identity")
        if len(set(intervention_ids)) != 2:
            failures.append("identical_reminders_do_not_have_distinct_interventions")
        if result_ids != intervention_ids:
            failures.append("service_result_does_not_return_each_intervention_identity")
        if first_message.id == second_message.id:
            failures.append("distinct_interventions_share_one_canonical_message")

        assert failures == []


@pytest.mark.asyncio
async def test_read_only_notification_guard_requires_original_intervention_channel() -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(
            owner_id="local",
            title="Archived reply guard",
            status="archived",
            archived_from_status="active",
        )
        session = Session(owner_id="local", title="Archived reply guard session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        canonical = ChatMessage(
            session_id=session.id,
            role="assistant",
            content="Original email reminder",
            version=1,
            content_hash=hashlib.sha256(b"Original email reminder").hexdigest(),
        )
        db.add(canonical)
        await db.flush()
        original = Intervention(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            canonical_message_id=canonical.id,
            title="Original reminder",
            body="Original email reminder",
            content_digest=hashlib.sha256(b"Original email reminder").hexdigest(),
            reason_code="test_read_only_reply",
            state="active",
        )
        db.add(original)
        await db.flush()
        delivery = Notification(
            owner_id="local",
            intervention_id=original.id,
            legacy_unlinked=False,
            plan_id=plan.id,
            session_id=session.id,
            channel="email",
            title=original.title,
            body=original.body,
            reply_token=original.reply_token,
            status="sent",
        )
        db.add(delivery)
        run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            trigger="email_reply",
            objective="Answer without mutating archived learning state",
            execution_mode="read_only",
            reply_to_intervention_id=original.id,
        )
        db.add(run)
        await db.commit()

        context = ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            trigger="email_reply",
            plan_id=plan.id,
            session_id=session.id,
            execution_mode="read_only",
            reply_to_intervention_id=original.id,
            tool_call_id="h5-read-only-wrong-channel",
        )
        rejected = await execute_tool(
            "notification_send",
            json.dumps(
                {
                    "title": "Unsafe channel expansion",
                    "body": "Do not send this through browser push.",
                    "plan_id": plan.id,
                    "channels": ["browser"],
                }
            ),
            context,
        )
        assert rejected["error_code"] == "read_only_effect_forbidden"
        assert rejected["guard_reason"] == "reply_channel_not_original"

        context.tool_call_id = "h5-read-only-original-channel"
        allowed = await execute_tool(
            "notification_send",
            json.dumps(
                {
                    "title": "Read-only answer",
                    "body": "This reply explains history without changing the archived plan.",
                    "plan_id": plan.id,
                    "channels": ["email"],
                }
            ),
            context,
        )
        assert allowed["ok"] is True
        await db.refresh(original)
        assert original.state == "replied"
        assert original.outcome == "reply_received"
        reply_messages = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.reply_to_intervention_id == original.id,
                        ChatMessage.role == "assistant",
                    )
                )
            ).scalars()
        )
        assert len(reply_messages) == 1

        run.status = "completed"
        run.phase = "terminal"
        await finalize_run_intervention(db, run)
        await db.commit()
        await db.refresh(original)
        assert original.state == "resolved"
        assert original.outcome == "run_completed"
