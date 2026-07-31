import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.agent import create_run, read_session_messages
from app.db.database import AsyncSessionLocal
from app.api.operations import undo_operation
from app.models import AgentRun, Notification, Operation, RunEvent, Session
from app.runtime.agent import AgentRuntime
from app.schemas import AgentRunCreate, PlanCreate, StageCreate, TaskCreate, TaskUpdate
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool


def plan_payload(title: str = "Python async mastery") -> PlanCreate:
    return PlanCreate(
        title=title,
        goal="Build and explain a robust asyncio service",
        current_level="Can write basic Python",
        weekly_minutes=300,
        expected_outcome="A tested async mini-project",
        stages=[
            StageCreate(
                title="Async foundations",
                tasks=[
                    TaskCreate(title="Read event-loop guide", estimated_minutes=30),
                    TaskCreate(
                        title="Implement a concurrent crawler",
                        is_core=True,
                        evidence_required=True,
                        estimated_minutes=90,
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_session_focus_cannot_be_rebound():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", plan_id=None, title="Global conversation")
        db.add(session)
        await db.commit()
        await db.refresh(session)

        with pytest.raises(HTTPException) as error:
            await create_run(
                AgentRunCreate(objective="Switch focus silently", session_id=session.id, plan_id=999),
                db,
            )

        assert error.value.status_code == 409
        assert error.value.detail == "Session focus does not match requested plan"


@pytest.mark.asyncio
async def test_core_evidence_progress_and_undo():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload())
        normal, core = plan.stages[0].tasks

        run = AgentRun(owner_id="local", trigger="user_message", objective="Update my progress")
        db.add(run)
        await db.commit()

        result = await execute_tool(
            "task_patch",
            json.dumps({"task_id": normal.id, "changes": {"status": "completed"}, "reason": "User finished it"}),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
        assert result["ok"] is True
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 0.5

        with pytest.raises(HTTPException) as error:
            await plan_service.update_task(db, "local", core.id, TaskUpdate(status="completed"))
        assert error.value.status_code == 409

        await plan_service.update_task(
            db,
            "local",
            core.id,
            TaskUpdate(status="completed", evidence=[{"kind": "repository", "value": "local/demo"}]),
        )
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 1.0
        assert refreshed.stages[0].status == "completed"

        operation = await undo_operation(result["data"]["operation_id"], db)
        assert operation.status == "undone"
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.stages[0].tasks[0].status == "pending"
        assert refreshed.progress == 0.5


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
    def __init__(self):
        self.calls = []
        self.responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="我先检查当前计划。",
                    reasoning_content="private planning tokens",
                    tool_calls=[FakeToolCall("call-1", "plan_list", "{}")],
                ))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="现在发送一条可见提醒。",
                    reasoning_content="private tool selection tokens",
                    tool_calls=[FakeToolCall(
                        "call-2",
                        "notification_send",
                        json.dumps({
                            "title": "该开始异步练习了",
                            "body": "先完成 25 分钟的事件循环练习。",
                            "channels": ["in_app", "browser"],
                        }, ensure_ascii=False),
                    )],
                ))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="我检查了计划，并把本次练习提醒放进了收件箱。",
                    reasoning_content="private final tokens",
                    tool_calls=None,
                ))]
            ),
        ]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_harness_runs_tools_and_keeps_reasoning_private():
    async with AsyncSessionLocal() as db:
        await plan_service.create_plan(db, "local", plan_payload("Harness demo"))
        run = AgentRun(owner_id="local", trigger="user_message", objective="监督我开始今天的学习")
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = FakeCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "completed"
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        event_types = [event.event_type for event in events]
        assert event_types.count("tool.started") == 2
        assert "run.completed" in event_types
        assert all("private" not in event.summary for event in events)
        notifications = list((await db.execute(select(Notification))).scalars())
        assert {item.channel for item in notifications} == {"in_app", "browser"}
        messages = await read_session_messages(run.session_id, db)
        assert [(message.role, message.content) for message in messages] == [
            ("user", "监督我开始今天的学习"),
            ("assistant", "我检查了计划，并把本次练习提醒放进了收件箱。"),
        ]
        assert all(message.run_id == run_id for message in messages)

    assert completions.calls[0]["extra_body"] == {"reasoning_effort": "high"}
    assert "The supplied tool schemas are the complete set" in completions.calls[0]["messages"][0]["content"]
    assert {tool["function"]["name"] for tool in completions.calls[0]["tools"]} == {
        "profile_get",
        "plan_list",
        "plan_get",
        "plan_create",
        "task_patch",
        "review_schedule",
        "quiz_create",
        "quiz_get",
        "quiz_grade",
        "memory_propose",
        "notification_send",
    }
    replayed_messages = completions.calls[1]["messages"]
    assert any(message.get("reasoning_content") == "private planning tokens" for message in replayed_messages)
