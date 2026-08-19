import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.time import parse_legacy_datetime
from app.db.database import get_db
from app.db.uow import (
    DatabaseBusyError,
    commit as commit_uow,
    flush as flush_uow,
    is_database_busy,
    rollback as rollback_uow,
)
from app.models import ActivityDay, CalendarEvent, Competency, CompetencyEdge, LearningEvent, LearningResource, Operation, Plan, PlanCompetencyLink, PlanProposal, Quiz, ResourceCompetencyLink, ReviewSchedule, Session, Stage, Task, TaskCompetencyLink, TaskSubmission, UserProfile
from app.outbox import (
    enqueue_workspace_delete,
    enqueue_workspace_write,
    prepare_workspace_delete,
    prepare_workspace_write,
)
from app.schemas import OperationRead
from app.services.plans import recompute_plan_state


router = APIRouter()


_UNDO_REPLAY_STATUSES = {"undo_pending", "undone", "needs_reconciliation"}


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
    if operation.status in _UNDO_REPLAY_STATUSES:
        # The endpoint body is only the stable Operation id. Repeating it must
        # therefore return the same durable state instead of applying the
        # inverse twice (or turning a successful retry into a conflict).
        return operation
    if operation.status != "committed":
        raise HTTPException(status_code=409, detail="Operation is not undoable")

    inverse = operation.inverse_patch
    prepared_workspace_undo = None
    if operation.entity_type == "workspace_file" and "path" in inverse:
        # db.get() opened a read transaction. End it before touching the
        # filesystem so a slow disk cannot stretch a caller-owned DB UoW.
        await commit_uow(db)
        try:
            prepared_workspace_undo = (
                await prepare_workspace_delete(path=inverse["path"])
                if inverse.get("delete")
                else await prepare_workspace_write(
                    path=inverse["path"],
                    content=inverse.get("previous", ""),
                    overwrite=True,
                )
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Workspace undo cannot be prepared: {type(exc).__name__}",
            ) from exc

        # A compensation may only replace the exact forward version produced
        # by this operation. Refuse legacy rows without a digest and refuse to
        # clobber a later user/tool edit.
        forward_digest = (
            operation.forward_patch.get("desired_sha256")
            or operation.forward_patch.get("sha256")
        )
        if not forward_digest:
            raise HTTPException(
                status_code=409,
                detail="Workspace undo requires reconciliation: forward digest is missing",
            )
        if prepared_workspace_undo.before_sha256 != forward_digest:
            raise HTTPException(
                status_code=409,
                detail="Workspace undo requires reconciliation: file changed after the operation",
            )

    # Every inverse, including DB-only ones, is guarded by the same conditional
    # transition. The winner applies the inverse and records its audit event in
    # this UoW; a concurrent loser reloads the winner's durable state.
    try:
        claim = await db.execute(
            update(Operation)
            .where(
                Operation.id == operation.id,
                Operation.owner_id == settings.DEFAULT_OWNER_ID,
                Operation.status == "committed",
            )
            .values(status="undo_pending", undone_at=None)
        )
    except OperationalError as exc:
        if not is_database_busy(exc):
            raise
        await rollback_uow(db)
        raise DatabaseBusyError() from exc
    if claim.rowcount != 1:
        await rollback_uow(db)
        current = await db.get(Operation, operation_id)
        if current is not None and current.status in _UNDO_REPLAY_STATUSES:
            return current
        raise HTTPException(status_code=409, detail="Operation is not undoable")
    operation.status = "undo_pending"
    audit_plan_id: int | None = None
    audit_task_id: int | None = None
    undo_outbox_action_id: str | None = None
    if operation.entity_type == "task" and "changes" in inverse:
        task = await db.get(Task, int(operation.entity_id))
        if not task:
            raise HTTPException(status_code=409, detail="Task no longer exists")
        for field, value in inverse["changes"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = parse_legacy_datetime(value)
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
                value = parse_legacy_datetime(value) if isinstance(value, str) else value
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
                value = parse_legacy_datetime(value)
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
                value = parse_legacy_datetime(value)
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
                value = parse_legacy_datetime(value)
            setattr(schedule, field, value)
        audit_plan_id = schedule.plan_id
        audit_task_id = schedule.task_id
    elif operation.entity_type == "stage" and "delete" in inverse:
        stage = await db.get(Stage, int(inverse["delete"]))
        if stage:
            audit_plan_id = stage.plan_id
            await db.delete(stage)
            await flush_uow(db)
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
            await flush_uow(db)
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
                value = parse_legacy_datetime(value)
            setattr(event, field, value)
        audit_plan_id = event.plan_id
    elif operation.entity_type == "submission" and "submission" in inverse:
        submission = await db.get(TaskSubmission, int(operation.entity_id))
        task = await db.get(Task, submission.task_id) if submission else None
        if not submission or not task:
            raise HTTPException(status_code=409, detail="Submission or task no longer exists")
        for field, value in inverse["submission"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = parse_legacy_datetime(value)
            setattr(submission, field, value)
        for field, value in inverse["task"].items():
            if field.endswith("_at") and isinstance(value, str):
                value = parse_legacy_datetime(value)
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
        if prepared_workspace_undo is None:  # pragma: no cover - guarded above
            raise HTTPException(status_code=409, detail="Workspace undo was not prepared")
        request_digest = hashlib.sha256(
            json.dumps(
                {"operation_id": operation.id, "inverse": inverse},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        action_key = f"undo:{operation.id}"
        if inverse.get("delete"):
            action = await enqueue_workspace_delete(
                db,
                owner_id=operation.owner_id,
                run_id=operation.run_id,
                action_key=action_key,
                request_digest=request_digest,
                prepared=prepared_workspace_undo,
                operation_id=operation.id,
                operation_status_on_delivery="undone",
            )
        else:
            action = await enqueue_workspace_write(
                db,
                owner_id=operation.owner_id,
                run_id=operation.run_id,
                action_key=action_key,
                request_digest=request_digest,
                prepared=prepared_workspace_undo,
                operation_id=operation.id,
                operation_status_on_delivery="undone",
            )
        undo_outbox_action_id = action.id
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
                value = parse_legacy_datetime(value)
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
    elif operation.entity_type == "competency" and "delete" in inverse:
        competency = await db.get(Competency, int(inverse["delete"]))
        if competency:
            audit_plan_id = competency.plan_id
            await db.delete(competency)
    elif operation.entity_type == "competency_edge" and "delete" in inverse:
        edge = await db.get(CompetencyEdge, int(inverse["delete"]))
        if edge:
            await db.delete(edge)
    elif operation.entity_type == "plan_competency_link" and "delete" in inverse:
        link = await db.get(PlanCompetencyLink, int(inverse["delete"]))
        if link:
            audit_plan_id = link.plan_id
            await db.delete(link)
    elif operation.entity_type == "task_competency_link" and "delete" in inverse:
        link = await db.get(TaskCompetencyLink, int(inverse["delete"]))
        if link:
            task = await db.get(Task, link.task_id)
            if task:
                stage = await db.get(Stage, task.stage_id)
                audit_plan_id = stage.plan_id if stage else None
                audit_task_id = task.id
            await db.delete(link)
    elif operation.entity_type == "resource_competency_link" and "delete" in inverse:
        link = await db.get(ResourceCompetencyLink, int(inverse["delete"]))
        if link:
            resource = await db.get(LearningResource, link.resource_id)
            audit_plan_id = resource.plan_id if resource else None
            await db.delete(link)
    else:
        raise HTTPException(status_code=409, detail="No supported inverse operation")

    workspace_undo_pending = undo_outbox_action_id is not None
    operation.status = "undo_pending" if workspace_undo_pending else "undone"
    operation.undone_at = None if workspace_undo_pending else datetime.now(timezone.utc)
    db.add(LearningEvent(
        owner_id=settings.DEFAULT_OWNER_ID,
        plan_id=audit_plan_id,
        task_id=audit_task_id,
        run_id=operation.run_id,
        event_type=(
            "operation.undo_requested"
            if workspace_undo_pending
            else "operation.undone"
        ),
        summary=(
            f"Requested undo for {operation.tool_name} on {operation.entity_type}:{operation.entity_id}"
            if workspace_undo_pending
            else f"Undid {operation.tool_name} on {operation.entity_type}:{operation.entity_id}"
        ),
        payload={
            "operation_id": operation.id,
            "tool_name": operation.tool_name,
            "entity_type": operation.entity_type,
            "entity_id": operation.entity_id,
            **(
                {"outbox_action_id": undo_outbox_action_id}
                if undo_outbox_action_id
                else {}
            ),
        },
        idempotency_key=(
            f"operation:{operation.id}:undo_requested"
            if workspace_undo_pending
            else f"operation:{operation.id}:undone"
        ),
    ))
    await commit_uow(db)
    await db.refresh(operation)
    return operation
