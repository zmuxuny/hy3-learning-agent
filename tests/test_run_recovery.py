import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.api.agent import decide_run_approval
from app.db.database import AsyncSessionLocal
from app.main import reconcile_interrupted_runs
from app.models import AgentRun, ChatMessage, Plan, RunApproval, RunEvent, Session, ToolInvocation
from app.runtime.agent import AgentRuntime
from app.runtime.checkpoints import normalize_checkpoint
from app.runtime.events import emit_event
from app.runtime.tasks import start_tracked_task, wake_tracked_task
from app.schemas import RunApprovalRequest
from app.services import plans as plan_service


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def plan_payload(title="审批创建的计划"):
    return {
        "title": title,
        "goal": "验证审批后创建计划",
        "current_level": "初级",
        "weekly_minutes": 300,
        "expected_outcome": "可运行的最小工具循环",
        "stages": [{
            "title": "第一阶段",
            "tasks": [{"title": "实现最小工具调用循环", "is_core": True, "evidence_required": True}],
        }],
    }


class ApprovalCompletions:
    def __init__(self):
        self.calls = []
        self.final_text = "计划已按你的批准创建。"

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        step = len(self.calls)
        if step == 1:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="后台发现一个合适的计划目标，先请求批准。",
                reasoning_content=None,
                tool_calls=[FakeToolCall("call-plan", "plan_create", json.dumps(plan_payload(), ensure_ascii=False))],
            ))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=self.final_text,
            reasoning_content=None,
            tool_calls=None,
        ))])


@pytest.mark.asyncio
async def test_sqlite_event_write_retries_transient_lock(monkeypatch):
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="事件锁重试")
        db.add(run)
        await db.commit()
        run_id = run.id

        original_commit = db.commit
        attempts = 0

        async def flaky_commit():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("INSERT run_events", {}, Exception("database is locked"))
            await original_commit()

        monkeypatch.setattr(db, "commit", flaky_commit)
        event = await emit_event(db, run_id, "assistant.status", "已恢复事件写入")

        assert event.sequence == 1
        assert attempts == 2


@pytest.mark.asyncio
async def test_sqlite_events_from_parallel_child_runs_are_serialized():
    async with AsyncSessionLocal() as db:
        runs = [AgentRun(owner_id="local", trigger="subagent", objective=f"并发子任务 {index}") for index in range(3)]
        db.add_all(runs)
        await db.commit()
        run_ids = [run.id for run in runs]

    async def write_event(run_id, index):
        async with AsyncSessionLocal() as event_db:
            await emit_event(event_db, run_id, "tool.completed", f"步骤 {index}")

    await asyncio.gather(*[
        write_event(run_id, index)
        for run_id in run_ids
        for index in range(8)
    ])

    async with AsyncSessionLocal() as db:
        for run_id in run_ids:
            events = list((await db.execute(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
            )).scalars())
            assert [event.sequence for event in events] == list(range(1, 9))


@pytest.mark.asyncio
async def test_blocking_approval_pauses_run_and_approve_resumes(monkeypatch):
    import app.api.agent as agent_api

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="检查计划并创建缺失的计划",
            model="hy3",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = ApprovalCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        paused = await db.get(AgentRun, run_id)
        assert paused.status == "waiting_approval"
        assert paused.pending_approval["tool_call"]["name"] == "plan_create"
        assert isinstance(paused.checkpoint["context_snapshot_id"], int)
        plans = list((await db.execute(select(Plan))).scalars())
        assert plans == []
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        approval_events = [event for event in events if event.event_type == "approval.required"]
        assert approval_events and approval_events[-1].payload["blocking"] is True

    started = {}

    def fake_start(run_id_value, *, wake_key, **kwargs):
        started["wake_key"] = wake_key
        started["kwargs"] = kwargs

    monkeypatch.setattr(agent_api, "_wake_runtime", fake_start)
    async with AsyncSessionLocal() as db:
        queued = await decide_run_approval(run_id, RunApprovalRequest(approved=True), db)
        assert queued.status == "queued"
    assert started["kwargs"] == {"resume": True}
    assert started["wake_key"].startswith("approval:")

    await runtime.run(run_id, resume=True)
    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert completed.output == "计划已按你的批准创建。"
        assert completed.pending_approval is None
        plans = list((await db.execute(select(Plan))).scalars())
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
        assert len(plans) == 1, invocations[0].result_payload if invocations else None
        assert plans[0].title == "审批创建的计划"
        resumed = [event for event in await _events(db, run_id) if event.event_type == "run.resumed"]
        resolved = [event for event in await _events(db, run_id) if event.event_type == "approval.resolved"]
        assert resumed and resolved[0].payload["decision"] == "approve"


@pytest.mark.asyncio
async def test_rejected_approval_feeds_model_and_does_not_write(monkeypatch):
    import app.api.agent as agent_api

    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="heartbeat", objective="创建计划", model="hy3")
        db.add(run)
        await db.commit()
        run_id = run.id

    monkeypatch.setattr(agent_api, "_wake_runtime", lambda _run_id, **_kwargs: None)
    completions = ApprovalCompletions()
    completions.final_text = "你拒绝了这次创建，我不写入计划。"
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        await decide_run_approval(run_id, RunApprovalRequest(approved=False), db)

    await runtime.run(run_id, resume=True)
    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert completed.output == "你拒绝了这次创建，我不写入计划。"
        assert list((await db.execute(select(Plan))).scalars()) == []
        resolved = [event for event in await _events(db, run_id) if event.event_type == "approval.resolved"]
        assert resolved[0].payload["decision"] == "reject"


@pytest.mark.asyncio
async def test_approval_wakeup_hands_off_after_old_task_and_coalesces_duplicates(monkeypatch):
    import app.api.agent as agent_api

    tool_call = {"id": "wakeup-call", "name": "plan_create", "arguments": "{}"}
    approval_id = "approval-wakeup-race"
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="approval wakeup race",
            status="waiting_approval",
            phase="waiting_approval",
            checkpoint_schema_version=1,
            checkpoint=normalize_checkpoint({
                "schema_version": 1,
                "kind": "agent",
                "phase": "waiting_approval",
                "step": 0,
                "messages": [],
                "current_tool_call": tool_call,
                "remaining_tool_calls": [],
                "budget_usage": {},
                "state_version": 1,
            }),
            pending_approval={
                "approval_id": approval_id,
                "tool_call": tool_call,
                "remaining_tool_calls": [],
                "reason": "deterministic wakeup fixture",
                "step": 0,
            },
        )
        db.add(run)
        await db.flush()
        db.add(RunApproval(
            id=approval_id,
            owner_id="local",
            run_id=run.id,
            tool_call_id=tool_call["id"],
            tool_name=tool_call["name"],
            tool_call=tool_call,
            remaining_tool_calls=[],
            reason="deterministic wakeup fixture",
        ))
        await db.commit()
        run_id = run.id

    old_started = asyncio.Event()
    allow_old_to_finish = asyncio.Event()
    resumed_started = asyncio.Event()
    allow_resumed_to_finish = asyncio.Event()
    resumed_finished = asyncio.Event()
    resumed_calls: list[dict] = []

    async def old_pause_task() -> None:
        old_started.set()
        await allow_old_to_finish.wait()

    async def resumed_run(run_id_value: str, **kwargs) -> None:
        resumed_calls.append({"run_id": run_id_value, "kwargs": kwargs})
        resumed_started.set()
        try:
            await allow_resumed_to_finish.wait()
        finally:
            resumed_finished.set()

    monkeypatch.setattr(agent_api.runtime, "run", resumed_run)
    old_task = start_tracked_task(run_id, old_pause_task())
    await old_started.wait()

    async with AsyncSessionLocal() as db:
        first = await decide_run_approval(run_id, RunApprovalRequest(approved=True), db)
    async with AsyncSessionLocal() as db:
        duplicate_before_handoff = await decide_run_approval(
            run_id,
            RunApprovalRequest(approved=True),
            db,
        )
    assert first.status == duplicate_before_handoff.status == "queued"
    assert not resumed_started.is_set()

    allow_old_to_finish.set()
    await old_task
    await asyncio.wait_for(resumed_started.wait(), timeout=1)
    assert resumed_calls == [{"run_id": run_id, "kwargs": {"resume": True}}]

    async with AsyncSessionLocal() as db:
        duplicate_while_running = await decide_run_approval(
            run_id,
            RunApprovalRequest(approved=True),
            db,
        )
    assert duplicate_while_running.status == "queued"
    await asyncio.sleep(0)
    assert len(resumed_calls) == 1

    allow_resumed_to_finish.set()
    await asyncio.wait_for(resumed_finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(resumed_calls) == 1


@pytest.mark.asyncio
async def test_wakeup_replaces_done_owner_before_its_cleanup_callback() -> None:
    run_id = "done-before-cleanup-window"
    wake_key = "approval:done-before-cleanup"
    window_observed = asyncio.Event()
    successor_started = asyncio.Event()
    allow_successor_to_finish = asyncio.Event()
    successor_finished = asyncio.Event()
    owner: dict[str, asyncio.Task] = {}
    successors: list[asyncio.Task] = []
    successor_calls = 0

    async def successor() -> None:
        nonlocal successor_calls
        successor_calls += 1
        successor_started.set()
        try:
            await allow_successor_to_finish.wait()
        finally:
            successor_finished.set()

    def wake_after_done_before_cleanup() -> None:
        assert owner["task"].done()
        window_observed.set()
        successors.append(wake_tracked_task(
            run_id,
            successor(),
            wake_key=wake_key,
        ))

    async def old_owner() -> None:
        # call_soon is queued before Task schedules its done callbacks.  The
        # callback therefore observes done()==True while the old task still
        # occupies the tracked slot.
        asyncio.get_running_loop().call_soon(wake_after_done_before_cleanup)

    owner["task"] = wake_tracked_task(
        run_id,
        old_owner(),
        wake_key=wake_key,
    )
    await asyncio.wait_for(window_observed.wait(), timeout=1)
    await asyncio.wait_for(successor_started.wait(), timeout=1)
    assert successor_calls == 1

    duplicate = wake_tracked_task(run_id, successor(), wake_key=wake_key)
    assert duplicate is successors[0]
    await asyncio.sleep(0)
    assert successor_calls == 1

    allow_successor_to_finish.set()
    await asyncio.wait_for(successor_finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert successor_calls == 1

@pytest.mark.asyncio
async def test_checkpointed_run_is_queued_and_resumes_after_restart(monkeypatch):
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_service_plan_payload())
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="读取计划",
            model="hy3",
            status="running",
            checkpoint_schema_version=1,
            checkpoint=normalize_checkpoint({
                "step": 0,
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "检查计划"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-plan-get",
                            "type": "function",
                            "function": {"name": "plan_get", "arguments": json.dumps({"plan_id": plan.id})},
                        }],
                    },
                ],
                "pending_tool_calls": [{
                    "id": "call-plan-get",
                    "type": "function",
                    "name": "plan_get",
                    "arguments": json.dumps({"plan_id": plan.id}),
                }],
            }),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    resumable = await reconcile_interrupted_runs()
    assert resumable == [run_id]
    async with AsyncSessionLocal() as db:
        queued = await db.get(AgentRun, run_id)
        assert queued.status == "queued"

    class ResumeCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="已读取计划状态。",
                reasoning_content=None,
                tool_calls=None,
            ))])

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=ResumeCompletions()))
    await runtime.run(run_id, resume=True)
    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        tool_events = [event for event in await _events(db, run_id) if event.event_type == "tool.completed"]
        assert tool_events and tool_events[0].payload["name"] == "plan_get"


@pytest.mark.asyncio
async def test_approval_endpoint_rejects_runs_without_pending_request():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="普通对话", model="hy3")
        db.add(run)
        await db.commit()
        run_id = run.id
        with pytest.raises(HTTPException) as error:
            await decide_run_approval(run_id, RunApprovalRequest(approved=True), db)
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_resume_preserves_inline_card_snapshots_in_final_message():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="卡片恢复")
        db.add(session)
        await db.flush()
        card = {
            "kind": "planning_questions",
            "source_run_id": "pending",
            "created_at": "2026-08-09T00:00:00+00:00",
            "intake": {"open_questions": [{"id": "goal", "prompt": "目标是什么？"}]},
        }
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="继续规划",
            status="queued",
            checkpoint_schema_version=1,
            checkpoint=normalize_checkpoint({
                "step": 1,
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "continue"},
                ],
                "pending_tool_calls": [],
                "cards": [card],
            }),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    class FinalCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="已恢复并保留提问卡。",
                reasoning_content=None,
                tool_calls=None,
            ))])

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=FinalCompletions()))
    await runtime.run(run_id, resume=True)

    async with AsyncSessionLocal() as db:
        message = (await db.execute(
            select(ChatMessage).where(ChatMessage.run_id == run_id, ChatMessage.role == "assistant")
        )).scalars().one()
        assert message.message_metadata["cards"][0]["kind"] == "planning_questions"


async def _events(db, run_id):
    return list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())


def plan_service_plan_payload():
    from app.schemas import PlanCreate, StageCreate, TaskCreate

    return PlanCreate(
        title="恢复测试计划",
        goal="验证检查点续跑",
        current_level="初级",
        weekly_minutes=300,
        expected_outcome="可运行",
        stages=[StageCreate(title="阶段一", tasks=[TaskCreate(title="任务一")])],
    )
