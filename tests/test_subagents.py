import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.tools.subagents as subagent_tools
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Operation, Plan, RunEvent
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool
from app.tools.registry import tool_contracts


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


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def final_message(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=text,
        reasoning_content=None,
        tool_calls=None,
    ))])


def tool_message(name, arguments="{}"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="先调用工具。",
        reasoning_content=None,
        tool_calls=[FakeToolCall(f"call-{name}", name, arguments)],
    ))])


@pytest.mark.asyncio
async def test_subagent_spawn_status_and_join_return_report(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions([
                final_message("调研结论：优先使用官方教程，并安排一次动手实验。"),
            ]))

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", FakeClient)
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="调研资源")
        db.add(parent)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=parent.id, trigger="user_message")

        spawned = await execute_tool(
            "subagent_spawn",
            json.dumps({"role": "资源调研", "objective": "比较三门课程", "max_steps": 3}),
            ctx,
        )
        assert spawned["ok"] is True
        child_id = spawned["data"]["run_id"]
        assert spawned["data"]["allowlist"]

        status = await execute_tool("subagent_status", json.dumps({"run_id": child_id}), ctx)
        assert status["ok"] is True
        assert status["data"]["run_id"] == child_id

        joined = await execute_tool(
            "subagent_join",
            json.dumps({"run_id": child_id, "timeout_seconds": 15}),
            ctx,
        )
        assert joined["ok"] is True
        assert joined["data"]["status"] == "completed"
        assert joined["data"]["timed_out"] is False
        assert "官方教程" in joined["data"]["output"]

        child = await db.get(AgentRun, child_id)
        assert child.status == "completed"
        assert child.parent_run_id == parent.id
        assert child.trigger == "subagent"
        parent_events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == parent.id)
        )).scalars())
        assert any(event.event_type == "subagent.started" for event in parent_events)
        assert any(event.event_type == "subagent.completed" for event in parent_events)


@pytest.mark.asyncio
async def test_subagent_allowlist_never_exposes_write_tools(monkeypatch):
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload())
        plan_id = plan.id
        parent = AgentRun(owner_id="local", plan_id=plan_id, trigger="user_message", objective="尝试写计划")
        db.add(parent)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=parent.id, trigger="user_message", plan_id=plan_id)

        class FakeClient:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions([
                    tool_message("plan_patch", json.dumps({
                        "plan_id": plan_id,
                        "weekly_minutes": 999,
                        "reason": "不应执行",
                    })),
                    final_message("写工具被拒绝，我只返回建议。"),
                ]))

        monkeypatch.setattr(subagent_tools, "AsyncOpenAI", FakeClient)
        spawned = await execute_tool(
            "subagent_spawn",
            json.dumps({"role": "受限调查", "objective": "尝试修改计划", "tool_whitelist": ["plan_get", "plan_patch"]}),
            ctx,
        )
        assert spawned["ok"] is True
        assert spawned["data"]["allowlist"] == ["plan_get"]

        joined = await execute_tool(
            "subagent_join",
            json.dumps({"run_id": spawned["data"]["run_id"], "timeout_seconds": 15}),
            ctx,
        )
        assert joined["data"]["status"] == "completed"
        assert "写工具被拒绝" in joined["data"]["output"]
        child_events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == spawned["data"]["run_id"])
        )).scalars())
        failed_tool = next(event for event in child_events if event.event_type == "tool.completed")
        assert failed_tool.payload["result"]["ok"] is False
        assert "read-only allowlist" in failed_tool.payload["result"]["error"]
        assert list((await db.execute(select(Operation).where(Operation.tool_name == "plan.patch"))).scalars()) == []
        refreshed = await plan_service.get_plan(db, "local", plan_id)
        assert refreshed.weekly_minutes != 999


@pytest.mark.asyncio
async def test_subagent_cancel_stops_child(monkeypatch):
    class SlowClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SlowCompletions())

    class SlowCompletions:
        async def create(self, **_kwargs):
            await asyncio.sleep(0.5)
            return final_message("太晚的结论。")

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", SlowClient)
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="取消子任务")
        db.add(parent)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=parent.id, trigger="user_message")

        spawned = await execute_tool(
            "subagent_spawn",
            json.dumps({"role": "慢任务", "objective": "长时间调研", "max_steps": 8}),
            ctx,
        )
        child_id = spawned["data"]["run_id"]
        cancelled = await execute_tool("subagent_cancel", json.dumps({"run_id": child_id}), ctx)
        assert cancelled["ok"] is True
        assert cancelled["data"]["status"] == "cancelled"

        joined = await execute_tool(
            "subagent_join",
            json.dumps({"run_id": child_id, "timeout_seconds": 10}),
            ctx,
        )
        assert joined["data"]["status"] == "cancelled"
        assert joined["data"]["timed_out"] is False
        parent_events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == parent.id)
        )).scalars())
        completed = [event for event in parent_events if event.event_type == "subagent.completed"]
        assert completed and completed[-1].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_subagent_failure_reaches_terminal_state_and_notifies_parent(monkeypatch):
    class BrokenCompletions:
        async def create(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    class BrokenClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=BrokenCompletions())

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", BrokenClient)
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="验证失败收口")
        db.add(parent)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=parent.id, trigger="user_message")

        spawned = await execute_tool(
            "subagent_spawn",
            json.dumps({"role": "失败调查", "objective": "模拟供应商异常"}),
            ctx,
        )
        joined = await execute_tool(
            "subagent_join",
            json.dumps({"run_id": spawned["data"]["run_id"], "timeout_seconds": 15}),
            ctx,
        )

        assert joined["data"]["status"] == "failed"
        assert "RuntimeError" in joined["data"]["output"]
        child = await db.get(AgentRun, spawned["data"]["run_id"])
        assert child.checkpoint is None
        parent_events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == parent.id)
        )).scalars())
        completed = [event for event in parent_events if event.event_type == "subagent.completed"]
        assert completed[-1].payload["status"] == "failed"


@pytest.mark.asyncio
async def test_checkpointed_subagent_resumes_with_its_own_runtime(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions([
                final_message("恢复后的调查结论。"),
            ]))

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", FakeClient)
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="恢复父任务")
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="[恢复调查] 继续读取资料",
            status="queued",
            checkpoint={
                "kind": "subagent",
                "role": "恢复调查",
                "objective": "继续读取资料",
                "context": "已持久化上下文",
                "allowlist": ["plan_list"],
                "max_steps": 3,
                "step": 0,
                "messages": [],
                "pending_tool_calls": [],
            },
        )
        db.add(child)
        await db.commit()
        child_id = child.id

    await subagent_tools.resume_subagent_run(child_id)

    async with AsyncSessionLocal() as db:
        recovered = await db.get(AgentRun, child_id)
        assert recovered.status == "completed"
        assert recovered.output == "恢复后的调查结论。"
        assert recovered.checkpoint is None


@pytest.mark.asyncio
async def test_subagent_reserves_a_tools_disabled_turn_for_final_report(monkeypatch):
    class ResearchCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools"):
                return tool_message("plan_list")
            return final_message("最终报告：已基于现有计划完成调查并给出可执行建议。")

    completions = ResearchCompletions()

    class ResearchClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(subagent_tools, "AsyncOpenAI", ResearchClient)
    async with AsyncSessionLocal() as db:
        parent = AgentRun(owner_id="local", trigger="user_message", objective="持续调用工具的调查")
        db.add(parent)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=parent.id, trigger="user_message")

        spawned = await execute_tool(
            "subagent_spawn",
            json.dumps({"role": "资源调查", "objective": "读取计划后形成报告", "max_steps": 2}),
            ctx,
        )
        joined = await execute_tool(
            "subagent_join",
            json.dumps({"run_id": spawned["data"]["run_id"], "timeout_seconds": 15}),
            ctx,
        )

        assert joined["data"]["status"] == "completed"
        assert "最终报告" in joined["data"]["output"]
        assert completions.calls[-1].get("tools") is None


def test_subagent_tools_expose_input_and_output_contracts():
    contracts = tool_contracts()
    names = {item["name"] for item in contracts}
    assert {"subagent_spawn", "subagent_status", "subagent_join", "subagent_cancel"}.issubset(names)
    for item in contracts:
        if item["name"].startswith("subagent_"):
            assert item["input_schema"] and item["output_schema"]


def plan_payload(title="子 Agent 测试计划"):
    from app.schemas import PlanCreate, StageCreate, TaskCreate

    return PlanCreate(
        title=title,
        goal="验证只读子 Agent",
        current_level="初级",
        weekly_minutes=300,
        expected_outcome="可运行",
        stages=[StageCreate(title="阶段一", tasks=[TaskCreate(title="任务一")])],
    )
