from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.db.uow as uow_module
import app.api.agent as agent_api
from app.db.database import AsyncSessionLocal
from app.core.config import settings
from app.models import (
    AgentRun,
    Intervention,
    LearningEvent,
    Plan,
    ProactiveDecision,
    QueuedMessage,
    Session,
)
from app.notifications.conversation import materialize_intervention_message
from app.runtime.checkpoints import make_checkpoint
from app.runtime.interventions import accept_intervention_reply
from app.runtime.proactive import capture_proactive_candidate, finalize_proactive_decision
from app.runtime.scheduler import ProactiveScheduler
from app.runtime.state import RunLease, finalize_run, reconcile_run_after_restart, terminate_run
from app.schemas import AgentRunCreate, QueuedMessageCreate
from app.services.queue import dispatch_queued_message
from app.services.evidence import append_observation


def _captured_run(*, objective: str) -> AgentRun:
    run = AgentRun(
        owner_id="local",
        trigger="heartbeat",
        objective=objective,
    )
    capture_proactive_candidate(
        run,
        {
            "candidate_key": f"candidate:{objective}",
            "candidate_kind": "progress_checkin_due",
            "candidate_payload": {"objective": objective},
            "source_watermark": 7,
            "source_projection_digest": "a" * 64,
        },
    )
    return run


async def _active_intervention(db) -> tuple[Session, Intervention]:
    session = Session(owner_id="local", title="typed reply target")
    db.add(session)
    await db.flush()
    title = "A durable check-in"
    body = "What would help you continue?"
    intervention = Intervention(
        owner_id="local",
        session_id=session.id,
        plan_id=None,
        title=title,
        body=body,
        content_digest=hashlib.sha256(f"{title}\n{body}".encode()).hexdigest(),
        reason_code="fixture",
        state="building",
    )
    db.add(intervention)
    await db.flush()
    await materialize_intervention_message(db, session=session, intervention=intervention)
    await db.commit()
    return session, intervention


@pytest.mark.asyncio
async def test_terminal_run_and_decision_rollback_together_before_commit_then_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with AsyncSessionLocal() as db:
        run = _captured_run(objective="kill before terminal commit")
        db.add(run)
        await db.commit()
        run_id = run.id

    original_commit = uow_module.commit

    async def injected_kill_before_commit(_db) -> None:
        raise RuntimeError("injected kill before terminal commit")

    monkeypatch.setattr(uow_module, "commit", injected_kill_before_commit)
    with pytest.raises(RuntimeError, match="injected kill"):
        await terminate_run(
            AsyncSessionLocal,
            run_id,
            status="failed",
            reason_code="model_retry_exhausted",
            summary="provider failed",
        )
    monkeypatch.setattr(uow_module, "commit", original_commit)

    async with AsyncSessionLocal() as db:
        rolled_back_run = await db.get(AgentRun, run_id)
        decision_count = await db.scalar(
            select(func.count(ProactiveDecision.id)).where(
                ProactiveDecision.source_run_id == run_id
            )
        )
        assert rolled_back_run is not None and rolled_back_run.status == "queued"
        assert decision_count == 0

    terminal = await terminate_run(
        AsyncSessionLocal,
        run_id,
        status="failed",
        reason_code="model_retry_exhausted",
        summary="provider failed",
    )
    assert terminal is not None and terminal.status == "failed"

    # A restart/replay is an exact no-op, not a duplicate terminal fact.
    await terminate_run(
        AsyncSessionLocal,
        run_id,
        status="failed",
        reason_code="model_retry_exhausted",
        summary="provider failed",
    )
    async with AsyncSessionLocal() as db:
        decisions = list(
            (
                await db.execute(
                    select(ProactiveDecision).where(
                        ProactiveDecision.source_run_id == run_id
                    )
                )
            ).scalars()
        )
        assert len(decisions) == 1
        assert decisions[0].status == "terminal"
        assert decisions[0].outcome == "model_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason_code", "expected_outcome"),
    [
        ("failed", "internal_error", "runtime_failed"),
        ("cancelled", "cancel_requested", "cancelled"),
    ],
)
async def test_terminal_failure_mapping_is_durable(
    status: str,
    reason_code: str,
    expected_outcome: str,
) -> None:
    async with AsyncSessionLocal() as db:
        run = _captured_run(objective=f"{status}:{reason_code}")
        db.add(run)
        await db.commit()
        run_id = run.id

    await terminate_run(
        AsyncSessionLocal,
        run_id,
        status=status,
        reason_code=reason_code,
        summary="terminal",
    )
    async with AsyncSessionLocal() as db:
        decision = await db.scalar(
            select(ProactiveDecision).where(ProactiveDecision.source_run_id == run_id)
        )
        assert decision is not None
        assert decision.outcome == expected_outcome


@pytest.mark.asyncio
async def test_restart_repairs_completed_captured_run_without_decision() -> None:
    async with AsyncSessionLocal() as db:
        run = _captured_run(objective="repair old terminal window")
        run.status = "completed"
        run.phase = "terminal"
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        await db.commit()
        run_id = run.id

    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        run_id,
        scope_valid=True,
    ) is False
    async with AsyncSessionLocal() as db:
        decision = await db.scalar(
            select(ProactiveDecision).where(ProactiveDecision.source_run_id == run_id)
        )
        assert decision is not None
        assert decision.outcome == "success_wait"


@pytest.mark.asyncio
async def test_normal_completion_commits_success_wait_with_root_terminal_state() -> None:
    now = datetime.now(timezone.utc)
    token = "captured-finalization-lease"
    state_version = 3
    async with AsyncSessionLocal() as db:
        run = _captured_run(objective="complete without intervention")
        run.id = "captured-success-wait"
        run.status = "running"
        run.phase = "finalizing"
        run.state_version = state_version
        run.checkpoint_schema_version = 1
        run.checkpoint = make_checkpoint(
            kind="agent",
            phase="finalizing",
            step=1,
            messages=[],
            state_version=state_version,
            final_text="No intervention is needed.",
            final_message_key=f"run:{run.id}:final",
        )
        run.lease_token = token
        run.lease_owner = "fixture-worker"
        run.lease_acquired_at = now
        run.lease_expires_at = now + timedelta(minutes=5)
        db.add(run)
        await db.commit()
        run_id = run.id

    completed = await finalize_run(
        AsyncSessionLocal,
        RunLease(
            run_id=run_id,
            token=token,
            owner="fixture-worker",
            version=state_version,
            status_before="queued",
            phase_before="awaiting_model",
            checkpoint=None,
            started_before_claim=True,
        ),
    )
    assert completed.status == "completed"
    async with AsyncSessionLocal() as db:
        decision = await db.scalar(
            select(ProactiveDecision).where(ProactiveDecision.source_run_id == run_id)
        )
        assert decision is not None
        assert decision.outcome == "success_wait"
        assert decision.reason_code == "completed_without_intervention"


@pytest.mark.asyncio
async def test_existing_intervention_decision_is_not_overwritten_by_run_terminal() -> None:
    async with AsyncSessionLocal() as db:
        run = _captured_run(objective="already sent intervention")
        db.add(run)
        await db.flush()
        decision = await finalize_proactive_decision(
            db,
            run,
            outcome="success_intervention",
            reason_code="notification_sent",
            decision_payload={"intervention_id": "fixture"},
        )
        assert decision is not None
        await db.commit()
        run_id = run.id

    await terminate_run(
        AsyncSessionLocal,
        run_id,
        status="failed",
        reason_code="internal_error",
        summary="terminal after delivery",
    )
    async with AsyncSessionLocal() as db:
        decision = await db.scalar(
            select(ProactiveDecision).where(ProactiveDecision.source_run_id == run_id)
        )
        assert decision is not None
        assert decision.outcome == "success_intervention"
        assert decision.reason_code == "notification_sent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_state", "expected_outcome"),
    [
        ("failed", "resolved", "run_failed"),
        ("cancelled", "cancelled", "run_cancelled"),
    ],
)
async def test_in_app_reply_accept_and_terminal_outcome_are_durable(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_state: str,
    expected_outcome: str,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        session, intervention = await _active_intervention(db)
        run = await agent_api.create_run(
            AgentRunCreate(
                objective=f"reply ending in {status}",
                session_id=session.id,
                reply_to_intervention_id=intervention.id,
            ),
            db,
        )
        run_id = run.id
        intervention_id = intervention.id

    async with AsyncSessionLocal() as db:
        accepted = await db.get(Intervention, intervention_id)
        assert accepted is not None
        assert accepted.state == "replied"
        assert accepted.outcome == "reply_received"

    await terminate_run(
        AsyncSessionLocal,
        run_id,
        status=status,
        reason_code=f"fixture_{status}",
        summary="terminal reply",
    )
    # Terminal replay must preserve the exact same Intervention outcome.
    await terminate_run(
        AsyncSessionLocal,
        run_id,
        status=status,
        reason_code=f"fixture_{status}",
        summary="terminal reply",
    )
    async with AsyncSessionLocal() as db:
        terminal = await db.get(Intervention, intervention_id)
        assert terminal is not None
        assert terminal.state == expected_state
        assert terminal.outcome == expected_outcome
        assert (terminal.resolved_at is not None) is (expected_state == "resolved")


@pytest.mark.asyncio
async def test_completed_reply_resolves_in_same_finalization_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    token = "reply-finalization-lease"
    version = 4
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        session, intervention = await _active_intervention(db)
        await accept_intervention_reply(db, intervention)
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="complete typed reply",
            reply_to_intervention_id=intervention.id,
            status="running",
            phase="finalizing",
            state_version=version,
            checkpoint_schema_version=1,
            checkpoint=make_checkpoint(
                kind="agent",
                phase="finalizing",
                step=1,
                messages=[],
                state_version=version,
                final_text="Reply completed.",
                final_message_key="run:reply-completed:final",
            ),
            lease_token=token,
            lease_owner="fixture-worker",
            lease_acquired_at=now,
            lease_expires_at=now + timedelta(minutes=5),
        )
        run.id = "reply-completed"
        db.add(run)
        await db.commit()
        run_id = run.id
        intervention_id = intervention.id

    await finalize_run(
        AsyncSessionLocal,
        RunLease(
            run_id=run_id,
            token=token,
            owner="fixture-worker",
            version=version,
            status_before="queued",
            phase_before="awaiting_model",
            checkpoint=None,
            started_before_claim=True,
        ),
    )
    async with AsyncSessionLocal() as db:
        terminal = await db.get(Intervention, intervention_id)
        assert terminal is not None
        assert terminal.state == "resolved"
        assert terminal.outcome == "run_completed"
        assert terminal.resolved_at is not None


@pytest.mark.asyncio
async def test_queued_reply_stays_dispatchable_and_duplicate_acceptance_is_rejected() -> None:
    async with AsyncSessionLocal() as db:
        session, intervention = await _active_intervention(db)
        queued = await agent_api.enqueue_message(
            QueuedMessageCreate(
                objective="queued typed reply",
                session_id=session.id,
                reply_to_intervention_id=intervention.id,
            ),
            db,
        )
        queued_id = queued.id
        intervention_id = intervention.id
        with pytest.raises(HTTPException) as exc_info:
            await agent_api.enqueue_message(
                QueuedMessageCreate(
                    objective="duplicate typed reply",
                    session_id=session.id,
                    reply_to_intervention_id=intervention.id,
                ),
                db,
            )
        assert exc_info.value.status_code == 409
        await db.rollback()

    async with AsyncSessionLocal() as db:
        accepted = await db.get(Intervention, intervention_id)
        queued = await db.get(QueuedMessage, queued_id)
        assert accepted is not None and accepted.state == "replied"
        assert queued is not None
        run = await dispatch_queued_message(db, queued, owner_id="local")
        await db.commit()
        run_id = run.id

    await terminate_run(
        AsyncSessionLocal,
        run_id,
        status="failed",
        reason_code="fixture_queue_failure",
        summary="queued reply terminal",
    )
    async with AsyncSessionLocal() as db:
        terminal = await db.get(Intervention, intervention_id)
        assert terminal is not None
        assert terminal.state == "resolved"
        assert terminal.outcome == "run_failed"


@pytest.mark.asyncio
async def test_restart_repairs_terminal_reply_outcome() -> None:
    async with AsyncSessionLocal() as db:
        session, intervention = await _active_intervention(db)
        intervention.state = "replied"
        intervention.outcome = "reply_received"
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="legacy terminal reply window",
            reply_to_intervention_id=intervention.id,
            status="completed",
            phase="terminal",
            completed_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
        intervention_id = intervention.id

    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        run_id,
        scope_valid=True,
    ) is False
    async with AsyncSessionLocal() as db:
        repaired = await db.get(Intervention, intervention_id)
        assert repaired is not None
        assert repaired.state == "resolved"
        assert repaired.outcome == "run_completed"
        assert repaired.resolved_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate_success", [False, True])
async def test_progress_checkin_uses_effective_occurred_at_not_recent_projection_events(
    monkeypatch: pytest.MonkeyPatch,
    invalidate_success: bool,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "AGENT_PROGRESS_CHECKIN_HOURS", 24)
    monkeypatch.setattr(settings, "AGENT_CANDIDATE_COOLDOWN_MINUTES", 0)
    async with AsyncSessionLocal() as db:
        plan = Plan(
            owner_id="local",
            title=f"Evidence activity {invalidate_success}",
            status="active",
            created_at=now - timedelta(days=10),
        )
        db.add(plan)
        await db.flush()
        success, _ = await append_observation(
            db,
            owner_id="local",
            source_type="task_evidence",
            source_id=f"late-recorded:{invalidate_success}",
            outcome="accepted",
            idempotency_key=f"late-recorded:{invalidate_success}",
            plan_id=plan.id,
            occurred_at=now - timedelta(days=5),
            payload={"has_text": True},
        )
        if invalidate_success:
            await append_observation(
                db,
                owner_id="local",
                source_type="operation",
                source_id=f"invalidate:{success.id}",
                outcome="invalidated",
                idempotency_key=f"invalidate:{success.id}",
                plan_id=plan.id,
                occurred_at=now,
                fact_kind="invalidation",
                target_observation_id=success.id,
                reason_code="TEST_INVALIDATION",
                evidence_role="control",
                include_task_assesses=False,
            )
        # This recent event is only a mutable projection of the producer. It
        # must not resurrect invalidated Evidence or substitute recorded_at for
        # the old fact's occurred_at.
        db.add(
            LearningEvent(
                owner_id="local",
                plan_id=plan.id,
                event_type="submission.checked",
                summary="recent projection of an old Evidence fact",
                occurred_at=now,
            )
        )
        await db.commit()
        plan_id = plan.id

    candidate = await ProactiveScheduler()._next_candidate()
    assert candidate is not None
    assert candidate["reason"] == "progress_checkin_due"
    assert candidate["plan_id"] == plan_id
    assert datetime.fromisoformat(candidate["candidate_payload"]["last_activity_at"]) < (
        now - timedelta(hours=24)
    )
