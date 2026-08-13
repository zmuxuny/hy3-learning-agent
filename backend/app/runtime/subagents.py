from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun
from app.runtime.events import emit_event
from app.runtime.tasks import cancel_tracked_task


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

    schemas = [TOOL_MAP[name].openai_schema() for name in sorted(allowlist)]
    checkpoint = checkpoint or {}
    messages: list[dict] = list(checkpoint.get("messages") or [])
    if not messages:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded sub-agent inside a personal learning harness. Work only on the assigned question. "
                    "You may use the supplied read-only tools, including web search/open when current external evidence "
                    "matters. Never request search-result saving and never create or modify application state. Return a "
                    "concise evidence-oriented report with sources, assumptions, recommendations, risks, and questions the "
                    "lead Agent should resolve. Do not expose chain-of-thought."
                ),
            },
            {
                "role": "user",
                "content": f"Assignment: {objective}\n\nShared context:\n{context}",
            },
        ]
    step = int(checkpoint.get("step") or 0)
    pending_calls: list[dict] = list(checkpoint.get("pending_tool_calls") or [])
    tool_calls_used = int(checkpoint.get("tool_calls_used") or 0)
    tool_call_limit = min(settings.AGENT_MAX_TOOL_CALLS, max(4, max_steps * 4))

    async def save_checkpoint() -> None:
        if checkpoint_callback is not None:
            await checkpoint_callback({
                "step": step,
                "messages": messages,
                "pending_tool_calls": pending_calls,
                "tool_calls_used": tool_calls_used,
            })

    final_text = ""
    while step < max(1, max_steps):
        if cancel_check is not None and await cancel_check():
            final_text = "子 Agent 已按要求停止。"
            break
        if not pending_calls:
            await save_checkpoint()
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
                pending_calls = [
                    {
                        "id": call.id,
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    }
                    for call in tool_calls
                ]
            messages.append(assistant_payload)
            if not tool_calls:
                final_text = (message.content or "").strip()
                break
            await save_checkpoint()

        while pending_calls:
            call = pending_calls.pop(0)
            try:
                raw_args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                raw_args = {}
            await child_event(child.id, "tool.started", f"调用只读工具 {call['name']}", {
                "tool_call_id": call["id"],
                "name": call["name"],
                "arguments": raw_args,
            })
            tool_calls_used += 1
            if tool_calls_used > tool_call_limit:
                result = {
                    "ok": False,
                    "error": "Sub-agent tool budget reached; synthesize from collected evidence",
                    "retryable": False,
                    "budget_exceeded": True,
                }
            elif call["name"] not in allowlist:
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
                        result = await execute_tool(
                            call["name"],
                            call["arguments"],
                            ToolContext(
                                db=tool_db,
                                owner_id=child.owner_id,
                                run_id=child.id,
                                trigger="subagent",
                                plan_id=child.plan_id,
                                session_id=child.session_id,
                                tool_call_id=call["id"],
                            ),
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
            await save_checkpoint()
            if cancel_check is not None and await cancel_check():
                final_text = "子 Agent 已按要求停止。"
                break
        if final_text == "子 Agent 已按要求停止。":
            break
        step += 1
        await save_checkpoint()
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
        await save_checkpoint()
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.MODEL_NAME,
                messages=synthesis_messages,
                temperature=settings.MODEL_TEMPERATURE,
                extra_body={"reasoning_effort": settings.MODEL_REASONING_EFFORT},
            ),
            timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
        )
        final_text = (response.choices[0].message.content or "").strip()
    return final_text or "子 Agent 已完成调查，但模型没有生成最终报告。"


async def child_event(run_id: str, event_type: str, summary: str, payload: dict | None = None) -> None:
    async with AsyncSessionLocal() as event_db:
        await emit_event(event_db, run_id, event_type, summary, payload)


async def child_cancel_requested(child_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        child = await db.get(AgentRun, child_id)
        return bool(child and child.cancel_requested)


async def cancel_child(child: AgentRun, reason: str = "父 Agent 取消") -> bool:
    """Safely stop a queued or running child Run and return True if it was active."""
    if child.status not in {"queued", "running"}:
        return False
    async with AsyncSessionLocal() as db:
        stored = await db.get(AgentRun, child.id)
        if stored is None or stored.status not in {"queued", "running"}:
            return False
        stored.cancel_requested = True
        stored.status = "cancelled"
        stored.completed_at = datetime.now(timezone.utc)
        await db.commit()
        cancel_tracked_task(child.id)
        await emit_event(db, child.id, "run.cancelled", reason, {"parent_run_id": child.parent_run_id})
        return True


async def cancel_children_for_parent(parent_run_id: str, reason: str) -> int:
    """Cancel every still-active child so terminal parent Runs leave no orphans."""
    async with AsyncSessionLocal() as db:
        children = list((await db.execute(
            select(AgentRun).where(
                AgentRun.parent_run_id == parent_run_id,
                AgentRun.trigger == "subagent",
                AgentRun.status.in_(["queued", "running"]),
            )
        )).scalars())
    cancelled = 0
    for child in children:
        if await cancel_child(child, reason):
            cancelled += 1
            async with AsyncSessionLocal() as event_db:
                await emit_event(event_db, parent_run_id, "subagent.completed", "子 Agent 随父 Run 停止", {
                    "child_run_id": child.id,
                    "status": "cancelled",
                    "report": "",
                })
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
