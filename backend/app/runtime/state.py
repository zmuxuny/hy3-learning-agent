"""Single durable state machine for root and child Agent Runs.

Every transition here is a short database Unit of Work.  Model, network,
subprocess and child waits happen only after the transaction has committed.
The in-process task map may wake work, but ownership is fenced by the Run
lease and ``state_version`` stored in SQLite.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.uow import (
    DatabaseBusyError,
    commit as commit_uow,
    ensure_sqlite_write_transaction,
    flush as flush_uow,
    rollback as rollback_uow,
    run_short_transaction,
)
from app.models import (
    AgentRun,
    ChatMessage,
    QueuedMessage,
    RunApproval,
    RunEvent,
    RunSteerMessage,
    Session,
    ToolInvocation,
)
from app.runtime.checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    make_checkpoint,
    normalize_checkpoint,
)
from app.runtime.events import publish_stream_event, stage_event


ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "waiting_approval", "retry_wait"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
NONTERMINAL_RUN_STATUSES = frozenset({*ACTIVE_RUN_STATUSES, "needs_reconciliation"})


class RunStateError(RuntimeError):
    """A requested transition is invalid for the durable current state."""


class RunLeaseLostError(RunStateError):
    """The caller no longer owns the lease/version it is trying to mutate."""


@dataclass
class RunLease:
    run_id: str
    token: str
    owner: str
    version: int
    status_before: str
    phase_before: str
    checkpoint: dict[str, Any] | None
    started_before_claim: bool


@dataclass(frozen=True)
class FinalizationPreparation:
    action: str
    checkpoint: dict[str, Any]


def _lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utc_now()) + timedelta(seconds=settings.AGENT_RUN_LEASE_SECONDS)


def _event_payload(event: RunEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "type": event.event_type,
        "summary": event.summary,
        "payload": event.payload,
        "created_at": canonical_utc(event.created_at),
    }


def _terminal_budget(run: AgentRun, checkpoint: dict[str, Any], now: datetime) -> dict[str, Any]:
    budget = dict(checkpoint.get("budget_usage") or run.budget_usage or {})
    started = run.started_at or run.created_at
    if started is not None:
        budget["elapsed_ms"] = max(
            int(budget.get("elapsed_ms") or 0),
            max(0, int((coerce_legacy_utc(now) - coerce_legacy_utc(started)).total_seconds() * 1000)),
        )
    return budget


async def claim_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    worker_id: str | None = None,
) -> RunLease | None:
    """Atomically claim queued/retry work or an expired running lease."""

    now = utc_now()
    token = uuid4().hex
    owner = worker_id or f"worker:{uuid4().hex}"

    async def operation(db: AsyncSession) -> RunLease | None:
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, run_id)
        if run is None:
            return None
        eligible = run.status == "queued"
        if run.status == "retry_wait":
            eligible = run.available_at is None or coerce_legacy_utc(run.available_at) <= now
        if run.status == "running":
            eligible = run.lease_expires_at is None or coerce_legacy_utc(run.lease_expires_at) <= now
        if not eligible:
            return None
        checkpoint = normalize_checkpoint(
            run.checkpoint,
            kind="subagent" if run.trigger == "subagent" else "agent",
        )
        status_before = run.status
        phase_before = run.phase or (checkpoint or {}).get("phase") or "not_started"
        started_before = run.started_at is not None
        run.status = "running"
        run.phase = "starting" if phase_before == "not_started" else phase_before
        run.lease_token = token
        run.lease_owner = owner
        run.lease_acquired_at = now
        run.lease_expires_at = _lease_deadline(now)
        run.attempt = int(run.attempt or 0) + 1
        run.available_at = None
        run.started_at = run.started_at or now
        run.updated_at = now
        run.state_version = int(run.state_version or 1) + 1
        if checkpoint is not None:
            checkpoint["state_version"] = run.state_version
            run.checkpoint = checkpoint
            run.checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
        return RunLease(
            run_id=run.id,
            token=token,
            owner=owner,
            version=run.state_version,
            status_before=status_before,
            phase_before=phase_before,
            checkpoint=checkpoint,
            started_before_claim=started_before,
        )

    return await run_short_transaction(session_factory, operation)


def _leased_predicate(lease: RunLease):
    return (
        AgentRun.id == lease.run_id,
        AgentRun.status == "running",
        AgentRun.lease_token == lease.token,
        AgentRun.state_version == lease.version,
    )


async def renew_run_lease(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
) -> bool:
    """Extend a live worker lease without changing the state-machine version.

    ``lease_token`` is the ownership fence.  Checkpoint CAS may advance
    ``state_version`` while a model or tool is running, so a heartbeat must not
    race that counter or manufacture a new semantic state transition.
    """

    async def operation(db: AsyncSession) -> bool:
        await ensure_sqlite_write_transaction(db)
        now = utc_now()
        result = await db.execute(
            update(AgentRun)
            .where(
                AgentRun.id == lease.run_id,
                AgentRun.status == "running",
                AgentRun.lease_token == lease.token,
            )
            .values(lease_expires_at=_lease_deadline(now), updated_at=now)
        )
        return result.rowcount == 1

    return await run_short_transaction(session_factory, operation)


@asynccontextmanager
async def maintain_run_lease(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
):
    """Keep ownership live while external work waits outside DB transactions."""

    stopped = asyncio.Event()
    interval = max(0.05, min(5.0, float(settings.AGENT_RUN_LEASE_SECONDS) / 3.0))

    async def heartbeat() -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                if not await renew_run_lease(session_factory, lease):
                    return
            except DatabaseBusyError:
                # A competing short writer also prevents a replacement claim;
                # retry on the next bounded heartbeat interval.
                continue

    task = asyncio.create_task(heartbeat(), name=f"run-lease-heartbeat:{lease.run_id}")
    try:
        yield
    finally:
        stopped.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def ensure_root_scope_available(
    db: AsyncSession,
    *,
    owner_id: str,
    plan_id: int | None,
    session_id: str | None,
) -> None:
    """Serialize and validate creation of one root Run scope.

    SQLite's partial unique indexes remain the final invariant.  Taking the
    write lock before this read turns the expected contention path into a
    stable domain conflict instead of leaking an ``IntegrityError`` after a
    caller has staged related Session/ChatMessage rows.
    """

    await ensure_sqlite_write_transaction(db)
    scope = [
        AgentRun.owner_id == owner_id,
        AgentRun.parent_run_id.is_(None),
        # A fenced legacy Run still owns its scope until an operator resolves
        # the ambiguity.  Allowing fresh work beside it would turn an unknown
        # external-side-effect boundary into concurrent execution.
        AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
    ]
    if plan_id is not None:
        scope.append(AgentRun.plan_id == plan_id)
    elif session_id is not None:
        scope.extend((AgentRun.plan_id.is_(None), AgentRun.session_id == session_id))
    else:
        scope.extend((
            AgentRun.plan_id.is_(None),
            AgentRun.session_id.is_(None),
            AgentRun.trigger != "subagent",
        ))
    existing = (await db.execute(select(AgentRun.id).where(*scope).limit(1))).scalar_one_or_none()
    if existing is not None:
        raise RunStateError("root_run_scope_busy")


async def persist_checkpoint(
    db: AsyncSession,
    lease: RunLease,
    checkpoint: dict[str, Any],
    *,
    phase: str,
    clear_approval_id: str | None = None,
) -> AgentRun:
    """CAS-replace the current checkpoint while retaining the Run lease."""

    await ensure_sqlite_write_transaction(db)
    new_version = lease.version + 1
    canonical = normalize_checkpoint(checkpoint) or make_checkpoint(
        kind="agent",
        phase=phase,
        step=0,
        messages=[],
    )
    canonical["phase"] = phase
    canonical["state_version"] = new_version
    result = await db.execute(
        update(AgentRun)
        .where(*_leased_predicate(lease))
        .values(
            checkpoint=canonical,
            checkpoint_schema_version=CHECKPOINT_SCHEMA_VERSION,
            phase=phase,
            budget_usage=dict(canonical.get("budget_usage") or {}),
            state_version=new_version,
            lease_expires_at=_lease_deadline(),
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await rollback_uow(db)
        raise RunLeaseLostError("run checkpoint lease/version changed")
    if clear_approval_id is not None:
        approval = await db.get(RunApproval, clear_approval_id)
        if approval is None or approval.run_id != lease.run_id:
            await rollback_uow(db)
            raise RunStateError("durable approval disappeared before consumption")
        approval.consumed_at = approval.consumed_at or utc_now()
        await db.execute(
            update(AgentRun)
            .where(AgentRun.id == lease.run_id)
            .values(pending_approval=None)
            .execution_options(synchronize_session=False)
        )
    await commit_uow(db)
    lease.version = new_version
    run = await db.get(AgentRun, lease.run_id, populate_existing=True)
    if run is None:  # pragma: no cover - the CAS target cannot disappear
        raise RunStateError("run disappeared after checkpoint commit")
    return run


async def pause_for_approval(
    db: AsyncSession,
    lease: RunLease,
    *,
    checkpoint: dict[str, Any],
    tool_call: dict[str, Any],
    remaining_tool_calls: list[dict[str, Any]],
    reason: str,
) -> AgentRun:
    """Atomically persist the approval request, event and waiting state."""

    await ensure_sqlite_write_transaction(db)
    tool_call_id = str(tool_call["id"])
    tool_name = str(tool_call["name"])
    invocation = (await db.execute(
        select(ToolInvocation).where(
            ToolInvocation.run_id == lease.run_id,
            or_(
                ToolInvocation.tool_call_id == tool_call_id,
                (
                    (ToolInvocation.tool_call_id.is_(None))
                    & (ToolInvocation.tool_name == tool_name)
                    & (ToolInvocation.status == "pending_approval")
                ),
            ),
        ).order_by(ToolInvocation.id.desc()).limit(1)
    )).scalars().one_or_none()
    if invocation is not None and invocation.tool_call_id is None:
        invocation.tool_call_id = tool_call_id
    approval_id = str(uuid5(
        NAMESPACE_URL,
        f"learning-travel:approval:{lease.run_id}:{tool_call_id}",
    ))
    approval = await db.get(RunApproval, approval_id)
    if approval is None:
        run = await db.get(AgentRun, lease.run_id)
        if run is None:  # pragma: no cover - protected by the lease predicate below
            raise RunStateError("run disappeared before approval pause")
        approval = RunApproval(
            id=approval_id,
            owner_id=run.owner_id,
            run_id=lease.run_id,
            invocation_id=invocation.id if invocation is not None else None,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_call=dict(tool_call),
            remaining_tool_calls=list(remaining_tool_calls),
            reason=reason,
        )
        db.add(approval)
    pending = {
        "approval_id": approval_id,
        "tool_call": dict(tool_call),
        "remaining_tool_calls": list(remaining_tool_calls),
        "reason": reason,
        "step": int(checkpoint.get("step") or 0),
    }
    canonical = normalize_checkpoint(checkpoint) or checkpoint
    new_version = lease.version + 1
    canonical["phase"] = "waiting_approval"
    canonical["state_version"] = new_version
    result = await db.execute(
        update(AgentRun)
        .where(*_leased_predicate(lease))
        .values(
            status="waiting_approval",
            phase="waiting_approval",
            checkpoint=canonical,
            checkpoint_schema_version=CHECKPOINT_SCHEMA_VERSION,
            pending_approval=pending,
            state_version=new_version,
            lease_token=None,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await rollback_uow(db)
        raise RunLeaseLostError("run lost its lease while pausing for approval")
    await stage_event(
        db,
        lease.run_id,
        "approval.required",
        reason,
        {"approval_id": approval_id, "tool_name": tool_name, "blocking": True},
        event_key=f"approval:{tool_call_id}:required",
    )
    await commit_uow(db)
    lease.version = new_version
    run = await db.get(AgentRun, lease.run_id, populate_existing=True)
    if run is None:  # pragma: no cover
        raise RunStateError("run disappeared after approval pause")
    return run


async def decide_approval(
    db: AsyncSession,
    run_id: str,
    *,
    owner_id: str,
    approved: bool,
    note: str | None,
    answer: str | None,
) -> AgentRun:
    """Persist a user decision without passing it through process memory."""

    await ensure_sqlite_write_transaction(db)
    run = await db.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id:
        raise RunStateError("run_not_found")
    if run.status not in {"waiting_approval", "queued", "running"} or not run.pending_approval:
        raise RunStateError("approval_not_pending")
    approval_id = str(run.pending_approval.get("approval_id") or "")
    approval = await db.get(RunApproval, approval_id) if approval_id else None
    if approval is None or approval.run_id != run.id:
        raise RunStateError("approval_fact_missing")
    decision = "approve" if approved else ("answer" if answer else "reject")
    if approval.decision != "pending":
        if approval.decision != decision or approval.note != note or approval.answer != answer:
            raise RunStateError("approval_decision_conflict")
        return run
    if run.status != "waiting_approval":
        raise RunStateError("approval_decision_in_progress")
    now = utc_now()
    approval.decision = decision
    approval.note = note
    approval.answer = answer
    approval.decided_at = now
    pending = dict(run.pending_approval)
    pending.update({
        "decision": decision,
        "note": note,
        "answer": answer,
        "decided_at": canonical_utc(now),
    })
    if decision != "approve" and approval.invocation_id is not None:
        invocation = await db.get(ToolInvocation, approval.invocation_id)
        if invocation is None or invocation.status != "pending_approval":
            raise RunStateError("approval_invocation_state_conflict")
        invocation.status = "rejected"
        invocation.result_payload = {
            "error": "用户拒绝了该操作",
            "approval": decision,
            "note": note,
            "answer": answer,
        }
        invocation.claim_token = None
        invocation.claimed_at = None
        invocation.claim_expires_at = None
        invocation.completed_at = now
        invocation.version = int(invocation.version or 1) + 1
    run.pending_approval = pending
    run.status = "queued"
    run.phase = "waiting_approval"
    run.state_version = int(run.state_version or 1) + 1
    run.updated_at = now
    await stage_event(
        db,
        run.id,
        "approval.resolved",
        "用户已批准该操作" if decision == "approve" else "用户拒绝了该操作",
        {
            "approval_id": approval.id,
            "decision": decision,
            "tool_name": approval.tool_name,
            "note": note,
            "answered": bool(answer),
        },
        event_key=f"approval:{approval.tool_call_id}:resolved",
    )
    await commit_uow(db)
    await db.refresh(run)
    return run


async def current_approval(db: AsyncSession, run: AgentRun) -> RunApproval | None:
    approval_id = str((run.pending_approval or {}).get("approval_id") or "")
    return await db.get(RunApproval, approval_id) if approval_id else None


async def consume_pending_steers(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
    *,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Atomically fold every steer already pending into the next model input."""

    async def operation(db: AsyncSession) -> dict[str, Any] | None:
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, lease.run_id)
        if (
            run is None
            or run.status != "running"
            or run.lease_token != lease.token
            or run.state_version != lease.version
        ):
            raise RunLeaseLostError("run lost its lease before steer consumption")
        pending = list((await db.execute(
            select(RunSteerMessage).where(
                RunSteerMessage.run_id == run.id,
                RunSteerMessage.disposition == "pending",
            ).order_by(RunSteerMessage.created_at, RunSteerMessage.id)
        )).scalars())
        if not pending:
            return None
        canonical = normalize_checkpoint(checkpoint) or checkpoint
        messages = list(canonical.get("messages") or [])
        now = utc_now()
        for steer in pending:
            steer.disposition = "applied"
            steer.applied_at = now
            steer.disposed_at = now
            messages.append({"role": "user", "content": f"[中途转向] {steer.content}"})
        new_version = lease.version + 1
        canonical["messages"] = messages
        canonical["phase"] = "awaiting_model"
        canonical["state_version"] = new_version
        run.checkpoint = canonical
        run.checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
        run.phase = "awaiting_model"
        run.state_version = new_version
        run.lease_expires_at = _lease_deadline()
        run.updated_at = now
        return canonical

    consumed = await run_short_transaction(session_factory, operation)
    if consumed is None:
        return checkpoint
    lease.version += 1
    return consumed


async def prepare_finalization(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
    *,
    checkpoint: dict[str, Any],
    final_text: str,
) -> FinalizationPreparation:
    """Atomically consume late steer or freeze a final response for commit."""

    async def operation(db: AsyncSession) -> FinalizationPreparation:
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, lease.run_id)
        if (
            run is None
            or run.status != "running"
            or run.lease_token != lease.token
            or run.state_version != lease.version
        ):
            raise RunLeaseLostError("run lost its lease before finalization")
        pending = list((await db.execute(
            select(RunSteerMessage).where(
                RunSteerMessage.run_id == run.id,
                RunSteerMessage.disposition == "pending",
            ).order_by(RunSteerMessage.created_at, RunSteerMessage.id)
        )).scalars())
        canonical = normalize_checkpoint(checkpoint) or checkpoint
        new_version = lease.version + 1
        if pending:
            messages = list(canonical.get("messages") or [])
            for steer in pending:
                steer.disposition = "applied"
                steer.applied_at = utc_now()
                steer.disposed_at = steer.applied_at
                messages.append({"role": "user", "content": f"[中途转向] {steer.content}"})
            canonical["messages"] = messages
            canonical["phase"] = "awaiting_model"
            canonical["state_version"] = new_version
            run.phase = "awaiting_model"
            action = "continue"
        else:
            canonical["phase"] = "finalizing"
            canonical["final_text"] = final_text
            canonical["final_message_key"] = f"run:{run.id}:final"
            canonical["state_version"] = new_version
            run.phase = "finalizing"
            action = "finalize"
        run.checkpoint = canonical
        run.checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
        run.budget_usage = dict(canonical.get("budget_usage") or {})
        run.state_version = new_version
        run.lease_expires_at = _lease_deadline()
        run.updated_at = utc_now()
        return FinalizationPreparation(action=action, checkpoint=canonical)

    prepared = await run_short_transaction(session_factory, operation)
    lease.version += 1
    return prepared


async def record_steer(
    db: AsyncSession,
    run_id: str,
    *,
    owner_id: str,
    content: str,
) -> tuple[AgentRun, RunSteerMessage]:
    """Serialize steer arrival against finalization and queue late arrivals."""

    await ensure_sqlite_write_transaction(db)
    run = await db.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id:
        raise RunStateError("run_not_found")
    if run.status == "waiting_approval":
        raise RunStateError("approval_pending")
    queue_late = run.phase == "finalizing" or run.status == "completed"
    if run.status not in {"queued", "running", "completed"}:
        raise RunStateError("run_not_steerable")
    if run.session_id is None:
        raise RunStateError("steer_requires_session")
    now = utc_now()
    steer = RunSteerMessage(
        owner_id=owner_id,
        run_id=run.id,
        content=content,
        disposition="queued" if queue_late else "pending",
        disposed_at=now if queue_late else None,
    )
    db.add(steer)
    await flush_uow(db)
    message = ChatMessage(
        session_id=run.session_id,
        run_id=run.id,
        message_key=f"steer:{steer.id}",
        role="user",
        content=content,
        message_metadata={"ui_kind": "steer", "steer_id": steer.id},
    )
    db.add(message)
    await flush_uow(db)
    if queue_late:
        max_position = await db.scalar(
            select(func.coalesce(func.max(QueuedMessage.position), -1)).where(
                QueuedMessage.owner_id == owner_id,
                QueuedMessage.session_id == run.session_id,
            )
        )
        position = int(max_position if max_position is not None else -1) + 1
        db.add(QueuedMessage(
            id=f"steer-{steer.id}",
            owner_id=owner_id,
            session_id=run.session_id,
            plan_id=run.plan_id,
            trigger="user_message",
            objective=content,
            user_content=content,
            message_metadata={
                "ui_kind": "steer_continuation",
                "source_run_id": run.id,
                "steer_id": steer.id,
            },
            source_steer_id=steer.id,
            source_message_id=message.id,
            position=position,
        ))
    await stage_event(
        db,
        run.id,
        "steer.received",
        "已收到你的中途转向",
        {
            "steer_id": steer.id,
            "content": content,
            "disposition": steer.disposition,
        },
        event_key=f"steer:{steer.id}:received",
    )
    await commit_uow(db)
    await db.refresh(run)
    await db.refresh(steer)
    return run, steer


async def finalize_run(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
) -> AgentRun:
    """Atomically commit final message, events, budget and terminal Run state."""

    async def operation(
        db: AsyncSession,
    ) -> tuple[str, str | None, list[tuple[str, dict[str, Any]]]]:
        published: list[tuple[str, dict[str, Any]]] = []
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, lease.run_id)
        if run is None:
            raise RunStateError("run disappeared during finalization")
        if run.status == "completed":
            return run.id, None, published
        if (
            run.status != "running"
            or run.phase != "finalizing"
            or run.lease_token != lease.token
            or run.state_version != lease.version
        ):
            raise RunLeaseLostError("run finalization lease/version changed")
        checkpoint = normalize_checkpoint(run.checkpoint)
        if checkpoint is None:
            raise RunStateError("finalizing run has no checkpoint")
        final_text = str(checkpoint.get("final_text") or "")
        message_key = str(checkpoint.get("final_message_key") or f"run:{run.id}:final")
        now = utc_now()
        terminal_budget = _terminal_budget(run, checkpoint, now)
        if run.session_id and final_text:
            message = (await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == run.session_id,
                    ChatMessage.message_key == message_key,
                )
            )).scalars().one_or_none()
            metadata = {"cards": checkpoint.get("cards") or []} if checkpoint.get("cards") else {}
            if message is None:
                db.add(ChatMessage(
                    session_id=run.session_id,
                    run_id=run.id,
                    message_key=message_key,
                    role="assistant",
                    content=final_text,
                    message_metadata=metadata,
                ))
            elif message.run_id != run.id or message.role != "assistant" or message.content != final_text:
                raise RunStateError("stable final message key conflicts with different content")
            session = await db.get(Session, run.session_id)
            if session is not None:
                session.updated_at = utc_now()
        if final_text:
            assistant_event = await stage_event(
                db,
                run.id,
                "assistant.message",
                final_text,
                {"final": True},
                event_key=f"run:{run.id}:assistant-final",
            )
            published.append((run.id, _event_payload(assistant_event)))
        completed_event = await stage_event(
            db,
            run.id,
            "run.completed",
            final_text or "Agent run completed",
            {"budget_usage": terminal_budget},
            event_key=f"run:{run.id}:completed",
        )
        published.append((run.id, _event_payload(completed_event)))
        run.output = final_text
        run.status = "completed"
        run.phase = "terminal"
        run.checkpoint = None
        run.checkpoint_schema_version = None
        run.pending_approval = None
        run.budget_usage = terminal_budget
        run.completed_at = now
        run.lease_token = None
        run.lease_owner = None
        run.lease_acquired_at = None
        run.lease_expires_at = None
        run.available_at = None
        run.state_version = int(run.state_version or 1) + 1
        run.updated_at = now
        await flush_uow(db)
        successor_id: str | None = None
        if run.parent_run_id is None and run.session_id is not None:
            from app.services.queue import dispatch_next_queued_message

            successor = await dispatch_next_queued_message(
                db,
                owner_id=run.owner_id,
                session_id=run.session_id,
            )
            successor_id = successor.id if successor is not None else None
        return run.id, successor_id, published

    _, successor_id, published = await run_short_transaction(session_factory, operation)
    for run_id, payload in published:
        publish_stream_event(run_id, payload)
    async with session_factory() as db:
        run = await db.get(AgentRun, lease.run_id)
        if run is None:  # pragma: no cover
            raise RunStateError("finalized run disappeared")
        run._queued_successor_id = successor_id
        return run


async def finalize_child(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
    *,
    status: str,
    report: str,
    role: str,
    reason_code: str | None = None,
) -> AgentRun:
    """Commit child terminal state and its parent projection in one UoW."""

    if status not in TERMINAL_RUN_STATUSES:
        raise RunStateError(f"invalid child terminal status {status!r}")
    async def operation(
        db: AsyncSession,
    ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        published: list[tuple[str, dict[str, Any]]] = []
        await ensure_sqlite_write_transaction(db)
        child = await db.get(AgentRun, lease.run_id)
        if child is None:
            raise RunStateError("child disappeared during finalization")
        if child.status in TERMINAL_RUN_STATUSES:
            return child.id, published
        if (
            child.status != "running"
            or child.lease_token != lease.token
            or child.state_version != lease.version
        ):
            raise RunLeaseLostError("child finalization lease/version changed")
        checkpoint = normalize_checkpoint(child.checkpoint, kind="subagent") or {}
        now = utc_now()
        terminal_budget = _terminal_budget(child, checkpoint, now)
        event_type = {
            "completed": "run.completed",
            "failed": "run.failed",
            "cancelled": "run.cancelled",
        }[status]
        child_event = await stage_event(
            db,
            child.id,
            event_type,
            report,
            {"role": role, "reason_code": reason_code},
            event_key=f"run:{child.id}:{status}",
        )
        published.append((child.id, _event_payload(child_event)))
        if child.parent_run_id:
            parent_event = await stage_event(
                db,
                child.parent_run_id,
                "subagent.completed",
                f"{role} 已返回结论" if status == "completed" else f"{role} 已停止",
                {
                    "child_run_id": child.id,
                    "role": role,
                    "status": status,
                    "report": report,
                    "budget_usage": terminal_budget,
                    "reason_code": reason_code,
                },
                event_key=f"child:{child.id}:terminal",
            )
            published.append((child.parent_run_id, _event_payload(parent_event)))
        child.status = status
        child.phase = "terminal"
        child.output = report
        child.budget_usage = terminal_budget
        child.checkpoint = None
        child.checkpoint_schema_version = None
        child.pending_approval = None
        child.completed_at = now
        child.lease_token = None
        child.lease_owner = None
        child.lease_acquired_at = None
        child.lease_expires_at = None
        child.available_at = None
        child.state_version = int(child.state_version or 1) + 1
        child.status_reason = reason_code
        child.updated_at = now
        return child.id, published

    _, published = await run_short_transaction(session_factory, operation)
    for run_id, payload in published:
        publish_stream_event(run_id, payload)
    async with session_factory() as db:
        child = await db.get(AgentRun, lease.run_id)
        if child is None:  # pragma: no cover
            raise RunStateError("finalized child disappeared")
        return child


async def schedule_retry(
    session_factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
    *,
    reason_code: str,
    retry_after_seconds: float,
) -> datetime:
    """Release a lease into a durable retry-wait state."""

    available_at = utc_now() + timedelta(seconds=max(0.0, retry_after_seconds))
    async def operation(db: AsyncSession) -> dict[str, Any]:
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, lease.run_id)
        if (
            run is None
            or run.status != "running"
            or run.lease_token != lease.token
            or run.state_version != lease.version
        ):
            raise RunLeaseLostError("run lost its lease before retry scheduling")
        checkpoint = normalize_checkpoint(
            run.checkpoint,
            kind="subagent" if run.trigger == "subagent" else "agent",
        )
        if checkpoint is None:
            raise RunStateError("retryable run has no durable checkpoint")
        run.retry_count = int(run.retry_count or 0) + 1
        checkpoint["phase"] = "retry_wait"
        checkpoint["retry_count"] = run.retry_count
        checkpoint["retry_not_before"] = canonical_utc(available_at)
        checkpoint["state_version"] = int(run.state_version or 1) + 1
        run.status = "retry_wait"
        run.phase = "retry_wait"
        run.available_at = available_at
        run.status_reason = reason_code
        run.checkpoint = checkpoint
        run.checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
        run.lease_token = None
        run.lease_owner = None
        run.lease_acquired_at = None
        run.lease_expires_at = None
        run.state_version = int(run.state_version or 1) + 1
        run.updated_at = utc_now()
        event = await stage_event(
            db,
            run.id,
            "run.retrying",
            "运行将在有界退避后重试",
            {
                "reason": reason_code,
                "retry_count": run.retry_count,
                "available_at": canonical_utc(available_at),
            },
            event_key=f"run:{run.id}:retry:{run.retry_count}",
        )
        return _event_payload(event)

    published = await run_short_transaction(session_factory, operation)
    publish_stream_event(lease.run_id, published)
    return available_at


async def _consume_terminal_approval(
    db: AsyncSession,
    run: AgentRun,
    *,
    now: datetime,
    reason_code: str,
) -> None:
    """Make a pending approval/invocation permanently non-executable."""

    approval_id = str((run.pending_approval or {}).get("approval_id") or "")
    if not approval_id:
        return
    approval = await db.get(RunApproval, approval_id)
    if approval is None or approval.run_id != run.id:
        return
    approval.consumed_at = approval.consumed_at or now
    if approval.invocation_id is None:
        return
    invocation = await db.get(ToolInvocation, approval.invocation_id)
    if invocation is None or invocation.status != "pending_approval":
        return
    invocation.status = "cancelled"
    invocation.result_payload = {
        "ok": False,
        "error": "运行已终止",
        "error_code": reason_code,
        "retryable": False,
    }
    invocation.claim_token = None
    invocation.claimed_at = None
    invocation.claim_expires_at = None
    invocation.completed_at = now
    invocation.version = int(invocation.version or 1) + 1


async def terminate_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    status: str,
    reason_code: str,
    summary: str,
    lease: RunLease | None = None,
) -> AgentRun | None:
    """Terminalize a root/child Run through one idempotent transition."""

    if status not in {"failed", "cancelled"}:
        raise RunStateError("terminate_run only supports failed/cancelled")
    async def operation(
        db: AsyncSession,
    ) -> tuple[str | None, str | None, list[tuple[str, dict[str, Any]]]]:
        published: list[tuple[str, dict[str, Any]]] = []
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, run_id)
        if run is None:
            return None, None, published
        if run.status in TERMINAL_RUN_STATUSES:
            return run.id, None, published
        if lease is not None and (
            run.status != "running"
            or run.lease_token != lease.token
            or run.state_version != lease.version
        ):
            raise RunLeaseLostError("run termination lease/version changed")
        now = utc_now()
        await _consume_terminal_approval(
            db,
            run,
            now=now,
            reason_code=reason_code,
        )
        if run.session_id is not None:
            pending_steers = list((await db.execute(
                select(RunSteerMessage).where(
                    RunSteerMessage.run_id == run.id,
                    RunSteerMessage.disposition == "pending",
                ).order_by(RunSteerMessage.created_at, RunSteerMessage.id)
            )).scalars())
            max_position = await db.scalar(
                select(func.coalesce(func.max(QueuedMessage.position), -1)).where(
                    QueuedMessage.owner_id == run.owner_id,
                    QueuedMessage.session_id == run.session_id,
                )
            )
            next_position = int(max_position if max_position is not None else -1) + 1
            for steer in pending_steers:
                source_message = (await db.execute(select(ChatMessage).where(
                    ChatMessage.session_id == run.session_id,
                    ChatMessage.message_key == f"steer:{steer.id}",
                ))).scalars().one_or_none()
                db.add(QueuedMessage(
                    id=f"steer-{steer.id}",
                    owner_id=run.owner_id,
                    session_id=run.session_id,
                    plan_id=run.plan_id,
                    trigger="user_message",
                    objective=steer.content,
                    user_content=steer.content,
                    message_metadata={
                        "ui_kind": "steer_continuation",
                        "source_run_id": run.id,
                        "steer_id": steer.id,
                    },
                    source_steer_id=steer.id,
                    source_message_id=source_message.id if source_message is not None else None,
                    position=next_position,
                ))
                steer.disposition = "queued"
                steer.disposed_at = now
                next_position += 1
        event_type = "run.failed" if status == "failed" else "run.cancelled"
        event = await stage_event(
            db,
            run.id,
            event_type,
            summary,
            {"code": reason_code},
            event_key=f"run:{run.id}:{status}",
        )
        published.append((run.id, _event_payload(event)))
        if run.parent_run_id:
            parent_event = await stage_event(
                db,
                run.parent_run_id,
                "subagent.completed",
                summary,
                {
                    "child_run_id": run.id,
                    "status": status,
                    "report": run.output or summary,
                    "reason_code": reason_code,
                },
                event_key=f"child:{run.id}:terminal",
            )
            published.append((run.parent_run_id, _event_payload(parent_event)))
        run.status = status
        run.phase = "terminal"
        run.cancel_requested = status == "cancelled" or run.cancel_requested
        run.checkpoint = None
        run.checkpoint_schema_version = None
        run.pending_approval = None
        run.completed_at = now
        run.status_reason = reason_code
        run.lease_token = None
        run.lease_owner = None
        run.lease_acquired_at = None
        run.lease_expires_at = None
        run.available_at = None
        run.state_version = int(run.state_version or 1) + 1
        run.updated_at = now
        await flush_uow(db)
        successor_id: str | None = None
        if run.parent_run_id is None and run.session_id is not None:
            from app.services.queue import dispatch_next_queued_message

            successor = await dispatch_next_queued_message(
                db,
                owner_id=run.owner_id,
                session_id=run.session_id,
            )
            successor_id = successor.id if successor is not None else None
        return run.id, successor_id, published

    result, successor_id, published = await run_short_transaction(
        session_factory,
        operation,
    )
    for target_run_id, payload in published:
        publish_stream_event(target_run_id, payload)
    if result is None:
        return None
    async with session_factory() as db:
        run = await db.get(AgentRun, run_id)
        if run is not None:
            run._queued_successor_id = successor_id
        return run


async def repair_child_terminal_projection(
    session_factory: async_sessionmaker[AsyncSession],
    child_id: str,
) -> bool:
    """Idempotently repair a legacy terminal child missing its parent event."""

    async def operation(
        db: AsyncSession,
    ) -> tuple[bool, list[tuple[str, dict[str, Any]]]]:
        published: list[tuple[str, dict[str, Any]]] = []
        await ensure_sqlite_write_transaction(db)
        child = await db.get(AgentRun, child_id)
        if (
            child is None
            or child.parent_run_id is None
            or child.status not in TERMINAL_RUN_STATUSES
        ):
            return False, published
        existing = (await db.execute(
            select(RunEvent.id).where(
                RunEvent.run_id == child.parent_run_id,
                RunEvent.event_key == f"child:{child.id}:terminal",
            )
        )).scalar_one_or_none()
        if existing is not None:
            return False, published
        event = await stage_event(
            db,
            child.parent_run_id,
            "subagent.completed",
            "已修复子 Agent 终态投影",
            {
                "child_run_id": child.id,
                "status": child.status,
                "report": child.output or "",
                "recovered": True,
            },
            event_key=f"child:{child.id}:terminal",
        )
        published.append((child.parent_run_id, _event_payload(event)))
        return True, published

    repaired, published = await run_short_transaction(session_factory, operation)
    for run_id, payload in published:
        publish_stream_event(run_id, payload)
    return repaired


async def reconcile_run_after_restart(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    scope_valid: bool,
) -> bool:
    """Classify one non-terminal Run after an exclusive process restart.

    A freshly queued Run is valid without a checkpoint: its creation is the
    durable work intent.  A previously running Run is recoverable only with a
    checkpoint; otherwise its last external boundary is unknowable and it is
    fenced for explicit reconciliation.
    """

    async def operation(
        db: AsyncSession,
    ) -> tuple[bool, list[tuple[str, dict[str, Any]]]]:
        published: list[tuple[str, dict[str, Any]]] = []
        await ensure_sqlite_write_transaction(db)
        run = await db.get(AgentRun, run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return False, published
        if not scope_valid:
            now = utc_now()
            summary = "运行所属的计划或会话已不可用。"
            await _consume_terminal_approval(
                db,
                run,
                now=now,
                reason_code="scope_unavailable",
            )
            run.status = "failed"
            run.phase = "terminal"
            run.status_reason = "scope_unavailable"
            run.output = run.output or summary
            run.checkpoint = None
            run.checkpoint_schema_version = None
            run.pending_approval = None
            run.completed_at = now
            run.lease_token = None
            run.lease_owner = None
            run.lease_acquired_at = None
            run.lease_expires_at = None
            run.available_at = None
            run.state_version = int(run.state_version or 1) + 1
            run.updated_at = now
            event = await stage_event(
                db,
                run.id,
                "run.failed",
                summary,
                {"code": "scope_unavailable", "recoverable": False},
                event_key=f"run:{run.id}:failed",
            )
            published.append((run.id, _event_payload(event)))
            if run.parent_run_id:
                parent_event = await stage_event(
                    db,
                    run.parent_run_id,
                    "subagent.completed",
                    summary,
                    {
                        "child_run_id": run.id,
                        "status": "failed",
                        "report": run.output,
                        "reason_code": "scope_unavailable",
                        "recovered": True,
                    },
                    event_key=f"child:{run.id}:terminal",
                )
                published.append((run.parent_run_id, _event_payload(parent_event)))
            return False, published
        checkpoint = normalize_checkpoint(
            run.checkpoint,
            kind="subagent" if run.trigger == "subagent" else "agent",
        )
        if run.status == "waiting_approval":
            # Undecided approvals are intentionally idle. A committed decision
            # is queued by decide_approval in the same transaction.
            return False, published
        if run.status == "needs_reconciliation":
            return False, published
        if run.status == "running" and checkpoint is None:
            run.status = "needs_reconciliation"
            run.phase = "reconciling"
            run.status_reason = "missing_running_checkpoint"
            run.lease_token = None
            run.lease_owner = None
            run.lease_acquired_at = None
            run.lease_expires_at = None
            run.state_version = int(run.state_version or 1) + 1
            run.updated_at = utc_now()
            event = await stage_event(
                db,
                run.id,
                "run.reconciliation_required",
                "运行中断时没有可证明的检查点，已停止自动重放。",
                {"code": "missing_running_checkpoint"},
                event_key=f"run:{run.id}:reconciliation-required",
            )
            published.append((run.id, _event_payload(event)))
            return False, published
        # Startup owns the exclusive runtime lifecycle lease, so no old
        # process can still be sleeping on this retry.  Requeue every durable
        # retry_wait item now; retaining a future timestamp here would strand
        # it because startup has no separate delayed-task scheduler.
        run.status = "queued"
        run.phase = (checkpoint or {}).get("phase") or "not_started"
        run.checkpoint = checkpoint
        run.checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION if checkpoint else None
        run.lease_token = None
        run.lease_owner = None
        run.lease_acquired_at = None
        run.lease_expires_at = None
        run.available_at = None
        run.state_version = int(run.state_version or 1) + 1
        if checkpoint is not None:
            checkpoint["state_version"] = run.state_version
        run.updated_at = utc_now()
        return True, published

    resumable, published = await run_short_transaction(session_factory, operation)
    for target_run_id, payload in published:
        publish_stream_event(target_run_id, payload)
    return resumable
