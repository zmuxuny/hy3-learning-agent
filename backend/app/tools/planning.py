from __future__ import annotations

import asyncio
import json
from uuid import NAMESPACE_URL, uuid5

from app.context import ContextAssembler
from app.core.config import settings
from app.core.prompt_envelope import estimate_context_message_tokens
from app.core.time import utc_now
from app.core.trust import (
    mark_external_untrusted_result,
    project_child_result_authority,
)
from app.db.database import AsyncSessionLocal
from app.db.uow import commit as commit_uow
from app.db.uow import flush as flush_uow
from app.models import AgentRun, PlanningIntake, PlanProposal, RunEvent, Session
from app.runtime.checkpoints import make_checkpoint
from app.runtime.events import emit_event
from app.runtime.model_clients import create_model_client
from app.runtime.tasks import start_tracked_task
from app.schemas import PlanCreate
from app.services.plans import plan_completeness_issues
from app.tools.base import EmptyArgs, ToolContext, ToolDefinition, ToolEffectKind
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select


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
    intake.updated_at = utc_now()
    await flush_uow(ctx.db)
    return _intake_data(intake)


def _planning_child_id(action_key: str, assignment_index: int) -> str:
    return str(uuid5(
        NAMESPACE_URL,
        f"learning-travel:planning:{action_key}:{assignment_index}",
    ))


def _own_planning_child(ctx: ToolContext, child: AgentRun | None) -> bool:
    return bool(
        child
        and child.owner_id == ctx.owner_id
        and child.parent_run_id == ctx.run_id
        and child.trigger == "subagent"
        and child.session_id == ctx.session_id
        and child.plan_id == ctx.plan_id
    )


class _PlanningDelegateCoordinator:
    """Own the durable phases around concurrent planning model calls."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def persist_children(
        self,
        children: list[tuple[AgentRun, PlanningAssignment]],
    ) -> None:
        if children:
            self.ctx.db.add_all(child for child, _ in children)
            await commit_uow(self.ctx.db)
            for child, assignment in children:
                await emit_event(
                    self.ctx.db,
                    child.id,
                    "run.queued",
                    f"{assignment.role} 子 Agent 已排队",
                    {
                        "parent_run_id": self.ctx.run_id,
                        "role": assignment.role,
                        "action_key": self.ctx.action_key,
                    },
                    event_key=f"run:{child.id}:queued",
                )
                await emit_event(
                    self.ctx.db,
                    self.ctx.run_id,
                    "subagent.started",
                    f"已委派给 {assignment.role}",
                    {
                        "child_run_id": child.id,
                        "role": assignment.role,
                        "objective": assignment.objective,
                        "action_key": self.ctx.action_key,
                    },
                )
        else:
            # Existing children were loaded in the caller session. Release its
            # read transaction before any child waits on the model provider.
            if self.ctx.db.new or self.ctx.db.dirty or self.ctx.db.deleted:
                raise RuntimeError("planning child replay requires a clean caller session")
            await commit_uow(self.ctx.db)

    @staticmethod
    async def cancel_children(
        children: list[tuple[str, str]],
    ) -> None:
        """Leave no child Run looking active when the parent tool is cancelled."""
        from app.runtime.subagents import cancel_child

        for child_id, role in children:
            async with AsyncSessionLocal() as cleanup_db:
                child = await cleanup_db.get(AgentRun, child_id)
            if child is not None:
                await cancel_child(child, f"{role} 子 Agent 随父运行停止")


def _planning_allowlist() -> set[str]:
    from app.runtime.subagents import PLANNING_CHILD_ALLOWLIST

    return set(PLANNING_CHILD_ALLOWLIST)


async def _run_planning_child(
    child_id: str,
    *,
    action_key: str,
    assignment_index: int,
    assignment: PlanningAssignment,
    context: str,
    parent_call_id: str | None = None,
    terminal_predecessor: asyncio.Event | None = None,
    terminal_signal: asyncio.Event | None = None,
) -> None:
    """Resume one deterministic planning child through the shared state machine."""

    from app.runtime.subagents import execute_durable_child

    del action_key, assignment_index

    async def await_predecessor() -> None:
        if terminal_predecessor is not None:
            await terminal_predecessor.wait()

    try:
        await execute_durable_child(
            child_id,
            role=assignment.role,
            objective=assignment.objective,
            context=context,
            allowlist=_planning_allowlist(),
            max_steps=4,
            client_factory=create_model_client,
            parent_call_id=parent_call_id,
            before_terminal_commit=await_predecessor,
        )
    finally:
        if terminal_signal is not None:
            terminal_signal.set()


async def planning_delegate(ctx: ToolContext, args: PlanningDelegateArgs) -> dict:
    session_id = _require_session(ctx)
    if not session_id:
        return {"error": "Planning delegation requires an active Session"}
    if not settings.OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY is not configured"}
    if not ctx.action_key:
        return {"error": "planning_delegate requires a durable action key"}
    child_ids = [
        _planning_child_id(ctx.action_key, index)
        for index in range(len(args.assignments))
    ]
    existing_children = [
        await ctx.db.get(AgentRun, child_id)
        for child_id in child_ids
    ]
    existing_count = sum(child is not None for child in existing_children)
    if existing_count not in {0, len(args.assignments)}:
        return {
            "error": "planning delegation has a partial durable child set and requires reconciliation",
            "error_code": "needs_reconciliation",
        }
    snapshot_markdown = ""
    if existing_count == 0:
        from app.runtime.subagents import SUBAGENT_SYSTEM_PROMPT, subagent_user_prefix
        from app.tools.registry import TOOL_MAP

        planning_allowlist = _planning_allowlist()
        child_schemas = [
            TOOL_MAP[name].openai_schema()
            for name in sorted(planning_allowlist)
        ]
        child_prefixes = [
            subagent_user_prefix(f"{item.role}: {item.objective}")
            for item in args.assignments
        ]
        budget_prefix = max(
            child_prefixes,
            key=lambda value: estimate_context_message_tokens(
                system_prompt=SUBAGENT_SYSTEM_PROMPT,
                user_content=value,
            ),
        )
        snapshot = await ContextAssembler(ctx.db).build(
            ctx.owner_id,
            plan_id=ctx.plan_id,
            session_id=session_id,
            run_id=ctx.run_id,
            objective="; ".join(item.objective for item in args.assignments),
            prompt_system=SUBAGENT_SYSTEM_PROMPT,
            prompt_tools=child_schemas,
            prompt_prefix=budget_prefix,
        )
        snapshot_markdown = snapshot.markdown
    child_runs: list[AgentRun] = []
    new_children: list[tuple[AgentRun, PlanningAssignment]] = []
    for index, assignment in enumerate(args.assignments):
        child_id = child_ids[index]
        child = existing_children[index]
        if child is None:
            child = AgentRun(
                id=child_id,
                owner_id=ctx.owner_id,
                session_id=session_id,
                plan_id=ctx.plan_id,
                parent_run_id=ctx.run_id,
                trigger="subagent",
                objective=f"[{assignment.role}] {assignment.objective}",
                execution_mode=ctx.execution_mode,
                reply_to_intervention_id=ctx.reply_to_intervention_id,
                status="queued",
                model=settings.MODEL_NAME,
                checkpoint_schema_version=1,
                checkpoint=make_checkpoint(
                    kind="subagent",
                    phase="awaiting_model",
                    step=0,
                    messages=[],
                    identity={
                        "action_key": ctx.action_key,
                        "assignment_index": index,
                        "role": assignment.role,
                        "objective": assignment.objective,
                        "context": snapshot_markdown,
                        "allowlist": sorted(_planning_allowlist()),
                        "max_steps": 4,
                        "parent_call_id": ctx.source_model_call_id,
                    },
                ),
            )
            new_children.append((child, assignment))
        else:
            checkpoint = dict(child.checkpoint or {})
            if (
                not _own_planning_child(ctx, child)
                or child.objective != f"[{assignment.role}] {assignment.objective}"
                or child.execution_mode != ctx.execution_mode
                or child.reply_to_intervention_id != ctx.reply_to_intervention_id
                or (
                    checkpoint
                    and (
                        checkpoint.get("action_key") != ctx.action_key
                        or checkpoint.get("assignment_index") != index
                    )
                )
            ):
                return {"error": "stable planning child identity conflicts with another action"}
            if child.status in {"queued", "running"} and not checkpoint.get("context"):
                return {
                    "error": "active planning child has no durable context checkpoint",
                    "error_code": "needs_reconciliation",
                }
        child_runs.append(child)

    coordinator = _PlanningDelegateCoordinator(ctx)
    await coordinator.persist_children(new_children)
    child_refs = [
        (child.id, assignment.role)
        for child, assignment in zip(child_runs, args.assignments, strict=True)
    ]
    try:
        active_tasks = []
        terminal_events = [asyncio.Event() for _ in args.assignments]
        for index, (child, assignment) in enumerate(
            zip(child_runs, args.assignments, strict=True)
        ):
            if child.status in {"completed", "failed", "cancelled"}:
                terminal_events[index].set()
                continue
            checkpoint = dict(child.checkpoint or {})
            context = str(checkpoint.get("context") or snapshot_markdown)
            active_tasks.append(start_tracked_task(
                child.id,
                _run_planning_child(
                    child.id,
                    action_key=ctx.action_key,
                    assignment_index=index,
                    assignment=assignment,
                    context=context,
                    parent_call_id=ctx.source_model_call_id,
                    terminal_predecessor=(
                        terminal_events[index - 1] if index > 0 else None
                    ),
                    terminal_signal=terminal_events[index],
                ),
            ))
        if active_tasks:
            await asyncio.gather(*active_tasks)
    except asyncio.CancelledError:
        await asyncio.shield(
            _PlanningDelegateCoordinator.cancel_children(child_refs)
        )
        raise
    reports: list[dict] = []
    external_untrusted = False
    for child, assignment in zip(child_runs, args.assignments, strict=True):
        # The child coordinator committed terminal state in an independent
        # short UoW. Refresh through this caller only after all model waits.
        stored = await ctx.db.get(AgentRun, child.id, populate_existing=True)
        if stored is None:
            return {"error": "planning child disappeared before join"}
        report = await project_child_result_authority(
            ctx.db,
            owner_id=ctx.owner_id,
            child_run_id=stored.id,
            payload={
                "child_run_id": stored.id,
                "role": assignment.role,
                "objective": assignment.objective,
                "status": stored.status,
                "report": stored.output or "",
            },
        )
        external_untrusted = external_untrusted or bool(
            report.get("external_untrusted", False)
        )
        reports.append(report)
    result = {"reports": reports}
    return mark_external_untrusted_result(result) if external_untrusted else result


async def plan_proposal_create(ctx: ToolContext, args: PlanProposalCreateArgs) -> dict:
    session_id = _require_session(ctx)
    if not session_id:
        return {"error": "Plan proposals require an active Session"}
    intake = await ctx.db.get(PlanningIntake, session_id)
    if not intake or intake.readiness != "ready":
        return {
            "error": "Requirements are not ready. Update planning_intake with the remaining questions first."
        }
    completeness_issues = plan_completeness_issues(args.plan)
    if completeness_issues:
        return {
            "error": "The formal plan is incomplete: " + "; ".join(completeness_issues),
            "issues": completeness_issues,
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
    proposal.updated_at = utc_now()
    ctx.db.add(proposal)
    await flush_uow(ctx.db)
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
        effect_kind=ToolEffectKind.PURE_READ,
    ),
    ToolDefinition(
        "planning_intake_update",
        "Record confirmed requirements, renderable follow-up questions, and the Agent's evidence-based readiness judgment.",
        PlanningIntakeUpdateArgs,
        planning_intake_update,
        effect_kind=ToolEffectKind.DATABASE_WRITE,
        idempotent=True,
    ),
    ToolDefinition(
        "planning_delegate",
        "Delegate up to three bounded planning investigations to real child Agent runs and join their reports.",
        PlanningDelegateArgs,
        planning_delegate,
        effect_kind=ToolEffectKind.EXTERNAL_WRITE,
        idempotent=True,
    ),
    ToolDefinition(
        "plan_proposal_create",
        "Create or revise a reviewable plan proposal after the planning intake is ready; does not create the Plan until the user accepts it.",
        PlanProposalCreateArgs,
        plan_proposal_create,
        effect_kind=ToolEffectKind.DATABASE_WRITE,
        idempotent=True,
    ),
]
