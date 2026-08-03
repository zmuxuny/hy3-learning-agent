import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.system import dashboard
from app.db.database import AsyncSessionLocal
from app.models import Achievement, ActivityDay, AgentRun, Quiz, UserProfile
from app.schemas import PlanCreate, StageCreate, TaskCreate, TaskUpdate
from app.services import plans as plan_service
from app.services.gamification import evaluate_achievements, refresh_streak
from app.tools import ToolContext, execute_tool


def plan_payload(title="成就测试计划"):
    return PlanCreate(
        title=title,
        goal="验证成就系统",
        current_level="初级",
        weekly_minutes=300,
        expected_outcome="可运行",
        stages=[StageCreate(title="阶段一", tasks=[TaskCreate(title="普通任务")])],
    )


def utc_day(offset: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()


@pytest.mark.asyncio
async def test_streak_counts_consecutive_activity_days():
    async with AsyncSessionLocal() as db:
        for offset in (1, 2, 3):
            db.add(ActivityDay(owner_id="local", date=utc_day(offset)))
        await db.commit()
        assert await refresh_streak(db, "local") == 3
        profile = await db.get(UserProfile, "local")
        assert profile.streak_days == 3

        db.add(ActivityDay(owner_id="local", date=utc_day(0)))
        await db.commit()
        assert await refresh_streak(db, "local") == 4


@pytest.mark.asyncio
async def test_streak_breaks_on_gap():
    async with AsyncSessionLocal() as db:
        for offset in (1, 3, 4):
            db.add(ActivityDay(owner_id="local", date=utc_day(offset)))
        await db.commit()
        assert await refresh_streak(db, "local") == 1


@pytest.mark.asyncio
async def test_achievements_unlock_idempotently():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload())
        task = plan.stages[0].tasks[0]
        await plan_service.update_task(
            db,
            "local",
            task.id,
            TaskUpdate(status="completed", evidence=[{"kind": "text", "value": "完成了"}], is_core=False),
        )
        db.add(Quiz(
            owner_id="local",
            plan_id=plan.id,
            task_id=task.id,
            prompt="测验",
            status="passed",
            score=88,
        ))
        profile = await db.get(UserProfile, "local")
        profile.xp = 120
        for offset in (1, 2, 3):
            db.add(ActivityDay(owner_id="local", date=utc_day(offset)))
        await db.commit()

        unlocked = await evaluate_achievements(db, "local")
        await db.commit()
        rows = list((await db.execute(select(Achievement))).scalars())
        keys = {row.key for row in rows}
        assert {"first_plan", "first_task_done", "first_quiz_passed", "streak_3", "xp_100"} <= keys
        assert "streak_7" not in keys
        assert "xp_500" not in keys
        assert len(rows) == len(unlocked) + 1  # first_plan was unlocked by create_plan

        await evaluate_achievements(db, "local")
        await db.commit()
        assert len(list((await db.execute(select(Achievement))).scalars())) == len(rows)


@pytest.mark.asyncio
async def test_quiz_grade_unlocks_first_quiz_passed():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("测验成就"))
        quiz = Quiz(
            owner_id="local",
            plan_id=plan.id,
            task_id=plan.stages[0].tasks[0].id,
            prompt="解释 asyncio 事件循环",
            status="open",
        )
        db.add(quiz)
        await db.commit()
        run = AgentRun(
            owner_id="local", plan_id=plan.id, trigger="user_message", objective="评分"
        )
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "quiz_grade",
            json.dumps({
                "quiz_id": quiz.id,
                "answer": "事件循环调度协程",
                "score": 85,
                "feedback": "回答正确",
                "next_review_at": None,
            }, ensure_ascii=False),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id),
        )
        assert result["ok"] is True
        achievements = list((await db.execute(select(Achievement))).scalars())
        assert any(item.key == "first_quiz_passed" for item in achievements)


@pytest.mark.asyncio
async def test_dashboard_refreshes_achievements_and_streak():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("仪表盘成就"))
        task = plan.stages[0].tasks[0]
        await plan_service.update_task(
            db,
            "local",
            task.id,
            TaskUpdate(status="completed", evidence=[{"kind": "text", "value": "证据"}], is_core=False),
        )
        profile = await db.get(UserProfile, "local")
        profile.xp = 150
        for offset in (0, 1, 2, 3):
            db.add(ActivityDay(owner_id="local", date=utc_day(offset)))
        await db.commit()

        result = await dashboard(db)
        assert any(item["key"] == "first_plan" for item in result["achievements"])
        assert any(item["key"] == "first_task_done" for item in result["achievements"])
        assert any(item["key"] == "streak_4" for item in result["achievements"]) is False
        refreshed = await db.get(UserProfile, "local")
        assert refreshed.streak_days == 4
