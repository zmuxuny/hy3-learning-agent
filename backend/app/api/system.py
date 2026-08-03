from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models import Achievement, LearningEvent, Quiz, ReviewSchedule
from app.services.gamification import evaluate_achievements


router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "model": settings.MODEL_NAME,
    }


@router.get("/dashboard")
async def dashboard(db: AsyncSession = Depends(get_db)):
    await evaluate_achievements(db, settings.DEFAULT_OWNER_ID)
    await db.commit()
    start = datetime.now(timezone.utc) - timedelta(days=83)
    event_result = await db.execute(
        select(LearningEvent)
        .where(LearningEvent.owner_id == settings.DEFAULT_OWNER_ID, LearningEvent.created_at >= start)
        .order_by(LearningEvent.created_at)
    )
    activity: dict[str, int] = {}
    for event in event_result.scalars():
        is_completion = event.event_type == "task.updated" and event.payload.get("after", {}).get("status") == "completed"
        is_quiz = event.event_type == "quiz.graded"
        if is_completion or is_quiz:
            day = event.created_at.date().isoformat()
            activity[day] = activity.get(day, 0) + 1

    achievement_result = await db.execute(
        select(Achievement)
        .where(Achievement.owner_id == settings.DEFAULT_OWNER_ID)
        .order_by(Achievement.unlocked_at.desc())
    )
    due_reviews = await db.scalar(
        select(ReviewSchedule)
        .where(
            ReviewSchedule.owner_id == settings.DEFAULT_OWNER_ID,
            ReviewSchedule.status == "scheduled",
            ReviewSchedule.due_at <= datetime.now(timezone.utc),
        )
        .limit(1)
    )
    open_quizzes = list(
        (
            await db.execute(
                select(Quiz).where(
                    Quiz.owner_id == settings.DEFAULT_OWNER_ID,
                    Quiz.status == "open",
                )
            )
        ).scalars()
    )
    today = date.today()
    return {
        "activity": [
            {
                "date": (today - timedelta(days=offset)).isoformat(),
                "count": activity.get((today - timedelta(days=offset)).isoformat(), 0),
            }
            for offset in reversed(range(84))
        ],
        "achievements": [
            {"key": item.key, "title": item.title, "description": item.description, "unlocked_at": item.unlocked_at}
            for item in achievement_result.scalars()
        ],
        "due_review_count": 1 if due_reviews else 0,
        "open_quiz_count": len(open_quizzes),
    }
