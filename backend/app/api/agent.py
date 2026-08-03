import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import AsyncSessionLocal, get_db
from app.models import (
    AgentRun,
    ChatMessage,
    ChatMessageRevision,
    Memory,
    Operation,
    Plan,
    PlanProposal,
    PlanningIntake,
    RunEvent,
    Session,
    SessionPlanLink,
)
from app.runtime import AgentRuntime
from app.runtime.session_titles import initial_session_title
from app.runtime.scheduler import proactive_scheduler
from app.schemas import (
    AgentRunCreate,
    AgentRunRead,
    ChatMessageRead,
    MessageEdit,
    PlanCreate,
    PlanningAnswersSubmit,
    PlanProposalDecision,
    PlanProposalRead,
    PlanningStateRead,
    RunEventRead,
    RunApprovalRequest,
    SessionHandoffCreate,
    SessionRead,
    SessionUpdate,
)
from app.services.sessions import build_handoff_summary, link_session_plan
from app.services import plans as plan_service


router = APIRouter()
runtime = AgentRuntime()
active_tasks: set[asyncio.Task] = set()


def _visible_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [message for message in messages if not message.message_metadata.get("superseded_by_edit")]


def _start_runtime(run_id: str, **kwargs) -> None:
    task = asyncio.create_task(runtime.run(run_id, **kwargs))
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
        active_run = (await db.execute(
            select(AgentRun.id).where(
                AgentRun.session_id == session.id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(["queued", "running", "waiting_approval"]),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise HTTPException(status_code=409, detail="This Session already has an active run")
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
        .where(AgentRun.owner_id == settings.DEFAULT_OWNER_ID, AgentRun.parent_run_id.is_(None))
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
        .where(AgentRun.session_id.in_(session_ids), AgentRun.parent_run_id.is_(None))
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
        messages = _visible_messages(list(session.messages))
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
    messages = _visible_messages(list(session.messages))
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
        "message_count": len(messages),
        "run_count": len((await db.execute(select(AgentRun.id).where(
            AgentRun.session_id == session.id, AgentRun.parent_run_id.is_(None)
        ))).all()),
        "last_message": messages[-1].content[:180] if messages else "",
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
        "run_count": len((await db.execute(select(AgentRun.id).where(
            AgentRun.session_id == child.id, AgentRun.parent_run_id.is_(None)
        ))).all()),
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
    return _visible_messages(list(result.scalars()))


@router.get("/sessions/{session_id}/planning", response_model=PlanningStateRead)
async def read_planning_state(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    intake = await db.get(PlanningIntake, session_id)
    proposal = (await db.execute(
        select(PlanProposal).where(
            PlanProposal.owner_id == settings.DEFAULT_OWNER_ID,
            PlanProposal.session_id == session_id,
        ).order_by(PlanProposal.created_at.desc()).limit(1)
    )).scalars().one_or_none()
    return {"intake": intake, "proposal": proposal}


@router.post("/sessions/{session_id}/planning/answers", response_model=AgentRunRead, status_code=202)
async def submit_planning_answers(
    session_id: str,
    data: PlanningAnswersSubmit,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the Session before answering planning questions")
    active_run = (await db.execute(
        select(AgentRun.id).where(
            AgentRun.session_id == session.id,
            AgentRun.parent_run_id.is_(None),
            AgentRun.status.in_(["queued", "running", "waiting_approval"]),
        ).limit(1)
    )).scalar_one_or_none()
    if active_run:
        raise HTTPException(status_code=409, detail="This Session already has an active run")
    intake = await db.get(PlanningIntake, session.id)
    if not intake or not intake.open_questions:
        raise HTTPException(status_code=409, detail="There are no open planning questions")

    questions = {str(item.get("id")): item for item in intake.open_questions}
    supplied = {item.question_id: item.answer for item in data.answers}
    if len(supplied) != len(data.answers):
        raise HTTPException(status_code=422, detail="Each planning question can only be answered once")
    unknown = sorted(set(supplied) - set(questions))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown planning question IDs: {', '.join(unknown)}")
    missing = [question_id for question_id in questions if question_id not in supplied]
    if missing:
        raise HTTPException(status_code=422, detail="Answer every visible planning question before submitting")

    answer_lines = [
        f"- {questions[question_id].get('prompt', question_id)}\n  {supplied[question_id]}"
        for question_id in questions
    ]
    objective = (
        "用户已通过计划澄清卡提交以下答案：\n"
        + "\n".join(answer_lines)
        + "\n请更新 planning_intake；如果仍不充分，提出下一组最高信息量问题；"
          "如果已经充分，进行必要的规划子 Agent 分工并生成可审阅提案。"
    )
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session.id,
        plan_id=session.plan_id,
        trigger="user_message",
        objective=objective,
        model=settings.MODEL_NAME,
    )
    db.add(run)
    await db.flush()
    db.add(ChatMessage(
        session_id=session.id,
        run_id=run.id,
        role="user",
        content=objective,
        message_metadata={
            "ui_kind": "planning_answers",
            "answer_count": len(data.answers),
            "answers": [item.model_dump(mode="json") for item in data.answers],
        },
    ))
    intake.open_questions = []
    intake.readiness = "collecting"
    intake.readiness_confidence = min(intake.readiness_confidence, 0.95)
    intake.rationale = "回答已提交，Agent 正在重新判断需求是否充分。"
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(run)
    _start_runtime(run.id)
    return run


@router.post("/plan-proposals/{proposal_id}/decision", response_model=PlanProposalRead)
async def decide_plan_proposal(
    proposal_id: str,
    data: PlanProposalDecision,
    db: AsyncSession = Depends(get_db),
):
    proposal = await db.get(PlanProposal, proposal_id)
    if not proposal or proposal.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Plan proposal not found")
    if proposal.status == "accepted":
        if not data.accepted:
            raise HTTPException(status_code=409, detail="An accepted proposal cannot be rejected")
        return proposal
    if proposal.status == "rejected":
        raise HTTPException(status_code=409, detail="This proposal was already rejected; ask the Agent for a revision")
    if not data.accepted:
        proposal.status = "rejected"
        proposal.decided_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(proposal)
        return proposal

    try:
        plan_data = PlanCreate.model_validate(proposal.plan_payload)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Proposal payload is invalid: {exc}") from exc
    plan = await plan_service.create_plan(
        db,
        settings.DEFAULT_OWNER_ID,
        plan_data,
        proposal.source_run_id,
        commit=False,
    )
    operation = Operation(
        owner_id=settings.DEFAULT_OWNER_ID,
        run_id=proposal.source_run_id,
        tool_name="plan.proposal.accept",
        entity_type="plan",
        entity_id=str(plan.id),
        forward_patch={"created": plan.id, "proposal_id": proposal.id},
        inverse_patch={"delete": plan.id},
    )
    db.add(operation)
    await link_session_plan(
        db,
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=proposal.session_id,
        plan_id=plan.id,
        relation_type="created",
        source_run_id=proposal.source_run_id,
    )
    proposal.plan_id = plan.id
    proposal.status = "accepted"
    proposal.decided_at = datetime.now(timezone.utc)
    await db.commit()
    if proposal.source_run_id:
        source_run = await db.get(AgentRun, proposal.source_run_id)
        if source_run is not None:
            source_run.created_plan_id = plan.id
            await db.commit()
    await db.refresh(proposal)
    return proposal


@router.post("/messages/{message_id}/edit", response_model=AgentRunRead, status_code=202)
async def edit_user_message(message_id: int, data: MessageEdit, db: AsyncSession = Depends(get_db)):
    message = await db.get(ChatMessage, message_id)
    if not message or message.role != "user":
        raise HTTPException(status_code=404, detail="Editable user message not found")
    session = await db.get(Session, message.session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the Session before editing a message")
    active_run = (await db.execute(
        select(AgentRun.id).where(
            AgentRun.session_id == session.id,
            AgentRun.status.in_(["queued", "running", "waiting_approval"]),
        ).limit(1)
    )).scalar_one_or_none()
    if active_run:
        raise HTTPException(status_code=409, detail="Stop the active run before editing an earlier message")

    db.add(ChatMessageRevision(
        message_id=message.id,
        session_id=session.id,
        previous_run_id=message.run_id,
        content=message.content,
        message_metadata=dict(message.message_metadata),
    ))
    downstream = list((await db.execute(
        select(ChatMessage).where(
            ChatMessage.session_id == session.id,
            ChatMessage.id > message.id,
        ).order_by(ChatMessage.id)
    )).scalars())
    edit_token = f"message:{message.id}:{datetime.now(timezone.utc).isoformat()}"
    for stale in downstream:
        stale.message_metadata = {**stale.message_metadata, "superseded_by_edit": edit_token}
    downstream_run_ids = {stale.run_id for stale in downstream if stale.run_id}
    if downstream_run_ids:
        derived_memories = list((await db.execute(
            select(Memory).where(
                Memory.owner_id == settings.DEFAULT_OWNER_ID,
                Memory.scope == "session",
                Memory.scope_id == session.id,
                Memory.source_id.in_(downstream_run_ids),
                Memory.status.in_(["proposed", "confirmed"]),
            )
        )).scalars())
        for memory in derived_memories:
            memory.status = "archived"
    previous_run_id = message.run_id
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session.id,
        plan_id=session.plan_id,
        trigger="user_message",
        objective=data.content,
        model=settings.MODEL_NAME,
    )
    db.add(run)
    await db.flush()
    message.content = data.content
    message.run_id = run.id
    message.message_metadata = {
        **{key: value for key, value in message.message_metadata.items() if key != "included_in_summary"},
        "edited_at": datetime.now(timezone.utc).isoformat(),
        "revises_run_id": previous_run_id,
    }
    session.summary = ""
    session.updated_at = datetime.now(timezone.utc)
    db.add(Operation(
        owner_id=settings.DEFAULT_OWNER_ID,
        run_id=run.id,
        tool_name="message.edit",
        entity_type="chat_message",
        entity_id=str(message.id),
        forward_patch={"content": data.content, "superseded_message_ids": [item.id for item in downstream]},
        inverse_patch={"revision_preserved": True, "previous_run_id": previous_run_id},
        status="recorded",
    ))
    await db.commit()
    await db.refresh(run)
    if data.rerun:
        _start_runtime(run.id)
    return run


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


@router.post("/runs/{run_id}/approval", response_model=AgentRunRead)
async def decide_run_approval(
    run_id: str,
    data: RunApprovalRequest,
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "waiting_approval" or not run.pending_approval:
        raise HTTPException(status_code=409, detail="This run has no pending approval request")
    if data.note:
        pending = dict(run.pending_approval)
        pending["note"] = data.note
        run.pending_approval = pending
    run.status = "queued"
    await db.commit()
    await db.refresh(run)
    _start_runtime(
        run.id,
        resume=True,
        approval_decision="approve" if data.approved else "reject",
    )
    return run


@router.post("/heartbeat", response_model=AgentRunRead, status_code=202)
async def trigger_heartbeat(db: AsyncSession = Depends(get_db)):
    try:
        return await proactive_scheduler.trigger_now("manual_heartbeat")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
