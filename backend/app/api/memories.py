from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import ContextAssembler
from app.core.config import settings
from app.db.database import get_db
from app.models import Memory
from app.schemas import ContextSnapshotRead, MemoryProposalCreate, MemoryRead


router = APIRouter()


@router.get("", response_model=list[MemoryRead])
async def read_memories(status: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(Memory).where(Memory.owner_id == settings.DEFAULT_OWNER_ID)
    if status:
        query = query.where(Memory.status == status)
    result = await db.execute(query.order_by(Memory.updated_at.desc()))
    return list(result.scalars())


@router.post("/proposals", response_model=MemoryRead, status_code=201)
async def create_memory_proposal(data: MemoryProposalCreate, db: AsyncSession = Depends(get_db)):
    memory = Memory(owner_id=settings.DEFAULT_OWNER_ID, status="proposed", **data.model_dump())
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    return memory


@router.post("/{memory_id}/confirm", response_model=MemoryRead)
async def confirm_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    memory = await db.get(Memory, memory_id)
    if not memory or memory.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Memory not found")
    memory.status = "confirmed"
    await db.commit()
    await db.refresh(memory)
    return memory


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    memory = await db.get(Memory, memory_id)
    if not memory or memory.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Memory not found")
    await db.delete(memory)
    await db.commit()


@router.post("/snapshots", response_model=ContextSnapshotRead, status_code=201)
async def create_snapshot(plan_id: int | None = None, db: AsyncSession = Depends(get_db)):
    snapshot = await ContextAssembler(db).build(settings.DEFAULT_OWNER_ID, plan_id=plan_id)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot
