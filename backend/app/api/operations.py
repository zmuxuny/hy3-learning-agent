from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.core.config import PROJECT_ROOT
from app.models import ActivityDay, CalendarEvent, LearningEvent, LearningResource, Operation, Plan, PlanProposal, Quiz, ReviewSchedule, Session, Stage, Task, TaskSubmission, UserProfile
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
    audit_plan_id: int | None = None
    audit_task_id: int | None = None
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
        audit_plan_id = task.stage.plan.id
        audit_task_id = task.id
    elif operation.entity_type == "plan" and "changes" in inverse:
        plan = await db.get(Plan, int(operation.entity_id))
        if not plan:
            raise HTTPException(status_code=409, detail="Plan no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") or field == "deadline":
                value = datetime.fromisoformat(value) if isinstance(value, str) else value
            setattr(plan, field, value)
        plan.version += 1
        audit_plan_id = plan.id
    elif operation.entity_type == "plan" and "delete" in inverse:
        plan = await db.get(Plan, int(inverse["delete"]))
        if plan:
            await db.delete(plan)
            # The compensating audit event describes the deleted entity in its
            # payload. Its FK must stay null after the plan itself is removed.
            audit_plan_id = None
        proposal_id = operation.forward_patch.get("proposal_id")
        if proposal_id:
            proposal = await db.get(PlanProposal, proposal_id)
            if proposal:
                proposal.status = "pending"
                proposal.plan_id = None
                proposal.decided_at = None
    elif operation.entity_type == "session" and "changes" in inverse:
        session = await db.get(Session, operation.entity_id)
        if not session:
            raise HTTPException(status_code=409, detail="Session no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(session, field, value)
        audit_plan_id = session.plan_id
        session.updated_at = datetime.now(timezone.utc)
    elif operation.entity_type == "learning_resource" and "delete" in inverse:
        resource = await db.get(LearningResource, int(inverse["delete"]))
        if resource:
            audit_plan_id = resource.plan_id
            await db.delete(resource)
    elif operation.entity_type == "learning_resource" and "changes" in inverse:
        resource = await db.get(LearningResource, int(operation.entity_id))
        if not resource:
            raise HTTPException(status_code=409, detail="Learning resource no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(resource, field, value)
        audit_plan_id = resource.plan_id
    elif operation.entity_type == "review_schedule" and "delete" in inverse:
        schedule = await db.get(ReviewSchedule, int(inverse["delete"]))
        if schedule:
            audit_plan_id = schedule.plan_id
            audit_task_id = schedule.task_id
            await db.delete(schedule)
    elif operation.entity_type == "review_schedule" and "changes" in inverse:
        schedule = await db.get(ReviewSchedule, int(operation.entity_id))
        if not schedule:
            raise HTTPException(status_code=409, detail="Review schedule no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(schedule, field, value)
        audit_plan_id = schedule.plan_id
        audit_task_id = schedule.task_id
    elif operation.entity_type == "stage" and "delete" in inverse:
        stage = await db.get(Stage, int(inverse["delete"]))
        if stage:
            audit_plan_id = stage.plan_id
            await db.delete(stage)
            await db.flush()
            plan = await db.get(Plan, audit_plan_id)
            if plan:
                await db.refresh(plan, ["stages"])
                for remaining_stage in plan.stages:
                    await db.refresh(remaining_stage, ["tasks"])
                await recompute_plan_state(plan)
                plan.version += 1
    elif operation.entity_type == "task" and "delete" in inverse:
        task = await db.get(Task, int(inverse["delete"]))
        if task:
            stage = await db.get(Stage, task.stage_id)
            audit_plan_id = stage.plan_id if stage else None
            # The deleted task remains identifiable in the event payload. The
            # FK must be null because this compensating event outlives it.
            audit_task_id = None
            await db.delete(task)
            await db.flush()
            plan = await db.get(Plan, audit_plan_id) if audit_plan_id else None
            if plan:
                await db.refresh(plan, ["stages"])
                for remaining_stage in plan.stages:
                    await db.refresh(remaining_stage, ["tasks"])
                await recompute_plan_state(plan)
                plan.version += 1
    elif operation.entity_type == "calendar_event" and "delete" in inverse:
        event = await db.get(CalendarEvent, int(inverse["delete"]))
        if event:
            audit_plan_id = event.plan_id
            await db.delete(event)
    elif operation.entity_type == "calendar_event" and "changes" in inverse:
        event = await db.get(CalendarEvent, int(operation.entity_id))
        if not event:
            raise HTTPException(status_code=409, detail="Calendar event no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(event, field, value)
        audit_plan_id = event.plan_id
    elif operation.entity_type == "submission" and "submission" in inverse:
        submission = await db.get(TaskSubmission, int(operation.entity_id))
        task = await db.get(Task, submission.task_id) if submission else None
        if not submission or not task:
            raise HTTPException(status_code=409, detail="Submission or task no longer exists")
        for field, value in inverse["submission"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(submission, field, value)
        for field, value in inverse["task"].items():
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
        audit_plan_id = task.stage.plan.id
        audit_task_id = task.id
        award = inverse.get("award")
        if award:
            profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
            if profile and award.get("profile"):
                profile.xp = award["profile"]["xp"]
                profile.level = award["profile"]["level"]
            day = await db.get(ActivityDay, award.get("day_id"))
            if day and award.get("day") is None:
                await db.delete(day)
            elif day:
                day.xp = award["day"]["xp"]
                day.completed_tasks = award["day"]["completed_tasks"]
                day.passed_quizzes = award["day"]["passed_quizzes"]
    elif operation.entity_type == "workspace_file" and "path" in inverse:
        workspace_root = (PROJECT_ROOT / "data" / "workspace").resolve()
        path = (workspace_root / inverse["path"]).resolve()
        if path != workspace_root and workspace_root not in path.parents:
            raise HTTPException(status_code=409, detail="Invalid workspace path")
        if inverse.get("delete"):
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inverse.get("previous", ""), encoding="utf-8")
    elif operation.entity_type == "quiz" and "delete" in inverse:
        quiz = await db.get(Quiz, int(inverse["delete"]))
        if quiz:
            audit_plan_id = quiz.plan_id
            audit_task_id = quiz.task_id
            await db.delete(quiz)
    elif operation.entity_type == "quiz" and "changes" in inverse:
        quiz = await db.get(Quiz, int(operation.entity_id))
        if not quiz:
            raise HTTPException(status_code=409, detail="Quiz no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(quiz, field, value)
        audit_plan_id = quiz.plan_id
        audit_task_id = quiz.task_id
        if inverse.get("profile"):
            profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
            if profile:
                profile.xp = inverse["profile"]["xp"]
                profile.level = inverse["profile"]["level"]
        if inverse.get("delete_review"):
            review = await db.get(ReviewSchedule, int(inverse["delete_review"]))
            if review:
                await db.delete(review)
    else:
        raise HTTPException(status_code=409, detail="No supported inverse operation")

    operation.status = "undone"
    operation.undone_at = datetime.now(timezone.utc)
    db.add(LearningEvent(
        owner_id=settings.DEFAULT_OWNER_ID,
        plan_id=audit_plan_id,
        task_id=audit_task_id,
        run_id=operation.run_id,
        event_type="operation.undone",
        summary=f"Undid {operation.tool_name} on {operation.entity_type}:{operation.entity_id}",
        payload={
            "operation_id": operation.id,
            "tool_name": operation.tool_name,
            "entity_type": operation.entity_type,
            "entity_id": operation.entity_id,
        },
    ))
    await db.commit()
    await db.refresh(operation)
    return operation
