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
