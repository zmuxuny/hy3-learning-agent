from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.context import ContextAssembler
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun
from app.runtime.events import emit_event
from app.runtime.tasks import start_tracked_task
from app.runtime.subagents import (
    READ_ONLY_TOOL_NAMES,
    cancel_child,
    child_cancel_requested,
    run_restricted_child,
    wait_for_child,
)
from app.tools.base import ToolContext, ToolDefinition


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


_active_child_tasks: set[asyncio.Task] = set()


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
    async def persist_checkpoint(runtime_checkpoint: dict) -> None:
        async with AsyncSessionLocal() as checkpoint_db:
            stored_child = await checkpoint_db.get(AgentRun, child_id)
            if stored_child is None or stored_child.status not in {"queued", "running"}:
                return
            stored_child.checkpoint = {
                "kind": "subagent",
                "role": role,
                "objective": objective,
                "context": context,
                "allowlist": sorted(allowlist),
                "max_steps": max_steps,
                **runtime_checkpoint,
            }
            await checkpoint_db.commit()

    try:
        async with AsyncSessionLocal() as db:
            child = await db.get(AgentRun, child_id)
            if child is None or child.status == "cancelled" or child.cancel_requested:
                return
            child.status = "running"
            child.started_at = child.started_at or datetime.now(timezone.utc)
            await db.commit()

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_API_BASE)
        report = await run_restricted_child(
            client=client,
            child=child,
            objective=f"{role}: {objective}",
            context=context,
            allowlist=allowlist,
            max_steps=max_steps,
            cancel_check=lambda: child_cancel_requested(child_id),
            checkpoint=checkpoint,
            checkpoint_callback=persist_checkpoint,
        )
        async with AsyncSessionLocal() as db:
            stored = await db.get(AgentRun, child_id)
            if stored is None:
                return
            if stored.cancel_requested or stored.status == "cancelled":
                if stored.status == "cancelled":
                    return
                stored.status = "cancelled"
                stored.checkpoint = None
                stored.completed_at = datetime.now(timezone.utc)
                await db.commit()
                await emit_event(db, child_id, "run.cancelled", "子 Agent 已按要求停止", {"role": role})
                return
            stored.status = "completed"
            stored.output = report
            stored.checkpoint = None
            stored.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await emit_event(db, child_id, "run.completed", report, {"role": role})
            await emit_event(db, stored.parent_run_id, "subagent.completed", f"{role} 已返回结论", {
                "child_run_id": child_id,
                "role": role,
                "status": "completed",
                "report": report,
            })
    except asyncio.CancelledError:
        # Explicit cancellation already commits its terminal state and events.
        raise
    except Exception as exc:
        safe_error = f"子 Agent 运行失败：{type(exc).__name__}"
        async with AsyncSessionLocal() as db:
            stored = await db.get(AgentRun, child_id)
            if stored is None or stored.status == "cancelled":
                return
            stored.status = "failed"
            stored.output = safe_error
            stored.checkpoint = None
            stored.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await emit_event(db, child_id, "run.failed", safe_error, {"role": role, "recoverable": False})
            await emit_event(db, stored.parent_run_id, "subagent.completed", f"{role} 调查失败", {
                "child_run_id": child_id,
                "role": role,
                "status": "failed",
                "report": safe_error,
            })


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
    allowlist = _effective_allowlist(args.tool_whitelist)
    snapshot = await ContextAssembler(ctx.db).build(
        ctx.owner_id,
        plan_id=ctx.plan_id,
        session_id=ctx.session_id,
        run_id=ctx.run_id,
        objective=args.objective,
    )
    child = AgentRun(
        owner_id=ctx.owner_id,
        session_id=ctx.session_id,
        plan_id=ctx.plan_id,
        parent_run_id=ctx.run_id,
        trigger="subagent",
        objective=f"[{args.role}] {args.objective}",
        status="queued",
        model=settings.MODEL_NAME,
        checkpoint={
            "kind": "subagent",
            "role": args.role,
            "objective": args.objective,
            "context": snapshot.markdown,
            "allowlist": sorted(allowlist),
            "max_steps": args.max_steps,
            "step": 0,
            "messages": [],
            "pending_tool_calls": [],
        },
    )
    ctx.db.add(child)
    await ctx.db.commit()
    await ctx.db.refresh(child)
    await emit_event(ctx.db, child.id, "run.started", f"{args.role} 子 Agent 已开始", {
        "parent_run_id": ctx.run_id,
        "role": args.role,
    })
    await emit_event(ctx.db, ctx.run_id, "subagent.started", f"已委派给 {args.role}", {
        "child_run_id": child.id,
        "role": args.role,
        "objective": args.objective,
        "allowlist": sorted(allowlist),
    })
    task = start_tracked_task(
        child.id,
        _run_child_async(child.id, args.role, args.objective, snapshot.markdown, allowlist, args.max_steps)
    )
    _active_child_tasks.add(task)
    task.add_done_callback(_active_child_tasks.discard)
    return {"run_id": child.id, "role": args.role, "status": child.status, "allowlist": sorted(allowlist)}


async def subagent_status(ctx: ToolContext, args: SubagentIdArgs) -> dict:
    child = await ctx.db.get(AgentRun, args.run_id)
    if not _own_child(ctx, child):
        return {"error": "Sub-agent run not found"}
    return {
        "run_id": child.id,
        "status": child.status,
        "objective": child.objective,
        "output": child.output,
    }


async def subagent_join(ctx: ToolContext, args: SubagentJoinArgs) -> dict:
    child = await ctx.db.get(AgentRun, args.run_id)
    if not _own_child(ctx, child):
        return {"error": "Sub-agent run not found"}
    terminal = await wait_for_child(child.id, args.timeout_seconds)
    timed_out = terminal.status not in {"completed", "failed", "cancelled"}
    return {
        "run_id": terminal.id,
        "status": terminal.status,
        "output": terminal.output or "",
        "timed_out": timed_out,
    }


async def subagent_cancel(ctx: ToolContext, args: SubagentIdArgs) -> dict:
    child = await ctx.db.get(AgentRun, args.run_id)
    if not _own_child(ctx, child):
        return {"error": "Sub-agent run not found"}
    cancelled = await cancel_child(child, "父 Agent 取消")
    if cancelled:
        await emit_event(ctx.db, ctx.run_id, "subagent.completed", f"{child.objective.split(']', 1)[0].lstrip('[')} 已停止", {
            "child_run_id": child.id,
            "status": "cancelled",
            "report": "",
        })
    return {"run_id": child.id, "status": "cancelled" if cancelled else child.status}


SUBAGENT_TOOLS = [
    ToolDefinition(
        "subagent_spawn",
        "Spawn one bounded read-only sub-agent for independent investigation; it can only use read-only tools and returns a structured report.",
        SubagentSpawnArgs,
        subagent_spawn,
    ),
    ToolDefinition(
        "subagent_status",
        "Check the status and output of a child sub-agent owned by this run.",
        SubagentIdArgs,
        subagent_status,
    ),
    ToolDefinition(
        "subagent_join",
        "Wait until a child sub-agent finishes and return its report; the parent remains responsible for all writes.",
        SubagentJoinArgs,
        subagent_join,
    ),
    ToolDefinition(
        "subagent_cancel",
        "Cancel a child sub-agent owned by this run.",
        SubagentIdArgs,
        subagent_cancel,
    ),
]
