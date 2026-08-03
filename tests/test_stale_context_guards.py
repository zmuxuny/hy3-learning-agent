import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.main import reconcile_interrupted_runs
from app.models import AgentRun, Memory, Plan, RunEvent, UserProfile
from app.notifications.service import NotificationService
from app.runtime.agent import AgentRuntime
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.context.memory import MemoryManager


class CapturingCompletions:
    def __init__(self):
        self.captured = []

    async def create(self, **kwargs):
        self.captured.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="当前没有需要提醒的计划。",
            reasoning_content=None,
            tool_calls=None,
        ))])


@pytest.mark.asyncio
async def test_heartbeat_resume_rebuilds_fresh_context_instead_of_stale_snapshot():
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="检查进行中的计划",
            model="hy3",
            status="queued",
            checkpoint={
                "step": 0,
                "messages": [
                    {"role": "system", "content": "system prompt"},
                    {
                        "role": "user",
                        "content": (
                            "Trigger: heartbeat\nObjective: 检查进行中的计划\n\n"
                            "## Active plan\n- Plan: 7 天 FastAPI + Agent Harness 实战"
                        ),
                    },
                ],
                "pending_tool_calls": [],
            },
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = CapturingCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id, resume=True)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        refreshed = [event for event in events if event.event_type == "context.built"]
        assert refreshed and refreshed[-1].payload.get("refreshed_on_resume") is True

    user_content = completions.captured[0][1]["content"]
    assert "FastAPI" not in user_content
    assert "Trigger: heartbeat" in user_content
    assert "## Active plan" not in user_content


@pytest.mark.asyncio
async def test_reconcile_does_not_resume_run_for_deleted_plan():
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective="检查计划 999",
            model="hy3",
            plan_id=999,
            status="running",
            checkpoint={"step": 0, "messages": [], "pending_tool_calls": []},
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    resumable = await reconcile_interrupted_runs()
    assert resumable == []
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "failed"


@pytest.mark.asyncio
async def test_notification_blocked_for_missing_or_archived_plan():
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        profile.quiet_hours = {"start": "00:00", "end": "00:00"}
        profile.daily_notification_limit = 10
        await db.commit()

        missing = await NotificationService(db).send(
            owner_id="local",
            run_id="run-x",
            session_id=None,
            trigger="manual_heartbeat",
            title="提醒",
            body="旧计划",
            plan_id=999,
            channels=["in_app"],
        )
        assert missing["blocked"] is True
        assert missing["reason"] == "plan no longer active"

        archived = await plan_service.create_plan(
            db,
            "local",
            PlanCreate(
                title="已归档计划",
                goal="x",
                current_level="初级",
                weekly_minutes=60,
                expected_outcome="x",
                stages=[StageCreate(title="s", tasks=[TaskCreate(title="t")])],
            ),
        )
        archived.status = "archived"
        await db.commit()
        archived_result = await NotificationService(db).send(
            owner_id="local",
            run_id="run-y",
            session_id=None,
            trigger="manual_heartbeat",
            title="提醒",
            body="归档计划",
            plan_id=archived.id,
            channels=["in_app"],
        )
        assert archived_result["blocked"] is True
        assert archived_result["reason"] == "plan no longer active"


@pytest.mark.asyncio
async def test_orphan_plan_memories_are_archived_on_maintain():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(
            db,
            "local",
            PlanCreate(
                title="存活计划",
                goal="x",
                current_level="初级",
                weekly_minutes=60,
                expected_outcome="x",
                stages=[StageCreate(title="s", tasks=[TaskCreate(title="t")])],
            ),
        )
        db.add_all([
            Memory(
                owner_id="local",
                scope="plan",
                scope_id="999",
                layer="semantic",
                content="已删除计划的旧记忆",
                confidence=0.9,
                status="confirmed",
            ),
            Memory(
                owner_id="local",
                scope="plan",
                scope_id=str(plan.id),
                layer="semantic",
                content="存活计划的有效记忆",
                confidence=0.9,
                status="confirmed",
            ),
        ])
        await db.commit()

        await MemoryManager(db).maintain("local")
        await db.commit()
        rows = list((await db.execute(select(Memory))).scalars())
        by_scope = {memory.scope_id: memory.status for memory in rows}
        assert by_scope["999"] == "archived"
        assert by_scope[str(plan.id)] == "confirmed"
