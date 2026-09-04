import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from app.context import ContextAssembler
from app.context.memory import MemoryManager
from app.context.provenance import canonical_digest
from app.core.config import settings
from app.core.prompt_envelope import ensure_request_fits
from app.core.redaction import redact_data, redact_text
from app.core.time import canonical_utc, utc_now
from app.db.database import AsyncSessionLocal
from app.db.uow import commit as commit_uow
from app.db.uow import flush as flush_uow
from app.db.uow import rollback as rollback_uow
from app.models import AgentRun, ChatMessage, PlanProposal, Session
from app.runtime.budget import (
    budget_reason as shared_budget_reason,
)
from app.runtime.budget import (
    default_budget,
    normalize_budget,
    record_model_usage,
    refresh_elapsed,
    reserve_model_call,
    reserve_tool_call,
)
from app.runtime.checkpoints import make_checkpoint, normalize_checkpoint
from app.runtime.events import emit_event, publish_stream_event
from app.runtime.model_clients import (
    create_model_client,
    model_call_scope,
)
from app.runtime.prompt import SYSTEM_PROMPT
from app.runtime.retry import is_transient_model_error
from app.runtime.session_titles import generate_session_title, initial_session_title
from app.runtime.state import (
    RunLease,
    RunLeaseLostError,
    RunStateError,
    claim_run,
    consume_pending_steers,
    current_approval,
    finalize_run,
    maintain_run_lease,
    pause_for_approval,
    persist_checkpoint,
    prepare_finalization,
    schedule_retry,
    terminate_run,
)
from app.runtime.tasks import start_tracked_task
from sqlalchemy import select


class AgentModelTimeout(RuntimeError):
    pass


class AgentModelProviderError(RuntimeError):
    """A provider call failed after the request passed local preflight."""

    def __init__(self, cause: Exception):
        self.cause = cause
        super().__init__(f"model provider failed: {type(cause).__name__}")


class _StreamingToolCall:
    """Accumulate an OpenAI-compatible tool call from streamed deltas."""

    def __init__(self) -> None:
        self.id = ""
        self.type = "function"
        self.name = ""
        self.arguments = ""

    @property
    def function(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.name, arguments=self.arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.name, "arguments": self.arguments},
        }


class _StreamingMessage:
    def __init__(
        self,
        content: str,
        reasoning_content: str,
        tool_calls: list[_StreamingToolCall],
        *,
        runtime_model_call_id: str | None = None,
    ) -> None:
        self.content = content
        self.reasoning_content = reasoning_content or None
        self.tool_calls = tool_calls or None
        self.runtime_model_call_id = runtime_model_call_id


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


def tool_timeout_seconds(call: dict) -> float:
    """Align outer runtime deadlines with tools that intentionally wait."""
    if call.get("name") == "planning_delegate":
        return max(
            settings.AGENT_TOOL_TIMEOUT_SECONDS,
            settings.AGENT_MODEL_TIMEOUT_SECONDS * 5 + 30,
        )
    if call.get("name") == "subagent_join":
        try:
            payload = json.loads(call.get("arguments") or "{}")
            requested = float(payload.get("timeout_seconds", 60))
        except (TypeError, ValueError, json.JSONDecodeError):
            requested = 60
        requested = min(max(requested, 1), 300)
        return max(settings.AGENT_TOOL_TIMEOUT_SECONDS, requested + 5)
    return settings.AGENT_TOOL_TIMEOUT_SECONDS


def _compact_tool_message(result: dict) -> str:
    """Keep model observations bounded without changing the persisted trace payload."""
    limit = settings.AGENT_TOOL_MESSAGE_CHAR_LIMIT
    safe_result = redact_data(result)

    def compact(value):
        if isinstance(value, str):
            return value if len(value) <= 3000 else f"{value[:3000]}…[truncated]"
        if isinstance(value, list):
            return [compact(item) for item in value[:20]]
        if isinstance(value, dict):
            return {str(key): compact(item) for key, item in value.items()}
        return value

    payload = json.dumps(compact(safe_result), ensure_ascii=False, default=_json_default)
    if len(payload) <= limit:
        return payload
    return json.dumps(
        {"ok": safe_result.get("ok", False), "truncated": True, "observation": payload[:limit]},
        ensure_ascii=False,
    )


def _call_to_dict(call) -> dict:
    return {
        "id": call.id,
        "type": getattr(call, "type", "function"),
        "name": call.function.name,
        "arguments": call.function.arguments,
    }


def _event_tool_arguments(raw_arguments: str) -> dict:
    """Keep a bounded, structured input snapshot for the expandable Run UI."""
    try:
        value = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {"raw": redact_text(raw_arguments or "")[:2000]}
    value = redact_data(value)
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) > 6000:
        return {"preview": encoded[:6000], "truncated": True}
    return value if isinstance(value, dict) else {"value": value}


def _json_default(value) -> str:
    if isinstance(value, datetime):
        return canonical_utc(value)
    return str(value)


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
            "decided_at": canonical_utc(row.decided_at),
            "created_at": canonical_utc(row.created_at),
            "updated_at": canonical_utc(row.updated_at),
        }


class AgentRuntime:
    def __init__(self):
        self.client = None

    async def run(
        self,
        run_id: str,
        resume: bool = False,
        approval_decision: str | None = None,
    ) -> None:
        """Execute only after winning the durable Run lease.

        ``resume`` and ``approval_decision`` remain accepted for callers from
        older releases, but neither carries state.  Recovery mode and approval
        decisions are derived exclusively from committed database facts.
        """

        del resume, approval_decision
        lease = await claim_run(AsyncSessionLocal, run_id)
        if lease is None:
            return
        async with maintain_run_lease(AsyncSessionLocal, lease):
            async with AsyncSessionLocal() as db:
                run = await db.get(AgentRun, run_id)
                if run is None:  # pragma: no cover - the claimed row cannot vanish
                    return
                try:
                    if not settings.OPENAI_API_KEY:
                        raise RuntimeError("OPENAI_API_KEY is not configured")
                    if self.client is None:
                        self.client = create_model_client()
                    checkpoint = normalize_checkpoint(
                        lease.checkpoint,
                        kind="subagent" if run.trigger == "subagent" else "agent",
                    )
                    if checkpoint is None:
                        checkpoint, session = await self._initialize_run(db, run, lease)
                        event_type = "run.started"
                        summary = "Agent run started"
                    else:
                        session = await db.get(Session, run.session_id) if run.session_id else None
                        checkpoint = await self._restore_approval(db, run, lease, checkpoint)
                        if checkpoint.get("phase") == "finalizing":
                            completed = await finalize_run(AsyncSessionLocal, lease)
                            await self._after_terminal(completed, session)
                            return
                        if (
                            run.session_id is None
                            and run.trigger in {
                                "heartbeat",
                                "manual_heartbeat",
                                "task_event",
                                "review_due",
                            }
                        ):
                            messages, snapshot_id = await self._refresh_stateless_context(
                                db,
                                run,
                                list(checkpoint.get("messages") or []),
                            )
                            checkpoint["messages"] = messages
                            checkpoint["context_snapshot_id"] = snapshot_id
                            stored = await persist_checkpoint(
                                db,
                                lease,
                                checkpoint,
                                phase=str(checkpoint["phase"]),
                            )
                            checkpoint = normalize_checkpoint(stored.checkpoint) or checkpoint
                        event_type = "run.resumed"
                        summary = "从耐久检查点恢复运行"
                    await emit_event(
                        db,
                        run.id,
                        event_type,
                        summary,
                        {"trigger": run.trigger, "attempt": run.attempt},
                        event_key=f"run:{run.id}:{event_type}",
                    )
                    await self._loop(db, run, lease, checkpoint, session=session)
                except RunLeaseLostError:
                    # A cancellation or a newer worker won the durable fence.  Its
                    # committed state is authoritative; this executor stops.
                    await rollback_uow(db)
                except Exception as exc:
                    await self._fail(db, run, lease, exc)

    async def _initialize_run(
        self,
        db,
        run: AgentRun,
        lease: RunLease,
    ) -> tuple[dict, Session | None]:
        session = await self._ensure_session(db, run)
        snapshot = await ContextAssembler(db).build(
            run.owner_id,
            plan_id=run.plan_id,
            session_id=session.id if session else None,
            run_id=run.id,
            objective=run.objective,
            prompt_prefix=f"Trigger: {run.trigger}\nObjective: {run.objective}\n\n",
        )
        await commit_uow(db)
        memory_ids = [
            item["id"] for item in snapshot.source_manifest if item.get("type") == "memory"
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
                "memory_matches": [
                    item for item in snapshot.source_manifest if item.get("type") == "memory"
                ],
            },
            event_key=f"run:{run.id}:context:{snapshot.id}",
        )
        if session and run.trigger in {"user_message", "email_reply"}:
            existing = (await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == session.id,
                    ChatMessage.message_key == f"run:{run.id}:input",
                )
            )).scalars().one_or_none()
            if existing is not None:
                if not existing.content_hash:
                    existing.content_hash = canonical_digest(existing.content)
                if run.reply_to_intervention_id and not existing.reply_to_intervention_id:
                    existing.reply_to_intervention_id = run.reply_to_intervention_id
            if existing is None:
                fallback = (await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.session_id == session.id,
                        ChatMessage.run_id == run.id,
                        ChatMessage.role == "user",
                    ).order_by(ChatMessage.id).limit(1)
                )).scalars().one_or_none()
                if fallback is None:
                    db.add(ChatMessage(
                        session_id=session.id,
                        run_id=run.id,
                        message_key=f"run:{run.id}:input",
                        role="user",
                        content=run.objective,
                        version=1,
                        content_hash=canonical_digest(run.objective),
                        reply_to_intervention_id=run.reply_to_intervention_id,
                    ))
                else:
                    fallback.message_key = f"run:{run.id}:input"
                    if not fallback.content_hash:
                        fallback.content_hash = canonical_digest(fallback.content)
                    if run.reply_to_intervention_id and not fallback.reply_to_intervention_id:
                        fallback.reply_to_intervention_id = run.reply_to_intervention_id
            session.updated_at = utc_now()
            await commit_uow(db)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Trigger: {run.trigger}\nObjective: {run.objective}\n\n{snapshot.markdown}",
            },
        ]
        checkpoint = make_checkpoint(
            kind="agent",
            phase="awaiting_model",
            step=0,
            messages=messages,
            context_snapshot_id=snapshot.id,
            budget_usage=self._budget(run),
            state_version=lease.version,
        )
        run = await persist_checkpoint(
            db,
            lease,
            checkpoint,
            phase="awaiting_model",
        )
        return normalize_checkpoint(run.checkpoint) or checkpoint, session

    async def _restore_approval(
        self,
        db,
        run: AgentRun,
        lease: RunLease,
        checkpoint: dict,
    ) -> dict:
        if not run.pending_approval:
            return checkpoint
        approval = await current_approval(db, run)
        if approval is None or approval.decision == "pending":
            raise RunStateError("approval has no committed decision")
        messages = list(checkpoint.get("messages") or [])
        current = dict(approval.tool_call)
        remaining = list(approval.remaining_tool_calls or [])
        granted: list[str] = list(checkpoint.get("granted_tool_call_ids") or [])
        if approval.decision == "approve":
            if approval.tool_call_id not in granted:
                granted.append(approval.tool_call_id)
            checkpoint["current_tool_call"] = current
        else:
            result = {
                "ok": False,
                "error": "用户拒绝了该操作",
                "approval": "answered" if approval.decision == "answer" else "rejected",
                "retryable": False,
                "note": approval.note,
                "answer": approval.answer,
            }
            messages.append({
                "role": "tool",
                "tool_call_id": approval.tool_call_id,
                "content": json.dumps(result, ensure_ascii=False),
            })
            checkpoint["current_tool_call"] = None
        checkpoint["messages"] = messages
        checkpoint["remaining_tool_calls"] = remaining
        checkpoint["granted_tool_call_ids"] = granted
        checkpoint["phase"] = "tool_ready" if checkpoint.get("current_tool_call") or remaining else "awaiting_model"
        stored = await persist_checkpoint(
            db,
            lease,
            checkpoint,
            phase=checkpoint["phase"],
            clear_approval_id=approval.id,
        )
        return normalize_checkpoint(stored.checkpoint) or checkpoint

    async def _refresh_stateless_context(self, db, run: AgentRun, messages: list[dict]) -> tuple[list[dict], int]:
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
            prompt_prefix=f"Trigger: {run.trigger}\nObjective: {run.objective}\n\n",
        )
        await commit_uow(db)
        memory_ids = [
            item["id"]
            for item in snapshot.source_manifest
            if item.get("type") == "memory"
        ]
        memory_matches = [
            item for item in snapshot.source_manifest if item.get("type") == "memory"
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
                "memory_matches": memory_matches,
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
        return messages, snapshot.id

    async def _loop(
        self,
        db,
        run: AgentRun,
        lease: RunLease,
        checkpoint: dict,
        *,
        session: Session | None,
    ) -> None:
        from app.tools import ToolContext, execute_tool

        failure_guard = ToolFailureGuard(settings.AGENT_TOOL_FAILURE_LIMIT)
        while int(checkpoint.get("step") or 0) < settings.AGENT_MAX_STEPS:
            fresh = await db.get(AgentRun, run.id, populate_existing=True)
            if fresh is None or fresh.status != "running" or fresh.cancel_requested:
                await commit_uow(db)
                return
            run = fresh
            messages = list(checkpoint.get("messages") or [])
            step = int(checkpoint.get("step") or 0)
            current = checkpoint.get("current_tool_call")
            remaining = list(checkpoint.get("remaining_tool_calls") or [])
            cards = list(checkpoint.get("cards") or [])
            budget = normalize_budget(checkpoint.get("budget_usage"))
            reason = self._budget_reason(run, budget)
            if reason:
                budget["stopped_reason"] = reason
                checkpoint["budget_usage"] = budget
                final_text = "运行预算已用尽，已安全停止。"
                await emit_event(
                    db,
                    run.id,
                    "run.budget_exceeded",
                    f"预算上限已触发：{reason}",
                    {"reason": reason, "budget_usage": budget},
                    event_key=f"run:{run.id}:budget:{reason}",
                )
                break

            if current is None and remaining:
                current = remaining.pop(0)
                checkpoint["current_tool_call"] = current
                checkpoint["remaining_tool_calls"] = remaining

            if current is None:
                checkpoint = await consume_pending_steers(
                    AsyncSessionLocal,
                    lease,
                    checkpoint=checkpoint,
                )
                checkpoint["phase"] = "awaiting_model"
                # Reserve the model call before the external wait.  A process
                # killed after the provider accepts the request must not make
                # the persisted budget look as if no call happened.
                reserve_model_call(budget)
                checkpoint["budget_usage"] = budget
                stored = await persist_checkpoint(
                    db,
                    lease,
                    checkpoint,
                    phase="awaiting_model",
                )
                run = stored
                checkpoint = normalize_checkpoint(stored.checkpoint) or checkpoint
                message, usage = await self._call_model(
                    db,
                    run,
                    list(checkpoint.get("messages") or []),
                    failure_guard,
                    step,
                )
                assistant_payload: dict = {
                    "role": "assistant",
                    "content": message.content or "",
                }
                reasoning_content = getattr(message, "reasoning_content", None)
                if reasoning_content:
                    assistant_payload["reasoning_content"] = reasoning_content
                calls: list[dict] = []
                if message.tool_calls:
                    assistant_payload["tool_calls"] = [call.model_dump() for call in message.tool_calls]
                    calls = [_call_to_dict(call) for call in message.tool_calls]
                    source_model_call_id = getattr(
                        message, "runtime_model_call_id", None
                    )
                    for call in calls:
                        call["source_model_call_id"] = source_model_call_id
                messages = list(checkpoint.get("messages") or [])
                messages.append(assistant_payload)
                record_model_usage(budget, usage)
                checkpoint.update({
                    "messages": messages,
                    "current_tool_call": calls[0] if calls else None,
                    "remaining_tool_calls": calls[1:] if calls else [],
                    "budget_usage": budget,
                    "phase": "tool_ready" if calls else "finalizing",
                })
                if calls:
                    if message.content:
                        await emit_event(
                            db,
                            run.id,
                            "assistant.status",
                            message.content,
                            {"step": step + 1},
                            event_key=f"run:{run.id}:step:{step}:assistant-status",
                        )
                    stored = await persist_checkpoint(
                        db,
                        lease,
                        checkpoint,
                        phase="tool_ready",
                    )
                    run = stored
                    checkpoint = normalize_checkpoint(stored.checkpoint) or checkpoint
                    continue
                final_text = message.content or ""
                preparation = await prepare_finalization(
                    AsyncSessionLocal,
                    lease,
                    checkpoint=checkpoint,
                    final_text=final_text,
                )
                checkpoint = preparation.checkpoint
                if preparation.action == "continue":
                    continue
                break

            call = dict(current)
            reserve_tool_call(budget, call["name"])
            checkpoint.update({
                "phase": "tool_running",
                "current_tool_call": call,
                "remaining_tool_calls": remaining,
                "budget_usage": budget,
            })
            stored = await persist_checkpoint(
                db,
                lease,
                checkpoint,
                phase="tool_running",
            )
            run = stored
            checkpoint = normalize_checkpoint(stored.checkpoint) or checkpoint
            await emit_event(
                db,
                run.id,
                "tool.started",
                f"调用工具 {call['name']}",
                {
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "arguments": _event_tool_arguments(call["arguments"]),
                },
                event_key=f"tool:{call['id']}:started",
            )
            result = failure_guard.before_call(call["name"])
            if result is None:
                async with AsyncSessionLocal() as tool_db:
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
                                execution_mode=run.execution_mode,
                                reply_to_intervention_id=run.reply_to_intervention_id,
                                approval_granted=call["id"] in set(
                                    checkpoint.get("granted_tool_call_ids") or []
                                ),
                                tool_call_id=call["id"],
                                source_model_call_id=call.get(
                                    "source_model_call_id"
                                ),
                            ),
                        ),
                        timeout=tool_timeout_seconds(call),
                    )
            result = failure_guard.observe(call["name"], result)
            data = result.get("data") or {}
            if data.get("approval_required") and data.get("blocking"):
                await pause_for_approval(
                    db,
                    lease,
                    checkpoint=checkpoint,
                    tool_call=call,
                    remaining_tool_calls=remaining,
                    reason=data.get("reason", "需要用户确认"),
                )
                return
            if result.get("completion_event_persisted") or data.get("completion_event_persisted"):
                committed_event = result.get("completion_event")
                if isinstance(committed_event, dict):
                    publish_stream_event(run.id, committed_event)
            else:
                await emit_event(
                    db,
                    run.id,
                    "tool.completed",
                    f"工具 {call['name']} {'完成' if result.get('ok') else '失败'}",
                    {
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "arguments": _event_tool_arguments(call["arguments"]),
                        "result": result,
                    },
                    event_key=f"tool:{call['id']}:completed",
                )
            messages = list(checkpoint.get("messages") or [])
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": _compact_tool_message(result),
            })
            if result.get("ok") and call["name"] == "planning_intake_update" and data.get("open_questions"):
                _upsert_card(cards, {
                    "kind": "planning_questions",
                    "source_run_id": run.id,
                    "created_at": canonical_utc(utc_now()),
                    "intake": {**data, "source_run_id": run.id},
                })
            if result.get("ok") and call["name"] == "plan_proposal_create" and data.get("proposal_id"):
                proposal_snapshot = await _proposal_snapshot(str(data["proposal_id"]))
                if proposal_snapshot:
                    _upsert_card(cards, {
                        "kind": "plan_proposal",
                        "source_run_id": run.id,
                        "created_at": canonical_utc(utc_now()),
                        "proposal": proposal_snapshot,
                    })
            if data.get("operation_id"):
                await emit_event(
                    db,
                    run.id,
                    "operation.committed",
                    f"{call['name']} 的修改已记录，可在操作记录中撤销",
                    {"operation_id": data["operation_id"], "tool": call["name"]},
                    event_key=f"tool:{call['id']}:operation",
                )
            if call["name"] == "notification_send" and result.get("ok") and not data.get("blocked"):
                await emit_event(
                    db,
                    run.id,
                    "notification.sent",
                    "学习提醒已进入通知渠道",
                    data,
                    event_key=f"tool:{call['id']}:notification",
                )
            checkpoint.update({
                "messages": messages,
                "current_tool_call": remaining[0] if remaining else None,
                "remaining_tool_calls": remaining[1:] if remaining else [],
                "cards": cards,
                "budget_usage": budget,
                "phase": "tool_ready" if remaining else "awaiting_model",
                "step": step if remaining else step + 1,
            })
            stored = await persist_checkpoint(
                db,
                lease,
                checkpoint,
                phase=checkpoint["phase"],
            )
            run = stored
            checkpoint = normalize_checkpoint(stored.checkpoint) or checkpoint
        else:
            checkpoint["budget_usage"] = normalize_budget(checkpoint.get("budget_usage"))
            preparation = await prepare_finalization(
                AsyncSessionLocal,
                lease,
                checkpoint=checkpoint,
                final_text="本次运行达到最大工具轮次，已安全停止。",
            )
            checkpoint = preparation.checkpoint
            if preparation.action == "continue":
                return await self._loop(db, run, lease, checkpoint, session=session)

        budget = normalize_budget(checkpoint.get("budget_usage"))
        self._refresh_elapsed(run, budget, ended_at=utc_now())
        checkpoint["budget_usage"] = budget
        if checkpoint.get("phase") != "finalizing":
            preparation = await prepare_finalization(
                AsyncSessionLocal,
                lease,
                checkpoint=checkpoint,
                final_text=locals().get("final_text", ""),
            )
            checkpoint = preparation.checkpoint
            if preparation.action == "continue":
                return await self._loop(db, run, lease, checkpoint, session=session)
        completed = await finalize_run(AsyncSessionLocal, lease)
        await self._after_terminal(completed, session)

    async def _after_terminal(self, run: AgentRun, session: Session | None) -> None:
        """Best-effort post-terminal enrichment; it is never part of final truth."""

        from app.runtime.subagents import cancel_children_for_parent

        await cancel_children_for_parent(run.id, "父 Run 已结束")
        successor_id = getattr(run, "_queued_successor_id", None)
        if successor_id:
            start_tracked_task(successor_id, AgentRuntime().run(successor_id))
        if not session:
            return
        async with AsyncSessionLocal() as db:
            stored_run = await db.get(AgentRun, run.id)
            stored_session = await db.get(Session, session.id)
            if stored_run is None or stored_session is None or not stored_run.output:
                return
            # Release the read snapshot before the optional title provider.
            # The title write is committed only after the external wait ends.
            await commit_uow(db)
            with model_call_scope(
                run_id=run.id,
                parent_run_id=run.parent_run_id,
                call_purpose="session_title",
                decision_relevant=False,
                depth=0 if run.parent_run_id is None else 1,
            ):
                await generate_session_title(
                    stored_session,
                    objective=stored_run.objective,
                    answer=stored_run.output,
                    client=self.client,
                )
            await commit_uow(db)
            with model_call_scope(
                run_id=run.id,
                parent_run_id=run.parent_run_id,
                call_purpose="memory_compression",
                decision_relevant=False,
                depth=0 if run.parent_run_id is None else 1,
            ):
                await MemoryManager(db).compress_session(stored_session, self.client)
            await commit_uow(db)

    async def _call_model(self, db, run: AgentRun, messages: list[dict], failure_guard: ToolFailureGuard, step: int):
        from app.tools import openai_tools

        if db.in_nested_transaction():
            raise RuntimeError("model calls cannot coordinate inside a SAVEPOINT")
        if db.new or db.dirty or db.deleted:
            raise RuntimeError(
                "model calls require a clean session; commit the owning Unit of Work first"
            )
        if db.in_transaction():
            # refresh()/steering probes may leave a read snapshot open even
            # when there was nothing to apply. Never retain it across model
            # connection, retry, or stream waits.
            await commit_uow(db)

        model_tools = [
            tool
            for tool in openai_tools()
            if tool["function"]["name"] not in failure_guard.blocked
        ]
        # The initial Context budget is only one component of a durable Run.
        # Re-check the exact checkpoint messages and currently exposed schemas
        # before every provider call because tool observations and steers grow
        # the envelope over time.
        ensure_request_fits(messages=messages, tools=model_tools)
        request = {
            "model": settings.MODEL_NAME,
            "messages": messages,
            "temperature": settings.MODEL_TEMPERATURE,
            "max_tokens": settings.AGENT_OUTPUT_TOKEN_RESERVE,
            "extra_body": {"reasoning_effort": settings.MODEL_REASONING_EFFORT},
        }
        if model_tools:
            request.update({"tools": model_tools, "tool_choice": "auto"})
        try:
            with model_call_scope(
                run_id=run.id,
                parent_run_id=run.parent_run_id,
                call_purpose="decision",
                decision_relevant=True,
                depth=0 if run.parent_run_id is None else 1,
            ):
                try:
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(
                            **request,
                            stream=True,
                            stream_options={"include_usage": True},
                        ),
                        timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                    )
                except Exception as stream_exc:
                    if "stream_options" not in str(stream_exc):
                        raise
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**request, stream=True),
                        timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                    )
                if hasattr(response, "__aiter__"):
                    return await asyncio.wait_for(
                        self._drain_stream(response, run, step),
                        timeout=settings.AGENT_MODEL_TIMEOUT_SECONDS,
                )
                message = response.choices[0].message
                object.__setattr__(
                    message,
                    "runtime_model_call_id",
                    getattr(
                        response,
                        "runtime_model_call_id",
                        getattr(message, "runtime_model_call_id", None),
                    ),
                )
                return message, getattr(response, "usage", None)
        except TimeoutError as exc:
            raise AgentModelTimeout("模型响应超时") from exc
        except Exception as exc:
            raise AgentModelProviderError(exc) from exc

    async def _drain_stream(self, stream, run: AgentRun, step: int):
        """Collect an OpenAI-compatible token stream and publish live deltas."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, _StreamingToolCall] = {}
        usage = None
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None) or getattr(choice, "message", None)
            if delta is None:
                continue
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                publish_stream_event(run.id, {
                    "type": "assistant.reasoning",
                    "summary": "",
                    "payload": {
                        "step": step + 1,
                        "delta": reasoning,
                        "text": "".join(reasoning_parts),
                    },
                    "created_at": canonical_utc(utc_now()),
                })
            content = getattr(delta, "content", None)
            if content:
                content_parts.append(content)
                publish_stream_event(run.id, {
                    "type": "assistant.delta",
                    "summary": "",
                    "payload": {
                        "step": step + 1,
                        "delta": content,
                        "text": "".join(content_parts),
                    },
                    "created_at": canonical_utc(utc_now()),
                })
            tool_deltas = getattr(delta, "tool_calls", None)
            if tool_deltas:
                for tool_delta in tool_deltas:
                    index = int(getattr(tool_delta, "index", 0))
                    entry = tool_calls.setdefault(index, _StreamingToolCall())
                    if getattr(tool_delta, "id", None):
                        entry.id = tool_delta.id
                    function = getattr(tool_delta, "function", None)
                    if function:
                        if getattr(function, "name", None):
                            entry.name += function.name
                        if getattr(function, "arguments", None):
                            entry.arguments += function.arguments
        return _StreamingMessage(
            "".join(content_parts),
            "".join(reasoning_parts),
            list(tool_calls.values()),
            runtime_model_call_id=getattr(stream, "runtime_model_call_id", None),
        ), usage

    async def _fail(
        self,
        db,
        run: AgentRun,
        lease: RunLease,
        exc: Exception,
    ) -> None:
        """Commit one fenced failure transition after releasing caller state."""

        try:
            try:
                await rollback_uow(db)
            except Exception:
                pass
            tool_timed_out = isinstance(exc, TimeoutError)
            model_timed_out = isinstance(exc, AgentModelTimeout)
            model_provider_failed = isinstance(exc, AgentModelProviderError)
            model_error = exc.cause if model_provider_failed else exc
            transient_model_failure = (
                not tool_timed_out
                and not model_timed_out
                and is_transient_model_error(model_error)
            )
            if (
                (model_timed_out or tool_timed_out or transient_model_failure)
                and int(run.retry_count or 0) < settings.AGENT_RUN_MAX_RETRIES
            ):
                retry_reason = "model_timeout"
                if tool_timed_out:
                    retry_reason = "tool_timeout"
                elif transient_model_failure:
                    retry_reason = "model_transient"
                delay = settings.AGENT_RUN_RETRY_BACKOFF_SECONDS * (
                    2 ** int(run.retry_count or 0)
                )
                await schedule_retry(
                    AsyncSessionLocal,
                    lease,
                    reason_code=retry_reason,
                    retry_after_seconds=delay,
                )
                await asyncio.sleep(delay)
                await self.run(run.id)
                return
            if isinstance(exc, AgentModelTimeout):
                summary = "模型暂时没有响应。本轮已执行的工具结果和会话内容均已保留。"
                error_code = "model_timeout"
            elif isinstance(exc, TimeoutError):
                summary = "某个工具执行超时。本轮状态已安全保留。"
                error_code = "tool_timeout"
            elif is_transient_model_error(model_error):
                summary = "模型服务在有界重试后仍不可用，本轮状态已安全保留。"
                error_code = "model_retry_exhausted"
            elif model_provider_failed:
                summary = "模型服务拒绝或无法完成本轮请求，本轮状态已安全保留。"
                error_code = "model_provider_error"
            elif isinstance(exc, RunStateError):
                summary = "运行的耐久状态需要人工核对。"
                error_code = "runtime_state_conflict"
            else:
                summary = "运行遇到内部错误，状态已安全收口。"
                error_code = "internal_error"
            terminal = await terminate_run(
                AsyncSessionLocal,
                run.id,
                status="failed",
                reason_code=error_code,
                summary=summary,
                lease=lease,
            )
            if terminal is None:
                return
            from app.runtime.subagents import cancel_children_for_parent

            await cancel_children_for_parent(run.id, "父 Run 失败")
            successor_id = getattr(terminal, "_queued_successor_id", None)
            if successor_id:
                start_tracked_task(successor_id, AgentRuntime().run(successor_id))
        except RunLeaseLostError:
            return
        except Exception as record_error:
            print(
                f"[learning-agent] run {run.id} failed ({type(exc).__name__}) "
                f"and failure record also failed ({type(record_error).__name__})",
                flush=True,
            )

    @staticmethod
    def _default_budget() -> dict:
        return default_budget()

    @staticmethod
    def _budget(run: AgentRun) -> dict:
        return normalize_budget(run.budget_usage)

    @staticmethod
    def _budget_reason(run: AgentRun, budget: dict) -> str | None:
        return shared_budget_reason(
            budget,
            started_at=run.started_at or run.created_at,
        )

    @staticmethod
    def _refresh_elapsed(
        run: AgentRun,
        budget: dict,
        *,
        ended_at: datetime | None = None,
    ) -> None:
        refresh_elapsed(
            budget,
            run.started_at or run.created_at,
            ended_at=ended_at,
        )

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
        await flush_uow(db)
        run.session_id = session.id
        await commit_uow(db)
        return session
