from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models import Operation, Plan
from app.schemas import PlanArchiveUpdate, PlanCreate, PlanRead, TaskRead, TaskUpdate
from app.services import plans as plan_service


router = APIRouter()


@router.get("", response_model=list[PlanRead])
async def read_plans(archived: bool = False, db: AsyncSession = Depends(get_db)):
    return await plan_service.list_plans(db, settings.DEFAULT_OWNER_ID, archived=archived)


@router.get("/{plan_id}", response_model=PlanRead)
async def read_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan_id)


@router.post("", response_model=PlanRead, status_code=201)
async def create_plan(data: PlanCreate, db: AsyncSession = Depends(get_db)):
    return await plan_service.create_plan(db, settings.DEFAULT_OWNER_ID, data)


@router.patch("/{plan_id}/archive", response_model=PlanRead)
async def set_plan_archived(plan_id: int, data: PlanArchiveUpdate, db: AsyncSession = Depends(get_db)):
    plan = await db.get(Plan, plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    before = {"status": plan.status, "archived_from_status": plan.archived_from_status}
    if data.archived:
        if plan.status == "archived":
            return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan.id)
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
    await db.commit()
    return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan.id)


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def update_task(task_id: int, data: TaskUpdate, db: AsyncSession = Depends(get_db)):
    return await plan_service.update_task(db, settings.DEFAULT_OWNER_ID, task_id, data)
