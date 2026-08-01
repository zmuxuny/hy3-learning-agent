import asyncio
import json
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select

from app.context import ContextAssembler
from app.context.memory import MemoryManager
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage, Session
from app.runtime.events import emit_event
from app.runtime.prompt import SYSTEM_PROMPT
from app.runtime.session_titles import generate_session_title, initial_session_title
from app.tools import ToolContext, execute_tool, openai_tools


class AgentModelTimeout(RuntimeError):
    pass


def _compact_tool_message(result: dict) -> str:
    """Keep model observations bounded without changing the persisted trace payload."""
    limit = settings.AGENT_TOOL_MESSAGE_CHAR_LIMIT

    def compact(value):
        if isinstance(value, str):
            return value if len(value) <= 3000 else f"{value[:3000]}…[truncated]"
        if isinstance(value, list):
            return [compact(item) for item in value[:20]]
        if isinstance(value, dict):
            return {str(key): compact(item) for key, item in value.items()}
        return value

    payload = json.dumps(compact(result), ensure_ascii=False, default=str)
    if len(payload) <= limit:
        return payload
    return json.dumps(
        {"ok": result.get("ok", False), "truncated": True, "observation": payload[:limit]},
        ensure_ascii=False,
    )


class AgentRuntime:
    def __init__(self):
        self.client: AsyncOpenAI | None = None

    async def run(self, run_id: str) -> None:
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            if not run:
                return
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            await db.commit()
            await emit_event(db, run.id, "run.started", "Agent run started", {"trigger": run.trigger})

            try:
                if not settings.OPENAI_API_KEY:
                    raise RuntimeError("OPENAI_API_KEY is not configured")
                if self.client is None:
                    self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_API_BASE)

                session = await self._ensure_session(db, run)
                snapshot = await ContextAssembler(db).build(
                    run.owner_id,
                    plan_id=run.plan_id,
                    session_id=session.id if session else None,
                    run_id=run.id,
                    objective=run.objective,
                )
                await db.commit()
                await emit_event(
                    db,
                    run.id,
                    "context.built",
                    "已组装本次运行所需的学习上下文",
                    {"snapshot_id": snapshot.id, "estimated_tokens": snapshot.estimated_tokens},
                )

                if session and run.trigger == "user_message":
                    db.add(ChatMessage(session_id=session.id, run_id=run.id, role="user", content=run.objective))
                    session.updated_at = datetime.now(timezone.utc)
                    await db.commit()

                messages: list[dict] = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Trigger: {run.trigger}\nObjective: {run.objective}\n\n{snapshot.markdown}",
                    },
                ]

                final_text = ""
                for step in range(settings.AGENT_MAX_STEPS):
                    await db.refresh(run)
                    if run.cancel_requested:
                        run.status = "cancelled"
                        await db.commit()
                        await emit_event(db, run.id, "run.cancelled", "Agent run cancelled")
                        return

                    response = None
                    for attempt in range(settings.AGENT_MODEL_RETRY_ATTEMPTS):
                        try:
                            response = await asyncio.wait_for(
                                self.client.chat.completions.create(
                                    model=settings.MODEL_NAME,
                                    messages=messages,
                                    tools=openai_tools(),
                                    tool_choice="auto",
                                    temperature=settings.MODEL_TEMPERATURE,
                                    extra_body={"reasoning_effort": settings.MODEL_REASONING_EFFORT},
                                ),
                                timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                            )
                            break
                        except TimeoutError as exc:
                            if attempt + 1 >= settings.AGENT_MODEL_RETRY_ATTEMPTS:
                                raise AgentModelTimeout("模型连续响应超时") from exc
                            await emit_event(
                                db,
                                run_id,
                                "run.retrying",
                                "模型响应超时，正在重新连接并保留当前进度",
                                {"attempt": attempt + 2, "max_attempts": settings.AGENT_MODEL_RETRY_ATTEMPTS},
                            )
                    if response is None:
                        raise AgentModelTimeout("模型未返回响应")
                    message = response.choices[0].message
                    assistant_payload: dict = {"role": "assistant", "content": message.content or ""}
                    reasoning_content = getattr(message, "reasoning_content", None)
                    if reasoning_content:
                        # TokenHub requires this field to be replayed between tool rounds.
                        # It is deliberately not persisted or exposed in RunEvent.
                        assistant_payload["reasoning_content"] = reasoning_content
                    if message.tool_calls:
                        assistant_payload["tool_calls"] = [call.model_dump() for call in message.tool_calls]
                    messages.append(assistant_payload)

                    if message.content:
                        await emit_event(
                            db,
                            run.id,
                            "assistant.status" if message.tool_calls else "assistant.message",
                            message.content,
                            {"step": step + 1},
                        )

                    if not message.tool_calls:
                        final_text = message.content or ""
                        break

                    for call in message.tool_calls:
                        await emit_event(
                            db,
                            run.id,
                            "tool.started",
                            f"调用工具 {call.function.name}",
                            {"tool_call_id": call.id, "name": call.function.name},
                        )
                        async with AsyncSessionLocal() as tool_db:
                            result = await asyncio.wait_for(
                                execute_tool(
                                    call.function.name,
                                    call.function.arguments,
                                    ToolContext(
                                        db=tool_db,
                                        owner_id=run.owner_id,
                                        run_id=run_id,
                                        trigger=run.trigger,
                                        plan_id=run.plan_id,
                                        session_id=session.id if session else None,
                                    ),
                                ),
                                timeout=settings.AGENT_TOOL_TIMEOUT_SECONDS,
                            )
                        await emit_event(
                            db,
                            run.id,
                            "tool.completed",
                            f"工具 {call.function.name} {'完成' if result['ok'] else '失败'}",
                            {"tool_call_id": call.id, "name": call.function.name, "result": result},
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": _compact_tool_message(result),
                            }
                        )
                        data = result.get("data") or {}
                        if data.get("approval_required"):
                            await emit_event(db, run.id, "approval.required", data.get("reason", "需要用户确认"), data)
                        if data.get("operation_id"):
                            await emit_event(
                                db,
                                run.id,
                                "operation.committed",
                                f"{call.function.name} 的修改已记录，可在操作记录中撤销",
                                {"operation_id": data["operation_id"], "tool": call.function.name},
                            )
                        if call.function.name == "notification_send" and not data.get("blocked"):
                            await emit_event(db, run.id, "notification.sent", "学习提醒已进入通知渠道", data)
                else:
                    final_text = "本次运行达到最大工具轮次，已安全停止。"

                if session and final_text:
                    db.add(ChatMessage(session_id=session.id, run_id=run.id, role="assistant", content=final_text))
                    session.updated_at = datetime.now(timezone.utc)
                    await db.flush()
                    await generate_session_title(
                        session,
                        objective=run.objective,
                        answer=final_text,
                        client=self.client,
                    )
                    await MemoryManager(db).compress_session(session, self.client)
                run.status = "completed"
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
                await emit_event(db, run.id, "run.completed", final_text or "Agent run completed")
            except Exception as exc:
                await db.rollback()
                run = await db.get(AgentRun, run_id)
                if run:
                    run.status = "failed"
                    run.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                    if isinstance(exc, AgentModelTimeout):
                        summary = "模型暂时没有响应。本轮已执行的工具结果和会话内容均已保留，可以直接重试。"
                        error_code = "model_timeout"
                    elif isinstance(exc, TimeoutError):
                        summary = "某个工具执行超时。本轮状态已安全保留，请重试或查看运行详情。"
                        error_code = "tool_timeout"
                    else:
                        summary = "运行遇到内部错误，状态已安全保留。请重试；技术详情已记录在运行轨迹中。"
                        error_code = "internal_error"
                    await emit_event(
                        db,
                        run_id,
                        "run.failed",
                        summary,
                        {"code": error_code, "technical_error": f"{type(exc).__name__}: {exc}"},
                    )

    async def _ensure_session(self, db, run: AgentRun) -> Session | None:
        if run.session_id:
            return await db.get(Session, run.session_id)
        if run.trigger != "user_message":
            return None
        session = Session(
            owner_id=run.owner_id,
            plan_id=run.plan_id,
            title=initial_session_title(run.objective),
        )
        db.add(session)
        await db.flush()
        run.session_id = session.id
        await db.commit()
        return session
