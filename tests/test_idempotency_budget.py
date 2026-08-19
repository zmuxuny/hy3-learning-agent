import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    Operation,
    OutboxAction,
    OutboxReceipt,
    Plan,
    RunEvent,
    ToolInvocation,
)
from app.outbox import dispatch_action
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
    assert contract["blocking"] is False

    guarded_contract = next(item for item in tool_contracts() if item["name"] == "plan_patch")
    assert guarded_contract["idempotent"] is True
    assert guarded_contract["blocking"] is True

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
        assert first["status"] == "pending_delivery"
        assert second["ok"] is True
        assert second["replayed"] is True
        assert second["status"] == "pending_delivery"
        assert second["data"]["operation_id"] == first["data"]["operation_id"]
        operations = list((await db.execute(
            select(Operation).where(Operation.tool_name == "file.write")
        )).scalars())
        assert len(operations) == 1
        assert operations[0].status == "pending_delivery"
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
        assert len(invocations) == 1
        assert invocations[0].status == "pending_delivery"
        action = (await db.execute(select(OutboxAction))).scalar_one()
        assert action.status == "queued"
        assert action.operation_id == operations[0].id
        assert action.invocation_id == invocations[0].id
        action_key = action.action_key
        operation_id = operations[0].id
        invocation_id = invocations[0].id
        await db.rollback()

    delivery = await dispatch_action(action_key=action_key)
    assert delivery["status"] == "delivered"
    assert delivery["delivered"] is True

    async with AsyncSessionLocal() as db:
        operation = await db.get(Operation, operation_id)
        invocation = await db.get(ToolInvocation, invocation_id)
        action = await db.scalar(
            select(OutboxAction).where(OutboxAction.action_key == action_key)
        )
        receipt = await db.scalar(
            select(OutboxReceipt).where(OutboxReceipt.action_key == action_key)
        )
        assert operation.status == "committed"
        assert invocation.status == "committed"
        assert action.status == "delivered"
        assert receipt.status == "delivered"


@pytest.mark.asyncio
async def test_idempotency_replays_only_the_same_provider_tool_call():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="写两份相同内容")
        db.add(run)
        await db.commit()
        run_id = run.id
        raw = json.dumps({"path": "call-id.txt", "content": "hello", "overwrite": True})

        first = await execute_tool(
            "file_write",
            raw,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="call-one",
            ),
        )
        replay = await execute_tool(
            "file_write",
            raw,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="call-one",
            ),
        )
        second_call = await execute_tool(
            "file_write",
            raw,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="call-two",
            ),
        )

        assert first["ok"] is True
        assert first["status"] == "pending_delivery"
        assert replay["replayed"] is True
        assert replay["status"] == "pending_delivery"
        assert second_call["ok"] is True
        assert second_call["status"] == "pending_delivery"
        assert "replayed" not in second_call
        invocations = list((await db.execute(
            select(ToolInvocation).where(ToolInvocation.run_id == run_id)
        )).scalars())
        assert len(invocations) == 2
        assert {item.status for item in invocations} == {"pending_delivery"}
        actions = list(
            (
                await db.execute(
                    select(OutboxAction).where(OutboxAction.run_id == run_id)
                )
            ).scalars()
        )
        assert len(actions) == 2
        assert {item.status for item in actions} == {"queued"}
        assert len({item.action_key for item in actions}) == 2


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
async def test_background_goal_change_is_a_real_blocking_approval():
    async with AsyncSessionLocal() as db:
        setup_run = AgentRun(owner_id="local", trigger="user_message", objective="准备计划")
        db.add(setup_run)
        await db.commit()
        created = await execute_tool(
            "plan_create",
            json.dumps(plan_payload("审批计划"), ensure_ascii=False),
            ToolContext(db=db, owner_id="local", run_id=setup_run.id, trigger="user_message"),
        )
        plan_id = created["data"]["plan_id"]
        background = AgentRun(owner_id="local", plan_id=plan_id, trigger="heartbeat", objective="调整长期目标")
        db.add(background)
        await db.commit()

        pending = await execute_tool(
            "plan_patch",
            json.dumps({"plan_id": plan_id, "goal": "新的长期目标", "reason": "后台发现目标需要调整"}, ensure_ascii=False),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=background.id,
                trigger="heartbeat",
                plan_id=plan_id,
                tool_call_id="goal-change",
            ),
        )

        assert pending["ok"] is True
        assert pending["data"]["approval_required"] is True
        assert pending["data"]["blocking"] is True
        plan = await db.get(Plan, plan_id)
        assert plan.goal == "验证幂等写入"


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
        started = completed.started_at.replace(tzinfo=None)
        ended = completed.completed_at.replace(tzinfo=None)
        actual_elapsed_ms = int((ended - started).total_seconds() * 1000)
        assert abs(budget["elapsed_ms"] - actual_elapsed_ms) <= 5
