import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import canonical_utc, utc_now
from app.db.uow import flush as flush_uow
from app.models import LearningEvent, Plan, ReviewSchedule, Stage, Task
from app.schemas import PlanCreate, TaskUpdate
from app.services.gamification import evaluate_achievements


PLAN_LOAD = selectinload(Plan.stages).selectinload(Stage.tasks)


def build_plan_memory_summary(plan: Plan) -> str:
    """Summarize already loaded plan facts without a provider or a database write."""

    tasks = [task for stage in plan.stages for task in stage.tasks]
    completed = [task for task in tasks if task.status == "completed"]
    blocked = [task.title for task in tasks if task.status == "blocked"]
    active = [task.title for task in tasks if task.status == "active"]
    next_pending = next((task.title for task in tasks if task.status == "pending"), "")
    return (
        f"进度 {len(completed)}/{len(tasks)}；"
        f"当前任务：{'、'.join(active[:3]) or next_pending or '无'}；"
        f"阻塞：{'、'.join(blocked[:3]) or '无'}；"
        f"计划版本 {plan.version}。"
    )


def plan_completeness_issues(data: PlanCreate) -> list[str]:
    """Return structural gaps that make a formal learning plan unusable."""
    issues: list[str] = []
    if not data.goal.strip():
        issues.append("goal is required")
    if not data.expected_outcome.strip():
        issues.append("expected_outcome is required")
    if not data.stages:
        issues.append("at least one stage is required")
    for index, stage in enumerate(data.stages, start=1):
        if not stage.tasks:
            issues.append(f"stage {index} must contain at least one task")
        for task_index, task in enumerate(stage.tasks, start=1):
            for field in ("due_at", "review_due_at"):
                value = getattr(task, field)
                if value is not None and data.deadline is not None and canonical_utc(value) > canonical_utc(data.deadline):
                    issues.append(f"stage {index} task {task_index}: {field} exceeds plan deadline")
            if task.due_at is not None and task.review_due_at is not None and canonical_utc(task.review_due_at) < canonical_utc(task.due_at):
                issues.append(f"stage {index} task {task_index}: review precedes task due date")
            if task.is_core and not task.evidence_required:
                issues.append(f"stage {index} task {task_index}: core tasks require evidence_required=true")
    return issues


async def list_plans(db: AsyncSession, owner_id: str, *, archived: bool = False) -> list[Plan]:
    query = select(Plan).where(Plan.owner_id == owner_id)
    query = query.where(Plan.status == "archived" if archived else Plan.status != "archived")
    result = await db.execute(query.options(PLAN_LOAD).order_by(Plan.updated_at.desc()))
    return list(result.scalars().unique())


async def get_plan(db: AsyncSession, owner_id: str, plan_id: int) -> Plan:
    result = await db.execute(
        select(Plan).where(Plan.id == plan_id, Plan.owner_id == owner_id).options(PLAN_LOAD)
    )
    plan = result.scalars().unique().one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


async def create_plan(
    db: AsyncSession,
    owner_id: str,
    data: PlanCreate,
    run_id: str | None = None,
) -> Plan:
    stage_count = len(data.stages)
    plan = Plan(
        owner_id=owner_id,
        title=data.title,
        description=data.description,
        goal=data.goal,
        current_level=data.current_level,
        deadline=data.deadline,
        weekly_minutes=data.weekly_minutes,
        preferences=data.preferences,
        expected_outcome=data.expected_outcome,
        available_resources=data.available_resources,
        avoid_methods=data.avoid_methods,
    )
    for stage_position, stage_data in enumerate(data.stages):
        stage = Stage(
            title=stage_data.title,
            description=stage_data.description,
            objectives=stage_data.objectives,
            position=stage_position,
        )
        for task_position, task_data in enumerate(stage_data.tasks):
            stage.tasks.append(
                Task(
                    title=task_data.title,
                    description=task_data.description,
                    kind=task_data.kind,
                    is_core=task_data.is_core,
                    evidence_required=task_data.evidence_required,
                    estimated_minutes=task_data.estimated_minutes,
                    due_at=task_data.due_at,
                    review_due_at=task_data.review_due_at,
                    resource_url=task_data.resource_url,
                    task_metadata=task_data.metadata,
                    position=task_position,
                )
            )
        plan.stages.append(stage)
    db.add(plan)
    await flush_uow(db)
    db.add(
        LearningEvent(
            owner_id=owner_id,
            plan_id=plan.id,
            run_id=run_id,
            event_type="plan.created",
            summary=f"Created plan: {plan.title}",
            payload={"title": plan.title, "stage_count": stage_count},
        )
    )
    await evaluate_achievements(db, owner_id)
    await flush_uow(db)
    return plan


async def update_task(
    db: AsyncSession,
    owner_id: str,
    task_id: int,
    data: TaskUpdate,
    run_id: str | None = None,
    *,
    session_id: str | None = None,
    emit_evidence: bool = True,
) -> Task:
    result = await db.execute(
        select(Task)
        .join(Stage)
        .join(Plan)
        .where(Task.id == task_id, Plan.owner_id == owner_id)
        .options(
            selectinload(Task.stage)
            .selectinload(Stage.plan)
            .selectinload(Plan.stages)
            .selectinload(Stage.tasks)
        )
    )
    task = result.scalars().one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.stage.plan.status == "archived":
        raise HTTPException(status_code=409, detail="Restore the plan before changing its learning state")

    changes = data.model_dump(exclude_unset=True)
    evidence = changes.pop("evidence", None)
    if changes.get("status") == "completed" and task.evidence_required and not evidence:
        raise HTTPException(status_code=409, detail="Core task completion requires evidence or a passed assessment")

    before = {field: getattr(task, field) for field in changes}
    for field, value in changes.items():
        setattr(task, field, value)
    if evidence is not None:
        task.task_metadata = {**task.task_metadata, "completion_evidence": evidence}
    if changes.get("status") == "completed" and task.completed_at is None:
        task.completed_at = utc_now()
        if task.review_due_at:
            existing_review = (await db.execute(
                select(ReviewSchedule.id).where(
                    ReviewSchedule.owner_id == owner_id,
                    ReviewSchedule.plan_id == task.stage.plan_id,
                    ReviewSchedule.task_id == task.id,
                    ReviewSchedule.due_at == task.review_due_at,
                    ReviewSchedule.status == "scheduled",
                ).limit(1)
            )).scalar_one_or_none()
            if existing_review is None:
                db.add(
                    ReviewSchedule(
                        owner_id=owner_id,
                        plan_id=task.stage.plan_id,
                        task_id=task.id,
                        due_at=task.review_due_at,
                    )
                )
    elif changes.get("status") and changes["status"] != "completed":
        task.completed_at = None
    await recompute_plan_state(task.stage.plan)
    task.stage.plan.version += 1
    learning_event = LearningEvent(
        owner_id=owner_id,
        plan_id=task.stage.plan_id,
        task_id=task.id,
        run_id=run_id,
        event_type="task.updated",
        summary=f"Updated task: {task.title}",
        correlation_id=run_id,
        payload={
            "before": {
                key: canonical_utc(value) if isinstance(value, datetime) else value
                for key, value in before.items()
            },
            "after": {
                key: canonical_utc(value) if isinstance(value, datetime) else value
                for key, value in changes.items()
            },
            "evidence": evidence or [],
        },
    )
    db.add(learning_event)
    await flush_uow(db)
    produced_evidence = []
    if evidence and emit_evidence:
        from app.services.evidence import (
            append_observation,
            artifact_ref,
            create_artifact,
            refresh_plan_evidence_projection,
        )

        completion_artifact, _ = await create_artifact(
            db,
            owner_id=owner_id,
            artifact_type="task_evidence",
            source_uri=f"task:{task.id}:event:{learning_event.id}",
            idempotency_key=f"task:{task.id}:event:{learning_event.id}:artifact",
            title=f"任务证据：{task.title}",
            content=json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=canonical_utc),
            metadata={"task_id": task.id, "event_id": learning_event.id},
            plan_id=task.stage.plan_id,
            task_id=task.id,
            run_id=run_id,
            session_id=session_id,
        )

        observation, _ = await append_observation(
            db,
            owner_id=owner_id,
            source_type="task_evidence",
            source_id=f"task:{task.id}:event:{learning_event.id}",
            outcome="verified" if changes.get("status") == "completed" else "observed",
            idempotency_key=f"task:{task.id}:event:{learning_event.id}:evidence",
            run_id=run_id,
            session_id=session_id,
            plan_id=task.stage.plan_id,
            task_id=task.id,
            payload={"evidence": evidence},
            evaluator={
                "type": "agent" if run_id else "explicit_task_evidence",
                "run_id": run_id,
            },
            artifact_refs=[artifact_ref(completion_artifact, kind="task_evidence")],
            occurred_at=task.completed_at or utc_now(),
            correlation_id=run_id,
            causation_id=f"learning_event:{learning_event.id}",
        )
        produced_evidence.append(observation)
        await refresh_plan_evidence_projection(
            db,
            owner_id,
            task.stage.plan_id,
        )
    task._produced_evidence = produced_evidence
    await flush_uow(db)
    return task


async def recompute_plan_state(plan: Plan) -> None:
    all_tasks = [task for stage in plan.stages for task in stage.tasks]
    progress_tasks = [task for task in all_tasks if task.status != "skipped"]
    plan.progress = (
        sum(task.status == "completed" for task in progress_tasks) / len(progress_tasks)
        if progress_tasks
        else 0.0
    )
    for stage in plan.stages:
        statuses = [task.status for task in stage.tasks]
        if statuses and any(status == "completed" for status in statuses) and all(
            status in {"completed", "skipped"} for status in statuses
        ):
            stage.status = "completed"
        elif any(status in {"active", "completed"} for status in statuses):
            stage.status = "active"
        else:
            stage.status = "pending"
