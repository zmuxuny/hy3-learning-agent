import asyncio
import json

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.context.provenance import canonical_digest
from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.database import AsyncSessionLocal, get_db
from app.db.uow import (
    commit as commit_uow,
    ensure_sqlite_write_transaction,
    flush as flush_uow,
    rollback as rollback_uow,
)
from app.models import (
    AgentRun,
    ChatMessage,
    ChatMessageRevision,
    ContextSnapshot,
    Notification,
    Operation,
    Plan,
    PlanProposal,
    PlanningIntake,
    QueuedMessage,
    RunEvent,
    Session,
    SessionPlanLink,
    SessionSummary,
    UserProfile,
)
from app.runtime import AgentRuntime
from app.runtime.events import emit_event, subscribe_stream, unsubscribe_stream
from app.runtime.interventions import InterventionStateError, accept_intervention_reply
from app.runtime.session_titles import initial_session_title
from app.runtime.state import (
    NONTERMINAL_RUN_STATUSES,
    RunStateError,
    decide_approval,
    ensure_root_scope_available,
    record_steer,
    terminate_run,
)
from app.runtime.scheduler import proactive_scheduler
from app.runtime.tasks import cancel_tracked_task, start_tracked_task, wake_tracked_task
from app.schemas import (
    AgentRunCreate,
    AgentRunRead,
    ChatMessageRead,
    ContextSnapshotRead,
    QueuedMessageCreate,
    QueuedMessageMutation,
    QueuedMessageRead,
    QueuedMessageUpdate,
    MessageEdit,
    PlanCreate,
    PlanningAnswersSubmit,
    PlanProposalDecision,
    PlanProposalRead,
    PlanningStateRead,
    RunEventRead,
    RunApprovalRequest,
    RunSteerCreate,
    SessionHandoffCreate,
    SessionRead,
    SessionSummaryRead,
    SessionUpdate,
)
from app.services.sessions import (
    SessionHandoffConflict,
    build_handoff_summary,
    ensure_session_handoff,
    invalidate_message_edit_derivations,
    link_session_plan,
)
from app.services import plans as plan_service
from app.services.queue import (
    QueueStateError,
    compact_queue_after_removal,
    dispatch_queued_message,
    reorder_queue,
    validate_intervention_reply_scope,
)


router = APIRouter()
runtime = AgentRuntime()


def _visible_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [
        message
        for message in messages
        if message.validity_state == "active"
        and not message.message_metadata.get("superseded_by_edit")
    ]


def _start_runtime(run_id: str, **kwargs) -> None:
    start_tracked_task(run_id, runtime.run(run_id, **kwargs))


def _wake_runtime(run_id: str, *, wake_key: str, **kwargs) -> None:
    wake_tracked_task(
        run_id,
        runtime.run(run_id, **kwargs),
        wake_key=wake_key,
    )


async def _ensure_intervention_reply_not_inflight(
    db: AsyncSession,
    intervention_id: str | None,
) -> None:
    if intervention_id is None:
        return
    duplicate_reply = await db.scalar(
        select(QueuedMessage.id).where(
            QueuedMessage.owner_id == settings.DEFAULT_OWNER_ID,
            QueuedMessage.reply_to_intervention_id == intervention_id,
        ).limit(1)
    )
    active_reply_run = await db.scalar(
        select(AgentRun.id).where(
            AgentRun.owner_id == settings.DEFAULT_OWNER_ID,
            AgentRun.reply_to_intervention_id == intervention_id,
            AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
        ).limit(1)
    )
    if duplicate_reply is not None or active_reply_run is not None:
        raise InterventionStateError("Intervention reply is already queued or running")


@router.post("/runs", response_model=AgentRunRead, status_code=202)
async def create_run(data: AgentRunCreate, db: AsyncSession = Depends(get_db)):
    await ensure_sqlite_write_transaction(db)
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
                AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise HTTPException(status_code=409, detail="This Session already has an active run")
        if session.plan_id != data.plan_id:
            raise HTTPException(status_code=409, detail="Session focus does not match requested plan")
        session.updated_at = utc_now()
    elif data.trigger == "user_message":
        session = Session(
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=data.plan_id,
            title=initial_session_title(data.objective),
        )
        db.add(session)
        await flush_uow(db)
        session_id = session.id
    if data.plan_id is not None:
        plan = await db.get(Plan, data.plan_id)
        if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan.status == "archived":
            raise HTTPException(status_code=409, detail="Archived plans must be restored before continuing")
    try:
        await ensure_root_scope_available(
            db,
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=data.plan_id,
            session_id=session_id,
        )
    except RunStateError as exc:
        await rollback_uow(db)
        raise HTTPException(
            status_code=409,
            detail="This plan or Session already has an active run",
        ) from exc
    message_metadata = {}
    reply_to_intervention_id = data.reply_to_intervention_id
    if data.reply_to_notification_id is not None:
        if data.trigger != "user_message" or session_id is None:
            raise HTTPException(status_code=422, detail="Notification replies require a user Session")
        notification = await db.get(Notification, data.reply_to_notification_id)
        if (
            not notification
            or notification.owner_id != settings.DEFAULT_OWNER_ID
            or notification.session_id != session_id
        ):
            raise HTTPException(status_code=409, detail="Reply target does not belong to this Session")
        message_metadata["reply_to_notification_id"] = notification.id
        if notification.intervention_id is not None:
            if (
                reply_to_intervention_id is not None
                and reply_to_intervention_id != notification.intervention_id
            ):
                raise HTTPException(status_code=409, detail="Reply targets identify different Interventions")
            reply_to_intervention_id = notification.intervention_id
    try:
        reply_target = await validate_intervention_reply_scope(
            db,
            intervention_id=reply_to_intervention_id,
            owner_id=settings.DEFAULT_OWNER_ID,
            session_id=session_id,
            plan_id=data.plan_id,
        )
        await _ensure_intervention_reply_not_inflight(
            db,
            reply_target.id if reply_target is not None else None,
        )
        await accept_intervention_reply(db, reply_target)
    except (ValueError, InterventionStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session_id,
        plan_id=data.plan_id,
        trigger=data.trigger,
        objective=data.objective,
        model=settings.MODEL_NAME,
        execution_mode=data.execution_mode,
        reply_to_intervention_id=reply_to_intervention_id,
    )
    db.add(run)
    await flush_uow(db)
    if session_id and data.trigger == "user_message":
        db.add(ChatMessage(
            session_id=session_id,
            run_id=run.id,
            message_key=f"run:{run.id}:input",
            role="user",
            content=data.objective,
            version=1,
            content_hash=canonical_digest(data.objective),
            reply_to_intervention_id=reply_to_intervention_id,
            message_metadata=message_metadata,
        ))
    if session_id and data.plan_id is not None:
        await link_session_plan(
            db,
            owner_id=settings.DEFAULT_OWNER_ID,
            session_id=session_id,
            plan_id=data.plan_id,
            relation_type="focused",
            source_run_id=run.id,
        )
    await commit_uow(db)
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
    rows.sort(key=lambda item: coerce_legacy_utc(item["updated_at"]), reverse=True)
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
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise HTTPException(status_code=409, detail="Stop the active run before archiving this session")
        queued_message = (await db.execute(
            select(QueuedMessage.id).where(
                QueuedMessage.owner_id == settings.DEFAULT_OWNER_ID,
                QueuedMessage.session_id == session.id,
            ).limit(1)
        )).scalar_one_or_none()
        if queued_message:
            raise HTTPException(status_code=409, detail="Send or delete queued messages before archiving this session")
    if data.title is not None:
        session.title = data.title
    if data.archived is not None:
        before = session.archived_at
        session.archived_at = utc_now() if data.archived else None
        db.add(Operation(
            owner_id=settings.DEFAULT_OWNER_ID,
            run_id=None,
            tool_name="session.archive" if data.archived else "session.restore",
            entity_type="session",
            entity_id=session.id,
            forward_patch={"changes": {"archived_at": canonical_utc(session.archived_at)}},
            inverse_patch={"changes": {"archived_at": canonical_utc(before)}},
        ))
    session.updated_at = utc_now()
    await commit_uow(db)
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
    await ensure_sqlite_write_transaction(db)
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
    if source.plan_id is not None and source.plan_id != plan.id:
        raise HTTPException(
            status_code=409,
            detail="The source Session is focused on a different plan",
        )

    children = list((await db.execute(
        select(Session).where(
            Session.owner_id == settings.DEFAULT_OWNER_ID,
            Session.parent_session_id == source.id,
            Session.plan_id == plan.id,
            Session.archived_at.is_(None),
        ).order_by(Session.created_at, Session.id)
    )).scalars())
    if len(children) > 1:
        raise HTTPException(status_code=409, detail="Multiple active handoff Sessions already exist")
    child = children[0] if children else None
    if child is None:
        frozen_handoff = await build_handoff_summary(db, source)
        child = Session(
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=plan.id,
            parent_session_id=source.id,
            title=plan.title[:80],
            handoff_summary=frozen_handoff,
        )
        db.add(child)
        await flush_uow(db)
    else:
        frozen_handoff = child.handoff_summary
        if not frozen_handoff:
            frozen_handoff = await build_handoff_summary(db, source)
            child.handoff_summary = frozen_handoff
            await flush_uow(db)
    try:
        await ensure_session_handoff(
            db,
            source=source,
            target=child,
            plan=plan,
            content=frozen_handoff,
        )
    except SessionHandoffConflict as exc:
        await rollback_uow(db)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    await commit_uow(db)
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


@router.get("/sessions/{session_id}/summaries", response_model=list[SessionSummaryRead])
async def read_session_summaries(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    result = await db.execute(
        select(SessionSummary)
        .where(SessionSummary.session_id == session_id)
        .order_by(SessionSummary.version.desc())
    )
    return list(result.scalars())


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
            AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
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
    try:
        await ensure_root_scope_available(
            db,
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=session.plan_id,
            session_id=session.id,
        )
    except RunStateError as exc:
        await rollback_uow(db)
        raise HTTPException(
            status_code=409,
            detail="This plan or Session already has an active run",
        ) from exc
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session.id,
        plan_id=session.plan_id,
        trigger="user_message",
        objective=objective,
        model=settings.MODEL_NAME,
    )
    db.add(run)
    await flush_uow(db)
    db.add(ChatMessage(
        session_id=session.id,
        run_id=run.id,
        message_key=f"run:{run.id}:input",
        role="user",
        content=objective,
        version=1,
        content_hash=canonical_digest(objective),
        message_metadata={
            "ui_kind": "planning_answers",
            "answer_count": len(data.answers),
            "answers": [
                {**item.model_dump(mode="json"), "prompt": questions[item.question_id].get("prompt", item.question_id)}
                for item in data.answers
            ],
        },
    ))
    intake.open_questions = []
    intake.readiness = "collecting"
    intake.readiness_confidence = min(intake.readiness_confidence, 0.95)
    intake.rationale = "回答已提交，Agent 正在重新判断需求是否充分。"
    session.updated_at = utc_now()
    await commit_uow(db)
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
    proposal_session = await db.get(Session, proposal.session_id)
    if not proposal_session or proposal_session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=409, detail="The proposal Session is no longer available")
    if proposal_session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the proposal Session before deciding this proposal")
    if not data.accepted:
        proposal.status = "rejected"
        proposal.decided_at = utc_now()
        await commit_uow(db)
        await db.refresh(proposal)
        return proposal

    try:
        plan_data = PlanCreate.model_validate(proposal.plan_payload)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Proposal payload is invalid: {exc}") from exc
    completeness_issues = plan_service.plan_completeness_issues(plan_data)
    if completeness_issues:
        raise HTTPException(
            status_code=409,
            detail="The proposal is incomplete: " + "; ".join(completeness_issues),
        )
    plan = await plan_service.create_plan(
        db,
        settings.DEFAULT_OWNER_ID,
        plan_data,
        proposal.source_run_id,
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
    proposal.decided_at = utc_now()
    if proposal.source_run_id:
        source_run = await db.get(AgentRun, proposal.source_run_id)
        if source_run is not None:
            source_run.created_plan_id = plan.id
    await commit_uow(db)
    await db.refresh(proposal)
    return proposal


@router.post("/messages/{message_id}/edit", response_model=AgentRunRead, status_code=202)
async def edit_user_message(message_id: int, data: MessageEdit, db: AsyncSession = Depends(get_db)):
    await ensure_sqlite_write_transaction(db)
    message = await db.get(ChatMessage, message_id)
    if not message or message.role != "user":
        raise HTTPException(status_code=404, detail="Editable user message not found")
    session = await db.get(Session, message.session_id)
    if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the Session before editing a message")
    requested_hash = canonical_digest(data.content)
    replay_run_id = (message.message_metadata or {}).get("edit_run_id")
    if (
        replay_run_id
        and message.content == data.content
        and message.content_hash == requested_hash
    ):
        replay_run = await db.get(AgentRun, str(replay_run_id))
        if (
            replay_run is not None
            and replay_run.owner_id == settings.DEFAULT_OWNER_ID
            and replay_run.session_id == session.id
            and replay_run.objective == data.content
        ):
            await commit_uow(db)
            await db.refresh(replay_run)
            if data.rerun and replay_run.status in NONTERMINAL_RUN_STATUSES:
                _start_runtime(replay_run.id)
            return replay_run
    if message.content == data.content:
        await rollback_uow(db)
        raise HTTPException(status_code=409, detail="Edited content is unchanged")
    active_run = (await db.execute(
        select(AgentRun.id).where(
            AgentRun.session_id == session.id,
            AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
        ).limit(1)
    )).scalar_one_or_none()
    if active_run:
        raise HTTPException(status_code=409, detail="Stop the active run before editing an earlier message")

    try:
        await ensure_root_scope_available(
            db,
            owner_id=settings.DEFAULT_OWNER_ID,
            plan_id=session.plan_id,
            session_id=session.id,
        )
    except RunStateError as exc:
        await rollback_uow(db)
        raise HTTPException(
            status_code=409,
            detail="This plan or Session already has an active run",
        ) from exc

    if not message.content_hash:
        message.content_hash = canonical_digest(message.content)
        await flush_uow(db)
    previous_version = message.version
    previous_content_hash = message.content_hash
    db.add(ChatMessageRevision(
        message_id=message.id,
        session_id=session.id,
        previous_run_id=message.run_id,
        version=previous_version,
        content=message.content,
        content_hash=previous_content_hash,
        message_metadata=dict(message.message_metadata),
    ))
    # Revision INSERT validates against the still-current immutable source.
    # Only after that row is durable in this UoW may the live message advance.
    await flush_uow(db)
    downstream = list((await db.execute(
        select(ChatMessage).where(
            ChatMessage.session_id == session.id,
            or_(
                ChatMessage.created_at > message.created_at,
                and_(
                    ChatMessage.created_at == message.created_at,
                    ChatMessage.id > message.id,
                ),
            ),
        ).order_by(ChatMessage.created_at, ChatMessage.id)
    )).scalars())
    edited_at = utc_now()
    edited_at_text = canonical_utc(edited_at)
    edit_token = f"message:{message.id}:{edited_at_text}"
    previous_run_id = message.run_id
    context_generation = await invalidate_message_edit_derivations(
        db,
        owner_id=settings.DEFAULT_OWNER_ID,
        session=session,
        message=message,
        previous_version=previous_version,
        previous_content_hash=previous_content_hash,
        previous_run_id=previous_run_id,
        downstream_messages=downstream,
        changed_at=edited_at,
    )
    for stale in downstream:
        if stale.validity_state == "active":
            stale.validity_state = "superseded"
            stale.invalidated_at = edited_at
            stale.invalidation_reason = "source_message_edited"
            stale.message_metadata = {**stale.message_metadata, "superseded_by_edit": edit_token}
    run = AgentRun(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=session.id,
        plan_id=session.plan_id,
        trigger="user_message",
        objective=data.content,
        model=settings.MODEL_NAME,
        execution_mode="normal",
        reply_to_intervention_id=message.reply_to_intervention_id,
    )
    db.add(run)
    await flush_uow(db)
    message.content = data.content
    message.version = previous_version + 1
    message.content_hash = requested_hash
    message.run_id = run.id
    message.message_key = f"run:{run.id}:input"
    message.message_metadata = {
        **{key: value for key, value in message.message_metadata.items() if key != "included_in_summary"},
        "edited_at": edited_at_text,
        "revises_run_id": previous_run_id,
        "edit_run_id": run.id,
        "edit_source_version": previous_version,
        "context_generation": context_generation,
    }
    session.summary = ""
    await flush_uow(db)
    visible_after_edit = list((await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session.id)
    )).scalars())
    visible_after_edit = [
        item for item in visible_after_edit
        if not (item.message_metadata or {}).get("superseded_by_edit")
    ]
    for visible in visible_after_edit:
        if (visible.message_metadata or {}).get("included_in_summary"):
            visible.message_metadata = {
                key: value
                for key, value in visible.message_metadata.items()
                if key != "included_in_summary"
            }
    session.updated_at = edited_at
    db.add(Operation(
        owner_id=settings.DEFAULT_OWNER_ID,
        run_id=run.id,
        tool_name="message.edit",
        entity_type="chat_message",
        entity_id=str(message.id),
        forward_patch={
            "content": data.content,
            "from_version": previous_version,
            "to_version": previous_version + 1,
            "context_generation": context_generation,
            "superseded_message_ids": [item.id for item in downstream],
        },
        inverse_patch={"revision_preserved": True, "previous_run_id": previous_run_id},
        status="recorded",
    ))
    await commit_uow(db)
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
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    result = await db.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
        .order_by(RunEvent.sequence)
    )
    return list(result.scalars())


@router.get("/runs/{run_id}/context", response_model=ContextSnapshotRead)
async def read_run_context(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    snapshot = (await db.execute(
        select(ContextSnapshot)
        .where(
            ContextSnapshot.owner_id == settings.DEFAULT_OWNER_ID,
            ContextSnapshot.run_id == run_id,
        )
        .order_by(ContextSnapshot.created_at.desc(), ContextSnapshot.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="This run has no context snapshot")
    return snapshot


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(run_id: str):
    async def event_stream():
        last_sequence = 0
        seen_sequences: set[int] = set()
        event_queue = await subscribe_stream(run_id)
        terminal_grace = 0
        try:
            while True:
                try:
                    # Starlette's disconnect scope repeatedly cancels awaits.
                    # Finish this short DB poll, including connection return,
                    # before yielding to a client that can stop reading.
                    with CancelScope(shield=True):
                        async with AsyncSessionLocal() as db:
                            run = await db.get(AgentRun, run_id)
                            run_status = run.status if run and run.owner_id == settings.DEFAULT_OWNER_ID else None
                            events = []
                            if run_status is not None:
                                result = await db.execute(
                                    select(RunEvent)
                                    .where(RunEvent.run_id == run_id, RunEvent.sequence > last_sequence)
                                    .order_by(RunEvent.sequence)
                                )
                                events = [
                                    {
                                        "sequence": event.sequence,
                                        "type": event.event_type,
                                        "summary": event.summary,
                                        "payload": event.payload,
                                        "created_at": canonical_utc(event.created_at),
                                    }
                                    for event in result.scalars()
                                ]
                    if run_status is None:
                        yield "event: error\ndata: {\"error\": \"Run not found\"}\n\n"
                        return
                    fresh_events = [event for event in events if event["sequence"] not in seen_sequences]
                    for event in events:
                        seen_sequences.add(event["sequence"])
                        last_sequence = event["sequence"]
                    for payload in fresh_events:
                        yield f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if run_status in {"completed", "failed", "cancelled"}:
                        if not fresh_events:
                            terminal_grace += 1
                            if terminal_grace >= 4:
                                return
                    else:
                        terminal_grace = 0
                except asyncio.CancelledError:
                    return
                except Exception:
                    # A transient database lock must never kill the live event stream.
                    await asyncio.sleep(0.5)
                    continue
                try:
                    item = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if "sequence" in item and int(item.get("sequence") or 0) <= last_sequence:
                    continue
                if "sequence" in item:
                    seen_sequences.add(int(item["sequence"]))
                yield f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            unsubscribe_stream(run_id, event_queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/cancel", response_model=AgentRunRead)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in {"completed", "failed", "cancelled"}:
        return run
    await commit_uow(db)
    cancel_tracked_task(run.id)
    terminal = await terminate_run(
        AsyncSessionLocal,
        run.id,
        status="cancelled",
        reason_code="user_cancelled",
        summary="Agent run cancelled by user",
    )
    if terminal is None:  # pragma: no cover - loaded immediately above
        raise HTTPException(status_code=404, detail="Run not found")
    if run.parent_run_id is None:
        from app.runtime.subagents import cancel_children_for_parent

        await cancel_children_for_parent(run.id, "父 Run 被用户取消")
        successor_id = getattr(terminal, "_queued_successor_id", None)
        if successor_id:
            _start_runtime(successor_id)
    return terminal


@router.post("/runs/{run_id}/approval", response_model=AgentRunRead)
async def decide_run_approval(
    run_id: str,
    data: RunApprovalRequest,
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(AgentRun, run_id)
    if not run or run.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in {"waiting_approval", "queued", "running"} or not run.pending_approval:
        raise HTTPException(status_code=409, detail="This run has no pending approval request")
    if run.session_id:
        session = await db.get(Session, run.session_id)
        if not session or session.archived_at is not None:
            raise HTTPException(status_code=409, detail="Restore the Session before resolving this approval")
    if run.plan_id is not None:
        plan = await db.get(Plan, run.plan_id)
        if not plan or plan.status == "archived":
            raise HTTPException(status_code=409, detail="Restore the plan before resolving this approval")
    try:
        run = await decide_approval(
            db,
            run.id,
            owner_id=settings.DEFAULT_OWNER_ID,
            approved=data.approved,
            note=data.note,
            answer=data.answer,
        )
    except RunStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    approval_id = str((run.pending_approval or {}).get("approval_id") or "")
    _wake_runtime(
        run.id,
        wake_key=f"approval:{approval_id}",
        resume=True,
    )
    return run


@router.post("/runs/{run_id}/steer", response_model=AgentRunRead)
async def steer_run(run_id: str, data: RunSteerCreate, db: AsyncSession = Depends(get_db)):
    try:
        run, _ = await record_steer(
            db,
            run_id,
            owner_id=settings.DEFAULT_OWNER_ID,
            content=data.content,
        )
    except RunStateError as exc:
        if str(exc) == "run_not_found":
            raise HTTPException(status_code=404, detail="Run not found") from exc
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.get("/queue", response_model=list[QueuedMessageRead])
async def list_queue(
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(QueuedMessage).where(QueuedMessage.owner_id == settings.DEFAULT_OWNER_ID)
    if session_id:
        query = query.where(QueuedMessage.session_id == session_id)
    result = await db.execute(query.order_by(QueuedMessage.position, QueuedMessage.created_at))
    return list(result.scalars())


@router.post("/queue", response_model=QueuedMessageRead, status_code=201)
async def enqueue_message(data: QueuedMessageCreate, db: AsyncSession = Depends(get_db)):
    await ensure_sqlite_write_transaction(db)
    if data.session_id:
        session = await db.get(Session, data.session_id)
        if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.archived_at is not None and data.execution_mode != "read_only":
            raise HTTPException(status_code=409, detail="Restore the Session before queueing a message")
        if session.plan_id != data.plan_id:
            raise HTTPException(status_code=409, detail="Session focus does not match queued message plan")
    if data.plan_id is not None:
        plan = await db.get(Plan, data.plan_id)
        if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan.status == "archived" and data.execution_mode != "read_only":
            raise HTTPException(status_code=409, detail="Restore the plan before queueing a message")
    try:
        reply_target = await validate_intervention_reply_scope(
            db,
            intervention_id=data.reply_to_intervention_id,
            owner_id=settings.DEFAULT_OWNER_ID,
            session_id=data.session_id,
            plan_id=data.plan_id,
        )
        await _ensure_intervention_reply_not_inflight(
            db,
            reply_target.id if reply_target is not None else None,
        )
        await accept_intervention_reply(db, reply_target)
    except (ValueError, InterventionStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scope_filter = QueuedMessage.session_id == data.session_id if data.session_id else QueuedMessage.session_id.is_(None)
    max_position = await db.scalar(
        select(func.coalesce(func.max(QueuedMessage.position), -1)).where(
            QueuedMessage.owner_id == settings.DEFAULT_OWNER_ID,
            scope_filter,
        )
    )
    if max_position is None:
        max_position = -1
    message = QueuedMessage(
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=data.session_id,
        plan_id=data.plan_id,
        execution_mode=data.execution_mode,
        objective=data.objective,
        reply_to_intervention_id=data.reply_to_intervention_id,
        position=int(max_position) + 1,
    )
    db.add(message)
    await commit_uow(db)
    await db.refresh(message)
    return message


@router.patch("/queue/{message_id}", response_model=QueuedMessageRead)
async def update_queued_message(
    message_id: str,
    data: QueuedMessageUpdate,
    db: AsyncSession = Depends(get_db),
):
    await ensure_sqlite_write_transaction(db)
    message = await db.get(QueuedMessage, message_id)
    if not message or message.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Queued message not found")
    if message.version != data.expected_version:
        raise HTTPException(status_code=409, detail="queued_message_version_conflict")
    if data.objective is not None:
        if message.trigger != "user_message":
            raise HTTPException(
                status_code=409,
                detail="Email and system queue items preserve their original content and cannot be edited",
            )
        message.objective = data.objective
    changed = data.objective is not None
    reordered = False
    if data.position is not None and data.position != message.position:
        scope_filter = (
            QueuedMessage.session_id == message.session_id
            if message.session_id
            else QueuedMessage.session_id.is_(None)
        )
        ordered = list((await db.execute(
            select(QueuedMessage).where(
                QueuedMessage.owner_id == settings.DEFAULT_OWNER_ID,
                scope_filter,
            ).order_by(QueuedMessage.position, QueuedMessage.created_at, QueuedMessage.id)
        )).scalars())
        ordered = [item for item in ordered if item.id != message.id]
        ordered.insert(min(data.position, len(ordered)), message)
        await flush_uow(db)
        await reorder_queue(
            db,
            owner_id=settings.DEFAULT_OWNER_ID,
            session_id=message.session_id,
            ordered=ordered,
        )
        changed = True
        reordered = True
    if changed and not reordered and message.version == data.expected_version:
        message.version += 1
        message.updated_at = utc_now()
    await commit_uow(db)
    await db.refresh(message)
    return message


@router.delete("/queue/{message_id}", status_code=204)
async def delete_queued_message(
    message_id: str,
    data: QueuedMessageMutation,
    db: AsyncSession = Depends(get_db),
):
    await ensure_sqlite_write_transaction(db)
    message = await db.get(QueuedMessage, message_id)
    if not message or message.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Queued message not found")
    if message.version != data.expected_version:
        raise HTTPException(status_code=409, detail="queued_message_version_conflict")
    deleted_position = message.position
    deleted_session_id = message.session_id
    await db.delete(message)
    await flush_uow(db)
    await compact_queue_after_removal(
        db,
        owner_id=settings.DEFAULT_OWNER_ID,
        session_id=deleted_session_id,
        deleted_position=deleted_position,
    )
    await commit_uow(db)


@router.post("/queue/{message_id}/send", response_model=AgentRunRead, status_code=202)
async def send_queued_message(
    message_id: str,
    data: QueuedMessageMutation,
    db: AsyncSession = Depends(get_db),
):
    await ensure_sqlite_write_transaction(db)
    message = await db.get(QueuedMessage, message_id)
    if not message or message.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Queued message not found")
    if message.version != data.expected_version:
        raise HTTPException(status_code=409, detail="queued_message_version_conflict")
    if message.session_id:
        session = await db.get(Session, message.session_id)
        if not session or session.owner_id != settings.DEFAULT_OWNER_ID:
            raise HTTPException(status_code=409, detail="Queued Session no longer exists")
        if session.archived_at is not None:
            raise HTTPException(status_code=409, detail="Restore the Session before sending this message")
        if session.plan_id != message.plan_id:
            raise HTTPException(status_code=409, detail="Queued message no longer matches the Session focus")
        active_run = (await db.execute(
            select(AgentRun.id).where(
                AgentRun.session_id == message.session_id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise HTTPException(status_code=409, detail="当前运行结束后会自动发送这条排队消息")
    if message.plan_id is not None:
        plan = await db.get(Plan, message.plan_id)
        if not plan or plan.owner_id != settings.DEFAULT_OWNER_ID or plan.status == "archived":
            raise HTTPException(status_code=409, detail="Queued plan is unavailable or archived")
    try:
        run = await dispatch_queued_message(
            db,
            message,
            owner_id=settings.DEFAULT_OWNER_ID,
            expected_version=data.expected_version,
        )
    except (QueueStateError, RunStateError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await commit_uow(db)
    await db.refresh(run)
    _start_runtime(run.id)
    return run


@router.post("/heartbeat", response_model=AgentRunRead, status_code=202)
async def trigger_heartbeat(db: AsyncSession = Depends(get_db)):
    try:
        return await proactive_scheduler.trigger_now("manual_heartbeat")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
