import json
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select

from app.context import ContextAssembler
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage, Session
from app.runtime.events import emit_event
from app.runtime.prompt import SYSTEM_PROMPT
from app.tools import ToolContext, execute_tool, openai_tools


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

                    response = await self.client.chat.completions.create(
                        model=settings.MODEL_NAME,
                        messages=messages,
                        tools=openai_tools(),
                        tool_choice="auto",
                        temperature=settings.MODEL_TEMPERATURE,
                        extra_body={"reasoning_effort": settings.MODEL_REASONING_EFFORT},
                    )
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
                        result = await execute_tool(
                            call.function.name,
                            call.function.arguments,
                            ToolContext(db=db, owner_id=run.owner_id, run_id=run.id, trigger=run.trigger),
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
                                "content": json.dumps(result, ensure_ascii=False, default=str),
                            }
                        )
                else:
                    final_text = "本次运行达到最大工具轮次，已安全停止。"

                if session and final_text:
                    db.add(ChatMessage(session_id=session.id, run_id=run.id, role="assistant", content=final_text))
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
                    await emit_event(
                        db,
                        run.id,
                        "run.failed",
                        "Agent run failed",
                        {"error": f"{type(exc).__name__}: {exc}"},
                    )

    async def _ensure_session(self, db, run: AgentRun) -> Session | None:
        if run.session_id:
            return await db.get(Session, run.session_id)
        if run.trigger != "user_message":
            return None
        session = Session(
            owner_id=run.owner_id,
            plan_id=run.plan_id,
            title=run.objective[:80] or "New conversation",
        )
        db.add(session)
        await db.flush()
        run.session_id = session.id
        await db.commit()
        return session
