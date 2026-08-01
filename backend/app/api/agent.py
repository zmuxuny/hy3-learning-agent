import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import AsyncSessionLocal, get_db
from app.models import AgentRun, ChatMessage, Operation, Plan, RunEvent, Session, SessionPlanLink
from app.runtime import AgentRuntime
from app.runtime.session_titles import initial_session_title
from app.runtime.scheduler import proactive_scheduler
from app.schemas import (
    AgentRunCreate,
    AgentRunRead,
    ChatMessageRead,
    RunEventRead,
    SessionHandoffCreate,
    SessionRead,
    SessionUpdate,
)
from app.services.sessions import build_handoff_summary, link_session_plan


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
        if session.archived_at is not None:
            raise HTTPException(status_code=409, detail="Archived sessions must be restored before continuing")
        if session.plan_id != data.plan_id:
            raise HTTPException(status_code=409, detail="Session focus does not match requested plan")
        session.updated_at = datetime.now(timezone.utc)
    elif data.trigger == "user_message":
        session = Session(
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=data.plan_id,
            title=initial_session_title(data.objective),
        )
        db.add(session)
        await db.flush()
        session_id = session.id
    if data.plan_id is not None:
        plan = await db.get(Plan, data.plan_id)
        if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan.status == "archived":
            raise HTTPException(status_code=409, detail="Archived plans must be restored before continuing")
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session_id,
        plan_id=data.plan_id,
        trigger=data.trigger,
        objective=data.objective,
        model=settings.MODEL_NAME,
    )
    db.add(run)
    await db.flush()
    if session_id and data.plan_id is not None:
        await link_session_plan(
            db,
            owner_id=settings.DEFAULT_OWNER_ID,
            session_id=session_id,
            plan_id=data.plan_id,
            relation_type="focused",
            source_run_id=run.id,
        )
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


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(limit: int = 30, db: AsyncSession = Depends(get_db), archived: bool = False):
    archive_filter = Session.archived_at.is_not(None) if archived else Session.archived_at.is_(None)
    sessions = list((await db.execute(
        select(Session)
        .where(Session.owner_id == settings.DEFAULT_OWNER_ID, archive_filter)
        .order_by(Session.updated_at.desc())
        .limit(min(max(limit, 1), 100))
    )).scalars())
    if not sessions:
        return []
    session_ids = [session.id for session in sessions]
    runs = list((await db.execute(
        select(AgentRun)
        .where(AgentRun.session_id.in_(session_ids))
        .order_by(AgentRun.created_at.desc())
    )).scalars())
    latest_runs: dict[str, AgentRun] = {}
    run_counts: dict[str, int] = {}
    for run in runs:
        run_counts[run.session_id] = run_counts.get(run.session_id, 0) + 1
        latest_runs.setdefault(run.session_id, run)

    links = list((await db.execute(
        select(SessionPlanLink).where(SessionPlanLink.session_id.in_(session_ids))
    )).scalars())
    linked_plans: dict[str, list[int]] = {}
    for link in links:
        ids = linked_plans.setdefault(link.session_id, [])
        if link.plan_id not in ids:
            ids.append(link.plan_id)

    rows = []
    for session in sessions:
        messages = list(session.messages)
        latest = latest_runs.get(session.id)
        rows.append({
            "id": session.id,
            "plan_id": session.plan_id,
            "parent_session_id": session.parent_session_id,
            "title": session.title,
            "summary": session.summary,
            "handoff_summary": session.handoff_summary,
            "archived_at": session.archived_at,
            "linked_plan_ids": linked_plans.get(session.id, []),
            "message_count": len(messages),
            "run_count": run_counts.get(session.id, 0),
            "last_message": messages[-1].content[:180] if messages else "",
            "last_run_id": latest.id if latest else None,
            "last_run_status": latest.status if latest else None,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        })
    rows.sort(key=lambda item: item["updated_at"], reverse=True)
    return rows


@router.patch("/sessions/{session_id}", response_model=SessionRead)
async def rename_session(session_id: str, data: SessionUpdate, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    if data.archived:
        active_run = (await db.execute(
            select(AgentRun.id).where(
                AgentRun.session_id == session.id,
                AgentRun.status.in_(["queued", "running"]),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise HTTPException(status_code=409, detail="Stop the active run before archiving this session")
    if data.title is not None:
        session.title = data.title
    if data.archived is not None:
        before = session.archived_at
        session.archived_at = datetime.now(timezone.utc) if data.archived else None
        db.add(Operation(
            owner_id=settings.DEFAULT_OWNER_ID,
            run_id=None,
            tool_name="session.archive" if data.archived else "session.restore",
            entity_type="session",
            entity_id=session.id,
            forward_patch={"changes": {"archived_at": session.archived_at.isoformat() if session.archived_at else None}},
            inverse_patch={"changes": {"archived_at": before.isoformat() if before else None}},
        ))
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    latest = (await db.execute(
        select(AgentRun).where(AgentRun.session_id == session.id).order_by(AgentRun.created_at.desc()).limit(1)
    )).scalars().one_or_none()
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "parent_session_id": session.parent_session_id,
        "title": session.title,
        "summary": session.summary,
        "handoff_summary": session.handoff_summary,
        "archived_at": session.archived_at,
        "linked_plan_ids": list((await db.execute(
            select(SessionPlanLink.plan_id).where(SessionPlanLink.session_id == session.id).distinct()
        )).scalars()),
        "message_count": len(session.messages),
        "run_count": len((await db.execute(select(AgentRun.id).where(AgentRun.session_id == session.id))).all()),
        "last_message": session.messages[-1].content[:180] if session.messages else "",
        "last_run_id": latest.id if latest else None,
        "last_run_status": latest.status if latest else None,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


@router.post("/sessions/{session_id}/handoff", response_model=SessionRead)
async def handoff_session(
    session_id: str,
    data: SessionHandoffCreate,
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(Session, session_id)
    if not source or source.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    if source.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the source session before creating a handoff")
    plan = await db.get(Plan, data.plan_id)
    if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.status == "archived":
        raise HTTPException(status_code=409, detail="Restore the plan before continuing in it")

    child = (await db.execute(
        select(Session).where(
            Session.owner_id == settings.DEFAULT_OWNER_ID,
            Session.parent_session_id == source.id,
            Session.plan_id == plan.id,
            Session.archived_at.is_(None),
        ).order_by(Session.updated_at.desc()).limit(1)
    )).scalars().one_or_none()
    if child is None:
        child = Session(
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=plan.id,
            parent_session_id=source.id,
            title=plan.title[:80],
            handoff_summary=await build_handoff_summary(db, source),
        )
        db.add(child)
        await db.flush()
    await link_session_plan(
        db,
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=source.id,
        plan_id=plan.id,
        relation_type="discussed",
    )
    await link_session_plan(
        db,
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=child.id,
        plan_id=plan.id,
        relation_type="focused",
    )
    await db.commit()
    await db.refresh(child)
    latest = (await db.execute(
        select(AgentRun).where(AgentRun.session_id == child.id).order_by(AgentRun.created_at.desc()).limit(1)
    )).scalars().one_or_none()
    return {
        "id": child.id,
        "plan_id": child.plan_id,
        "parent_session_id": child.parent_session_id,
        "title": child.title,
        "summary": child.summary,
        "handoff_summary": child.handoff_summary,
        "archived_at": child.archived_at,
        "linked_plan_ids": [plan.id],
        "message_count": len(child.messages),
        "run_count": len((await db.execute(select(AgentRun.id).where(AgentRun.session_id == child.id))).all()),
        "last_message": child.messages[-1].content[:180] if child.messages else "",
        "last_run_id": latest.id if latest else None,
        "last_run_status": latest.status if latest else None,
        "created_at": child.created_at,
        "updated_at": child.updated_at,
    }


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
