from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.db.uow import commit as commit_uow
from app.models import AgentRun, LearningResource, Operation, Plan, QueuedMessage
from app.runtime.state import NONTERMINAL_RUN_STATUSES
from app.schemas import (
    CompetencyGraphOutput,
    EvidenceListOutput,
    LearningResourceRead,
    PlanArchiveUpdate,
    PlanCreate,
    PlanRead,
    TaskRead,
    TaskUpdate,
)
from app.services import plans as plan_service
from app.services.competencies import graph_for_plan
from app.services.competency_protocol import graph_revision
from app.services.evidence import (
    build_plan_evidence_state,
    list_observations,
    observation_dict,
)


router = APIRouter()


@router.get("", response_model=list[PlanRead])
async def read_plans(archived: bool = False, db: AsyncSession = Depends(get_db)):
    return await plan_service.list_plans(db, settings.DEFAULT_OWNER_ID, archived=archived)


@router.get("/{plan_id}/resources", response_model=list[LearningResourceRead])
async def read_plan_resources(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    result = await db.execute(
        select(LearningResource)
        .where(
            LearningResource.owner_id == settings.DEFAULT_OWNER_ID,
            LearningResource.plan_id == plan_id,
            LearningResource.url.not_like("%duckduckgo.com/y.js%"),
        )
        .order_by(LearningResource.verified_at.desc(), LearningResource.created_at.desc())
        .limit(50)
    )
    return list(result.scalars())


@router.get("/{plan_id}/evidence")
@router.get("/{plan_id}/evidence-state")
async def read_plan_evidence(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    return await build_plan_evidence_state(db, settings.DEFAULT_OWNER_ID, plan_id)


@router.get("/{plan_id}/competencies", response_model=CompetencyGraphOutput)
async def read_plan_competencies(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    graph = await graph_for_plan(db, settings.DEFAULT_OWNER_ID, plan_id)
    graph["revision"] = await graph_revision(db, settings.DEFAULT_OWNER_ID)
    return graph


@router.get("/{plan_id}/evidence-observations", response_model=EvidenceListOutput)
async def read_plan_evidence_observations(
    plan_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    plan = await db.get(Plan, plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    observations = await list_observations(
        db,
        settings.DEFAULT_OWNER_ID,
        plan_id=plan_id,
        limit=limit,
    )
    return {"observations": [observation_dict(item) for item in observations]}


@router.get("/{plan_id}", response_model=PlanRead)
async def read_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan_id)


@router.post("", response_model=PlanRead, status_code=201)
async def create_plan(data: PlanCreate, db: AsyncSession = Depends(get_db)):
    completeness_issues = plan_service.plan_completeness_issues(data)
    if completeness_issues:
        raise HTTPException(
            status_code=422,
            detail="A formal plan is incomplete: " + "; ".join(completeness_issues),
        )
    plan = await plan_service.create_plan(db, settings.DEFAULT_OWNER_ID, data)
    await commit_uow(db)
    return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan.id)


@router.patch("/{plan_id}/archive", response_model=PlanRead)
async def set_plan_archived(plan_id: int, data: PlanArchiveUpdate, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    before = {"status": plan.status, "archived_from_status": plan.archived_from_status}
    if data.archived:
        if plan.status == "archived":
            return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan.id)
        active_run = (await db.execute(
            select(AgentRun.id).where(
                AgentRun.owner_id == settings.DEFAULT_OWNER_ID,
                AgentRun.plan_id == plan.id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise HTTPException(
                status_code=409,
                detail="Stop or resolve the active plan run before archiving this plan",
            )
        queued_message = (await db.execute(
            select(QueuedMessage.id).where(
                QueuedMessage.owner_id == settings.DEFAULT_OWNER_ID,
                QueuedMessage.plan_id == plan.id,
            ).limit(1)
        )).scalar_one_or_none()
        if queued_message:
            raise HTTPException(
                status_code=409,
                detail="Send or delete queued messages before archiving this plan",
            )
        plan.archived_from_status = plan.status
        plan.status = "archived"
        action = "archive"
    else:
        if plan.status != "archived":
            return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan.id)
        plan.status = plan.archived_from_status or "active"
        plan.archived_from_status = None
        action = "restore"
    plan.version += 1
    db.add(Operation(
        owner_id=settings.DEFAULT_OWNER_ID,
        run_id=None,
        tool_name=f"plan.{action}",
        entity_type="plan",
        entity_id=str(plan.id),
        forward_patch={"changes": {"status": plan.status, "archived_from_status": plan.archived_from_status}},
        inverse_patch={"changes": before},
        created_at=datetime.now(timezone.utc),
    ))
    await commit_uow(db)
    return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan.id)


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def update_task(task_id: int, data: TaskUpdate, db: AsyncSession = Depends(get_db)):
    task = await plan_service.update_task(
        db,
        settings.DEFAULT_OWNER_ID,
        task_id,
        data,
    )
    await commit_uow(db)
    await db.refresh(task)
    return task
