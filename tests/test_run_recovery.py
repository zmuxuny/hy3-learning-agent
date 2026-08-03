import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.agent import decide_run_approval
from app.db.database import AsyncSessionLocal
from app.main import reconcile_interrupted_runs
from app.models import AgentRun, Plan, RunEvent
from app.runtime.agent import AgentRuntime
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
        plans = list((await db.execute(select(Plan))).scalars())
        assert plans == []
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        approval_events = [event for event in events if event.event_type == "approval.required"]
        assert approval_events and approval_events[-1].payload["blocking"] is True

    started = {}

    def fake_start(run_id_value, **kwargs):
        started["kwargs"] = kwargs

    monkeypatch.setattr(agent_api, "_start_runtime", fake_start)
    async with AsyncSessionLocal() as db:
        queued = await decide_run_approval(run_id, RunApprovalRequest(approved=True), db)
        assert queued.status == "queued"
    assert started["kwargs"]["approval_decision"] == "approve"

    await runtime.run(run_id, resume=True, approval_decision="approve")
    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert completed.output == "计划已按你的批准创建。"
        assert completed.pending_approval is None
        plans = list((await db.execute(select(Plan))).scalars())
        assert len(plans) == 1
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

    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id, **_kwargs: None)
    completions = ApprovalCompletions()
    completions.final_text = "你拒绝了这次创建，我不写入计划。"
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        await decide_run_approval(run_id, RunApprovalRequest(approved=False), db)

    await runtime.run(run_id, resume=True, approval_decision="reject")
    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert completed.output == "你拒绝了这次创建，我不写入计划。"
        assert list((await db.execute(select(Plan))).scalars()) == []
        resolved = [event for event in await _events(db, run_id) if event.event_type == "approval.resolved"]
        assert resolved[0].payload["decision"] == "reject"


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
            checkpoint={
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
            },
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
