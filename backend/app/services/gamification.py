from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Achievement, ActivityDay, Plan, Quiz, Stage, Task, UserProfile


ACHIEVEMENT_RULES: list[tuple[str, str, str]] = [
    ("first_plan", "开启学习计划", "创建第一份正式学习计划"),
    ("first_task_done", "迈出第一步", "完成第一个学习任务"),
    ("first_quiz_passed", "首次通关", "第一次通过测验（≥70 分）"),
    ("streak_3", "三天连续", "连续学习 3 天"),
    ("streak_7", "一周坚持", "连续学习 7 天"),
    ("xp_100", "积累 100 XP", "累计获得 100 XP"),
    ("xp_500", "积累 500 XP", "累计获得 500 XP"),
]


async def refresh_streak(db: AsyncSession, owner_id: str) -> int:
    """Compute consecutive activity days ending today (or yesterday until today ends)."""
    profile = await db.get(UserProfile, owner_id)
    if profile is None:
        return 0
    rows = (await db.execute(
        select(ActivityDay.date).where(ActivityDay.owner_id == owner_id)
    )).scalars()
    active_dates = {row for row in rows}
    today = datetime.now(timezone.utc).date()
    cursor = today if today.isoformat() in active_dates else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in active_dates:
        streak += 1
        cursor -= timedelta(days=1)
    profile.streak_days = streak
    await db.flush()
    return streak


async def evaluate_achievements(db: AsyncSession, owner_id: str) -> list[Achievement]:
    """Unlock rule-based achievements idempotently and refresh the streak."""
    profile = await db.get(UserProfile, owner_id)
    if profile is None:
        return []
    streak = await refresh_streak(db, owner_id)
    plans = int((await db.execute(
        select(func.count(Plan.id)).where(Plan.owner_id == owner_id)
    )).scalar_one() or 0)
    completed_tasks = int((await db.execute(
        select(func.count(Task.id))
        .select_from(Task)
        .join(Stage, Task.stage_id == Stage.id)
        .join(Plan, Stage.plan_id == Plan.id)
        .where(Plan.owner_id == owner_id, Task.status == "completed")
    )).scalar_one() or 0)
    passed_quizzes = int((await db.execute(
        select(func.count(Quiz.id)).where(Quiz.owner_id == owner_id, Quiz.status == "passed")
    )).scalar_one() or 0)
    checks = {
        "first_plan": plans >= 1,
        "first_task_done": completed_tasks >= 1,
        "first_quiz_passed": passed_quizzes >= 1,
        "streak_3": streak >= 3,
        "streak_7": streak >= 7,
        "xp_100": profile.xp >= 100,
        "xp_500": profile.xp >= 500,
    }
    existing = {
        row
        for row in (await db.execute(
            select(Achievement.key).where(Achievement.owner_id == owner_id)
        )).scalars()
    }
    unlocked: list[Achievement] = []
    for key, title, description in ACHIEVEMENT_RULES:
        if key in existing or not checks[key]:
            continue
        achievement = Achievement(
            owner_id=owner_id,
            key=key,
            title=title,
            description=description,
            badge_kind="rule",
        )
        db.add(achievement)
        unlocked.append(achievement)
    if unlocked:
        await db.flush()
    return unlocked
