from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import ContextAssembler
from app.context.memory import MemoryManager
from app.context.provenance import MemoryLifecycleConflict
from app.core.config import settings
from app.db.database import get_db
from app.db.uow import commit as commit_uow, ensure_sqlite_write_transaction
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
    try:
        await ensure_sqlite_write_transaction(db)
        memory, _ = await MemoryManager(db).propose(
            settings.DEFAULT_OWNER_ID,
            source_type="user",
            source_id=None,
            **data.model_dump(),
        )
    except (MemoryLifecycleConflict, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await commit_uow(db)
    await db.refresh(memory)
    return memory


@router.post("/{memory_id}/confirm", response_model=MemoryRead)
async def confirm_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    try:
        await ensure_sqlite_write_transaction(db)
        memory = await MemoryManager(db).confirm(settings.DEFAULT_OWNER_ID, memory_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Memory not found")
    except (MemoryLifecycleConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await commit_uow(db)
    await db.refresh(memory)
    return memory


@router.delete("/{memory_id}", response_model=MemoryRead)
async def delete_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    """Compatibility route: deletion is recoverable archival, never physical removal."""
    try:
        await ensure_sqlite_write_transaction(db)
        memory = await MemoryManager(db).archive(settings.DEFAULT_OWNER_ID, memory_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Memory not found")
    except MemoryLifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await commit_uow(db)
    await db.refresh(memory)
    return memory


@router.post("/{memory_id}/restore", response_model=MemoryRead)
async def restore_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    try:
        await ensure_sqlite_write_transaction(db)
        memory = await MemoryManager(db).restore(settings.DEFAULT_OWNER_ID, memory_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Memory not found")
    except (MemoryLifecycleConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await commit_uow(db)
    await db.refresh(memory)
    return memory


@router.post("/snapshots", response_model=ContextSnapshotRead, status_code=201)
async def create_snapshot(plan_id: int | None = None, db: AsyncSession = Depends(get_db)):
    snapshot = await ContextAssembler(db).build(settings.DEFAULT_OWNER_ID, plan_id=plan_id)
    await commit_uow(db)
    await db.refresh(snapshot)
    return snapshot
