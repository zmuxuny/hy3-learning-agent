import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Operation, Plan, RunEvent, ToolInvocation
from app.runtime.agent import AgentRuntime
from app.tools import ToolContext, execute_tool
from app.tools.registry import tool_contracts


def plan_payload(title="幂等计划"):
    return {
        "title": title,
        "goal": "验证幂等写入",
        "current_level": "初级",
        "weekly_minutes": 300,
        "expected_outcome": "可运行",
        "stages": [{"title": "阶段一", "tasks": [{"title": "任务一"}]}],
    }


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


class ToolCallCompletions:
    def __init__(self, calls_per_message=1, final_text="完成。", usage=None):
        self.calls_per_message = calls_per_message
        self.final_text = final_text
        self.usage = usage
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            calls = [
                FakeToolCall(f"call-{index}", "plan_list", "{}")
                for index in range(self.calls_per_message)
            ]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="先查看计划。",
                reasoning_content=None,
                tool_calls=calls,
            ))], usage=self.usage)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=self.final_text,
            reasoning_content=None,
            tool_calls=None,
        ))], usage=self.usage)


@pytest.mark.asyncio
async def test_idempotent_write_returns_original_result_and_marks_contract():
    contract = next(item for item in tool_contracts() if item["name"] == "file_write")
    assert contract["idempotent"] is True

    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="写文件")
        db.add(run)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message")

        first = await execute_tool(
            "file_write",
            json.dumps({"path": "notes.txt", "content": "hello", "overwrite": True}),
            ctx,
        )
        second = await execute_tool(
            "file_write",
            json.dumps({"path": "notes.txt", "content": "hello", "overwrite": True}),
            ctx,
        )

        assert first["ok"] is True
        assert second["ok"] is True
        assert second["replayed"] is True
        assert second["data"]["operation_id"] == first["data"]["operation_id"]
        operations = list((await db.execute(
            select(Operation).where(Operation.tool_name == "file.write")
        )).scalars())
        assert len(operations) == 1
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
        assert len(invocations) == 1
        assert invocations[0].status == "committed"


@pytest.mark.asyncio
async def test_blocking_approval_invocation_commits_only_after_grant():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="heartbeat", objective="后台创建计划")
        db.add(run)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="heartbeat")
        raw = json.dumps(plan_payload(), ensure_ascii=False)

        pending = await execute_tool("plan_create", raw, ctx)
        assert pending["ok"] is True
        assert pending["data"]["approval_required"] is True
        assert pending["data"]["blocking"] is True
        assert list((await db.execute(select(Plan))).scalars()) == []
        invocation = (await db.execute(select(ToolInvocation))).scalars().one()
        assert invocation.status == "pending_approval"

        ctx.approval_granted = True
        granted = await execute_tool("plan_create", raw, ctx)
        assert granted["ok"] is True
        assert "approval_required" not in granted["data"]
        plans = list((await db.execute(select(Plan))).scalars())
        assert len(plans) == 1
        invocation = (await db.execute(select(ToolInvocation))).scalars().one()
        assert invocation.status == "committed"

        replayed = await execute_tool("plan_create", raw, ctx)
        assert replayed["ok"] is True
        assert replayed["replayed"] is True
        assert replayed["data"]["plan_id"] == granted["data"]["plan_id"]
        assert len(list((await db.execute(select(Plan))).scalars())) == 1


@pytest.mark.asyncio
async def test_model_call_budget_stops_run_observably(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 1)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="触发预算")
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = ToolCallCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert "预算已用尽" in completed.output
        assert completed.budget_usage["model_calls"] == 1
        assert completed.budget_usage["stopped_reason"] == "model_call_limit"
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        exceeded = [event for event in events if event.event_type == "run.budget_exceeded"]
        assert exceeded and exceeded[0].payload["reason"] == "model_call_limit"
    assert completions.calls >= 1


@pytest.mark.asyncio
async def test_tool_call_budget_stops_before_second_tool(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 1)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="触发工具预算")
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = ToolCallCompletions(calls_per_message=2)
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert completed.budget_usage["tool_calls"] == 1
        assert completed.budget_usage["stopped_reason"] == "tool_call_limit"
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        tool_events = [event for event in events if event.event_type == "tool.completed"]
        assert len(tool_events) == 1


@pytest.mark.asyncio
async def test_budget_usage_tracks_tokens_and_estimated_cost(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_INPUT_PRICE_PER_1M", 1.0)
    monkeypatch.setattr(settings, "MODEL_OUTPUT_PRICE_PER_1M", 2.0)
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="统计用量")
        db.add(run)
        await db.commit()
        run_id = run.id

    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    completions = ToolCallCompletions(usage=usage)
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        budget = completed.budget_usage
        assert budget["model_calls"] == 2
        assert budget["prompt_tokens"] == 2000
        assert budget["completion_tokens"] == 1000
        assert budget["estimated_cost_usd"] == pytest.approx(0.004)
