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


async def run_restricted_child(
    *,
    client: Any,
    child: AgentRun,
    objective: str,
    context: str,
    allowlist: set[str],
    max_steps: int,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """Run one bounded read-only child Agent and return its concise report.

    Write tools are never exposed: the effective allowlist is always a subset of
    the read-only capability set. The child emits its own run events and leaves
    all committed writes to the parent Agent.
    """
    from app.tools import ToolContext, execute_tool
    from app.tools.registry import TOOL_MAP

    schemas = [TOOL_MAP[name].openai_schema() for name in sorted(allowlist)]
    messages: list[dict] = [
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
    final_text = ""
    for _ in range(max(1, max_steps)):
        if cancel_check is not None and await cancel_check():
            final_text = "子 Agent 已按要求停止。"
            break
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
            await child_event(child.id, "tool.started", f"调用只读工具 {call.function.name}", {
                "tool_call_id": call.id,
                "name": call.function.name,
            })
            if call.function.name not in allowlist:
                result = {
                    "ok": False,
                    "error": "Tool is outside this sub-agent's read-only allowlist",
                    "retryable": False,
                }
            else:
                try:
                    raw_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    raw_args = {}
                if call.function.name == "web_search" and raw_args.get("save_results"):
                    result = {"ok": False, "error": "Sub-agents cannot save search results", "retryable": False}
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
            await child_event(child.id, "tool.completed", f"只读工具 {call.function.name} {'完成' if result.get('ok') else '失败'}", {
                "tool_call_id": call.id,
                "name": call.function.name,
                "result": result,
            })
            content = json.dumps(result, ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content[:12000]})
            if cancel_check is not None and await cancel_check():
                final_text = "子 Agent 已按要求停止。"
                break
        if final_text == "子 Agent 已按要求停止。":
            break
    return final_text or "子 Agent 已完成受限调查，但没有返回可用的总结。"


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
        await emit_event(db, child.id, "run.cancelled", reason, {"parent_run_id": child.parent_run_id})
        return True


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
