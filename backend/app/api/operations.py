from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models import LearningEvent, Operation, Plan, Quiz, ReviewSchedule, Task, UserProfile
from app.schemas import OperationRead
from app.services.plans import recompute_plan_state


router = APIRouter()


@router.get("", response_model=list[OperationRead])
async def list_operations(limit: int = 100, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Operation)
        .where(Operation.owner_id == settings.DEFAULT_OWNER_ID)
        .order_by(Operation.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    return list(result.scalars())


@router.post("/{operation_id}/undo", response_model=OperationRead)
async def undo_operation(operation_id: str, db: AsyncSession = Depends(get_db)):
    operation = await db.get(Operation, operation_id)
    if not operation or operation.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Operation not found")
    if operation.status != "committed":
        raise HTTPException(status_code=409, detail="Operation is not undoable")

    inverse = operation.inverse_patch
    if operation.entity_type == "task" and "changes" in inverse:
        task = await db.get(Task, int(operation.entity_id))
        if not task:
            raise HTTPException(status_code=409, detail="Task no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(task, field, value)
        await db.refresh(task, ["stage"])
        await db.refresh(task.stage, ["plan"])
        await db.refresh(task.stage.plan, ["stages"])
        for stage in task.stage.plan.stages:
            await db.refresh(stage, ["tasks"])
        await recompute_plan_state(task.stage.plan)
        task.stage.plan.version += 1
    elif operation.entity_type == "plan" and "delete" in inverse:
        plan = await db.get(Plan, int(inverse["delete"]))
        if plan:
            await db.delete(plan)
    elif operation.entity_type == "review_schedule" and "delete" in inverse:
        schedule = await db.get(ReviewSchedule, int(inverse["delete"]))
        if schedule:
            await db.delete(schedule)
    elif operation.entity_type == "quiz" and "delete" in inverse:
        quiz = await db.get(Quiz, int(inverse["delete"]))
        if quiz:
            await db.delete(quiz)
    elif operation.entity_type == "quiz" and "changes" in inverse:
        quiz = await db.get(Quiz, int(operation.entity_id))
        if not quiz:
            raise HTTPException(status_code=409, detail="Quiz no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(quiz, field, value)
        if inverse.get("profile"):
            profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
            if profile:
                profile.xp = inverse["profile"]["xp"]
                profile.level = inverse["profile"]["level"]
        if inverse.get("delete_learning_event"):
            event = await db.get(LearningEvent, int(inverse["delete_learning_event"]))
            if event:
                await db.delete(event)
        if inverse.get("delete_review"):
            review = await db.get(ReviewSchedule, int(inverse["delete_review"]))
            if review:
                await db.delete(review)
    else:
        raise HTTPException(status_code=409, detail="No supported inverse operation")

    operation.status = "undone"
    operation.undone_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(operation)
    return operation
