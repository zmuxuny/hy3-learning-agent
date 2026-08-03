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
    # Imported lazily to avoid a registry/planning import cycle. The allowlist
    # is deliberately read-only: child Agents advise, the lead Agent commits.
    from app.tools.registry import TOOL_MAP, execute_tool

    allowed_names = {
        "profile_get",
        "memory_search",
        "web_search",
        "web_open",
        "file_list",
        "file_read",
        "calendar_list",
    }
    schemas = [TOOL_MAP[name].openai_schema() for name in sorted(allowed_names)]
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are a bounded planning sub-agent inside a personal learning harness. Work only on the assigned "
                "planning question. You may use the supplied read-only tools, including web search/open when current "
                "external evidence matters. Never request search-result saving and never create or modify application "
                "state. Return a concise evidence-oriented report with sources, assumptions, recommendations, risks, "
                "and questions the lead Agent should resolve. Do not expose chain-of-thought."
            ),
        },
        {
            "role": "user",
            "content": f"Role: {assignment.role}\nAssignment: {assignment.objective}\n\nShared context:\n{context}",
        },
    ]
    final_text = ""
    for _ in range(4):
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.MODEL_NAME,
                messages=messages,
                tools=schemas,
                tool_choice="auto",
                temperature=settings.MODEL_TEMPERATURE,
                extra_body={"reasoning_effort": settings.MODEL_REASONING_EFFORT},
            ),
            timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        assistant_payload: dict = {"role": "assistant", "content": message.content or ""}
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content:
            assistant_payload["reasoning_content"] = reasoning_content
        if tool_calls:
            assistant_payload["tool_calls"] = [call.model_dump() for call in tool_calls]
        messages.append(assistant_payload)
        if not tool_calls:
            final_text = (message.content or "").strip()
            break
        for call in tool_calls:
            await _child_event(child.id, "tool.started", f"调用只读工具 {call.function.name}", {
                "tool_call_id": call.id,
                "name": call.function.name,
            })
            try:
                raw_args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                raw_args = {}
            if call.function.name == "web_search" and raw_args.get("save_results"):
                result = {"ok": False, "error": "Planning child Agents cannot save search results", "retryable": False}
            elif call.function.name not in allowed_names:
                result = {"ok": False, "error": "Tool is outside the planning child allowlist", "retryable": False}
            else:
                async with AsyncSessionLocal() as tool_db:
                    result = await execute_tool(
                        call.function.name,
                        call.function.arguments,
                        ToolContext(
                            db=tool_db,
                            owner_id=child.owner_id,
                            run_id=child.id,
                            trigger="subagent",
                            plan_id=child.plan_id,
                            session_id=child.session_id,
                        ),
                    )
            await _child_event(child.id, "tool.completed", f"只读工具 {call.function.name} {'完成' if result.get('ok') else '失败'}", {
                "tool_call_id": call.id,
                "name": call.function.name,
                "result": result,
            })
            content = json.dumps(result, ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content[:12000]})
    return final_text or "子 Agent 已完成受限调查，但没有返回可用的总结。"


async def _child_event(run_id: str, event_type: str, summary: str, payload: dict | None = None) -> None:
    async with AsyncSessionLocal() as event_db:
        await emit_event(event_db, run_id, event_type, summary, payload)


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
