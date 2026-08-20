import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.database import AsyncSessionLocal
from app.main import reconcile_interrupted_runs
from app.models import AgentRun, Memory, Plan, RunEvent, UserProfile
from app.notifications.service import NotificationService
from app.runtime.agent import AgentRuntime
from app.runtime.checkpoints import normalize_checkpoint
from app.runtime.state import claim_run
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.context.memory import MemoryManager
from app.main import verify_database_writable


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
            checkpoint_schema_version=1,
            checkpoint=normalize_checkpoint({
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
            }),
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
async def test_reconcile_does_not_resume_run_for_archived_plan():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(
            db,
            "local",
            PlanCreate(title="已归档计划", goal="不应恢复后台运行"),
        )
        plan.status = "archived"
        await db.commit()
        run = AgentRun(
            owner_id="local",
            trigger="heartbeat",
            objective=f"检查计划 {plan.id}",
            model="hy3",
            plan_id=plan.id,
            status="running",
            checkpoint_schema_version=1,
            checkpoint=normalize_checkpoint({"step": 0, "messages": [], "pending_tool_calls": []}),
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
async def test_orphan_plan_memory_is_rejected_by_database_scope_guard():
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
        await db.commit()
        plan_id = plan.id

        db.add(
            Memory(
                owner_id="local",
                scope="plan",
                scope_id="999",
                layer="semantic",
                content="不存在计划的非法记忆",
                confidence=0.9,
                status="confirmed",
            )
        )
        with pytest.raises(IntegrityError, match="memory owner or scope mismatch"):
            await db.flush()
        await db.rollback()

        manager = MemoryManager(db)
        active, reused = await manager.propose(
            "local",
            scope="plan",
            scope_id=str(plan_id),
            layer="semantic",
            content="存活计划的有效记忆",
            source_type="user",
            confidence=0.9,
        )
        assert reused is False
        active = await manager.confirm("local", active.id)
        await db.commit()

        await manager.maintain("local")
        await db.commit()
        rows = list((await db.execute(select(Memory))).scalars())
        by_scope = {memory.scope_id: memory.status for memory in rows}
        assert "999" not in by_scope
        assert by_scope[str(plan_id)] == "confirmed"


@pytest.mark.asyncio
async def test_database_writability_check_passes():
    await verify_database_writable()


@pytest.mark.asyncio
async def test_fail_records_run_failure_with_fresh_session():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="会失败")
        db.add(run)
        await db.commit()
        run_id = run.id

    broken_db = object()  # a broken session must not cascade into failure recording
    runtime = AgentRuntime()
    lease = await claim_run(AsyncSessionLocal, run_id, worker_id="failure-test")
    assert lease is not None
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        await runtime._fail(broken_db, run, lease, RuntimeError("boom"))

    async with AsyncSessionLocal() as db:
        failed = await db.get(AgentRun, run_id)
        assert failed.status == "failed"
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        assert any(event.event_type == "run.failed" for event in events)
