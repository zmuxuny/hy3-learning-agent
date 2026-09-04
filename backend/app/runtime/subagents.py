from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import settings
from app.core.prompt_envelope import ensure_request_fits
from app.db.database import AsyncSessionLocal
from app.db.uow import rollback as rollback_uow
from app.models import AgentRun
from app.runtime.budget import (
    budget_reason,
    normalize_budget,
    record_model_usage,
    refresh_elapsed,
    reserve_model_call,
    reserve_tool_call,
)
from app.runtime.checkpoints import make_checkpoint, normalize_checkpoint
from app.runtime.events import emit_event
from app.runtime.model_clients import model_call_scope
from app.runtime.retry import is_transient_model_error
from app.runtime.state import (
    RunLeaseLostError,
    claim_run,
    finalize_child,
    maintain_run_lease,
    persist_checkpoint,
    schedule_retry,
    terminate_run,
)
from app.runtime.tasks import cancel_and_wait_tracked_task
from sqlalchemy import select

READ_ONLY_TOOL_NAMES: set[str] = {
    "profile_get",
    "plan_list",
    "plan_get",
    "study_state_get",
    "learning_event_list",
    "resource_list",
    "memory_search",
    "file_list",
    "file_read",
    "web_search",
    "web_open",
    "calendar_list",
    "quiz_get",
    "submission_get",
    "submission_list",
    "planning_intake_get",
}


PLANNING_CHILD_ALLOWLIST: set[str] = {
    "profile_get",
    "memory_search",
    "web_search",
    "web_open",
    "file_list",
    "file_read",
    "calendar_list",
}


SUBAGENT_SYSTEM_PROMPT = (
    "You are a bounded sub-agent inside a personal learning harness. Work only on the assigned question. "
    "You may use the supplied read-only tools, including web search/open when current external evidence "
    "matters. Never request search-result saving and never create or modify application state. Return a "
    "concise evidence-oriented report with sources, assumptions, recommendations, risks, and questions the "
    "lead Agent should resolve. Do not expose chain-of-thought."
)


def subagent_user_prefix(assignment: str) -> str:
    return f"Assignment: {assignment}\n\nShared context:\n"


def _compact_child_observation(result: dict) -> str:
    """Keep research evidence useful without exhausting the child context."""
    limit = min(settings.AGENT_TOOL_MESSAGE_CHAR_LIMIT, 8000)

    def compact(value):
        if isinstance(value, str):
            return value if len(value) <= 2600 else f"{value[:2600]}…[truncated]"
        if isinstance(value, list):
            return [compact(item) for item in value[:12]]
        if isinstance(value, dict):
            return {str(key): compact(item) for key, item in value.items()}
        return value

    encoded = json.dumps(compact(result), ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return encoded
    return json.dumps(
        {"ok": result.get("ok", False), "truncated": True, "observation": encoded[:limit]},
        ensure_ascii=False,
    )


def _bounded_child_event_result(result: dict) -> dict:
    """Persist an inspectable result preview instead of entire fetched pages."""
    encoded = _compact_child_observation(result)
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:  # pragma: no cover - json.dumps above is authoritative
        return {"ok": result.get("ok", False), "preview": encoded}
    return value if isinstance(value, dict) else {"value": value}


async def run_restricted_child(
    *,
    client: Any,
    child: AgentRun,
    objective: str,
    context: str,
    allowlist: set[str],
    max_steps: int,
    parent_call_id: str | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    checkpoint: dict | None = None,
    checkpoint_callback: Callable[[dict], Awaitable[None]] | None = None,
) -> str:
    """Run one bounded read-only child Agent and return its concise report.

    Write tools are never exposed: the effective allowlist is always a subset of
    the read-only capability set. The child emits its own run events and leaves
    all committed writes to the parent Agent.
    """
    from app.tools import ToolContext, execute_tool
    from app.tools.registry import TOOL_MAP

    effective_allowlist = READ_ONLY_TOOL_NAMES.intersection(allowlist)
    schemas = [TOOL_MAP[name].openai_schema() for name in sorted(effective_allowlist)]
    checkpoint = normalize_checkpoint(checkpoint, kind="subagent") or {}
    messages: list[dict] = list(checkpoint.get("messages") or [])
    if not messages:
        messages = [
            {
                "role": "system",
                "content": SUBAGENT_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": subagent_user_prefix(objective) + context,
            },
        ]
    step = int(checkpoint.get("step") or 0)
    current_call: dict | None = checkpoint.get("current_tool_call")
    pending_calls: list[dict] = list(checkpoint.get("remaining_tool_calls") or [])
    tool_calls_used = int(checkpoint.get("tool_calls_used") or 0)
    budget = normalize_budget(checkpoint.get("budget_usage"))
    budget["tool_calls"] = max(int(budget.get("tool_calls") or 0), tool_calls_used)
    tool_call_limit = min(settings.AGENT_MAX_TOOL_CALLS, max(4, max_steps * 4))

    async def save_checkpoint(
        phase: str,
        *,
        freeze_final_text: bool = False,
    ) -> None:
        refresh_elapsed(budget, child.started_at or child.created_at)
        if checkpoint_callback is not None:
            payload = {
                "phase": phase,
                "step": step,
                "messages": messages,
                "current_tool_call": current_call,
                "remaining_tool_calls": pending_calls,
                "tool_calls_used": tool_calls_used,
                "budget_usage": budget,
            }
            if freeze_final_text:
                payload["final_text"] = final_text
            await checkpoint_callback(payload)

    async def stop_for_budget(reason: str) -> str:
        budget["stopped_reason"] = reason
        await child_event(
            child.id,
            "run.budget_exceeded",
            f"子 Agent 预算上限已触发：{reason}",
            {"reason": reason, "budget_usage": dict(budget)},
            event_key=f"run:{child.id}:budget:{reason}",
        )
        return "子 Agent 运行预算已用尽，已安全停止。"

    final_text = ""
    if checkpoint.get("phase") == "finalizing" and "final_text" in checkpoint:
        return str(checkpoint.get("final_text") or "")
    while step < max(1, max_steps):
        if cancel_check is not None and await cancel_check():
            final_text = "子 Agent 已按要求停止。"
            break
        reason = budget_reason(
            budget,
            started_at=child.started_at or child.created_at,
            tool_call_limit=tool_call_limit,
        )
        if reason:
            final_text = await stop_for_budget(reason)
            break
        if current_call is None and not pending_calls:
            ensure_request_fits(messages=messages, tools=schemas)
            reserve_model_call(budget)
            await save_checkpoint("awaiting_model")
            with model_call_scope(
                run_id=child.id,
                parent_run_id=getattr(child, "parent_run_id", None),
                parent_call_id=parent_call_id,
                call_purpose="subagent_decision",
                decision_relevant=True,
                depth=1,
            ):
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=settings.MODEL_NAME,
                        messages=messages,
                        tools=schemas,
                        tool_choice="auto",
                        temperature=settings.MODEL_TEMPERATURE,
                        max_tokens=settings.AGENT_OUTPUT_TOKEN_RESERVE,
                        extra_body={
                            "reasoning_effort": settings.MODEL_REASONING_EFFORT
                        },
                    ),
                    timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                )
            message = response.choices[0].message
            runtime_model_call_id = getattr(
                response,
                "runtime_model_call_id",
                getattr(message, "runtime_model_call_id", None),
            )
            usage = getattr(response, "usage", None)
            record_model_usage(budget, usage)
            tool_calls = getattr(message, "tool_calls", None)
            assistant_payload: dict = {"role": "assistant", "content": message.content or ""}
            reasoning_content = getattr(message, "reasoning_content", None)
            if reasoning_content:
                assistant_payload["reasoning_content"] = reasoning_content
            if tool_calls:
                assistant_payload["tool_calls"] = [call.model_dump() for call in tool_calls]
                calls = [
                    {
                        "id": call.id,
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                        "source_model_call_id": runtime_model_call_id,
                    }
                    for call in tool_calls
                ]
                current_call = calls[0]
                pending_calls = calls[1:]
            messages.append(assistant_payload)
            if not tool_calls:
                final_text = (message.content or "").strip()
                break
            await save_checkpoint("tool_ready")

        while current_call is not None:
            call = dict(current_call)
            reason = budget_reason(
                budget,
                started_at=child.started_at or child.created_at,
                tool_call_limit=tool_call_limit,
            )
            if reason:
                final_text = await stop_for_budget(reason)
                break
            tool_calls_used += 1
            reserve_tool_call(budget, call["name"])
            await save_checkpoint("tool_running")
            try:
                raw_args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                raw_args = {}
            await child_event(child.id, "tool.started", f"调用只读工具 {call['name']}", {
                "tool_call_id": call["id"],
                "name": call["name"],
                "arguments": raw_args,
            })
            if call["name"] not in effective_allowlist:
                result = {
                    "ok": False,
                    "error": "Tool is outside this sub-agent's read-only allowlist",
                    "retryable": False,
                }
            else:
                if call["name"] == "web_search" and raw_args.get("save_results"):
                    result = {"ok": False, "error": "Sub-agents cannot save search results", "retryable": False}
                else:
                    async with AsyncSessionLocal() as tool_db:
                        result = await asyncio.wait_for(
                            execute_tool(
                                call["name"],
                                call["arguments"],
                                ToolContext(
                                    db=tool_db,
                                    owner_id=child.owner_id,
                                    run_id=child.id,
                                    trigger="subagent",
                                    plan_id=child.plan_id,
                                    session_id=child.session_id,
                                    execution_mode=child.execution_mode,
                                    reply_to_intervention_id=child.reply_to_intervention_id,
                                    tool_call_id=call["id"],
                                    source_model_call_id=call.get(
                                        "source_model_call_id"
                                    ),
                                ),
                            ),
                            timeout=settings.AGENT_TOOL_TIMEOUT_SECONDS,
                        )
            await child_event(child.id, "tool.completed", f"只读工具 {call['name']} {'完成' if result.get('ok') else '失败'}", {
                "tool_call_id": call["id"],
                "name": call["name"],
                "arguments": raw_args,
                "result": _bounded_child_event_result(result),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": _compact_child_observation(result),
            })
            current_call = pending_calls.pop(0) if pending_calls else None
            await save_checkpoint("tool_ready" if current_call is not None else "awaiting_model")
            if cancel_check is not None and await cancel_check():
                final_text = "子 Agent 已按要求停止。"
                break
        if final_text:
            break
        step += 1
        await save_checkpoint("awaiting_model")
    if not final_text:
        # A research model may spend every allowed turn calling tools. Reserve
        # one tools-disabled call for the deliverable so a successful child Run
        # never degrades into an empty "completed" result.
        synthesis_messages = [*messages, {
            "role": "user",
            "content": (
                "工具调查阶段已经结束。请仅依据上面的证据直接给出最终调研报告：包含结论、具体资源或依据、"
                "建议、风险与仍需主 Agent 判断的问题。不要再调用工具，不要描述内部思维过程。"
            ),
        }]
        reason = budget_reason(
            budget,
            started_at=child.started_at or child.created_at,
            tool_call_limit=tool_call_limit,
        )
        if reason:
            final_text = await stop_for_budget(reason)
        else:
            ensure_request_fits(messages=synthesis_messages, tools=[])
            reserve_model_call(budget)
            await save_checkpoint("awaiting_model")
            with model_call_scope(
                run_id=child.id,
                parent_run_id=getattr(child, "parent_run_id", None),
                parent_call_id=parent_call_id,
                call_purpose="subagent_synthesis",
                decision_relevant=True,
                depth=1,
            ):
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=settings.MODEL_NAME,
                        messages=synthesis_messages,
                        temperature=settings.MODEL_TEMPERATURE,
                        max_tokens=settings.AGENT_OUTPUT_TOKEN_RESERVE,
                        extra_body={
                            "reasoning_effort": settings.MODEL_REASONING_EFFORT
                        },
                    ),
                    timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                )
            record_model_usage(budget, getattr(response, "usage", None))
            final_text = (response.choices[0].message.content or "").strip()
    final_text = final_text or "子 Agent 已完成调查，但模型没有生成最终报告。"
    await save_checkpoint("finalizing", freeze_final_text=True)
    return final_text


async def child_event(
    run_id: str,
    event_type: str,
    summary: str,
    payload: dict | None = None,
    *,
    event_key: str | None = None,
) -> None:
    async with AsyncSessionLocal() as event_db:
        tool_call_id = (payload or {}).get("tool_call_id")
        event_key = event_key or (
            f"tool:{tool_call_id}:{event_type.rsplit('.', 1)[-1]}"
            if tool_call_id
            else None
        )
        await emit_event(event_db, run_id, event_type, summary, payload, event_key=event_key)


async def execute_durable_child(
    child_id: str,
    *,
    role: str,
    objective: str,
    context: str,
    allowlist: set[str],
    max_steps: int,
    client_factory: Callable[[], Any],
    parent_call_id: str | None = None,
) -> None:
    """Run every child kind through the same claim/checkpoint/retry protocol."""

    client: Any | None = None
    while True:
        lease = await claim_run(AsyncSessionLocal, child_id)
        if lease is None:
            return
        async with AsyncSessionLocal() as db:
            child = await db.get(AgentRun, child_id)
            if child is None or child.cancel_requested:
                await rollback_uow(db)
                await terminate_run(
                    AsyncSessionLocal,
                    child_id,
                    status="cancelled",
                    reason_code="cancel_requested",
                    summary="子 Agent 已按要求停止",
                    lease=lease,
                )
                return
            identity = dict(lease.checkpoint or child.checkpoint or {})
            checkpoint = normalize_checkpoint(identity, kind="subagent") or make_checkpoint(
                kind="subagent",
                phase="awaiting_model",
                step=0,
                messages=[],
                identity={
                    "role": role,
                    "objective": objective,
                    "context": context,
                    "allowlist": sorted(allowlist),
                    "max_steps": max_steps,
                    "action_key": identity.get("action_key"),
                    "assignment_index": identity.get("assignment_index"),
                    "parent_call_id": parent_call_id
                    or identity.get("parent_call_id"),
                },
            )
            checkpoint.update({
                "role": role,
                "objective": objective,
                "context": context,
                "allowlist": sorted(allowlist),
                "max_steps": max_steps,
                "action_key": identity.get("action_key"),
                "assignment_index": identity.get("assignment_index"),
                "parent_call_id": parent_call_id
                or identity.get("parent_call_id"),
            })
            child = await persist_checkpoint(
                db,
                lease,
                checkpoint,
                phase=str(checkpoint.get("phase") or "awaiting_model"),
            )

        async def save_checkpoint(runtime_checkpoint: dict) -> None:
            nonlocal checkpoint
            checkpoint = {
                **checkpoint,
                **runtime_checkpoint,
                "kind": "subagent",
                "role": role,
                "objective": objective,
                "context": context,
                "allowlist": sorted(allowlist),
                "max_steps": max_steps,
            }
            async with AsyncSessionLocal() as checkpoint_db:
                stored = await persist_checkpoint(
                    checkpoint_db,
                    lease,
                    checkpoint,
                    phase=str(runtime_checkpoint.get("phase") or "awaiting_model"),
                )
                checkpoint = normalize_checkpoint(stored.checkpoint, kind="subagent") or checkpoint

        try:
            await child_event(
                child.id,
                "run.started",
                f"{role} 子 Agent 已开始",
                {
                    "parent_run_id": child.parent_run_id,
                    "role": role,
                    "attempt": child.attempt,
                },
                event_key=f"run:{child.id}:started",
            )
            if checkpoint.get("phase") == "finalizing" and "final_text" in checkpoint:
                await finalize_child(
                    AsyncSessionLocal,
                    lease,
                    status="completed",
                    report=str(checkpoint.get("final_text") or ""),
                    role=role,
                )
                return
            client = client or client_factory()
            async with maintain_run_lease(AsyncSessionLocal, lease):
                report = await run_restricted_child(
                    client=client,
                    child=child,
                    objective=f"{role}: {objective}",
                    context=context,
                    allowlist=allowlist,
                    max_steps=max_steps,
                    parent_call_id=checkpoint.get("parent_call_id"),
                    cancel_check=lambda: child_cancel_requested(child_id),
                    checkpoint=checkpoint,
                    checkpoint_callback=save_checkpoint,
                )
            await finalize_child(
                AsyncSessionLocal,
                lease,
                status="completed",
                report=report,
                role=role,
            )
            return
        except asyncio.CancelledError:
            raise
        except RunLeaseLostError:
            return
        except Exception as exc:
            if not is_transient_model_error(exc):
                await finalize_child(
                    AsyncSessionLocal,
                    lease,
                    status="failed",
                    report=f"子 Agent 运行失败：{type(exc).__name__}",
                    role=role,
                    reason_code="child_error",
                )
                return
            if int(checkpoint.get("retry_count") or 0) >= settings.AGENT_RUN_MAX_RETRIES:
                await finalize_child(
                    AsyncSessionLocal,
                    lease,
                    status="failed",
                    report="子 Agent 运行失败：TimeoutError",
                    role=role,
                    reason_code="retry_exhausted",
                )
                return
            delay = settings.AGENT_RUN_RETRY_BACKOFF_SECONDS * (
                2 ** int(checkpoint.get("retry_count") or 0)
            )
            await schedule_retry(
                AsyncSessionLocal,
                lease,
                reason_code="model_timeout",
                retry_after_seconds=delay,
            )
            await asyncio.sleep(delay)


async def child_cancel_requested(child_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        child = await db.get(AgentRun, child_id)
        return bool(child and child.cancel_requested)


async def cancel_child(child: AgentRun, reason: str = "父 Agent 取消") -> bool:
    """Safely stop a queued or running child Run and return True if it was active."""
    if child.status not in {
        "queued", "running", "waiting_approval", "retry_wait", "needs_reconciliation",
    }:
        return False
    terminal = await terminate_run(
        AsyncSessionLocal,
        child.id,
        status="cancelled",
        reason_code="parent_cancelled",
        summary=reason,
    )
    await cancel_and_wait_tracked_task(child.id)
    return terminal is not None and terminal.status == "cancelled"


async def cancel_children_for_parent(parent_run_id: str, reason: str) -> int:
    """Cancel every still-active child so terminal parent Runs leave no orphans."""
    async with AsyncSessionLocal() as db:
        children = list((await db.execute(
            select(AgentRun).where(
                AgentRun.parent_run_id == parent_run_id,
                AgentRun.trigger == "subagent",
                AgentRun.status.in_([
                    "queued", "running", "waiting_approval", "retry_wait",
                    "needs_reconciliation",
                ]),
            )
        )).scalars())
    cancelled = 0
    for child in children:
        if await cancel_child(child, reason):
            cancelled += 1
    return cancelled


async def wait_for_child(child_id: str, timeout_seconds: float = 60.0) -> AgentRun:
    deadline = asyncio.get_event_loop().time() + max(1.0, timeout_seconds)
    while True:
        async with AsyncSessionLocal() as db:
            child = await db.get(AgentRun, child_id)
            if child is None or child.status in {"completed", "failed", "cancelled"}:
                return child
        if asyncio.get_event_loop().time() >= deadline:
            return child
        await asyncio.sleep(0.2)
