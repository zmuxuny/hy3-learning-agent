from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.context import ContextAssembler
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, PlanProposal, PlanningIntake, RunEvent, Session
from app.runtime.events import emit_event
from app.schemas import PlanCreate
from app.tools.base import EmptyArgs, ToolContext, ToolDefinition


class PlanningFact(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=2000)
    source: str = Field(default="user", max_length=40)


class PlanningQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=500)
    why: str = Field(default="", max_length=500)
    options: list[str] = Field(default_factory=list, max_length=6)
    allow_custom: bool = True


class PlanningIntakeUpdateArgs(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    confirmed_facts: list[PlanningFact] = Field(default_factory=list, max_length=24)
    open_questions: list[PlanningQuestion] = Field(default_factory=list, max_length=6)
    readiness: str = Field(pattern="^(collecting|ready)$")
    readiness_confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2000)


class PlanningAssignment(BaseModel):
    role: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=1200)


class PlanningDelegateArgs(BaseModel):
    assignments: list[PlanningAssignment] = Field(min_length=1, max_length=3)


class PlanProposalCreateArgs(BaseModel):
    plan: PlanCreate
    rationale: str = Field(min_length=1, max_length=4000)

    @field_validator("plan", mode="before")
    @classmethod
    def decode_serialized_plan(cls, value):
        """Accept nested JSON emitted by OpenAI-compatible tool callers.

        Some providers serialize a nested object twice even though the tool
        schema declares it as an object. Keeping this normalization at the
        contract boundary makes the tool tolerant without weakening the
        validation performed by ``PlanCreate``.
        """
        if isinstance(value, str):
            return json.loads(value)
        return value


def _require_session(ctx: ToolContext) -> str | None:
    return ctx.session_id or None


def _intake_data(intake: PlanningIntake | None) -> dict:
    if intake is None:
        return {
            "exists": False,
            "session_id": "",
            "goal": "",
            "confirmed_facts": [],
            "open_questions": [],
            "readiness": "collecting",
            "readiness_confidence": 0.0,
            "rationale": "No planning intake has been recorded yet.",
        }
    return {
        "exists": True,
        "session_id": intake.session_id,
        "goal": intake.goal,
        "confirmed_facts": intake.confirmed_facts,
        "open_questions": intake.open_questions,
        "readiness": intake.readiness,
        "readiness_confidence": intake.readiness_confidence,
        "rationale": intake.rationale,
    }


async def planning_intake_get(ctx: ToolContext, _: EmptyArgs) -> dict:
    session_id = _require_session(ctx)
    if not session_id:
        return {"error": "Planning intake requires an active Session"}
    intake = await ctx.db.get(PlanningIntake, session_id)
    return _intake_data(intake)


async def planning_intake_update(ctx: ToolContext, args: PlanningIntakeUpdateArgs) -> dict:
    session_id = _require_session(ctx)
    if not session_id:
        return {"error": "Planning intake requires an active Session"}
    session = await ctx.db.get(Session, session_id)
    if not session or session.owner_id != ctx.owner_id:
        return {"error": "Session not found"}
    if args.readiness == "ready" and args.open_questions:
        return {"error": "A ready intake cannot still contain open questions"}
    intake = await ctx.db.get(PlanningIntake, session_id)
    if intake is None:
        intake = PlanningIntake(session_id=session_id, owner_id=ctx.owner_id)
        ctx.db.add(intake)
    intake.source_run_id = ctx.run_id
    intake.goal = args.goal
    intake.confirmed_facts = [item.model_dump(mode="json") for item in args.confirmed_facts]
    intake.open_questions = [item.model_dump(mode="json") for item in args.open_questions]
    intake.readiness = args.readiness
    intake.readiness_confidence = args.readiness_confidence
    intake.rationale = args.rationale
    intake.updated_at = datetime.now(timezone.utc)
    await ctx.db.commit()
    return _intake_data(intake)


async def _specialist_report(
    client: AsyncOpenAI,
    child: AgentRun,
    assignment: PlanningAssignment,
    context: str,
) -> str:
    # Shared bounded engine: read-only allowlist, own run events, no writes.
    from app.runtime.subagents import PLANNING_CHILD_ALLOWLIST, run_restricted_child

    return await run_restricted_child(
        client=client,
        child=child,
        objective=f"{assignment.role}: {assignment.objective}",
        context=context,
        allowlist=set(PLANNING_CHILD_ALLOWLIST),
        max_steps=4,
    )


async def _cancel_child_runs(children: list[tuple[str, str]], parent_run_id: str) -> None:
    """Leave no child Run looking active when the parent tool is cancelled."""
    async with AsyncSessionLocal() as cleanup_db:
        now = datetime.now(timezone.utc)
        for child_id, role in children:
            child = await cleanup_db.get(AgentRun, child_id)
            if child is None or child.status not in {"queued", "running"}:
                continue
            child.status = "cancelled"
            child.completed_at = now
            await cleanup_db.commit()
            await emit_event(
                cleanup_db,
                child.id,
                "run.cancelled",
                f"{role} 子 Agent 随父运行停止",
                {"parent_run_id": parent_run_id, "role": role},
            )
            await emit_event(
                cleanup_db,
                parent_run_id,
                "subagent.completed",
                f"{role} 已停止",
                {"child_run_id": child.id, "role": role, "status": "cancelled", "report": ""},
            )


async def planning_delegate(ctx: ToolContext, args: PlanningDelegateArgs) -> dict:
    session_id = _require_session(ctx)
    if not session_id:
        return {"error": "Planning delegation requires an active Session"}
    if not settings.OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY is not configured"}
    snapshot = await ContextAssembler(ctx.db).build(
        ctx.owner_id,
        plan_id=ctx.plan_id,
        session_id=session_id,
        run_id=ctx.run_id,
        objective="; ".join(item.objective for item in args.assignments),
    )
    child_runs: list[AgentRun] = []
    for assignment in args.assignments:
        child = AgentRun(
            owner_id=ctx.owner_id,
            session_id=session_id,
            plan_id=ctx.plan_id,
            parent_run_id=ctx.run_id,
            trigger="subagent",
            objective=f"[{assignment.role}] {assignment.objective}",
            status="running",
            model=settings.MODEL_NAME,
            started_at=datetime.now(timezone.utc),
        )
        ctx.db.add(child)
        child_runs.append(child)
    await ctx.db.commit()
    for child, assignment in zip(child_runs, args.assignments, strict=True):
        await emit_event(ctx.db, child.id, "run.started", f"{assignment.role} 子 Agent 已开始", {
            "parent_run_id": ctx.run_id,
            "role": assignment.role,
        })
        await emit_event(ctx.db, ctx.run_id, "subagent.started", f"已委派给 {assignment.role}", {
            "child_run_id": child.id,
            "role": assignment.role,
            "objective": assignment.objective,
        })

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_API_BASE)
    child_refs = [(child.id, assignment.role) for child, assignment in zip(child_runs, args.assignments, strict=True)]
    try:
        outcomes = await asyncio.gather(
            *[
                _specialist_report(client, child, assignment, snapshot.markdown)
                for child, assignment in zip(child_runs, args.assignments, strict=True)
            ],
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        await asyncio.shield(_cancel_child_runs(child_refs, ctx.run_id))
        raise
    reports = []
    for child, assignment, outcome in zip(child_runs, args.assignments, outcomes, strict=True):
        if isinstance(outcome, Exception):
            child.status = "failed"
            report = f"Specialist failed: {type(outcome).__name__}"
            event_type = "run.failed"
        else:
            child.status = "completed"
            report = outcome
            event_type = "run.completed"
        child.completed_at = datetime.now(timezone.utc)
        await ctx.db.commit()
        await emit_event(ctx.db, child.id, event_type, report, {"role": assignment.role})
        await emit_event(ctx.db, ctx.run_id, "subagent.completed", f"{assignment.role} 已返回结论", {
            "child_run_id": child.id,
            "role": assignment.role,
            "status": child.status,
            "report": report,
        })
        reports.append({
            "child_run_id": child.id,
            "role": assignment.role,
            "objective": assignment.objective,
            "status": child.status,
            "report": report,
        })
    return {"reports": reports}


async def plan_proposal_create(ctx: ToolContext, args: PlanProposalCreateArgs) -> dict:
    session_id = _require_session(ctx)
    if not session_id:
        return {"error": "Plan proposals require an active Session"}
    intake = await ctx.db.get(PlanningIntake, session_id)
    if not intake or intake.readiness != "ready":
        return {
            "error": "Requirements are not ready. Update planning_intake with the remaining questions first."
        }
    existing = (await ctx.db.execute(
        select(PlanProposal).where(
            PlanProposal.owner_id == ctx.owner_id,
            PlanProposal.session_id == session_id,
            PlanProposal.status == "pending",
        ).order_by(PlanProposal.created_at.desc()).limit(1)
    )).scalars().one_or_none()
    reports = list((await ctx.db.execute(
        select(AgentRun).where(
            AgentRun.parent_run_id == ctx.run_id,
            AgentRun.trigger == "subagent",
        ).order_by(AgentRun.created_at)
    )).scalars())
    specialist_reports = []
    for child in reports:
        final_event = (await ctx.db.execute(
            select(RunEvent).where(
                RunEvent.run_id == child.id,
                RunEvent.event_type.in_(["run.completed", "run.failed"]),
            ).order_by(RunEvent.sequence.desc()).limit(1)
        )).scalars().one_or_none()
        specialist_reports.append({
            "child_run_id": child.id,
            "objective": child.objective,
            "status": child.status,
            "report": final_event.summary if final_event else "",
        })
    proposal = existing or PlanProposal(
        owner_id=ctx.owner_id,
        session_id=session_id,
        source_run_id=ctx.run_id,
        title=args.plan.title,
    )
    proposal.source_run_id = ctx.run_id
    proposal.title = args.plan.title
    proposal.rationale = args.rationale
    proposal.plan_payload = args.plan.model_dump(mode="json")
    proposal.specialist_reports = specialist_reports
    proposal.updated_at = datetime.now(timezone.utc)
    ctx.db.add(proposal)
    await ctx.db.commit()
    await ctx.db.refresh(proposal)
    return {
        "proposal_id": proposal.id,
        "title": proposal.title,
        "status": proposal.status,
        "stage_count": len(args.plan.stages),
        "task_count": sum(len(stage.tasks) for stage in args.plan.stages),
        "approval_required": True,
    }


PLANNING_TOOLS = [
    ToolDefinition(
        "planning_intake_get",
        "Read durable requirement-discovery state for the active planning Session.",
        EmptyArgs,
        planning_intake_get,
    ),
    ToolDefinition(
        "planning_intake_update",
        "Record confirmed requirements, renderable follow-up questions, and the Agent's evidence-based readiness judgment.",
        PlanningIntakeUpdateArgs,
        planning_intake_update,
        idempotent=True,
    ),
    ToolDefinition(
        "planning_delegate",
        "Delegate up to three bounded planning investigations to real child Agent runs and join their reports.",
        PlanningDelegateArgs,
        planning_delegate,
    ),
    ToolDefinition(
        "plan_proposal_create",
        "Create or revise a reviewable plan proposal after the planning intake is ready; does not create the Plan until the user accepts it.",
        PlanProposalCreateArgs,
        plan_proposal_create,
        idempotent=True,
    ),
]
