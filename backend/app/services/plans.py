from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import LearningEvent, Plan, ReviewSchedule, Stage, Task
from app.schemas import PlanCreate, TaskUpdate


PLAN_LOAD = selectinload(Plan.stages).selectinload(Stage.tasks)


async def list_plans(db: AsyncSession, owner_id: str) -> list[Plan]:
    result = await db.execute(
        select(Plan).where(Plan.owner_id == owner_id).options(PLAN_LOAD).order_by(Plan.updated_at.desc())
    )
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
    *,
    commit: bool = True,
) -> Plan:
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
    await db.flush()
    db.add(
        LearningEvent(
            owner_id=owner_id,
            plan_id=plan.id,
            run_id=run_id,
            event_type="plan.created",
            summary=f"Created plan: {plan.title}",
            payload={"title": plan.title, "stage_count": len(plan.stages)},
        )
    )
    if commit:
        await db.commit()
        return await get_plan(db, owner_id, plan.id)
    await db.flush()
    return plan


async def update_task(
    db: AsyncSession,
    owner_id: str,
    task_id: int,
    data: TaskUpdate,
    run_id: str | None = None,
    *,
    commit: bool = True,
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
        task.completed_at = datetime.now(timezone.utc)
        if task.review_due_at:
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
    db.add(
        LearningEvent(
            owner_id=owner_id,
            plan_id=task.stage.plan_id,
            task_id=task.id,
            run_id=run_id,
            event_type="task.updated",
            summary=f"Updated task: {task.title}",
            payload={
                "before": {k: str(v) if isinstance(v, datetime) else v for k, v in before.items()},
                "after": {k: str(v) if isinstance(v, datetime) else v for k, v in changes.items()},
                "evidence": evidence or [],
            },
        )
    )
    if commit:
        await db.commit()
        await db.refresh(task)
    else:
        await db.flush()
    return task


async def recompute_plan_state(plan: Plan) -> None:
    all_tasks = [task for stage in plan.stages for task in stage.tasks]
    plan.progress = (
        sum(task.status == "completed" for task in all_tasks) / len(all_tasks)
        if all_tasks
        else 0.0
    )
    for stage in plan.stages:
        statuses = [task.status for task in stage.tasks]
        if statuses and all(status == "completed" for status in statuses):
            stage.status = "completed"
        elif any(status in {"active", "completed"} for status in statuses):
            stage.status = "active"
        else:
            stage.status = "pending"
