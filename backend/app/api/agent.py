import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import AsyncSessionLocal, get_db
from app.models import AgentRun, ChatMessage, RunEvent, Session
from app.runtime import AgentRuntime
from app.runtime.scheduler import proactive_scheduler
from app.schemas import AgentRunCreate, AgentRunRead, ChatMessageRead, RunEventRead


router = APIRouter()
runtime = AgentRuntime()
active_tasks: set[asyncio.Task] = set()


def _start_runtime(run_id: str) -> None:
    task = asyncio.create_task(runtime.run(run_id))
    active_tasks.add(task)
    task.add_done_callback(active_tasks.discard)


@router.post("/runs", response_model=AgentRunRead, status_code=202)
async def create_run(data: AgentRunCreate, db: AsyncSession = Depends(get_db)):
    session_id = data.session_id
    if data.trigger == "user_message" and session_id:
        session = await db.get(Session, session_id)
        if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.plan_id != data.plan_id:
            raise HTTPException(status_code=409, detail="Session focus does not match requested plan")
    elif data.trigger == "user_message":
        session = Session(
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=data.plan_id,
            title=data.objective[:80] or "New conversation",
        )
        db.add(session)
        await db.flush()
        session_id = session.id
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session_id,
        plan_id=data.plan_id,
        trigger=data.trigger,
        objective=data.objective,
        model=settings.MODEL_NAME,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    _start_runtime(run.id)
    return run


@router.get("/runs", response_model=list[AgentRunRead])
async def list_runs(limit: int = 30, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AgentRun)
        .where(AgentRun.owner_id == settings.DEFAULT_OWNER_ID)
        .order_by(AgentRun.created_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    return list(result.scalars())


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
async def read_session_messages(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    return list(result.scalars())


@router.get("/runs/{run_id}", response_model=AgentRunRead)
async def read_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
async def read_run_events(run_id: str, after: int = 0, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
        .order_by(RunEvent.sequence)
    )
    return list(result.scalars())


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(run_id: str):
    async def event_stream():
        last_sequence = 0
        while True:
            async with AsyncSessionLocal() as db:
                run = await db.get(AgentRun, run_id)
                if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
                    yield "event: error\ndata: {\"error\": \"Run not found\"}\n\n"
                    return
                result = await db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.sequence > last_sequence)
                    .order_by(RunEvent.sequence)
                )
                events = list(result.scalars())
                for event in events:
                    last_sequence = event.sequence
                    payload = {
                        "sequence": event.sequence,
                        "type": event.event_type,
                        "summary": event.summary,
                        "payload": event.payload,
                        "created_at": event.created_at.isoformat(),
                    }
                    yield f"event: {event.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if run.status in {"completed", "failed", "cancelled"} and not events:
                    return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/cancel", response_model=AgentRunRead)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    run.cancel_requested = True
    await db.commit()
    await db.refresh(run)
    return run


@router.post("/heartbeat", response_model=AgentRunRead, status_code=202)
async def trigger_heartbeat(db: AsyncSession = Depends(get_db)):
    try:
        return await proactive_scheduler.trigger_now("manual_heartbeat")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
