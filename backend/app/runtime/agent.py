import asyncio
import json
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select

from app.context import ContextAssembler
from app.context.memory import MemoryManager
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage, PlanProposal, Session
from app.runtime.events import emit_event
from app.runtime.prompt import SYSTEM_PROMPT
from app.runtime.session_titles import generate_session_title, initial_session_title


class AgentModelTimeout(RuntimeError):
    pass


class ToolFailureGuard:
    """Bound repeated failures so one broken capability cannot consume a whole run."""

    def __init__(self, failure_limit: int):
        self.failure_limit = failure_limit
        self.failures: dict[str, int] = {}
        self.blocked: set[str] = set()

    def before_call(self, tool_name: str) -> dict | None:
        if tool_name not in self.blocked:
            return None
        return {
            "ok": False,
            "error": f"{tool_name} is disabled for the remainder of this run after repeated failures",
            "retryable": False,
            "circuit_open": True,
        }

    def observe(self, tool_name: str, result: dict) -> dict:
        if result.get("ok"):
            self.failures.pop(tool_name, None)
            return result
        failure_count = self.failures.get(tool_name, 0) + 1
        self.failures[tool_name] = failure_count
        if failure_count >= self.failure_limit:
            self.blocked.add(tool_name)
            return {
                **result,
                "retryable": False,
                "circuit_open": True,
                "instruction": f"Do not call {tool_name} again in this run; use existing evidence or explain the blocker.",
            }
        return result


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


def _call_to_dict(call) -> dict:
    return {
        "id": call.id,
        "type": getattr(call, "type", "function"),
        "name": call.function.name,
        "arguments": call.function.arguments,
    }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _upsert_card(run_cards: list[dict], card: dict) -> None:
    """Keep one snapshot per card kind so a message never duplicates its artifact."""
    for index, existing in enumerate(run_cards):
        if existing.get("kind") == card.get("kind"):
            run_cards[index] = card
            return
    run_cards.append(card)


async def _proposal_snapshot(proposal_id: str) -> dict | None:
    """Read the committed proposal on a fresh session and shape it like PlanProposalRead."""
    async with AsyncSessionLocal() as snapshot_db:
        row = await snapshot_db.get(PlanProposal, proposal_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "session_id": row.session_id,
            "source_run_id": row.source_run_id,
            "title": row.title,
            "rationale": row.rationale,
            "plan_payload": row.plan_payload,
            "specialist_reports": row.specialist_reports,
            "status": row.status,
            "plan_id": row.plan_id,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }


class AgentRuntime:
    def __init__(self):
        self.client: AsyncOpenAI | None = None

    async def run(self, run_id: str, resume: bool = False, approval_decision: str | None = None) -> None:
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            if not run:
                return
            if resume:
                await self._resume(db, run, approval_decision)
            else:
                await self._start(db, run)

    async def _start(self, db, run: AgentRun) -> None:
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
            memory_ids = [
                item["id"]
                for item in snapshot.source_manifest
                if item.get("type") == "memory"
            ]
            await emit_event(
                db,
                run.id,
                "context.built",
                "已组装本次运行所需的学习上下文",
                {
                    "snapshot_id": snapshot.id,
                    "estimated_tokens": snapshot.estimated_tokens,
                    "memory_ids": memory_ids,
                },
            )

            if session and run.trigger in {"user_message", "email_reply"}:
                existing_user_message = (await db.execute(
                    select(ChatMessage.id).where(
                        ChatMessage.session_id == session.id,
                        ChatMessage.run_id == run.id,
                        ChatMessage.role == "user",
                    ).limit(1)
                )).scalar_one_or_none()
                if existing_user_message is None:
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
            await self._loop(db, run, messages, start_step=0, pending_calls=[], granted=set(), session=session)
        except Exception as exc:
            await self._fail(db, run.id, exc)

    async def _resume(self, db, run: AgentRun, approval_decision: str | None) -> None:
        approval = run.pending_approval
        checkpoint = run.checkpoint or {}
        messages: list[dict] = list(checkpoint.get("messages") or [])
        start_step = int(checkpoint.get("step") or 0)
        pending_calls: list[dict] = list(checkpoint.get("pending_tool_calls") or [])
        granted: set[str] = set()

        if approval is not None:
            run.pending_approval = None
            decision = approval_decision or "approve"
            if decision == "approve":
                pending_calls = [approval["tool_call"]] + list(approval.get("remaining_tool_calls") or [])
                granted.add(approval["tool_call"]["id"])
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": approval["tool_call"]["id"],
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": "用户拒绝了该操作",
                            "approval": "rejected",
                            "retryable": True,
                        },
                        ensure_ascii=False,
                    ),
                })
                pending_calls = list(approval.get("remaining_tool_calls") or [])

        run.status = "running"
        run.checkpoint = None
        await db.commit()
        await emit_event(db, run.id, "run.resumed", "从检查点恢复运行", {"approval_decision": approval_decision})
        if approval is not None:
            await emit_event(
                db,
                run.id,
                "approval.resolved",
                "用户已批准该操作" if decision == "approve" else "用户拒绝了该操作",
                {"decision": decision, "tool_name": approval["tool_call"]["name"]},
            )

        try:
            if not settings.OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            if self.client is None:
                self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_API_BASE)
            session = await db.get(Session, run.session_id) if run.session_id else None
            if run.session_id is None and run.trigger in {"heartbeat", "manual_heartbeat", "task_event", "review_due"}:
                messages = await self._refresh_stateless_context(db, run, messages)
            await self._loop(db, run, messages, start_step=start_step, pending_calls=pending_calls, granted=granted, session=session)
        except Exception as exc:
            await self._fail(db, run.id, exc)

    async def _refresh_stateless_context(self, db, run: AgentRun, messages: list[dict]) -> list[dict]:
        """Rebuild the context snapshot for stateless background runs resumed after restart.

        Checkpoints store the markdown assembled before the process stopped; the world may
        have changed (plans deleted, data reset, memories updated). Rebuilding guarantees the
        model observes the current database instead of a stale snapshot.
        """
        snapshot = await ContextAssembler(db).build(
            run.owner_id,
            plan_id=run.plan_id,
            session_id=None,
            run_id=run.id,
            objective=run.objective,
        )
        await db.commit()
        memory_ids = [
            item["id"]
            for item in snapshot.source_manifest
            if item.get("type") == "memory"
        ]
        await emit_event(
            db,
            run.id,
            "context.built",
            "已按当前数据重建上下文",
            {
                "snapshot_id": snapshot.id,
                "estimated_tokens": snapshot.estimated_tokens,
                "memory_ids": memory_ids,
                "refreshed_on_resume": True,
            },
        )
        replacement = f"Trigger: {run.trigger}\nObjective: {run.objective}\n\n{snapshot.markdown}"
        replaced = False
        for index, message in enumerate(messages):
            if message.get("role") == "user" and str(message.get("content", "")).startswith("Trigger:"):
                messages[index] = {**message, "content": replacement}
                replaced = True
                break
        if not replaced:
            messages.insert(1, {"role": "user", "content": replacement})
        return messages

    async def _loop(
        self,
        db,
        run: AgentRun,
        messages: list[dict],
        *,
        start_step: int,
        pending_calls: list[dict],
        granted: set[str],
        session: Session | None,
    ) -> None:
        from app.tools import ToolContext, execute_tool

        final_text = ""
        failure_guard = ToolFailureGuard(settings.AGENT_TOOL_FAILURE_LIMIT)
        step = start_step
        calls = list(pending_calls)
        run_cards: list[dict] = []
        try:
            while step < settings.AGENT_MAX_STEPS:
                await db.refresh(run)
                if run.cancel_requested:
                    run.status = "cancelled"
                    run.checkpoint = None
                    await db.commit()
                    await emit_event(db, run.id, "run.cancelled", "Agent run cancelled")
                    return

                if not calls:
                    budget = self._budget(run)
                    reason = self._budget_reason(run, budget)
                    if reason:
                        budget["stopped_reason"] = reason
                        run.budget_usage = budget
                        final_text = "运行预算已用尽，已安全停止。"
                        await db.commit()
                        await emit_event(
                            db,
                            run.id,
                            "run.budget_exceeded",
                            f"预算上限已触发：{reason}",
                            {"reason": reason, "budget_usage": budget},
                        )
                        break
                    response = await self._call_model(db, run, messages, failure_guard, step)
                    message = response.choices[0].message
                    assistant_payload: dict = {"role": "assistant", "content": message.content or ""}
                    reasoning_content = getattr(message, "reasoning_content", None)
                    if reasoning_content:
                        assistant_payload["reasoning_content"] = reasoning_content
                    if message.tool_calls:
                        assistant_payload["tool_calls"] = [call.model_dump() for call in message.tool_calls]
                        calls = [_call_to_dict(call) for call in message.tool_calls]
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
                    budget = self._budget(run)
                    budget["model_calls"] += 1
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                        budget["prompt_tokens"] += prompt_tokens
                        budget["completion_tokens"] += completion_tokens
                        budget["estimated_cost_usd"] += (
                            prompt_tokens / 1_000_000 * settings.MODEL_INPUT_PRICE_PER_1M
                            + completion_tokens / 1_000_000 * settings.MODEL_OUTPUT_PRICE_PER_1M
                        )
                    run.budget_usage = budget

                call = calls.pop(0)
                budget = self._budget(run)
                reason = self._budget_reason(run, budget)
                if reason:
                    budget["stopped_reason"] = reason
                    run.budget_usage = budget
                    final_text = "运行预算已用尽，已安全停止。"
                    await db.commit()
                    await emit_event(
                        db,
                        run.id,
                        "run.budget_exceeded",
                        f"预算上限已触发：{reason}",
                        {"reason": reason, "budget_usage": budget},
                    )
                    break
                budget["tool_calls"] += 1
                if call["name"] in {"web_search", "web_open"}:
                    budget["network_requests"] += 1
                run.budget_usage = budget
                run.checkpoint = {"step": step, "messages": messages, "pending_tool_calls": calls}
                await db.commit()

                await emit_event(
                    db,
                    run.id,
                    "tool.started",
                    f"调用工具 {call['name']}",
                    {"tool_call_id": call["id"], "name": call["name"]},
                )
                result = failure_guard.before_call(call["name"])
                if result is None:
                    async with AsyncSessionLocal() as tool_db:
                        tool_timeout = (
                            max(settings.AGENT_TOOL_TIMEOUT_SECONDS, settings.AGENT_MODEL_TIMEOUT_SECONDS + 20)
                            if call["name"] == "planning_delegate"
                            else settings.AGENT_TOOL_TIMEOUT_SECONDS
                        )
                        result = await asyncio.wait_for(
                            execute_tool(
                                call["name"],
                                call["arguments"],
                                ToolContext(
                                    db=tool_db,
                                    owner_id=run.owner_id,
                                    run_id=run.id,
                                    trigger=run.trigger,
                                    plan_id=run.plan_id,
                                    session_id=session.id if session else None,
                                    approval_granted=call["id"] in granted,
                                ),
                            ),
                            timeout=tool_timeout,
                        )
                result = failure_guard.observe(call["name"], result)
                await emit_event(
                    db,
                    run.id,
                    "tool.completed",
                    f"工具 {call['name']} {'完成' if result['ok'] else '失败'}",
                    {"tool_call_id": call["id"], "name": call["name"], "result": result},
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": _compact_tool_message(result),
                    }
                )
                data = result.get("data") or {}
                if result.get("ok") and call["name"] == "planning_intake_update" and data.get("open_questions"):
                    _upsert_card(run_cards, {
                        "kind": "planning_questions",
                        "source_run_id": run.id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "intake": {**data, "source_run_id": run.id},
                    })
                if result.get("ok") and call["name"] == "plan_proposal_create" and data.get("proposal_id"):
                    proposal_snapshot = await _proposal_snapshot(str(data["proposal_id"]))
                    if proposal_snapshot:
                        _upsert_card(run_cards, {
                            "kind": "plan_proposal",
                            "source_run_id": run.id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "proposal": proposal_snapshot,
                        })
                if data.get("approval_required") and data.get("blocking"):
                    run.pending_approval = {
                        "tool_call": call,
                        "remaining_tool_calls": calls,
                        "reason": data.get("reason", "需要用户确认"),
                        "step": step,
                    }
                    run.status = "waiting_approval"
                    await db.commit()
                    await emit_event(
                        db,
                        run.id,
                        "approval.required",
                        data.get("reason", "需要用户确认"),
                        {**data, "tool_name": call["name"], "blocking": True},
                    )
                    return
                if data.get("approval_required"):
                    await emit_event(db, run.id, "approval.required", data.get("reason", "需要用户确认"), data)
                if data.get("operation_id"):
                    await emit_event(
                        db,
                        run.id,
                        "operation.committed",
                        f"{call['name']} 的修改已记录，可在操作记录中撤销",
                        {"operation_id": data["operation_id"], "tool": call["name"]},
                    )
                if call["name"] == "notification_send" and result.get("ok") and not data.get("blocked"):
                    await emit_event(db, run.id, "notification.sent", "学习提醒已进入通知渠道", data)

                run.checkpoint = {"step": step, "messages": messages, "pending_tool_calls": calls}
                await db.commit()
                if not calls:
                    step += 1
            else:
                final_text = "本次运行达到最大工具轮次，已安全停止。"

            run.checkpoint = None
            run.pending_approval = None
            run.output = final_text
            if session and final_text:
                message_metadata = {"cards": run_cards} if run_cards else {}
                db.add(ChatMessage(
                    session_id=session.id,
                    run_id=run.id,
                    role="assistant",
                    content=final_text,
                    message_metadata=message_metadata,
                ))
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
            await self._fail(db, run.id, exc)

    async def _call_model(self, db, run: AgentRun, messages: list[dict], failure_guard: ToolFailureGuard, step: int):
        from app.tools import openai_tools

        response = None
        for attempt in range(settings.AGENT_MODEL_RETRY_ATTEMPTS):
            try:
                model_tools = [
                    tool
                    for tool in openai_tools()
                    if tool["function"]["name"] not in failure_guard.blocked
                ]
                request = {
                    "model": settings.MODEL_NAME,
                    "messages": messages,
                    "temperature": settings.MODEL_TEMPERATURE,
                    "extra_body": {"reasoning_effort": settings.MODEL_REASONING_EFFORT},
                }
                if model_tools:
                    request.update({"tools": model_tools, "tool_choice": "auto"})
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**request),
                    timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                )
                break
            except TimeoutError as exc:
                if attempt + 1 >= settings.AGENT_MODEL_RETRY_ATTEMPTS:
                    raise AgentModelTimeout("模型连续响应超时") from exc
                await emit_event(
                    db,
                    run.id,
                    "run.retrying",
                    "模型响应超时，正在重新连接并保留当前进度",
                    {"attempt": attempt + 2, "max_attempts": settings.AGENT_MODEL_RETRY_ATTEMPTS},
                )
        if response is None:
            raise AgentModelTimeout("模型未返回响应")
        return response

    async def _fail(self, db, run_id: str, exc: Exception) -> None:
        """Mark a run as failed using a fresh session so a broken caller session cannot cascade."""
        try:
            async with AsyncSessionLocal() as failure_db:
                run = await failure_db.get(AgentRun, run_id)
                if not run:
                    return
                run.status = "failed"
                run.completed_at = datetime.now(timezone.utc)
                await failure_db.commit()
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
                    failure_db,
                    run_id,
                    "run.failed",
                    summary,
                    {"code": error_code, "technical_error": f"{type(exc).__name__}: {exc}"},
                )
        except Exception as record_error:
            # If even the failure record cannot be written (e.g. readonly DB), surface it loudly.
            print(
                f"[learning-agent] run {run_id} failed ({type(exc).__name__}: {exc}) "
                f"and failure record also failed ({type(record_error).__name__}: {record_error})",
                flush=True,
            )

    @staticmethod
    def _default_budget() -> dict:
        return {
            "model_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tool_calls": 0,
            "network_requests": 0,
            "elapsed_ms": 0,
            "estimated_cost_usd": 0.0,
            "stopped_reason": "",
        }

    @staticmethod
    def _budget(run: AgentRun) -> dict:
        if not run.budget_usage:
            return AgentRuntime._default_budget()
        return {**AgentRuntime._default_budget(), **run.budget_usage}

    @staticmethod
    def _budget_reason(run: AgentRun, budget: dict) -> str | None:
        started = run.started_at or run.created_at
        if started:
            budget["elapsed_ms"] = int((datetime.now(timezone.utc) - _aware(started)).total_seconds() * 1000)
        if settings.AGENT_MAX_ELAPSED_SECONDS and budget["elapsed_ms"] >= settings.AGENT_MAX_ELAPSED_SECONDS * 1000:
            return "elapsed_limit"
        if budget["model_calls"] >= settings.AGENT_MAX_MODEL_CALLS:
            return "model_call_limit"
        if budget["tool_calls"] >= settings.AGENT_MAX_TOOL_CALLS:
            return "tool_call_limit"
        if (
            settings.AGENT_MAX_ESTIMATED_COST_USD > 0
            and budget["estimated_cost_usd"] >= settings.AGENT_MAX_ESTIMATED_COST_USD
        ):
            return "cost_limit"
        return None

    async def _ensure_session(self, db, run: AgentRun) -> Session | None:
        if run.session_id:
            return await db.get(Session, run.session_id)
        if run.trigger not in {"user_message", "email_reply"}:
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
