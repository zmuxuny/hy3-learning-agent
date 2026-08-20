"""H3 durable Run state machine, approval, fencing and replay tests."""

from __future__ import annotations

import asyncio
import ast
import copy
import json
import random
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools as tool_package
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    ChatMessage,
    QueuedMessage,
    Plan,
    ReviewSchedule,
    RunApproval,
    RunEvent,
    RunSteerMessage,
    Session,
    ToolInvocation,
)
from app.api.agent import (
    cancel_run,
    delete_queued_message,
    enqueue_message,
    send_queued_message,
    update_queued_message,
)
from app.runtime.agent import AgentRuntime
from app.runtime.checkpoints import make_checkpoint
from app.runtime.state import (
    RunLeaseLostError,
    RunStateError,
    claim_run,
    decide_approval,
    ensure_root_scope_available,
    finalize_child,
    finalize_run,
    maintain_run_lease,
    pause_for_approval,
    persist_checkpoint,
    prepare_finalization,
    record_steer,
    reconcile_run_after_restart,
    schedule_retry,
    terminate_run,
)
from app.core.time import utc_now
from app.core.config import settings
from app.schemas import QueuedMessageCreate, QueuedMessageMutation, QueuedMessageUpdate


class ProcessCrash(BaseException):
    pass


def _checkpoint(*, kind: str = "agent", text: str = "state") -> dict:
    return make_checkpoint(
        kind=kind,
        phase="awaiting_model",
        step=0,
        messages=[{"role": "user", "content": text}],
        budget_usage={"model_calls": 0, "tool_calls": 0, "elapsed_ms": 0},
    )


async def _seed_root_with_queued_successor(title: str) -> tuple[str, str, str]:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title=title)
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="finish the active turn",
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(),
        )
        queued = QueuedMessage(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="durable successor",
            user_content="durable successor",
            position=0,
        )
        db.add_all((run, queued))
        await db.commit()
        return run.id, session.id, queued.id


async def _load_persisted_checkpoint(run_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None and run.checkpoint is not None
        return copy.deepcopy(run.checkpoint)


@pytest.mark.asyncio
async def test_claim_is_single_winner_and_stale_worker_is_fenced() -> None:
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="subagent",
            objective="one durable owner",
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(kind="subagent"),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    claims = await asyncio.gather(*[
        claim_run(AsyncSessionLocal, run_id, worker_id=f"worker-{index}")
        for index in range(8)
    ])
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    stale = winners[0]

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )
        await db.commit()
    replacement = await claim_run(AsyncSessionLocal, run_id, worker_id="replacement")
    assert replacement is not None and replacement.token != stale.token

    async with AsyncSessionLocal() as db:
        with pytest.raises(RunLeaseLostError):
            await persist_checkpoint(
                db,
                stale,
                _checkpoint(kind="subagent", text="stale write"),
                phase="awaiting_model",
            )
    async with AsyncSessionLocal() as db:
        stored = await persist_checkpoint(
            db,
            replacement,
            _checkpoint(kind="subagent", text="replacement write"),
            phase="awaiting_model",
        )
    assert stored.checkpoint["messages"][0]["content"] == "replacement write"


@pytest.mark.asyncio
async def test_live_lease_heartbeat_prevents_claim_during_long_external_wait(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_RUN_LEASE_SECONDS", 0.15)
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="subagent",
            objective="long model wait",
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(kind="subagent"),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    lease = await claim_run(AsyncSessionLocal, run_id, worker_id="live-worker")
    assert lease is not None
    async with maintain_run_lease(AsyncSessionLocal, lease):
        # Cross the original expiry more than once while simulating a model or
        # network wait. A second worker must still lose the durable claim.
        await asyncio.sleep(0.4)
        replacement = await claim_run(
            AsyncSessionLocal,
            run_id,
            worker_id="would-be-replacement",
        )
        assert replacement is None
        async with AsyncSessionLocal() as db:
            stored = await db.get(AgentRun, run_id)
            assert stored is not None
            assert stored.lease_token == lease.token
            assert stored.lease_expires_at > utc_now()


@pytest.mark.asyncio
async def test_multi_tool_recovery_preserves_order_and_replays_committed_effect_once(
    monkeypatch,
) -> None:
    """A committed tool effect may replay, but its domain fact must not duplicate."""

    due_times = [utc_now() + timedelta(days=offset) for offset in (1, 2)]
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="multi-tool durable ordering")
        db.add(plan)
        await db.flush()
        calls = [
            {
                "id": f"call-review-{index}",
                "name": "review_schedule",
                "arguments": json.dumps(
                    {
                        "plan_id": plan.id,
                        "due_at": due_at.isoformat(),
                        "review_type": "quiz",
                    },
                    sort_keys=True,
                ),
            }
            for index, due_at in enumerate(due_times, start=1)
        ]
        checkpoint = make_checkpoint(
            kind="agent",
            phase="tool_ready",
            step=0,
            messages=[
                {"role": "user", "content": "schedule two reviews"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in calls
                    ],
                },
            ],
            current_tool_call=calls[0],
            remaining_tool_calls=[calls[1]],
        )
        run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            trigger="user_message",
            objective="schedule two reviews in order",
            checkpoint_schema_version=1,
            checkpoint=checkpoint,
        )
        db.add(run)
        await db.commit()
        run_id = run.id
        plan_id = plan.id

    original_execute_tool = tool_package.execute_tool
    attempts: list[str] = []
    killed_after_second_commit = False

    async def kill_after_second_commit(name, arguments, context):
        nonlocal killed_after_second_commit
        result = await original_execute_tool(name, arguments, context)
        attempts.append(str(context.tool_call_id))
        if context.tool_call_id == calls[1]["id"] and not killed_after_second_commit:
            killed_after_second_commit = True
            raise ProcessCrash("killed after durable tool effect and before Run checkpoint")
        return result

    monkeypatch.setattr(tool_package, "execute_tool", kill_after_second_commit)
    with pytest.raises(ProcessCrash):
        await AgentRuntime().run(run_id)

    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        run_id,
        scope_valid=True,
    ) is True

    model_messages: list[list[dict]] = []
    resumed_runtime = AgentRuntime()

    async def final_model(_db, _run, messages, *_args, **_kwargs):
        model_messages.append(copy.deepcopy(messages))
        return SimpleNamespace(
            content="two reviews scheduled",
            reasoning_content=None,
            tool_calls=None,
        ), None

    monkeypatch.setattr(resumed_runtime, "_call_model", final_model)
    await resumed_runtime.run(run_id, resume=True)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        reviews = list((await db.execute(
            select(ReviewSchedule)
            .where(ReviewSchedule.plan_id == plan_id)
            .order_by(ReviewSchedule.due_at)
        )).scalars())
        invocations = list((await db.execute(
            select(ToolInvocation)
            .where(ToolInvocation.run_id == run_id)
            .order_by(ToolInvocation.id)
        )).scalars())
        completions = list((await db.execute(
            select(RunEvent)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "tool.completed",
            )
            .order_by(RunEvent.sequence)
        )).scalars())

    assert completed is not None and completed.status == "completed"
    assert attempts == [calls[0]["id"], calls[1]["id"], calls[1]["id"]]
    assert [review.due_at for review in reviews] == due_times
    assert [(item.tool_call_id, item.status) for item in invocations] == [
        (calls[0]["id"], "committed"),
        (calls[1]["id"], "committed"),
    ]
    assert [event.payload["tool_call_id"] for event in completions] == [
        calls[0]["id"],
        calls[1]["id"],
    ]
    assert [
        item["tool_call_id"]
        for item in model_messages[0]
        if item.get("role") == "tool"
    ] == [calls[0]["id"], calls[1]["id"]]


@pytest.mark.asyncio
async def test_approval_answer_is_durable_idempotent_and_conflict_checked() -> None:
    call = {"id": "approval-answer", "name": "plan_create", "arguments": "{}"}
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="approval facts",
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    lease = await claim_run(AsyncSessionLocal, run_id)
    assert lease is not None
    async with AsyncSessionLocal() as db:
        db.add(ToolInvocation(
            owner_id="local",
            run_id=run_id,
            idempotency_key="approval-answer-key",
            tool_name="plan_create",
            tool_call_id=call["id"],
            args_hash="a" * 64,
            request_digest="a" * 64,
            canonical_args={},
            effect_kind="database_write",
            status="pending_approval",
            result_payload={"approval_required": True},
        ))
        await db.commit()
    async with AsyncSessionLocal() as db:
        await pause_for_approval(
            db,
            lease,
            checkpoint=lease.checkpoint or _checkpoint(),
            tool_call=call,
            remaining_tool_calls=[],
            reason="need a durable answer",
        )
    async with AsyncSessionLocal() as db:
        first = await decide_approval(
            db,
            run_id,
            owner_id="local",
            approved=False,
            note="use the alternative",
            answer="choose B",
        )
        assert first.status == "queued"
    async with AsyncSessionLocal() as db:
        duplicate = await decide_approval(
            db,
            run_id,
            owner_id="local",
            approved=False,
            note="use the alternative",
            answer="choose B",
        )
        assert duplicate.status == "queued"
    async with AsyncSessionLocal() as db:
        with pytest.raises(RunStateError, match="approval_decision_conflict"):
            await decide_approval(
                db,
                run_id,
                owner_id="local",
                approved=False,
                note="different",
                answer="choose B",
            )
        await db.rollback()
        approval = (await db.execute(select(RunApproval))).scalars().one()
        invocation = (await db.execute(select(ToolInvocation))).scalars().one()
        resolved = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "approval.resolved",
        ))).scalars())
    assert approval.decision == "answer"
    assert approval.decided_at is not None and approval.answer == "choose B"
    assert invocation.status == "rejected" and invocation.claim_token is None
    assert len(resolved) == 1 and resolved[0].payload["decision"] == "answer"


@pytest.mark.asyncio
async def test_finalization_kill_rolls_back_whole_terminal_set_then_replays_once() -> None:
    run_id, session_id, queued_id = await _seed_root_with_queued_successor(
        "atomic finalization"
    )
    lease = await claim_run(AsyncSessionLocal, run_id)
    assert lease is not None
    prepared = await prepare_finalization(
        AsyncSessionLocal,
        lease,
        checkpoint=lease.checkpoint or _checkpoint(),
        final_text="one durable answer",
    )
    assert prepared.action == "finalize"

    commit_attempted = False

    def crash_terminal_commit(_sync_session) -> None:
        nonlocal commit_attempted
        commit_attempted = True
        raise ProcessCrash("kill before terminal and successor commit")

    event.listen(AsyncSession.sync_session_class, "before_commit", crash_terminal_commit)
    try:
        with pytest.raises(ProcessCrash):
            await finalize_run(AsyncSessionLocal, lease)
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", crash_terminal_commit)
    assert commit_attempted is True

    async with AsyncSessionLocal() as db:
        interrupted = await db.get(AgentRun, run_id)
        queued = await db.get(QueuedMessage, queued_id)
        session_runs = list((await db.execute(select(AgentRun).where(
            AgentRun.session_id == session_id,
        ))).scalars())
        assert (interrupted.status, interrupted.phase) == ("running", "finalizing")
        assert queued is not None and queued.position == 0
        assert [item.id for item in session_runs] == [run_id]
        assert list((await db.execute(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role.in_(["assistant", "user"]),
        ))).scalars()) == []
        assert list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type.in_(["assistant.message", "run.completed"]),
        ))).scalars()) == []

    completed = await finalize_run(AsyncSessionLocal, lease)
    assert completed.status == "completed" and completed.output == "one durable answer"
    successor_id = getattr(completed, "_queued_successor_id", None)
    assert successor_id is not None
    async with AsyncSessionLocal() as db:
        messages = list((await db.execute(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
        ))).scalars())
        events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type.in_(["assistant.message", "run.completed"]),
        ))).scalars())
        successor = await db.get(AgentRun, successor_id)
        consumed = await db.get(QueuedMessage, queued_id)
    assert successor is not None and successor.status == "queued"
    assert successor.objective == "durable successor" and consumed is None
    assert sorted((message.role, message.run_id, message.message_key) for message in messages) == [
        ("assistant", run_id, f"run:{run_id}:final"),
        ("user", successor_id, f"run:{successor_id}:input"),
    ]
    assert [item.event_type for item in events] == ["assistant.message", "run.completed"]

    # Simulate a process exit after the atomic commit but before the caller can
    # start its in-memory task. Startup must rediscover and reclaim the durable
    # successor Run without relying on ``_queued_successor_id``.
    from app.main import reconcile_interrupted_runs

    assert await reconcile_interrupted_runs() == [successor_id]
    successor_lease = await claim_run(
        AsyncSessionLocal,
        successor_id,
        worker_id="restarted-successor",
    )
    assert successor_lease is not None


@pytest.mark.asyncio
async def test_cancel_kill_rolls_back_terminal_and_successor_then_replays_once() -> None:
    run_id, session_id, queued_id = await _seed_root_with_queued_successor(
        "atomic cancellation"
    )
    lease = await claim_run(AsyncSessionLocal, run_id, worker_id="cancel-owner")
    assert lease is not None

    commit_attempted = False

    def crash_cancel_commit(_sync_session) -> None:
        nonlocal commit_attempted
        commit_attempted = True
        raise ProcessCrash("kill before cancellation and successor commit")

    event.listen(AsyncSession.sync_session_class, "before_commit", crash_cancel_commit)
    try:
        with pytest.raises(ProcessCrash):
            await terminate_run(
                AsyncSessionLocal,
                run_id,
                status="cancelled",
                reason_code="test_cancelled",
                summary="cancel the active turn",
                lease=lease,
            )
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", crash_cancel_commit)
    assert commit_attempted is True

    async with AsyncSessionLocal() as db:
        interrupted = await db.get(AgentRun, run_id)
        queued = await db.get(QueuedMessage, queued_id)
        session_runs = list((await db.execute(select(AgentRun).where(
            AgentRun.session_id == session_id,
        ))).scalars())
        cancelled_events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "run.cancelled",
        ))).scalars())
    assert interrupted is not None and interrupted.status == "running"
    assert queued is not None and queued.position == 0
    assert [item.id for item in session_runs] == [run_id]
    assert cancelled_events == []

    cancelled = await terminate_run(
        AsyncSessionLocal,
        run_id,
        status="cancelled",
        reason_code="test_cancelled",
        summary="cancel the active turn",
        lease=lease,
    )
    assert cancelled is not None and cancelled.status == "cancelled"
    successor_id = getattr(cancelled, "_queued_successor_id", None)
    assert successor_id is not None
    async with AsyncSessionLocal() as db:
        successor = await db.get(AgentRun, successor_id)
        consumed = await db.get(QueuedMessage, queued_id)
        successor_messages = list((await db.execute(select(ChatMessage).where(
            ChatMessage.run_id == successor_id,
        ))).scalars())
        cancelled_events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "run.cancelled",
        ))).scalars())
    assert successor is not None and successor.status == "queued"
    assert successor.objective == "durable successor" and consumed is None
    assert [(message.role, message.content) for message in successor_messages] == [
        ("user", "durable successor")
    ]
    assert len(cancelled_events) == 1

    from app.main import reconcile_interrupted_runs

    assert await reconcile_interrupted_runs() == [successor_id]
    successor_lease = await claim_run(
        AsyncSessionLocal,
        successor_id,
        worker_id="restarted-after-cancel",
    )
    assert successor_lease is not None


@pytest.mark.asyncio
async def test_steer_after_finalizing_is_one_durable_successor_turn() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="late steer")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="finish current turn",
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    lease = await claim_run(AsyncSessionLocal, run_id)
    assert lease is not None
    await prepare_finalization(
        AsyncSessionLocal,
        lease,
        checkpoint=lease.checkpoint or _checkpoint(),
        final_text="current answer",
    )
    async with AsyncSessionLocal() as db:
        _, steer = await record_steer(
            db,
            run_id,
            owner_id="local",
            content="continue with this correction",
        )
        steer_id = steer.id
    completed = await finalize_run(AsyncSessionLocal, lease)
    successor_id = getattr(completed, "_queued_successor_id", None)
    assert successor_id is not None

    async with AsyncSessionLocal() as db:
        queued = list((await db.execute(select(QueuedMessage))).scalars())
        message = (await db.execute(select(ChatMessage).where(
            ChatMessage.role == "user",
        ))).scalars().one()
        successor = await db.get(AgentRun, successor_id)
        stored_steer = await db.get(RunSteerMessage, steer_id)
    assert queued == []
    assert successor is not None and successor.status == "queued"
    assert successor.objective == "continue with this correction"
    assert (message.run_id, message.message_key) == (
        successor_id,
        f"run:{successor_id}:input",
    )
    assert message.message_metadata["steer_id"] == steer_id
    assert stored_steer is not None
    assert stored_steer.disposition == "queued" and stored_steer.disposed_at is not None


@pytest.mark.asyncio
async def test_queue_edit_delete_and_send_use_versions_and_contiguous_order(monkeypatch) -> None:
    monkeypatch.setattr("app.api.agent._start_runtime", lambda _run_id, **_kwargs: None)
    async with AsyncSessionLocal() as db:
        first = await enqueue_message(QueuedMessageCreate(objective="first"), db)
        second = await enqueue_message(QueuedMessageCreate(objective="second"), db)
        first_id = first.id
        second_id = second.id
        moved = await update_queued_message(
            first.id,
            QueuedMessageUpdate(expected_version=first.version, position=1),
            db,
        )
        await db.refresh(second)
        assert (moved.position, moved.version) == (1, 2)
        assert (second.position, second.version) == (0, 2)

        with pytest.raises(HTTPException) as stale_edit:
            await update_queued_message(
                second_id,
                QueuedMessageUpdate(expected_version=1, objective="stale overwrite"),
                db,
            )
        assert stale_edit.value.status_code == 409
        await db.rollback()

        second = await db.get(QueuedMessage, second_id)
        assert second is not None
        edited = await update_queued_message(
            second.id,
            QueuedMessageUpdate(expected_version=second.version, objective="edited second"),
            db,
        )
        await delete_queued_message(
            edited.id,
            QueuedMessageMutation(expected_version=edited.version),
            db,
        )
        remaining = await db.get(QueuedMessage, first_id)
        assert remaining is not None
        assert (remaining.position, remaining.version) == (0, 3)

        with pytest.raises(HTTPException) as stale_send:
            await send_queued_message(
                remaining.id,
                QueuedMessageMutation(expected_version=2),
                db,
            )
        assert stale_send.value.status_code == 409
        await db.rollback()
        remaining = await db.get(QueuedMessage, first_id)
        assert remaining is not None
        run = await send_queued_message(
            remaining.id,
            QueuedMessageMutation(expected_version=remaining.version),
            db,
        )
        assert run.status == "queued"
        assert await db.get(QueuedMessage, first_id) is None


@pytest.mark.asyncio
async def test_plan_root_arbitration_has_one_winner_and_retry_wait_stays_exclusive() -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="one root scope")
        first_session = Session(owner_id="local", plan_id=None, title="first")
        second_session = Session(owner_id="local", plan_id=None, title="second")
        db.add_all((plan, first_session, second_session))
        await db.flush()
        plan_id = plan.id
        first_session.plan_id = plan_id
        second_session.plan_id = plan_id
        first_session_id = first_session.id
        second_session_id = second_session.id
        await db.commit()

    async def contender(session_id: str) -> str:
        async with AsyncSessionLocal() as db:
            try:
                await ensure_root_scope_available(
                    db,
                    owner_id="local",
                    plan_id=plan_id,
                    session_id=session_id,
                )
            except RunStateError:
                await db.rollback()
                return "busy"
            run = AgentRun(
                owner_id="local",
                session_id=session_id,
                plan_id=plan_id,
                trigger="user_message",
                objective="winner",
                checkpoint_schema_version=1,
                checkpoint=_checkpoint(),
            )
            db.add(run)
            await db.commit()
            return run.id

    results = await asyncio.gather(contender(first_session_id), contender(second_session_id))
    winners = [result for result in results if result != "busy"]
    assert len(winners) == 1 and results.count("busy") == 1
    winner_id = winners[0]

    lease = await claim_run(AsyncSessionLocal, winner_id, worker_id="retry-owner")
    assert lease is not None
    await schedule_retry(
        AsyncSessionLocal,
        lease,
        reason_code="transient_model_error",
        retry_after_seconds=3600,
    )
    assert await contender(second_session_id) == "busy"
    await terminate_run(
        AsyncSessionLocal,
        winner_id,
        status="cancelled",
        reason_code="test_cleanup",
        summary="release root scope",
    )
    replacement = await contender(second_session_id)
    assert replacement != "busy"


@pytest.mark.asyncio
async def test_restart_requeues_future_retry_wait_instead_of_stranding_it() -> None:
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="subagent",
            objective="future retry survives restart",
            status="retry_wait",
            phase="retry_wait",
            available_at=utc_now() + timedelta(hours=1),
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(kind="subagent"),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        run_id,
        scope_valid=True,
    ) is True
    async with AsyncSessionLocal() as db:
        recovered = await db.get(AgentRun, run_id)
    assert recovered is not None
    assert recovered.status == "queued" and recovered.available_at is None
    assert recovered.checkpoint is not None


@pytest.mark.asyncio
async def test_cancel_queues_pending_steer_and_atomically_advances_existing_queue(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        "app.api.agent._start_runtime",
        lambda run_id, **_kwargs: started.append(run_id),
    )
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="cancel continuation")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="active turn",
            status="running",
            checkpoint_schema_version=1,
            checkpoint=_checkpoint(),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
        session_id = session.id
    async with AsyncSessionLocal() as db:
        queued_first = await enqueue_message(
            QueuedMessageCreate(objective="already queued", session_id=session_id),
            db,
        )
        queued_first_id = queued_first.id
    async with AsyncSessionLocal() as db:
        await record_steer(
            db,
            run_id,
            owner_id="local",
            content="preserve me after cancellation",
        )
    async with AsyncSessionLocal() as db:
        cancelled = await cancel_run(run_id, db)
    assert cancelled.status == "cancelled"

    async with AsyncSessionLocal() as db:
        replacement = (await db.execute(select(AgentRun).where(
            AgentRun.session_id == session_id,
            AgentRun.id != run_id,
        ))).scalars().one()
        remaining = list((await db.execute(select(QueuedMessage).where(
            QueuedMessage.session_id == session_id,
        ))).scalars())
        steers = list((await db.execute(select(RunSteerMessage).where(
            RunSteerMessage.run_id == run_id,
        ))).scalars())
        consumed = await db.get(QueuedMessage, queued_first_id)
    assert replacement.objective == "already queued" and replacement.status == "queued"
    assert replacement.id in started
    assert consumed is None
    assert len(remaining) == 1
    assert remaining[0].objective == "preserve me after cancellation"
    assert (remaining[0].position, remaining[0].version) == (0, 2)
    assert steers[0].disposition == "queued" and steers[0].disposed_at is not None


@pytest.mark.asyncio
async def test_root_model_timeout_uses_durable_bounded_retry(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_MODEL_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "AGENT_RUN_MAX_RETRIES", 2)
    monkeypatch.setattr(settings, "AGENT_RUN_RETRY_BACKOFF_SECONDS", 0)

    class RecoveringCompletions:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            if self.calls <= 2:
                raise TimeoutError("fixture transient timeout")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="completed after durable retries",
                    reasoning_content=None,
                    tool_calls=[],
                ))],
                usage=None,
            )

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="retry the root model call",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = RecoveringCompletions()
    from app.runtime.agent import AgentRuntime

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        retry_events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "run.retrying",
        ))).scalars())
    assert completed is not None
    assert completed.status == "completed" and completed.retry_count == 2
    assert completed.output == "completed after durable retries"
    assert len(retry_events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    (
        (TimeoutError("tool wait expired"), "tool_timeout"),
        (ConnectionError("provider unavailable"), "model_retry_exhausted"),
    ),
)
async def test_terminal_failure_reason_distinguishes_tool_and_model_waits(
    monkeypatch,
    failure: Exception,
    expected_reason: str,
) -> None:
    monkeypatch.setattr(settings, "AGENT_RUN_MAX_RETRIES", 0)
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="classify the exhausted external wait",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    lease = await claim_run(AsyncSessionLocal, run_id, worker_id="classification-test")
    assert lease is not None
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        await AgentRuntime()._fail(object(), run, lease, failure)
    async with AsyncSessionLocal() as db:
        failed = await db.get(AgentRun, run_id)
    assert failed is not None
    assert (failed.status, failed.status_reason) == ("failed", expected_reason)


@pytest.mark.asyncio
async def test_fixed_seed_hundred_round_random_failpoints_match_semantic_oracle() -> None:
    rng = random.Random(20260820)
    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="randomized recovery parent",
            status="running",
        )
        db.add(parent)
        await db.commit()
        parent_id = parent.id

    async def create_child(index: int) -> str:
        async with AsyncSessionLocal() as db:
            child = AgentRun(
                owner_id="local",
                parent_run_id=parent_id,
                trigger="subagent",
                objective=f"round-{index}",
                checkpoint_schema_version=1,
                checkpoint=_checkpoint(kind="subagent", text=f"round-{index}"),
            )
            db.add(child)
            await db.commit()
            return child.id

    async def restart_and_reclaim(child_id: str, worker_id: str):
        assert await reconcile_run_after_restart(
            AsyncSessionLocal,
            child_id,
            scope_valid=True,
        ) is True
        lease = await claim_run(AsyncSessionLocal, child_id, worker_id=worker_id)
        assert lease is not None
        return lease

    async def advance_semantics(
        child_id: str,
        *,
        index: int,
        steps: int,
        restart_boundaries: tuple[int, ...],
        retry_once: bool,
    ) -> tuple[object, dict]:
        lease = await claim_run(AsyncSessionLocal, child_id, worker_id=f"round-{child_id}")
        assert lease is not None
        restart_number = 0
        for _ in range(restart_boundaries.count(0)):
            restart_number += 1
            lease = await restart_and_reclaim(
                child_id,
                f"restart-{child_id}-{restart_number}",
            )
        for step in range(steps):
            # Every mutation starts from a fresh read of the committed
            # checkpoint. This prevents a mutable in-process fixture object from
            # becoming the oracle for state that was never durably persisted.
            checkpoint = await _load_persisted_checkpoint(child_id)
            checkpoint["step"] = step + 1
            checkpoint["budget_usage"] = {
                "model_calls": step + 1,
                "tool_calls": index % 3,
                "elapsed_ms": index,
            }
            async with AsyncSessionLocal() as db:
                stored = await persist_checkpoint(
                    db,
                    lease,
                    checkpoint,
                    phase="awaiting_model",
                )
                assert stored.checkpoint is not None
                assert stored.checkpoint["step"] == step + 1
            for _ in range(restart_boundaries.count(step + 1)):
                restart_number += 1
                lease = await restart_and_reclaim(
                    child_id,
                    f"restart-{child_id}-{restart_number}",
                )
        if retry_once:
            await schedule_retry(
                AsyncSessionLocal,
                lease,
                reason_code="fixed_seed_retry",
                retry_after_seconds=0,
            )
            lease = await claim_run(
                AsyncSessionLocal,
                child_id,
                worker_id=f"retry-{child_id}",
            )
            assert lease is not None
        return lease, await _load_persisted_checkpoint(child_id)

    def semantic_checkpoint(checkpoint: dict) -> dict:
        budget = checkpoint.get("budget_usage") or {}
        return {
            "step": checkpoint.get("step"),
            "messages": checkpoint.get("messages"),
            "current_tool_call": checkpoint.get("current_tool_call"),
            "remaining_tool_calls": checkpoint.get("remaining_tool_calls"),
            "budget_usage": {
                "model_calls": budget.get("model_calls", 0),
                "tool_calls": budget.get("tool_calls", 0),
            },
        }

    pairs: list[tuple[int, str, str, dict]] = []
    restart_count = 0
    for index in range(100):
        steps = rng.randrange(3)
        restart_boundaries = tuple(
            rng.randrange(steps + 1)
            for _ in range(rng.randint(1, 3))
        )
        retry_once = rng.randrange(4) == 0
        restart_count += len(restart_boundaries)
        baseline_id = await create_child(index)
        recovered_id = await create_child(index)

        baseline_lease, baseline_checkpoint = await advance_semantics(
            baseline_id,
            index=index,
            steps=steps,
            restart_boundaries=(),
            retry_once=False,
        )
        recovered_lease, recovered_checkpoint = await advance_semantics(
            recovered_id,
            index=index,
            steps=steps,
            restart_boundaries=restart_boundaries,
            retry_once=retry_once,
        )
        independent_oracle = {
            "step": steps,
            "messages": [{"role": "user", "content": f"round-{index}"}],
            "current_tool_call": None,
            "remaining_tool_calls": [],
            "budget_usage": {
                "model_calls": steps,
                "tool_calls": index % 3 if steps else 0,
            },
        }
        assert semantic_checkpoint(baseline_checkpoint) == independent_oracle
        assert semantic_checkpoint(recovered_checkpoint) == independent_oracle

        await finalize_child(
            AsyncSessionLocal,
            baseline_lease,
            status="completed",
            report=f"report-{index}",
            role="fixture",
        )
        await finalize_child(
            AsyncSessionLocal,
            recovered_lease,
            status="completed",
            report=f"report-{index}",
            role="fixture",
        )
        pairs.append((index, baseline_id, recovered_id, independent_oracle["budget_usage"]))

    async with AsyncSessionLocal() as db:
        children = list((await db.execute(select(AgentRun).where(
            AgentRun.parent_run_id == parent_id,
        ))).scalars())
        projected = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == parent_id,
            RunEvent.event_type == "subagent.completed",
        ))).scalars())
        completed = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id.in_([child.id for child in children]),
            RunEvent.event_type == "run.completed",
        ))).scalars())
    assert restart_count >= 100
    assert len(children) == len(projected) == len(completed) == 200
    assert all(child.status == "completed" and child.checkpoint is None for child in children)
    assert len({event.event_key for event in projected}) == 200
    assert len({event.event_key for event in completed}) == 200

    children_by_id = {child.id: child for child in children}
    projection_by_child = {event.payload["child_run_id"]: event for event in projected}
    for index, baseline_id, recovered_id, expected_budget in pairs:
        baseline = children_by_id[baseline_id]
        recovered = children_by_id[recovered_id]

        def semantic_child(child: AgentRun) -> dict:
            budget = child.budget_usage or {}
            return {
                "status": child.status,
                "output": child.output,
                "budget_usage": {
                    "model_calls": budget.get("model_calls", 0),
                    "tool_calls": budget.get("tool_calls", 0),
                },
            }

        expected_child = {
            "status": "completed",
            "output": f"report-{index}",
            "budget_usage": expected_budget,
        }
        assert semantic_child(baseline) == expected_child
        assert semantic_child(recovered) == expected_child

        def semantic_projection(child_id: str) -> dict:
            payload = projection_by_child[child_id].payload
            budget = payload.get("budget_usage") or {}
            return {
                "role": payload.get("role"),
                "status": payload.get("status"),
                "report": payload.get("report"),
                "reason_code": payload.get("reason_code"),
                "budget_usage": {
                    "model_calls": budget.get("model_calls", 0),
                    "tool_calls": budget.get("tool_calls", 0),
                },
            }

        assert semantic_projection(baseline_id) == semantic_projection(recovered_id)


def test_agent_run_state_writes_are_centralized() -> None:
    root = Path(__file__).resolve().parents[2]
    inspected = [
        root / "backend/app/runtime/agent.py",
        root / "backend/app/runtime/subagents.py",
        root / "backend/app/api/agent.py",
        root / "backend/app/tools/subagents.py",
        root / "backend/app/tools/planning.py",
    ]
    forbidden = ("run.status =", "run.checkpoint =", "run.pending_approval =", "child.status =", "child.checkpoint =")
    offenders = [
        f"{path.relative_to(root)}:{marker}"
        for path in inspected
        for marker in forbidden
        if marker in path.read_text(encoding="utf-8")
    ]
    for path in inspected:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute)
                else ""
            )
            if function_name != "AgentRun":
                continue
            status = next((item.value for item in node.keywords if item.arg == "status"), None)
            if (
                isinstance(status, ast.Constant)
                and isinstance(status.value, str)
                and status.value != "queued"
            ):
                offenders.append(
                    f"{path.relative_to(root)}:{node.lineno}:AgentRun(status={status.value!r})"
                )
    assert offenders == []
