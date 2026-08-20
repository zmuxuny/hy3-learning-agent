from __future__ import annotations

import asyncio
from uuid import NAMESPACE_URL, uuid5

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.context import ContextAssembler
from app.core.config import settings
from app.core.trust import project_child_result_authority
from app.db.database import AsyncSessionLocal
from app.db.uow import commit as commit_uow
from app.models import AgentRun
from app.runtime.checkpoints import make_checkpoint
from app.runtime.events import emit_event
from app.runtime.tasks import cancel_and_wait_tracked_task, start_tracked_task
from app.runtime.subagents import (
    READ_ONLY_TOOL_NAMES,
    SUBAGENT_SYSTEM_PROMPT,
    execute_durable_child,
    subagent_user_prefix,
    wait_for_child,
)
from app.runtime.state import terminate_run
from app.tools.base import ToolContext, ToolDefinition, ToolEffectKind


class SubagentSpawnArgs(BaseModel):
    role: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2000)
    tool_whitelist: list[str] | None = None
    max_steps: int = Field(default=6, ge=1, le=12)


class SubagentIdArgs(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)


class SubagentJoinArgs(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    timeout_seconds: float = Field(default=60, ge=1, le=300)


_active_child_tasks: dict[str, asyncio.Task] = {}


def _stable_child_id(action_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"learning-travel:subagent:{action_key}"))


class _SubagentToolCoordinator:
    """Own the short durable phases around in-process child execution."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def persist_new_child(
        self,
        child: AgentRun,
        *,
        role: str,
        objective: str,
        allowlist: set[str],
    ) -> None:
        self.ctx.db.add(child)
        await commit_uow(self.ctx.db)
        await self.ctx.db.refresh(child)
        await emit_event(self.ctx.db, child.id, "run.queued", f"{role} 子 Agent 已排队", {
            "parent_run_id": self.ctx.run_id,
            "role": role,
            "action_key": self.ctx.action_key,
        }, event_key=f"run:{child.id}:queued")
        await emit_event(self.ctx.db, self.ctx.run_id, "subagent.started", f"已委派给 {role}", {
            "child_run_id": child.id,
            "role": role,
            "objective": objective,
            "allowlist": sorted(allowlist),
            "action_key": self.ctx.action_key,
        })

    async def release_replay_read(self) -> None:
        if self.ctx.db.new or self.ctx.db.dirty or self.ctx.db.deleted:
            raise RuntimeError("sub-agent wait requires a clean caller session")
        await commit_uow(self.ctx.db)

def _effective_allowlist(requested: list[str] | None) -> set[str]:
    if not requested:
        return set(READ_ONLY_TOOL_NAMES)
    return READ_ONLY_TOOL_NAMES.intersection(requested)


def _own_child(ctx: ToolContext, child: AgentRun | None) -> bool:
    return bool(
        child
        and child.parent_run_id == ctx.run_id
        and child.trigger == "subagent"
        and child.owner_id == ctx.owner_id
    )


async def _run_child_async(
    child_id: str,
    role: str,
    objective: str,
    context: str,
    allowlist: set[str],
    max_steps: int,
    checkpoint: dict | None = None,
) -> None:
    del checkpoint
    await execute_durable_child(
        child_id,
        role=role,
        objective=objective,
        context=context,
        allowlist=allowlist,
        max_steps=max_steps,
        client_factory=lambda: AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_API_BASE,
        ),
    )


async def resume_subagent_run(child_id: str) -> None:
    """Resume a generic child Run from its own durable checkpoint."""
    async with AsyncSessionLocal() as db:
        child = await db.get(AgentRun, child_id)
        if child is None or child.trigger != "subagent" or not child.checkpoint:
            return
        checkpoint = dict(child.checkpoint)
    if checkpoint.get("kind") != "subagent":
        return
    await _run_child_async(
        child_id,
        str(checkpoint.get("role") or "调查"),
        str(checkpoint.get("objective") or child.objective),
        str(checkpoint.get("context") or ""),
        set(checkpoint.get("allowlist") or READ_ONLY_TOOL_NAMES),
        int(checkpoint.get("max_steps") or 6),
        checkpoint,
    )


async def subagent_spawn(ctx: ToolContext, args: SubagentSpawnArgs) -> dict:
    if not settings.OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY is not configured"}
    if not ctx.action_key:
        return {"error": "subagent_spawn requires a durable action key"}
    allowlist = _effective_allowlist(args.tool_whitelist)
    child_id = _stable_child_id(ctx.action_key)
    coordinator = _SubagentToolCoordinator(ctx)
    child = await ctx.db.get(AgentRun, child_id)
    created = child is None
    if child is not None:
        checkpoint = dict(child.checkpoint or {})
        expected_objective = f"[{args.role}] {args.objective}"
        if (
            not _own_child(ctx, child)
            or child.objective != expected_objective
            or child.execution_mode != ctx.execution_mode
            or child.reply_to_intervention_id != ctx.reply_to_intervention_id
            or (
                checkpoint
                and checkpoint.get("action_key") != ctx.action_key
            )
        ):
            return {"error": "stable sub-agent identity conflicts with another action"}
        child_status = child.status
        stored_context = str(checkpoint.get("context") or "")
        await coordinator.release_replay_read()
    else:
        from app.tools.registry import TOOL_MAP

        child_schemas = [
            TOOL_MAP[name].openai_schema()
            for name in sorted(allowlist)
        ]
        snapshot = await ContextAssembler(ctx.db).build(
            ctx.owner_id,
            plan_id=ctx.plan_id,
            session_id=ctx.session_id,
            run_id=ctx.run_id,
            objective=args.objective,
            prompt_system=SUBAGENT_SYSTEM_PROMPT,
            prompt_tools=child_schemas,
            prompt_prefix=subagent_user_prefix(f"{args.role}: {args.objective}"),
        )
        stored_context = snapshot.markdown
        child = AgentRun(
            id=child_id,
            owner_id=ctx.owner_id,
            session_id=ctx.session_id,
            plan_id=ctx.plan_id,
            parent_run_id=ctx.run_id,
            trigger="subagent",
            objective=f"[{args.role}] {args.objective}",
            execution_mode=ctx.execution_mode,
            reply_to_intervention_id=ctx.reply_to_intervention_id,
            # Creation records durable intent only.  The shared child
            # executor must claim the queued Run before any model/tool work.
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
                    "role": args.role,
                    "objective": args.objective,
                    "context": stored_context,
                    "allowlist": sorted(allowlist),
                    "max_steps": args.max_steps,
                },
            ),
        )
        await coordinator.persist_new_child(
            child,
            role=args.role,
            objective=args.objective,
            allowlist=allowlist,
        )
        child_status = child.status

    if child_status in {"queued", "running"}:
        task = start_tracked_task(
            child_id,
            _run_child_async(
                child_id,
                args.role,
                args.objective,
                stored_context,
                allowlist,
                args.max_steps,
            ),
        )
        _active_child_tasks[child_id] = task

        def discard(completed: asyncio.Task) -> None:
            if _active_child_tasks.get(child_id) is completed:
                _active_child_tasks.pop(child_id, None)

        task.add_done_callback(discard)
    return {
        "run_id": child_id,
        "role": args.role,
        "status": child_status,
        "allowlist": sorted(allowlist),
        "replayed_child": not created,
    }


async def subagent_status(ctx: ToolContext, args: SubagentIdArgs) -> dict:
    child = await ctx.db.get(AgentRun, args.run_id)
    if not _own_child(ctx, child):
        return {"error": "Sub-agent run not found"}
    return await project_child_result_authority(
        ctx.db,
        owner_id=ctx.owner_id,
        child_run_id=child.id,
        payload={
            "run_id": child.id,
            "status": child.status,
            "objective": child.objective,
            "output": child.output,
        },
    )


async def subagent_join(ctx: ToolContext, args: SubagentJoinArgs) -> dict:
    child = await ctx.db.get(AgentRun, args.run_id)
    if not _own_child(ctx, child):
        return {"error": "Sub-agent run not found"}
    child_id = child.id
    # Release the ownership read transaction before awaiting another actor.
    await _SubagentToolCoordinator(ctx).release_replay_read()
    terminal = await wait_for_child(child_id, args.timeout_seconds)
    timed_out = terminal.status not in {"completed", "failed", "cancelled"}
    return await project_child_result_authority(
        ctx.db,
        owner_id=ctx.owner_id,
        child_run_id=terminal.id,
        payload={
            "run_id": terminal.id,
            "status": terminal.status,
            "output": terminal.output or "",
            "timed_out": timed_out,
        },
    )


async def subagent_cancel(ctx: ToolContext, args: SubagentIdArgs) -> dict:
    child = await ctx.db.get(AgentRun, args.run_id)
    if not _own_child(ctx, child):
        return {"error": "Sub-agent run not found"}
    cancelled = child.status in {
        "queued", "running", "waiting_approval", "retry_wait", "needs_reconciliation",
    }
    if cancelled:
        await commit_uow(ctx.db)
        await terminate_run(
            AsyncSessionLocal,
            child.id,
            status="cancelled",
            reason_code="parent_cancelled",
            summary="父 Agent 取消",
        )
        await cancel_and_wait_tracked_task(child.id)
    return {"run_id": child.id, "status": "cancelled" if cancelled else child.status}


SUBAGENT_TOOLS = [
    ToolDefinition(
        "subagent_spawn",
        "Spawn one bounded read-only sub-agent for independent investigation; it can only use read-only tools and returns a structured report.",
        SubagentSpawnArgs,
        subagent_spawn,
        effect_kind=ToolEffectKind.EXTERNAL_WRITE,
        idempotent=True,
    ),
    ToolDefinition(
        "subagent_status",
        "Check the status and output of a child sub-agent owned by this run.",
        SubagentIdArgs,
        subagent_status,
        effect_kind=ToolEffectKind.EXTERNAL_READ,
        idempotent=True,
    ),
    ToolDefinition(
        "subagent_join",
        "Wait until a child sub-agent finishes and return its report; the parent remains responsible for all writes.",
        SubagentJoinArgs,
        subagent_join,
        effect_kind=ToolEffectKind.EXTERNAL_READ,
    ),
    ToolDefinition(
        "subagent_cancel",
        "Cancel a child sub-agent owned by this run.",
        SubagentIdArgs,
        subagent_cancel,
        effect_kind=ToolEffectKind.EXTERNAL_WRITE,
        idempotent=True,
    ),
]
