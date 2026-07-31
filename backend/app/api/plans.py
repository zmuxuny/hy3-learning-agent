from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.schemas import PlanCreate, PlanRead, TaskRead, TaskUpdate
from app.services import plans as plan_service


router = APIRouter()


@router.get("", response_model=list[PlanRead])
async def read_plans(db: AsyncSession = Depends(get_db)):
    return await plan_service.list_plans(db, settings.DEFAULT_OWNER_ID)


@router.get("/{plan_id}", response_model=PlanRead)
async def read_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    return await plan_service.get_plan(db, settings.DEFAULT_OWNER_ID, plan_id)


@router.post("", response_model=PlanRead, status_code=201)
async def create_plan(data: PlanCreate, db: AsyncSession = Depends(get_db)):
    return await plan_service.create_plan(db, settings.DEFAULT_OWNER_ID, data)


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def update_task(task_id: int, data: TaskUpdate, db: AsyncSession = Depends(get_db)):
    return await plan_service.update_task(db, settings.DEFAULT_OWNER_ID, task_id, data)
