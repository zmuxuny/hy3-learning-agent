"""User approval time is durable idle time, separate from execution and usage."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app.core.config import settings
from app.core.time import frozen_utc
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, RunApproval, RunEvent, ToolInvocation
from app.runtime.agent import AgentRuntime
from app.runtime.budget import budget_reason, normalize_budget, refresh_elapsed
from app.runtime.checkpoints import make_checkpoint, normalize_checkpoint
from app.runtime.state import (
    claim_run,
    decide_approval,
    finalize_child,
    finalize_run,
    pause_for_approval,
    prepare_finalization,
    reconcile_run_after_restart,
    terminate_run,
)
from sqlalchemy import select

START = datetime(2026, 9, 6, tzinfo=timezone.utc)
USAGE = {
    "model_calls": 2, "tool_calls": 3, "network_requests": 1,
    "prompt_tokens": 123, "completion_tokens": 45, "estimated_cost_usd": 0.25,
}


def at(seconds):
    return frozen_utc(START + timedelta(seconds=seconds))


async def stored(run_id):
    async with AsyncSessionLocal() as db:
        return await db.get(AgentRun, run_id)


async def seed(*, parent_run_id=None, budget=None):
    with at(0):
        async with AsyncSessionLocal() as db:
            run = AgentRun(
                owner_id="local", trigger="subagent" if parent_run_id else "user_message",
                parent_run_id=parent_run_id, objective="Approval elapsed probe", created_at=START,
                checkpoint_schema_version=1,
                checkpoint=make_checkpoint(
                    kind="subagent" if parent_run_id else "agent", phase="awaiting_model",
                    step=0, messages=[{"role": "user", "content": "Choose an option"}],
                    budget_usage=normalize_budget(USAGE) if budget is None else budget,
                ),
            )
            db.add(run)
            await db.commit()
            run_id = run.id
        lease = await claim_run(AsyncSessionLocal, run_id)
        assert lease is not None
    return run_id, lease


async def pause(run_id, lease, seconds, *, call_id="approval-1"):
    call = {"id": call_id, "name": "plan_create", "arguments": "{}"}
    with at(seconds):
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            checkpoint = normalize_checkpoint(run.checkpoint)
            checkpoint["current_tool_call"] = call
            checkpoint["messages"].append({
                "role": "assistant", "content": "Please confirm an option.",
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": call["name"], "arguments": call["arguments"],
                }}],
            })
            db.add(ToolInvocation(
                owner_id="local", run_id=run_id, idempotency_key=f"approval:{run_id}:{call_id}",
                tool_name=call["name"], tool_call_id=call_id, args_hash="a" * 64,
                request_digest="a" * 64, canonical_args={}, effect_kind="database_write",
                status="pending_approval", result_payload={"approval_required": True},
            ))
            await db.commit()
            return await pause_for_approval(
                db, lease, checkpoint=checkpoint, tool_call=call,
                remaining_tool_calls=[], reason="Need a user decision",
            )


async def answer(run_id, seconds):
    with at(seconds):
        async with AsyncSessionLocal() as db:
            return await decide_approval(
                db, run_id, owner_id="local", approved=False, note=None, answer="Option B",
            )


def assert_usage(budget):
    assert {key: budget[key] for key in USAGE} == USAGE


@pytest.mark.asyncio
async def test_multiple_approvals_and_restarts_preserve_one_wait_total_and_real_start():
    run_id, lease = await seed()
    paused = await pause(run_id, lease, 10)
    assert paused.budget_usage["elapsed_ms"] == 10_000
    assert paused.budget_usage["approval_wait_ms"] == 0
    with at(500):
        assert not await reconcile_run_after_restart(AsyncSessionLocal, run_id, scope_valid=True)
        assert await claim_run(AsyncSessionLocal, run_id) is None
    first = await answer(run_id, 1030)  # 10 seconds executing, 17 minutes awaiting the user.
    assert first.budget_usage["approval_wait_ms"] == 1_020_000
    assert first.budget_usage["elapsed_ms"] == 10_000
    duplicate = await answer(run_id, 1032)
    assert duplicate.budget_usage == first.budget_usage
    with at(1035):
        lease = await claim_run(AsyncSessionLocal, run_id)
    with at(1040):
        assert await reconcile_run_after_restart(AsyncSessionLocal, run_id, scope_valid=True)
    with at(1045):
        lease = await claim_run(AsyncSessionLocal, run_id)
        assert lease.checkpoint["budget_usage"]["elapsed_ms"] == 25_000
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            await AgentRuntime()._restore_approval(db, run, lease, lease.checkpoint)
    await pause(run_id, lease, 1050, call_id="approval-2")
    second = await answer(run_id, 1650)  # A second 10-minute approval wait.
    assert second.budget_usage["approval_wait_ms"] == 1_620_000
    assert second.budget_usage["elapsed_ms"] == 30_000
    with at(1655):
        lease = await claim_run(AsyncSessionLocal, run_id)
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            checkpoint = await AgentRuntime()._restore_approval(db, run, lease, lease.checkpoint)
    with at(1660):
        await prepare_finalization(AsyncSessionLocal, lease, checkpoint=checkpoint, final_text="Done")
        completed = await finalize_run(AsyncSessionLocal, lease)
    assert completed.status == "completed" and completed.started_at == START
    assert completed.budget_usage["elapsed_ms"] == 40_000
    assert completed.budget_usage["approval_wait_ms"] == 1_620_000
    assert_usage(completed.budget_usage)
    async with AsyncSessionLocal() as db:
        approvals = (await db.execute(select(RunApproval).where(RunApproval.run_id == run_id))).scalars().all()
        assert len(approvals) == 2 and all(row.consumed_at is not None for row in approvals)
        event = (await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.event_type == "run.completed",
        ))).scalar_one()
    assert event.payload["budget_usage"] == completed.budget_usage


@pytest.mark.asyncio
async def test_legacy_checkpoint_rebases_old_elapsed_without_resetting_usage():
    run_id, lease = await seed()
    await pause(run_id, lease, 10)
    await answer(run_id, 1030)
    # Emulate an old worker which included the first wait in its stored elapsed.
    with at(1040):
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            checkpoint = deepcopy(run.checkpoint)
            legacy = dict(USAGE, elapsed_ms=1_040_000, stopped_reason="")
            checkpoint["budget_usage"] = legacy
            run.checkpoint = checkpoint
            run.budget_usage = legacy
            await db.commit()
    with at(1045):
        lease = await claim_run(AsyncSessionLocal, run_id)
    budget = lease.checkpoint["budget_usage"]
    assert budget["elapsed_ms"] == 25_000 and budget["approval_wait_ms"] == 1_020_000
    assert_usage(budget)
    with at(1050):
        assert await reconcile_run_after_restart(AsyncSessionLocal, run_id, scope_valid=True)
        lease = await claim_run(AsyncSessionLocal, run_id)
    assert lease.checkpoint["budget_usage"]["elapsed_ms"] == 30_000
    assert lease.checkpoint["budget_usage"]["approval_wait_ms"] == 1_020_000
    assert (await stored(run_id)).started_at == START


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "failed", "scope_unavailable"])
async def test_terminal_during_approval_closes_wait_once(terminal):
    run_id, lease = await seed()
    await pause(run_id, lease, 10)
    with at(1030):
        if terminal == "scope_unavailable":
            assert not await reconcile_run_after_restart(AsyncSessionLocal, run_id, scope_valid=False)
        else:
            await terminate_run(
                AsyncSessionLocal, run_id, status=terminal,
                reason_code="synthetic_terminal", summary="Stopped while waiting",
            )
    ended = await stored(run_id)
    assert ended.budget_usage["elapsed_ms"] == 10_000
    assert ended.budget_usage["approval_wait_ms"] == 1_020_000
    assert_usage(ended.budget_usage)
    with at(2000):
        await terminate_run(
            AsyncSessionLocal, run_id, status="cancelled", reason_code="duplicate", summary="Duplicate",
        )
    assert (await stored(run_id)).budget_usage == ended.budget_usage


@pytest.mark.asyncio
async def test_child_time_is_preserved_without_inheriting_parent_wait():
    parent_id, parent_lease = await seed()
    child_id, child_lease = await seed(parent_run_id=parent_id)
    await pause(parent_id, parent_lease, 10)
    await answer(parent_id, 1030)
    with at(1040):
        child = await finalize_child(
            AsyncSessionLocal, child_lease, status="completed", report="Child result", role="Reader",
        )
    assert child.id == child_id
    assert child.budget_usage["approval_wait_ms"] == 0
    assert child.budget_usage["elapsed_ms"] == 1_040_000
    parent = await stored(parent_id)
    assert parent.budget_usage["approval_wait_ms"] == 1_020_000
    assert_usage(parent.budget_usage)
    async with AsyncSessionLocal() as db:
        event = (await db.execute(select(RunEvent).where(
            RunEvent.run_id == parent_id, RunEvent.event_type == "subagent.completed",
        ))).scalar_one()
    assert event.payload["budget_usage"] == child.budget_usage


@pytest.mark.asyncio
async def test_clock_rollback_during_approval_does_not_create_negative_wait():
    run_id, lease = await seed()
    await pause(run_id, lease, 10)
    decided = await answer(run_id, 5)
    assert decided.budget_usage["approval_wait_ms"] == 0
    assert decided.budget_usage["elapsed_ms"] == 10_000
    with at(7):
        lease = await claim_run(AsyncSessionLocal, run_id)
    assert lease.checkpoint["budget_usage"]["elapsed_ms"] == 10_000
    assert_usage(lease.checkpoint["budget_usage"])


@pytest.mark.parametrize(
    "elapsed,wait,seconds,expected",
    [(0, 0, -1, 0), (-50, 0, -1, 0), (7, 1000, -1, 7),
     (0, -5000, 1, 1000), (0, 2000, 1, 0)],
)
def test_elapsed_is_nonnegative_and_never_credits_a_negative_wait(elapsed, wait, seconds, expected):
    budget = normalize_budget({"elapsed_ms": elapsed, "approval_wait_ms": wait})
    refresh_elapsed(budget, START.replace(tzinfo=None), ended_at=START + timedelta(seconds=seconds))
    assert budget["elapsed_ms"] == expected


def test_execution_limit_excludes_wait_but_still_expires(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_ELAPSED_SECONDS", 60)
    monkeypatch.setattr(settings, "AGENT_MAX_ESTIMATED_COST_USD", 0)
    budget = normalize_budget({**USAGE, "approval_wait_ms": 1_020_000})
    with at(1070):
        assert budget_reason(budget, started_at=START) is None
    with at(1090):
        assert budget_reason(budget, started_at=START) == "elapsed_limit"
    assert budget["elapsed_ms"] == 70_000
    assert_usage(budget)


@pytest.mark.asyncio
async def test_runtime_reaches_model_after_seventeen_minute_approval(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_ELAPSED_SECONDS", 60)
    monkeypatch.setattr(settings, "AGENT_MAX_ESTIMATED_COST_USD", 0)
    monkeypatch.setattr(settings, "MODEL_INPUT_PRICE_PER_1M", 1)
    monkeypatch.setattr(settings, "MODEL_OUTPUT_PRICE_PER_1M", 4)
    run_id, lease = await seed()
    await pause(run_id, lease, 10)
    await answer(run_id, 1030)
    model_calls = []

    async def complete(**kwargs):
        model_calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="已收到你的选择。", reasoning_content=None, tool_calls=None,
            ))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    with at(1035):
        await runtime.run(run_id)
    completed = await stored(run_id)
    assert len(model_calls) == 1
    assert completed.status == "completed" and completed.output == "已收到你的选择。"
    assert completed.started_at == START
    budget = completed.budget_usage
    assert budget["elapsed_ms"] == 15_000 and budget["approval_wait_ms"] == 1_020_000
    assert budget["model_calls"] == 3 and budget["tool_calls"] == 3
    assert budget["prompt_tokens"] == 223 and budget["completion_tokens"] == 65
    assert budget["estimated_cost_usd"] == pytest.approx(0.25018)
    assert budget["stopped_reason"] == ""
