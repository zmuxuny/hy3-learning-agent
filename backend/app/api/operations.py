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
    ensure_sqlite_write_transaction,
    rollback as rollback_uow,
)
from app.models import Achievement, ActivityDay, Artifact, CalendarEvent, Competency, CompetencyEdge, EvidenceObservation, EvidenceProjectionState, LearningEvent, LearningResource, Operation, OperationEvidenceLink, Plan, PlanCompetencyLink, PlanProposal, Quiz, ResourceCompetencyLink, ReviewSchedule, Session, Stage, Task, TaskCompetencyLink, TaskSubmission, UserProfile
from app.outbox import (
    enqueue_workspace_delete,
    enqueue_workspace_write,
    prepare_workspace_delete,
    prepare_workspace_write,
)
from app.schemas import OperationRead
from app.services.plans import recompute_plan_state
from app.services.evidence import (
    append_operation_invalidations,
    append_operation_redo_generation,
    operation_evidence_generation,
    refresh_plan_evidence_projection,
)
from app.services.competency_protocol import (
    active_operation_dependents,
    competency_node_dependencies,
    record_graph_mutation,
)


router = APIRouter()


_UNDO_REPLAY_STATUSES = {"undo_pending", "undone", "needs_reconciliation"}


async def _undo_dependency_conflicts(
    db: AsyncSession,
    operation: Operation,
) -> list[str]:
    conflicts = [
        f"operation:{item.operation_id}"
        for item in await active_operation_dependents(db, operation_id=operation.id)
    ]
    inverse = operation.inverse_patch or {}
    if operation.entity_type == "competency" and "delete" in inverse:
        dependencies = await competency_node_dependencies(
            db,
            owner_id=operation.owner_id,
            competency_id=int(inverse["delete"]),
        )
        conflicts.extend(
            f"{item.entity_type}:{item.entity_id}" for item in dependencies
        )
    elif operation.entity_type == "plan" and "delete" in inverse:
        plan_id = int(inverse["delete"])
        for model, label in (
            (EvidenceObservation, "evidence_observation"),
            (Artifact, "artifact"),
            (EvidenceProjectionState, "evidence_projection"),
        ):
            row_id = await db.scalar(select(model.id).where(model.plan_id == plan_id).limit(1))
            if row_id is not None:
                conflicts.append(f"{label}:{row_id}")
        for model, label in (
            (Competency, "plan_competency"),
            (PlanCompetencyLink, "plan_competency_link"),
        ):
            row_id = await db.scalar(select(model.id).where(model.plan_id == plan_id).limit(1))
            if row_id is not None:
                conflicts.append(f"{label}:{row_id}")
        task_link_id = await db.scalar(
            select(TaskCompetencyLink.id)
            .join(Task, Task.id == TaskCompetencyLink.task_id)
            .join(Stage, Stage.id == Task.stage_id)
            .where(Stage.plan_id == plan_id)
            .limit(1)
        )
        if task_link_id is not None:
            conflicts.append(f"task_competency_link:{task_link_id}")
        resource_link_id = await db.scalar(
            select(ResourceCompetencyLink.id)
            .join(
                LearningResource,
                LearningResource.id == ResourceCompetencyLink.resource_id,
            )
            .where(LearningResource.plan_id == plan_id)
            .limit(1)
        )
        if resource_link_id is not None:
            conflicts.append(f"resource_competency_link:{resource_link_id}")
    elif operation.entity_type in {"task", "stage"} and "delete" in inverse:
        if operation.entity_type == "task":
            task_ids = [int(inverse["delete"])]
        else:
            task_ids = list(
                (
                    await db.execute(
                        select(Task.id).where(Task.stage_id == int(inverse["delete"]))
                    )
                ).scalars()
            )
        if task_ids:
            for model, label in (
                (EvidenceObservation, "evidence_observation"),
                (Artifact, "artifact"),
            ):
                row_id = await db.scalar(select(model.id).where(model.task_id.in_(task_ids)).limit(1))
                if row_id is not None:
                    conflicts.append(f"{label}:{row_id}")
            task_link_id = await db.scalar(
                select(TaskCompetencyLink.id)
                .where(TaskCompetencyLink.task_id.in_(task_ids))
                .limit(1)
            )
            if task_link_id is not None:
                conflicts.append(f"task_competency_link:{task_link_id}")
    elif operation.entity_type == "learning_resource" and "delete" in inverse:
        link_id = await db.scalar(
            select(ResourceCompetencyLink.id)
            .where(ResourceCompetencyLink.resource_id == int(inverse["delete"]))
            .limit(1)
        )
        if link_id is not None:
            conflicts.append(f"resource_competency_link:{link_id}")
    return sorted(set(conflicts))


async def _restore_award_before(
    db: AsyncSession,
    *,
    owner_id: str,
    award: dict | None,
) -> None:
    if not isinstance(award, dict):
        return
    profile_state = award.get("profile")
    if isinstance(profile_state, dict):
        profile = await db.get(UserProfile, owner_id)
        if profile is None:
            raise HTTPException(status_code=409, detail="Award profile no longer exists")
        for field in ("xp", "level", "streak_days"):
            if field in profile_state:
                setattr(profile, field, profile_state[field])
    day_id = award.get("day_id")
    if day_id is not None:
        day = await db.get(ActivityDay, int(day_id))
        before_day = award.get("day")
        if before_day is None:
            if day is not None:
                await db.delete(day)
        elif day is None:
            raise HTTPException(status_code=409, detail="Award activity day no longer exists")
        else:
            for field in ("xp", "completed_tasks", "passed_quizzes"):
                day_value = before_day.get(field)
                if day_value is not None:
                    setattr(day, field, day_value)
    for achievement_id in award.get("delete_achievements", []):
        achievement = await db.get(Achievement, int(achievement_id))
        if achievement is not None:
            await db.delete(achievement)


async def _apply_award_after(
    db: AsyncSession,
    *,
    owner_id: str,
    award: dict | None,
) -> None:
    if not isinstance(award, dict):
        raise HTTPException(status_code=409, detail="Award redo snapshot is incomplete")
    if not {"profile", "day", "achievements"}.issubset(award):
        raise HTTPException(status_code=409, detail="Award redo snapshot is incomplete")
    profile_state = award.get("profile")
    if isinstance(profile_state, dict):
        if not {"xp", "level", "streak_days"}.issubset(profile_state):
            raise HTTPException(status_code=409, detail="Award profile snapshot is incomplete")
        profile = await db.get(UserProfile, owner_id)
        if profile is None:
            raise HTTPException(status_code=409, detail="Award profile no longer exists")
        for field in ("xp", "level", "streak_days"):
            if field in profile_state:
                setattr(profile, field, profile_state[field])
    day_state = award.get("day")
    if isinstance(day_state, dict):
        if not {
            "id",
            "date",
            "xp",
            "completed_tasks",
            "passed_quizzes",
        }.issubset(day_state):
            raise HTTPException(status_code=409, detail="Award day snapshot is incomplete")
        natural_day = await db.scalar(
            select(ActivityDay).where(
                ActivityDay.owner_id == owner_id,
                ActivityDay.date == day_state["date"],
            )
        )
        if natural_day is not None and natural_day.id != int(day_state["id"]):
            raise HTTPException(
                status_code=409,
                detail="Award activity day identity was reused",
            )
        day = await db.get(ActivityDay, int(day_state["id"]))
        if day is None:
            day = ActivityDay(
                id=int(day_state["id"]),
                owner_id=owner_id,
                date=day_state["date"],
            )
            db.add(day)
        elif day.owner_id != owner_id or day.date != day_state["date"]:
            raise HTTPException(
                status_code=409,
                detail="Award activity day identity was reused",
            )
        for field in ("xp", "completed_tasks", "passed_quizzes"):
            setattr(day, field, day_state[field])
    if not isinstance(award.get("achievements"), list):
        raise HTTPException(status_code=409, detail="Achievement redo snapshot is incomplete")
    for snapshot in award["achievements"]:
        if not isinstance(snapshot, dict) or not {
            "id",
            "key",
            "title",
            "unlocked_at",
        }.issubset(snapshot):
            raise HTTPException(status_code=409, detail="Achievement redo snapshot is malformed")
        existing = await db.scalar(
            select(Achievement).where(
                Achievement.owner_id == owner_id,
                Achievement.key == snapshot["key"],
            )
        )
        if existing is not None:
            semantic_snapshot = (
                snapshot["title"],
                snapshot.get("description", ""),
                snapshot.get("badge_kind", "rule"),
                snapshot.get("badge_image_url", ""),
            )
            semantic_existing = (
                existing.title,
                existing.description,
                existing.badge_kind,
                existing.badge_image_url,
            )
            if semantic_existing != semantic_snapshot:
                raise HTTPException(
                    status_code=409,
                    detail="Achievement identity was reused with different content",
                )
            # A later action may have independently unlocked the same
            # semantic achievement. It owns that row; this redo is a no-op for
            # the already-satisfied side effect, and a later undo won't delete it.
        else:
            occupied = await db.get(Achievement, int(snapshot["id"]))
            if occupied is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Achievement identity was reused",
                )
            db.add(
                Achievement(
                    id=int(snapshot["id"]),
                    owner_id=owner_id,
                    key=snapshot["key"],
                    title=snapshot["title"],
                    description=snapshot.get("description", ""),
                    badge_kind=snapshot.get("badge_kind", "rule"),
                    badge_image_url=snapshot.get("badge_image_url", ""),
                    unlocked_at=parse_legacy_datetime(snapshot["unlocked_at"]),
                )
            )


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
    await ensure_sqlite_write_transaction(db)
    operation = await db.get(Operation, operation_id)
    if not operation or operation.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Operation not found")
    if operation.status in _UNDO_REPLAY_STATUSES:
        # The endpoint body is only the stable Operation id. Repeating it must
        # therefore return the same durable state instead of applying the
        # inverse twice (or turning a successful retry into a conflict).
        await commit_uow(db)
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

        await ensure_sqlite_write_transaction(db)
        operation = await db.get(Operation, operation_id)
        if operation is None or operation.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=404, detail="Operation not found")

    conflicts = await _undo_dependency_conflicts(db, operation)
    if conflicts:
        await rollback_uow(db)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OPERATION_HAS_DEPENDENTS",
                "dependencies": conflicts,
            },
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
        try:
            await _restore_award_before(
                db,
                owner_id=operation.owner_id,
                award=inverse.get("award"),
            )
        except HTTPException:
            await rollback_uow(db)
            raise
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
        try:
            await _restore_award_before(
                db,
                owner_id=operation.owner_id,
                award=(
                    inverse.get("award")
                    if "award" in inverse
                    else {"profile": inverse.get("profile")}
                ),
            )
        except HTTPException:
            await rollback_uow(db)
            raise
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

    invalidations = await append_operation_invalidations(db, operation)
    undo_evidence_generation = await operation_evidence_generation(db, operation.id)
    affected_evidence_plans = {
        item.plan_id for item in invalidations if item.plan_id is not None
    }
    for evidence_plan_id in sorted(affected_evidence_plans):
        await refresh_plan_evidence_projection(
            db,
            operation.owner_id,
            evidence_plan_id,
        )
    if operation.entity_type in {
        "competency",
        "competency_edge",
        "plan_competency_link",
        "task_competency_link",
        "resource_competency_link",
    }:
        await record_graph_mutation(
            db,
            owner_id=operation.owner_id,
            action_key=f"operation:{operation.id}:undo",
            action="undo",
            entity_type=operation.entity_type,
            entity_id=operation.entity_id,
            operation_id=operation.id,
        )

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
            else (
                f"operation:{operation.id}:undone:{undo_evidence_generation}"
                if undo_evidence_generation is not None
                else f"operation:{operation.id}:undone"
            )
        ),
    ))
    await commit_uow(db)
    await db.refresh(operation)
    return operation


@router.post("/{operation_id}/redo", response_model=OperationRead)
async def redo_operation(operation_id: str, db: AsyncSession = Depends(get_db)):
    await ensure_sqlite_write_transaction(db)
    operation = await db.get(Operation, operation_id)
    if operation is None or operation.owner_id != settings.DEFAULT_OWNER_ID:
        await rollback_uow(db)
        raise HTTPException(status_code=404, detail="Operation not found")

    generation = await operation_evidence_generation(db, operation.id)
    if operation.status == "committed" and generation is not None and generation > 0:
        await commit_uow(db)
        return operation
    if operation.status != "undone":
        await rollback_uow(db)
        raise HTTPException(status_code=409, detail="Operation is not redoable")
    forward = operation.forward_patch or {}
    invalidation_link = (
        await db.scalar(
            select(OperationEvidenceLink.id).where(
                OperationEvidenceLink.operation_id == operation.id,
                OperationEvidenceLink.generation == generation,
                OperationEvidenceLink.role == "invalidation",
            ).limit(1)
        )
        if generation is not None
        else None
    )
    if (
        forward.get("evidence_protocol") != 1
        or operation.entity_type not in {"task", "submission", "quiz"}
        or generation is None
        or invalidation_link is None
    ):
        await rollback_uow(db)
        raise HTTPException(
            status_code=409,
            detail="Operation redo requires an unambiguous Evidence protocol snapshot",
        )

    try:
        claim = await db.execute(
            update(Operation)
            .where(
                Operation.id == operation.id,
                Operation.owner_id == settings.DEFAULT_OWNER_ID,
                Operation.status == "undone",
            )
            .values(status="redo_pending")
        )
    except OperationalError as exc:
        if not is_database_busy(exc):
            raise
        await rollback_uow(db)
        raise DatabaseBusyError() from exc
    if claim.rowcount != 1:
        await rollback_uow(db)
        current = await db.get(Operation, operation_id)
        current_generation = (
            await operation_evidence_generation(db, operation_id)
            if current is not None
            else None
        )
        if (
            current is not None
            and current.status == "committed"
            and current_generation is not None
            and current_generation > generation
        ):
            return current
        raise HTTPException(status_code=409, detail="Operation is not redoable")
    operation.status = "redo_pending"

    audit_plan_id: int | None = None
    audit_task_id: int | None = None
    if operation.entity_type == "task":
        task = await db.get(Task, int(operation.entity_id))
        changes = forward.get("changes")
        if task is None or not isinstance(changes, dict):
            await rollback_uow(db)
            raise HTTPException(status_code=409, detail="Task redo snapshot is incomplete")
        for field, value in changes.items():
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
    elif operation.entity_type == "submission":
        submission = await db.get(TaskSubmission, int(operation.entity_id))
        task = await db.get(Task, submission.task_id) if submission else None
        submission_changes = forward.get("submission")
        task_changes = forward.get("task")
        if (
            submission is None
            or task is None
            or not isinstance(submission_changes, dict)
            or not isinstance(task_changes, dict)
        ):
            await rollback_uow(db)
            raise HTTPException(status_code=409, detail="Submission redo snapshot is incomplete")
        for target, changes in ((submission, submission_changes), (task, task_changes)):
            for field, value in changes.items():
                if field.endswith("_at") and isinstance(value, str):
                    value = parse_legacy_datetime(value)
                setattr(target, field, value)
        award = forward.get("award")
        if award is not None:
            try:
                await _apply_award_after(db, owner_id=operation.owner_id, award=award)
            except HTTPException:
                await rollback_uow(db)
                raise
        await db.refresh(task, ["stage"])
        await db.refresh(task.stage, ["plan"])
        await db.refresh(task.stage.plan, ["stages"])
        for stage in task.stage.plan.stages:
            await db.refresh(stage, ["tasks"])
        await recompute_plan_state(task.stage.plan)
        task.stage.plan.version += 1
        audit_plan_id = task.stage.plan.id
        audit_task_id = task.id
    else:
        quiz = await db.get(Quiz, int(operation.entity_id))
        changes = forward.get("changes")
        if quiz is None or not isinstance(changes, dict):
            await rollback_uow(db)
            raise HTTPException(status_code=409, detail="Quiz redo snapshot is incomplete")
        for field, value in changes.items():
            if field.endswith("_at") and isinstance(value, str):
                value = parse_legacy_datetime(value)
            setattr(quiz, field, value)
        try:
            await _apply_award_after(
                db,
                owner_id=operation.owner_id,
                award=forward.get("award"),
            )
        except HTTPException:
            await rollback_uow(db)
            raise
        review_state = forward.get("review")
        if isinstance(review_state, dict):
            review = await db.get(ReviewSchedule, int(review_state["id"]))
            if review is not None:
                await rollback_uow(db)
                raise HTTPException(
                    status_code=409,
                    detail="Quiz redo review identity was reused",
                )
            db.add(
                ReviewSchedule(
                    id=int(review_state["id"]),
                    owner_id=operation.owner_id,
                    plan_id=int(review_state["plan_id"]),
                    task_id=review_state.get("task_id"),
                    due_at=parse_legacy_datetime(review_state["due_at"]),
                    review_type=review_state["review_type"],
                    status=review_state["status"],
                )
            )
        audit_plan_id = quiz.plan_id
        audit_task_id = quiz.task_id

    redo_generation, produced = await append_operation_redo_generation(db, operation)
    for plan_id in sorted({item.plan_id for item in produced if item.plan_id is not None}):
        await refresh_plan_evidence_projection(db, operation.owner_id, plan_id)
    operation.status = "committed"
    operation.undone_at = None
    db.add(
        LearningEvent(
            owner_id=operation.owner_id,
            plan_id=audit_plan_id,
            task_id=audit_task_id,
            run_id=operation.run_id,
            event_type="operation.redone",
            summary=(
                f"Redid {operation.tool_name} on "
                f"{operation.entity_type}:{operation.entity_id}"
            ),
            payload={
                "operation_id": operation.id,
                "generation": redo_generation,
            },
            idempotency_key=f"operation:{operation.id}:redo:{redo_generation}",
        )
    )
    await commit_uow(db)
    await db.refresh(operation)
    return operation
