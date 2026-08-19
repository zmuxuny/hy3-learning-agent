"""H0 failure baselines for durable Runtime invariants.

These tests describe reviewed Runtime defects and remain strict xfails until
their matching repair lands.  H3-RUN-008 closed early because H2 needed a
durable planning-child intent before model waits; the other H3 gates stay open.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.api.agent as agent_api
import app.tools as tool_package
import app.tools.planning as planning_tools
import app.tools.subagents as subagent_tools
from app.api.agent import decide_run_approval, steer_run
from app.context.memory import MemoryManager
from app.db.database import AsyncSessionLocal
from app.main import reconcile_interrupted_runs
from app.models import (
    AgentRun,
    ChatMessage,
    Plan,
    QueuedMessage,
    RunEvent,
    RunSteerMessage,
    Session,
    ToolInvocation,
)
from app.runtime.agent import AgentRuntime
from app.runtime.events import emit_event as persist_run_event
from app.schemas import RunApprovalRequest, RunSteerCreate
from app.services.queue import dispatch_queued_message
from app.tools import ToolContext, execute_tool


class ProcessCrash(BaseException):
    """A kill-point that bypasses ordinary in-process Exception recovery."""


class HarnessError(RuntimeError):
    """A fixture failure that must never be swallowed by an xfail marker."""


def _require_harness(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str = "{}") -> None:
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


def model_message(content: str = "", tool_calls: list[FakeToolCall] | None = None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content,
            reasoning_content=None,
            tool_calls=tool_calls,
        ))],
        usage=None,
    )


class SequenceCompletions:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise HarnessError("Unexpected extra model call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def fake_client(completions) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def checkpoint(*, pending_calls: list[dict] | None = None) -> dict:
    return {
        "step": 0,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "resume"},
        ],
        "pending_tool_calls": pending_calls or [],
        "cards": [],
    }


def child_checkpoint(*, role: str = "调查", objective: str = "读取本地状态") -> dict:
    return {
        "kind": "subagent",
        "role": role,
        "objective": objective,
        "context": "deterministic local context",
        "allowlist": ["profile_get"],
        "max_steps": 3,
        "step": 0,
        "messages": [],
        "pending_tool_calls": [],
        "tool_calls_used": 0,
    }


def complete_plan_payload() -> dict:
    return {
        "title": "拒绝后不得创建的计划",
        "goal": "验证审批决定可以跨进程恢复",
        "current_level": "初级",
        "weekly_minutes": 120,
        "expected_outcome": "一个不会被默认批准的计划",
        "stages": [{
            "title": "阶段一",
            "tasks": [{
                "title": "验证恢复语义",
                "is_core": True,
                "evidence_required": True,
            }],
        }],
    }


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-001: rejection is only an in-process resume argument and restart defaults it to approval",
)
async def test_rejected_approval_remains_rejected_after_restart(monkeypatch):
    call = FakeToolCall(
        "call-plan-create",
        "plan_create",
        json.dumps(complete_plan_payload(), ensure_ascii=False),
    )
    completions = SequenceCompletions(
        model_message("需要用户确认。", [call]),
        model_message("已遵循你的决定。"),
    )
    runtime = AgentRuntime()
    runtime.client = fake_client(completions)

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="创建一个需要批准的计划",
            model="hy3",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    await runtime.run(run_id)

    monkeypatch.setattr(agent_api, "_start_runtime", lambda *_args, **_kwargs: None)
    async with AsyncSessionLocal() as db:
        paused = await db.get(AgentRun, run_id)
        _require_harness(
            paused is not None and paused.status == "waiting_approval",
            "approval fixture did not reach waiting_approval",
        )
        await decide_run_approval(
            run_id,
            RunApprovalRequest(approved=False, note="明确拒绝"),
            db,
        )

    resumable = await reconcile_interrupted_runs()
    # A correct implementation may either terminalize the durable rejection in
    # the decision transaction or queue a resume that lets the model observe it.
    if run_id in resumable:
        await runtime.run(run_id, resume=True)

    async with AsyncSessionLocal() as db:
        stored_run = await db.get(AgentRun, run_id)
        plans = list((await db.execute(select(Plan))).scalars())
        decisions = list((await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "approval.resolved",
            )
        )).scalars())
        invocations = list((await db.execute(
            select(ToolInvocation).where(ToolInvocation.run_id == run_id)
        )).scalars())

    assert plans == []
    assert [event.payload.get("decision") for event in decisions] == ["reject"]
    assert [invocation.status for invocation in invocations] == ["rejected"]
    assert stored_run.status not in {"queued", "running", "waiting_approval"}


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-002: resume clears the old checkpoint before a replacement checkpoint or terminal state exists",
)
async def test_second_crash_during_resume_keeps_previous_checkpoint(monkeypatch):
    pending = [{
        "id": "call-resume",
        "type": "function",
        "name": "profile_get",
        "arguments": "{}",
    }]
    original_checkpoint = checkpoint(pending_calls=pending)
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="连续恢复",
            status="queued",
            checkpoint=original_checkpoint,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    runtime = AgentRuntime()
    runtime.client = fake_client(SequenceCompletions())

    async def crash_before_replacement_checkpoint(*_args, **_kwargs):
        raise ProcessCrash("second process interruption")

    monkeypatch.setattr(runtime, "_loop", crash_before_replacement_checkpoint)
    with pytest.raises(ProcessCrash):
        await runtime.run(run_id, resume=True)

    resumable = await reconcile_interrupted_runs()
    async with AsyncSessionLocal() as db:
        recovered = await db.get(AgentRun, run_id)

    assert run_id in resumable
    assert recovered.status == "queued"
    assert recovered.checkpoint == original_checkpoint


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-003: pre-tool checkpoint drops the popped current call and records only later calls",
)
async def test_current_tool_and_remaining_calls_are_checkpointed_separately(monkeypatch):
    first = FakeToolCall("call-current", "profile_get")
    second = FakeToolCall("call-remaining", "plan_list")
    completions = SequenceCompletions(model_message("读取两项状态。", [first, second]))
    runtime = AgentRuntime()
    runtime.client = fake_client(completions)

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="执行两个只读工具",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async def crash_current_tool(*_args, **_kwargs):
        raise ProcessCrash("killed while current tool was executing")

    monkeypatch.setattr(tool_package, "execute_tool", crash_current_tool)
    with pytest.raises(ProcessCrash):
        await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        interrupted = await db.get(AgentRun, run_id)

    current_tool_call = interrupted.checkpoint.get("current_tool_call")
    remaining_tool_calls = interrupted.checkpoint.get("remaining_tool_calls")
    assert current_tool_call is not None, "current tool must remain durable while it is executing"
    assert remaining_tool_calls is not None, "checkpoint must distinguish current and remaining calls"
    assert current_tool_call["id"] == "call-current"
    assert [item["id"] for item in remaining_tool_calls] == [
        "call-remaining"
    ]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-004: a committed queued run without a checkpoint is classified as process_interrupted failure",
)
async def test_queued_message_dispatched_before_process_crash_is_reclaimable():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="耐久队列")
        db.add(session)
        await db.flush()
        queued = QueuedMessage(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="队列提交后进程立即退出",
            user_content="队列提交后进程立即退出",
            position=0,
        )
        db.add(queued)
        await db.commit()
        run = await dispatch_queued_message(db, queued, owner_id="local")
        run_id = run.id

    # The process dies after dispatch commit and before start_tracked_task.
    resumable = await reconcile_interrupted_runs()
    async with AsyncSessionLocal() as db:
        recovered = await db.get(AgentRun, run_id)
        messages = list((await db.execute(
            select(ChatMessage).where(ChatMessage.run_id == run_id)
        )).scalars())
        queue_rows = list((await db.execute(select(QueuedMessage))).scalars())

    assert run_id in resumable
    assert recovered.status == "queued"
    assert recovered.completed_at is None
    assert [(message.role, message.content) for message in messages] == [
        ("user", "队列提交后进程立即退出")
    ]
    assert queue_rows == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-005: final response has no durable finalizing phase or stable message key before final commit",
)
async def test_final_response_is_durable_at_finalization_kill_point(monkeypatch):
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="固定标题避免额外模型调用")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="最终消息故障注入",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    runtime = AgentRuntime()
    runtime.client = fake_client(SequenceCompletions(model_message("唯一且耐久的最终回答")))

    async def crash_after_final_message_flush(*_args, **_kwargs):
        raise ProcessCrash("killed while finalizing the assistant message")

    monkeypatch.setattr(MemoryManager, "compress_session", crash_after_final_message_flush)
    with pytest.raises(ProcessCrash):
        await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        interrupted = await db.get(AgentRun, run_id)
        final_messages = list((await db.execute(
            select(ChatMessage).where(
                ChatMessage.run_id == run_id,
                ChatMessage.role == "assistant",
            )
        )).scalars())
        final_events = list((await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "assistant.message",
            )
        )).scalars())

    committed_once = (
        interrupted.status == "completed"
        and interrupted.output == "唯一且耐久的最终回答"
        and len(final_messages) == 1
        and len(final_events) == 1
    )
    recoverable_final = (
        interrupted.status in {"queued", "running"}
        and bool(interrupted.checkpoint)
        and interrupted.checkpoint.get("phase") == "finalizing"
        and interrupted.checkpoint.get("final_text") == "唯一且耐久的最终回答"
        and len(final_messages) <= 1
        and len(final_events) <= 1
    )
    assert committed_once or recoverable_final


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-006: steer received during the final model stream is left unapplied on a terminal run",
)
async def test_steer_arriving_during_final_stream_is_consumed_or_queued():
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    class LateSteerCompletions:
        async def create(self, **_kwargs):
            async def stream():
                stream_started.set()
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="原始",
                        reasoning_content=None,
                        tool_calls=None,
                    ))],
                    usage=None,
                )
                await release_stream.wait()
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="回答",
                        reasoning_content=None,
                        tool_calls=None,
                    ))],
                    usage=None,
                )

            return stream()

    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="固定的流式会话")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="先生成一个回答",
        )
        db.add(run)
        await db.commit()
        run_id = run.id
        session_id = session.id

    runtime = AgentRuntime()
    runtime.client = fake_client(LateSteerCompletions())
    run_task = asyncio.create_task(runtime.run(run_id))
    try:
        await asyncio.wait_for(stream_started.wait(), timeout=3)
        async with AsyncSessionLocal() as db:
            await steer_run(
                run_id,
                RunSteerCreate(content="请把这个转向用于下一次模型决策"),
                db,
            )
        release_stream.set()
        await asyncio.wait_for(run_task, timeout=5)
    finally:
        release_stream.set()
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    async with AsyncSessionLocal() as db:
        steer = (await db.execute(
            select(RunSteerMessage).where(RunSteerMessage.run_id == run_id)
        )).scalars().one()
        queued = (await db.execute(
            select(QueuedMessage).where(
                QueuedMessage.session_id == session_id,
                QueuedMessage.objective == "请把这个转向用于下一次模型决策",
            )
        )).scalars().one_or_none()
        continuation = (await db.execute(
            select(AgentRun).where(
                AgentRun.session_id == session_id,
                AgentRun.id != run_id,
                AgentRun.objective == "请把这个转向用于下一次模型决策",
            )
        )).scalars().one_or_none()

    assert steer.applied_at is not None or queued is not None or continuation is not None


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-007: concurrent recovery workers have no durable lease or CAS claim and both execute the run",
)
async def test_concurrent_recovery_workers_execute_under_one_durable_lease():
    first_worker_entered_model = asyncio.Event()
    both_workers_entered_model = asyncio.Event()
    release_model = asyncio.Event()

    class OverlappingCompletions:
        def __init__(self) -> None:
            self.calls = 0
            self.in_flight = 0
            self.max_in_flight = 0

        async def create(self, **_kwargs):
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            first_worker_entered_model.set()
            if self.in_flight >= 2:
                both_workers_entered_model.set()
            try:
                await release_model.wait()
                return model_message("只能提交一次的恢复结果")
            finally:
                self.in_flight -= 1

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="只能由一个 worker 恢复",
            status="running",
            checkpoint=checkpoint(),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = OverlappingCompletions()
    first_runtime = AgentRuntime()
    second_runtime = AgentRuntime()
    first_runtime.client = fake_client(completions)
    second_runtime.client = fake_client(completions)
    workers = [
        asyncio.create_task(first_runtime.run(run_id, resume=True)),
        asyncio.create_task(second_runtime.run(run_id, resume=True)),
    ]
    try:
        await asyncio.wait_for(first_worker_entered_model.wait(), timeout=3)
        try:
            await asyncio.wait_for(both_workers_entered_model.wait(), timeout=0.5)
        except TimeoutError:
            # Correct lease behavior keeps the second worker outside the model.
            pass
        release_model.set()
        await asyncio.wait_for(asyncio.gather(*workers), timeout=5)
    finally:
        release_model.set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async with AsyncSessionLocal() as db:
        events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == run_id)
        )).scalars())
    event_counts = {
        event_type: sum(event.event_type == event_type for event in events)
        for event_type in ("run.resumed", "assistant.message", "run.completed")
    }

    assert (
        completions.calls,
        completions.max_in_flight,
        event_counts,
    ) == (
        1,
        1,
        {"run.resumed": 1, "assistant.message": 1, "run.completed": 1},
    )


@pytest.mark.asyncio
async def test_planning_delegate_child_has_checkpoint_before_model_wait(monkeypatch):
    model_waiting = asyncio.Event()
    release_model = asyncio.Event()

    class BlockingCompletions:
        async def create(self, **_kwargs):
            model_waiting.set()
            await release_model.wait()
            return model_message("不会在断言前完成")

    class BlockingClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=BlockingCompletions())

    monkeypatch.setattr(planning_tools, "AsyncOpenAI", BlockingClient)

    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="规划子 Run 检查点")
        db.add(session)
        await db.flush()
        parent = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="委派规划调查",
            status="running",
        )
        db.add(parent)
        await db.commit()
        parent_id = parent.id
        context = ToolContext(
            db=db,
            owner_id="local",
            run_id=parent_id,
            trigger="user_message",
            session_id=session.id,
        )
        tool_task = asyncio.create_task(execute_tool(
            "planning_delegate",
            json.dumps({
                "assignments": [{
                    "role": "课程调查",
                    "objective": "读取本地上下文并给出阶段建议",
                }],
            }, ensure_ascii=False),
            context,
        ))
        try:
            await asyncio.wait_for(model_waiting.wait(), timeout=3)
            async with AsyncSessionLocal() as read_db:
                children = list((await read_db.execute(
                    select(AgentRun).where(AgentRun.parent_run_id == parent_id)
                )).scalars())
                observed_checkpoints = [child.checkpoint for child in children]
        finally:
            release_model.set()
            tool_task.cancel()
            await asyncio.gather(tool_task, return_exceptions=True)

    _require_harness(len(observed_checkpoints) == 1, "planning delegate did not create one child")
    assert observed_checkpoints[0]
    assert observed_checkpoints[0].get("kind") == "subagent"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-009: child terminal state and parent subagent.completed projection use separate commits",
)
async def test_terminal_child_repairs_missing_parent_completion_event(monkeypatch):
    completions = SequenceCompletions(model_message("耐久的子 Agent 报告"))

    class ChildClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", ChildClient)

    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="等待子 Agent",
            status="running",
        )
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="[调查] 返回报告",
            status="queued",
            checkpoint=child_checkpoint(),
        )
        db.add(child)
        await db.commit()
        parent_id = parent.id
        child_id = child.id

    async def crash_parent_projection(db, run_id, event_type, summary="", payload=None):
        if run_id == parent_id and event_type == "subagent.completed":
            raise ProcessCrash("child committed before parent projection")
        return await persist_run_event(db, run_id, event_type, summary, payload)

    monkeypatch.setattr(subagent_tools, "emit_event", crash_parent_projection)
    with pytest.raises(ProcessCrash):
        await subagent_tools._run_child_async(
            child_id,
            "调查",
            "返回报告",
            "deterministic local context",
            {"profile_get"},
            3,
            child_checkpoint(),
        )

    # A startup pass must reconcile the already-terminal child into its parent.
    resumable = await reconcile_interrupted_runs()
    async with AsyncSessionLocal() as db:
        stored_child = await db.get(AgentRun, child_id)
        parent_events = list((await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == parent_id,
                RunEvent.event_type == "subagent.completed",
            )
        )).scalars())

    atomically_projected = (
        stored_child.status == "completed"
        and stored_child.output == "耐久的子 Agent 报告"
        and len(parent_events) == 1
        and parent_events[0].payload.get("child_run_id") == child_id
    )
    atomically_rolled_back = (
        stored_child.status in {"queued", "running"}
        and bool(stored_child.checkpoint)
        and child_id in resumable
        and parent_events == []
    )
    assert atomically_projected or atomically_rolled_back


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-010: transient child model errors immediately fail and clear the checkpoint instead of retrying",
)
async def test_subagent_retries_transient_model_failure(monkeypatch):
    completions = SequenceCompletions(
        TimeoutError("transient provider timeout"),
        model_message("重试后完成的报告"),
    )

    class FlakyChildClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", FlakyChildClient)

    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="等待可重试子任务",
            status="running",
        )
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="[调查] 瞬时失败后重试",
            status="queued",
            checkpoint=child_checkpoint(),
        )
        db.add(child)
        await db.commit()
        child_id = child.id

    await subagent_tools._run_child_async(
        child_id,
        "调查",
        "瞬时失败后重试",
        "deterministic local context",
        {"profile_get"},
        3,
        child_checkpoint(),
    )

    async with AsyncSessionLocal() as db:
        stored = await db.get(AgentRun, child_id)
        retry_events = list((await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == child_id,
                RunEvent.event_type == "run.retrying",
            )
        )).scalars())

    assert (stored.status, stored.output, len(completions.calls)) == (
        "completed",
        "重试后完成的报告",
        2,
    )
    assert len(retry_events) == 1


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H3-RUN-011: child runtime tracks only a local tool counter and never persists the shared run budget",
)
async def test_subagent_persists_model_tool_and_elapsed_budget(monkeypatch):
    completions = SequenceCompletions(
        model_message("先读取画像。", [FakeToolCall("call-profile", "profile_get")]),
        model_message("已完成本地调查。"),
    )

    class BudgetChildClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", BudgetChildClient)

    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="等待预算化子任务",
            status="running",
        )
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="[调查] 读取画像并汇总",
            status="queued",
            checkpoint=child_checkpoint(),
        )
        db.add(child)
        await db.commit()
        child_id = child.id

    await subagent_tools._run_child_async(
        child_id,
        "调查",
        "读取画像并汇总",
        "deterministic local context",
        {"profile_get"},
        3,
        child_checkpoint(),
    )

    async with AsyncSessionLocal() as db:
        stored = await db.get(AgentRun, child_id)

    _require_harness(stored is not None and stored.status == "completed", "budget child did not complete")
    _require_harness(stored.output == "已完成本地调查。", "budget child output fixture drifted")
    assert isinstance(stored.budget_usage, dict)
    assert stored.budget_usage["model_calls"] == 2
    assert stored.budget_usage["tool_calls"] == 1
    assert stored.budget_usage["elapsed_ms"] >= 0
