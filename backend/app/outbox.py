"""Durable delivery coordinator for non-transactional side effects.

The outbox deliberately persists an uncertainty fence *before* invoking a
transport.  A process can therefore lose availability between claim and
receipt persistence, but it never blindly repeats an SMTP, Web Push, or file
publication whose outcome may already be externally visible.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.redaction import redact_data
from app.core.time import utc_now
from app.db.database import AsyncSessionLocal


ACTIVE_DELIVERY_STALE_SECONDS = 60
ACTIVE_DELIVERY_RECEIPT_GRACE_SECONDS = 5.0


class OutboxProtocolError(RuntimeError):
    """Base class for typed outbox protocol failures."""


class OutboxConflictError(OutboxProtocolError):
    """A stable action key was reused with different content."""


class OutboxNeedsReconciliation(OutboxProtocolError):
    """An action cannot be retried because its external outcome is uncertain."""


class OutboxPreflightUnavailable(OutboxProtocolError):
    """A known pre-call dependency is unavailable; no side effect was attempted."""


class UnsupportedOutboxDestination(OutboxProtocolError):
    """The persisted destination has no registered delivery adapter."""


@dataclass(frozen=True)
class ClaimedAction:
    id: str
    owner_id: str
    run_id: str | None
    invocation_id: int | None
    notification_id: int | None
    operation_id: str | None
    action_key: str
    request_digest: str
    destination: str
    payload: dict[str, Any]
    claim_token: str


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str = "delivered"
    action_status: str = "delivered"
    provider_id: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    dead_subscription_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PreparedWorkspaceIntent:
    """Filesystem precondition captured before any caller-owned DB write."""

    operation: str
    path: str
    content: str | None
    desired_sha256: str | None
    before_exists: bool
    before_sha256: str | None
    before_content: bytes | None
    overwrite: bool


def _models():
    from app.models import Notification, Operation, OutboxAction, OutboxReceipt

    return Notification, Operation, OutboxAction, OutboxReceipt


def _payload_digest(payload: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _action_matches(
    existing: Any,
    *,
    owner_id: str,
    run_id: str | None,
    invocation_id: int | None,
    notification_id: int | None,
    operation_id: str | None,
    request_digest: str,
    destination: str,
    payload: dict[str, Any],
) -> bool:
    return (
        existing.owner_id == owner_id
        and existing.run_id == run_id
        and existing.invocation_id == invocation_id
        and existing.notification_id == notification_id
        and existing.operation_id == operation_id
        and existing.request_digest == request_digest
        and existing.destination == destination
        and _payload_digest(dict(existing.payload or {})) == _payload_digest(payload)
    )


async def _validate_linked_ownership(
    db: AsyncSession,
    *,
    owner_id: str,
    run_id: str | None,
    invocation_id: int | None,
    notification_id: int | None,
    operation_id: str | None,
) -> None:
    from app.models import Notification, Operation, ToolInvocation

    if invocation_id is not None:
        invocation = await db.get(ToolInvocation, invocation_id)
        if (
            invocation is None
            or invocation.owner_id != owner_id
            or invocation.run_id != run_id
        ):
            raise OutboxConflictError("outbox invocation scope mismatch")
    if notification_id is not None:
        notification = await db.get(Notification, notification_id)
        if (
            notification is None
            or notification.owner_id != owner_id
            or notification.run_id != run_id
        ):
            raise OutboxConflictError("outbox notification scope mismatch")
    if operation_id is not None:
        operation = await db.get(Operation, operation_id)
        if (
            operation is None
            or operation.owner_id != owner_id
            or operation.run_id != run_id
        ):
            raise OutboxConflictError("outbox operation scope mismatch")


async def enqueue_action(
    db: AsyncSession,
    *,
    owner_id: str,
    action_key: str,
    request_digest: str,
    destination: str,
    payload: dict[str, Any],
    run_id: str | None = None,
    invocation_id: int | None = None,
    notification_id: int | None = None,
    operation_id: str | None = None,
) -> Any:
    """Stage one delivery intent in the caller-owned Unit of Work.

    This function only flushes.  The tool coordinator commits the outbox row,
    its domain record, and the invocation state atomically.
    """

    _, _, OutboxAction, _ = _models()
    from app.db.uow import ensure_sqlite_write_transaction

    await ensure_sqlite_write_transaction(db)
    await _validate_linked_ownership(
        db,
        owner_id=owner_id,
        run_id=run_id,
        invocation_id=invocation_id,
        notification_id=notification_id,
        operation_id=operation_id,
    )
    existing = (
        await db.execute(
            select(OutboxAction).where(OutboxAction.action_key == action_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not _action_matches(
            existing,
            owner_id=owner_id,
            run_id=run_id,
            invocation_id=invocation_id,
            notification_id=notification_id,
            operation_id=operation_id,
            request_digest=request_digest,
            destination=destination,
            payload=payload,
        ):
            raise OutboxConflictError(
                "outbox action key already belongs to a different request"
            )
        return existing

    action = OutboxAction(
        owner_id=owner_id,
        run_id=run_id,
        invocation_id=invocation_id,
        notification_id=notification_id,
        operation_id=operation_id,
        action_key=action_key,
        request_digest=request_digest,
        effect_kind="external_write",
        destination=destination,
        payload=payload,
        status="queued",
    )
    try:
        # The savepoint contains only the unique intent insert.  A concurrent
        # exact request can therefore lose this race without poisoning or
        # rolling back the caller's larger domain UoW.
        async with db.begin_nested():
            db.add(action)
            from app.db.uow import flush as flush_uow

            await flush_uow(db)
        return action
    except IntegrityError:
        existing = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        if not _action_matches(
            existing,
            owner_id=owner_id,
            run_id=run_id,
            invocation_id=invocation_id,
            notification_id=notification_id,
            operation_id=operation_id,
            request_digest=request_digest,
            destination=destination,
            payload=payload,
        ):
            raise OutboxConflictError(
                "outbox action key already belongs to a different request"
            )
        return existing


async def enqueue_subprocess(
    db: AsyncSession,
    *,
    owner_id: str,
    action_key: str,
    request_digest: str,
    arguments: dict[str, Any],
    run_id: str | None = None,
    invocation_id: int | None = None,
) -> Any:
    """Stage one bounded code execution; the dispatcher runs it after fencing."""

    return await enqueue_action(
        db,
        owner_id=owner_id,
        run_id=run_id,
        invocation_id=invocation_id,
        action_key=action_key,
        request_digest=request_digest,
        destination="subprocess",
        payload={"arguments": arguments},
    )


async def enqueue_smtp_diagnostic(
    db: AsyncSession,
    *,
    owner_id: str,
    action_id: str,
    title: str,
    body: str,
) -> Any:
    """Stage a user-requested SMTP test message with a stable client action id."""

    from app.notifications.service import NotificationService

    normalized_action_id = action_id.strip()
    if not normalized_action_id or len(normalized_action_id) > 120:
        raise ValueError("SMTP diagnostic action_id must contain 1-120 characters")
    identity_digest = hashlib.sha256(
        f"{owner_id}\0{normalized_action_id}".encode("utf-8")
    ).hexdigest()
    route_digest = NotificationService._smtp_route_digest()
    request_payload = {
        "action_id": normalized_action_id,
        "title": title,
        "body": body,
        "route_digest": route_digest,
    }
    request_digest = _payload_digest(request_payload)
    return await enqueue_action(
        db,
        owner_id=owner_id,
        action_key=f"smtp-diagnostic:{identity_digest}",
        request_digest=request_digest,
        destination="smtp",
        payload={
            "reply_token": f"diag-{identity_digest[:32]}",
            "title": title,
            "body": body,
            "route_digest": route_digest,
            "diagnostic": True,
        },
    )


async def enqueue_workspace_write(
    db: AsyncSession,
    *,
    owner_id: str,
    action_key: str,
    request_digest: str,
    prepared: PreparedWorkspaceIntent,
    run_id: str | None = None,
    invocation_id: int | None = None,
    operation_id: str | None = None,
    operation_status_on_delivery: str = "committed",
) -> Any:
    """Stage a prepared workspace publication without awaiting filesystem I/O."""

    if prepared.operation != "write" or prepared.content is None:
        raise ValueError("prepared intent is not a workspace write")
    if operation_status_on_delivery not in {"committed", "undone"}:
        raise ValueError("unsupported operation delivery status")
    return await enqueue_action(
        db,
        owner_id=owner_id,
        run_id=run_id,
        invocation_id=invocation_id,
        operation_id=operation_id,
        action_key=action_key,
        request_digest=request_digest,
        destination="workspace_file",
        payload={
            "operation": "write",
            "path": prepared.path,
            "content": prepared.content,
            "desired_sha256": prepared.desired_sha256,
            "before_exists": prepared.before_exists,
            "before_sha256": prepared.before_sha256,
            "overwrite": prepared.overwrite,
            "operation_status_on_delivery": operation_status_on_delivery,
        },
    )


async def enqueue_workspace_delete(
    db: AsyncSession,
    *,
    owner_id: str,
    action_key: str,
    request_digest: str,
    prepared: PreparedWorkspaceIntent,
    run_id: str | None = None,
    invocation_id: int | None = None,
    operation_id: str | None = None,
    operation_status_on_delivery: str = "undone",
) -> Any:
    """Stage a prepared workspace deletion without awaiting filesystem I/O."""

    if prepared.operation != "delete":
        raise ValueError("prepared intent is not a workspace deletion")
    if operation_status_on_delivery not in {"committed", "undone"}:
        raise ValueError("unsupported operation delivery status")
    return await enqueue_action(
        db,
        owner_id=owner_id,
        run_id=run_id,
        invocation_id=invocation_id,
        operation_id=operation_id,
        action_key=action_key,
        request_digest=request_digest,
        destination="workspace_file",
        payload={
            "operation": "delete",
            "path": prepared.path,
            "before_exists": prepared.before_exists,
            "before_sha256": prepared.before_sha256,
            "operation_status_on_delivery": operation_status_on_delivery,
        },
    )


async def prepare_workspace_write(
    *,
    path: str,
    content: str,
    overwrite: bool,
) -> PreparedWorkspaceIntent:
    """Capture a write precondition before the caller starts its DB UoW."""

    target = _workspace_target(path)
    before = await asyncio.to_thread(target.read_bytes) if target.exists() else None
    if before is not None and not overwrite:
        raise FileExistsError("File already exists; set overwrite=true to replace it")
    return PreparedWorkspaceIntent(
        operation="write",
        path=path,
        content=content,
        desired_sha256=_sha256_bytes(content.encode("utf-8")),
        before_exists=before is not None,
        before_sha256=_sha256_bytes(before) if before is not None else None,
        before_content=before,
        overwrite=overwrite,
    )


async def prepare_workspace_delete(*, path: str) -> PreparedWorkspaceIntent:
    """Capture a delete precondition before the caller starts its DB UoW."""

    target = _workspace_target(path)
    before = await asyncio.to_thread(target.read_bytes) if target.exists() else None
    return PreparedWorkspaceIntent(
        operation="delete",
        path=path,
        content=None,
        desired_sha256=None,
        before_exists=before is not None,
        before_sha256=_sha256_bytes(before) if before is not None else None,
        before_content=before,
        overwrite=True,
    )


def _snapshot(action: Any, claim_token: str) -> ClaimedAction:
    return ClaimedAction(
        id=action.id,
        owner_id=action.owner_id,
        run_id=action.run_id,
        invocation_id=action.invocation_id,
        notification_id=action.notification_id,
        operation_id=action.operation_id,
        action_key=action.action_key,
        request_digest=action.request_digest,
        destination=action.destination,
        payload=dict(action.payload or {}),
        claim_token=claim_token,
    )


async def _claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    action_key: str | None = None,
) -> ClaimedAction | None:
    """CAS-claim one row and atomically arm every linked durable state."""

    from app.db.uow import run_short_transaction
    from app.models import ToolInvocation

    Notification, Operation, OutboxAction, _ = _models()
    now = utc_now()
    claim_token = str(uuid4())

    async def claim(db: AsyncSession) -> ClaimedAction | None:
        candidate = select(OutboxAction.id).where(
            OutboxAction.status.in_(["queued", "retry_pending"]),
            OutboxAction.available_at <= now,
        )
        if action_key is not None:
            candidate = candidate.where(OutboxAction.action_key == action_key)
        candidate = candidate.order_by(
            OutboxAction.created_at, OutboxAction.id
        ).limit(1)
        result = await db.execute(
            update(OutboxAction)
            .where(
                OutboxAction.id == candidate.scalar_subquery(),
                OutboxAction.status.in_(["queued", "retry_pending"]),
            )
            .values(
                # Persist the fail-closed uncertainty fence before syscall.
                # Active ownership stays distinguishable from an abandoned
                # uncertain action, so a reconciler cannot steal it mid-call.
                status="delivering",
                claim_token=claim_token,
                claimed_at=now,
                attempt=OutboxAction.attempt + 1,
                version=OutboxAction.version + 1,
                last_error="",
                updated_at=now,
            )
            .returning(
                OutboxAction.id,
                OutboxAction.owner_id,
                OutboxAction.run_id,
                OutboxAction.invocation_id,
                OutboxAction.notification_id,
                OutboxAction.operation_id,
                OutboxAction.action_key,
                OutboxAction.request_digest,
                OutboxAction.destination,
                OutboxAction.payload,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if row["invocation_id"] is not None:
            await db.execute(
                update(ToolInvocation)
                .where(ToolInvocation.id == row["invocation_id"])
                .values(
                    status="needs_reconciliation",
                    version=ToolInvocation.version + 1,
                    updated_at=now,
                )
            )
        if row["notification_id"] is not None:
            await db.execute(
                update(Notification)
                .where(Notification.id == row["notification_id"])
                .values(status="needs_reconciliation")
            )
        if row["operation_id"] is not None:
            await db.execute(
                update(Operation)
                .where(Operation.id == row["operation_id"])
                .values(status="needs_reconciliation")
            )
        return ClaimedAction(
            id=row["id"],
            owner_id=row["owner_id"],
            run_id=row["run_id"],
            invocation_id=row["invocation_id"],
            notification_id=row["notification_id"],
            operation_id=row["operation_id"],
            action_key=row["action_key"],
            request_digest=row["request_digest"],
            destination=row["destination"],
            payload=dict(row["payload"] or {}),
            claim_token=claim_token,
        )

    return await run_short_transaction(session_factory, claim)


async def _dispatch_claimed(
    action: ClaimedAction,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Run one already-fenced adapter and persist its single durable outcome."""

    try:
        outcome = await _deliver(action, session_factory=session_factory)
    except OutboxPreflightUnavailable as exc:
        await _record_retry_pending(
            action,
            type(exc).__name__,
            session_factory=session_factory,
        )
        return {
            "status": "retry_pending",
            "action_key": action.action_key,
            "delivered": False,
            "retryable": True,
        }
    except Exception as exc:
        # Transport exceptions do not prove non-delivery.  Persist only the
        # exception type so payloads, recipients, and credentials never leak.
        await _record_uncertain_error(
            action,
            type(exc).__name__,
            session_factory=session_factory,
        )
        return {
            "status": "needs_reconciliation",
            "action_key": action.action_key,
            "delivered": False,
            "error_code": "needs_reconciliation",
        }

    # BaseException is intentionally not caught.  SIGKILL-style test faults
    # leave the pre-call fence durable and exercise real restart behaviour.
    try:
        await _store_receipt(action, outcome, session_factory=session_factory)
    except Exception:
        try:
            await _record_uncertain_error(
                action,
                "ReceiptPersistenceFailure",
                session_factory=session_factory,
            )
        except Exception:
            # The original delivering fence and linked reconciliation states
            # remain durable even if SQLite is still unavailable.
            pass
        return {
            "status": "needs_reconciliation",
            "action_key": action.action_key,
            "delivered": False,
            "error_code": "needs_reconciliation",
            "uncertain_outcome": True,
        }
    return {
        "status": outcome.action_status,
        "action_key": action.action_key,
        "delivered": outcome.action_status == "delivered",
        "data": redact_data(outcome.response),
    }


async def dispatch_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> dict[str, Any]:
    """Deliver at most one action without holding a database writer lock."""

    action = await _claim_next(session_factory)
    if action is None:
        return {"status": "idle", "delivered": False}
    return await _dispatch_claimed(action, session_factory=session_factory)


# Compatibility name used by the architecture/fault-injection harness.
dispatch_pending_once = dispatch_once


async def dispatch_action(
    *,
    action_key: str,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    wait_for_active_seconds: float | None = None,
) -> dict[str, Any]:
    """Target one queued action, primarily for deterministic operators/tests."""

    action = await _claim_next(session_factory, action_key=action_key)
    if action is None:
        current = await _current_action_result(
            action_key=action_key,
            session_factory=session_factory,
        )
        if current.get("status") != "in_progress":
            return current
        wait_budget = (
            ACTIVE_DELIVERY_STALE_SECONDS
            + ACTIVE_DELIVERY_RECEIPT_GRACE_SECONDS
            if wait_for_active_seconds is None
            else max(float(wait_for_active_seconds), 0.0)
        )
        deadline = asyncio.get_running_loop().time() + wait_budget
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.025)
            current = await _current_action_result(
                action_key=action_key,
                session_factory=session_factory,
            )
            if current.get("status") != "in_progress":
                return current
        return current
    return await _dispatch_claimed(action, session_factory=session_factory)


async def _current_action_result(
    *,
    action_key: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Return the durable result when a targeted worker lost the claim race."""

    _, _, OutboxAction, OutboxReceipt = _models()
    async with session_factory() as db:
        action = await db.scalar(
            select(OutboxAction).where(OutboxAction.action_key == action_key)
        )
        if action is None:
            return {
                "status": "not_found",
                "action_key": action_key,
                "delivered": False,
                "retryable": False,
            }
        receipt = await db.scalar(
            select(OutboxReceipt).where(OutboxReceipt.outbox_action_id == action.id)
        )
        if action.status == "delivered" and receipt is not None:
            return {
                "status": "delivered",
                "action_key": action_key,
                "delivered": True,
                "data": redact_data(dict(receipt.response or {})),
                "replayed": True,
            }
        if action.status == "cancelled":
            return {
                "status": "cancelled",
                "action_key": action_key,
                "delivered": False,
                "retryable": False,
                "data": (
                    redact_data(dict(receipt.response or {}))
                    if receipt is not None
                    else {}
                ),
            }
        if action.status == "retry_pending":
            return {
                "status": "retry_pending",
                "action_key": action_key,
                "delivered": False,
                "retryable": True,
            }
        if action.status == "queued":
            return {
                "status": "pending_delivery",
                "action_key": action_key,
                "delivered": False,
                "retryable": True,
            }
        if action.status == "delivering":
            stale = bool(
                action.claimed_at is None
                or action.claimed_at
                <= utc_now() - timedelta(seconds=ACTIVE_DELIVERY_STALE_SECONDS)
            )
            if not stale:
                return {
                    "status": "in_progress",
                    "action_key": action_key,
                    "delivered": False,
                    "retryable": True,
                }
        return {
            "status": "needs_reconciliation",
            "action_key": action_key,
            "delivered": False,
            "error_code": "needs_reconciliation",
            "retryable": False,
        }


async def _record_retry_pending(
    action: ClaimedAction,
    error_code: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.db.uow import run_short_transaction
    from app.models import ToolInvocation

    Notification, Operation, OutboxAction, _ = _models()

    async def persist(db: AsyncSession) -> None:
        row = await db.get(OutboxAction, action.id)
        if row is None or row.claim_token != action.claim_token:
            return
        row.status = "retry_pending"
        row.available_at = utc_now() + timedelta(seconds=30)
        row.last_error = error_code[:120]
        row.updated_at = utc_now()
        if row.invocation_id is not None:
            sibling_states = set(
                (
                    await db.execute(
                        select(OutboxAction.status).where(
                            OutboxAction.invocation_id == row.invocation_id
                        )
                    )
                ).scalars()
            )
            invocation_status = (
                "needs_reconciliation"
                if sibling_states.intersection({"needs_reconciliation", "delivering"})
                else "pending_delivery"
            )
            await db.execute(
                update(ToolInvocation)
                .where(ToolInvocation.id == row.invocation_id)
                .values(
                    status=invocation_status,
                    version=ToolInvocation.version + 1,
                    updated_at=utc_now(),
                )
            )
        if row.notification_id is not None:
            sibling_states = set(
                (
                    await db.execute(
                        select(OutboxAction.status).where(
                            OutboxAction.notification_id == row.notification_id
                        )
                    )
                ).scalars()
            )
            notification_status = (
                "needs_reconciliation"
                if sibling_states.intersection({"needs_reconciliation", "delivering"})
                else "queued"
            )
            await db.execute(
                update(Notification)
                .where(Notification.id == row.notification_id)
                .values(status=notification_status)
            )
        if row.operation_id is not None:
            pending_status = (
                "undo_pending"
                if row.payload.get("operation_status_on_delivery") == "undone"
                else "pending_delivery"
            )
            await db.execute(
                update(Operation)
                .where(Operation.id == row.operation_id)
                .values(status=pending_status)
            )

    await run_short_transaction(session_factory, persist)


async def _record_uncertain_error(
    action: ClaimedAction,
    error_code: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.db.uow import run_short_transaction
    from app.models import ToolInvocation

    Notification, Operation, OutboxAction, _ = _models()

    async def persist(db: AsyncSession) -> None:
        row = await db.get(OutboxAction, action.id)
        if row is None or row.claim_token != action.claim_token:
            return
        row.status = "needs_reconciliation"
        row.last_error = error_code[:120]
        row.updated_at = utc_now()
        if row.invocation_id is not None:
            await db.execute(
                update(ToolInvocation)
                .where(ToolInvocation.id == row.invocation_id)
                .values(
                    status="needs_reconciliation",
                    version=ToolInvocation.version + 1,
                    updated_at=utc_now(),
                )
            )
        if row.notification_id is not None:
            await db.execute(
                update(Notification)
                .where(Notification.id == row.notification_id)
                .values(status="needs_reconciliation")
            )
        if row.operation_id is not None:
            await db.execute(
                update(Operation)
                .where(Operation.id == row.operation_id)
                .values(status="needs_reconciliation")
            )

    await run_short_transaction(session_factory, persist)


async def _deliver(
    action: ClaimedAction,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> DeliveryOutcome:
    if action.destination == "smtp":
        return await _deliver_smtp(action)
    if action.destination == "web_push":
        return await _deliver_web_push(action, session_factory=session_factory)
    if action.destination == "workspace_file":
        return await _deliver_workspace_file(action)
    if action.destination == "subprocess":
        return await _deliver_subprocess(action)
    raise UnsupportedOutboxDestination(action.destination)


async def _deliver_smtp(action: ClaimedAction) -> DeliveryOutcome:
    # Local import avoids a NotificationService -> outbox import cycle and
    # preserves the existing injectable transport seam for deterministic tests.
    from app.notifications.service import NotificationService

    payload = action.payload
    service = NotificationService(None)  # type: ignore[arg-type]
    if not service._email_configured():
        raise OutboxPreflightUnavailable("SMTP transport is not configured")
    if payload.get("route_digest") != NotificationService._smtp_route_digest():
        raise OutboxPreflightUnavailable("SMTP route changed after enqueue")
    await asyncio.to_thread(
        service._send_email,
        str(payload["reply_token"]),
        str(payload["title"]),
        str(payload["body"]),
    )
    return DeliveryOutcome(status="accepted", response={"transport": "smtp"})


async def _deliver_subprocess(action: ClaimedAction) -> DeliveryOutcome:
    from app.core.execution_policy import (
        CODE_EXECUTION_ERROR_CODE,
        current_code_execution_policy,
    )
    from app.tools.workspace import CodeExecuteArgs, _run_code

    policy = current_code_execution_policy()
    if not policy.available:
        return DeliveryOutcome(
            status="reconciled",
            action_status="cancelled",
            response={
                "transport": "subprocess",
                "error_code": CODE_EXECUTION_ERROR_CODE,
                "reason_code": policy.reason_code,
                "policy_version": policy.policy_version,
            },
        )
    arguments = CodeExecuteArgs.model_validate(action.payload.get("arguments") or {})
    result = await asyncio.to_thread(_run_code, arguments)
    return DeliveryOutcome(response=result)


async def _deliver_web_push(
    action: ClaimedAction,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> DeliveryOutcome:
    from app.models import PushSubscription
    from app.notifications.push import push_service

    subscription_id = int(action.payload["subscription_id"])
    async with session_factory() as db:
        current = await db.get(PushSubscription, subscription_id)
        subscription_matches = bool(
            current
            and current.owner_id == action.owner_id
            and current.endpoint == action.payload.get("endpoint")
            and dict(current.keys or {}) == dict(action.payload.get("keys") or {})
        )
    if not subscription_matches:
        return DeliveryOutcome(
            status="reconciled",
            action_status="cancelled",
            response={"transport": "web_push", "reason": "subscription_revoked"},
        )
    if not push_service.configured:
        raise OutboxPreflightUnavailable("web push transport is not configured")

    payload = push_service.build_payload(
        str(action.payload["title"]),
        str(action.payload["body"]),
        dict(action.payload.get("data") or {}),
    )
    subscription = SimpleNamespace(
        endpoint=str(action.payload["endpoint"]),
        keys=dict(action.payload.get("keys") or {}),
    )
    try:
        await asyncio.to_thread(push_service._send_one, subscription, payload)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) in {404, 410}:
            return DeliveryOutcome(
                status="reconciled",
                action_status="cancelled",
                response={"transport": "web_push", "reason": "subscription_gone"},
                dead_subscription_ids=(subscription_id,),
            )
        raise
    return DeliveryOutcome(
        response={"transport": "web_push", "delivered": 1},
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _workspace_target(relative_path: str) -> Path:
    # Resolve against the live module value so pytest and isolated runtimes can
    # replace WORKSPACE_ROOT without touching repository/user data.
    from app.tools import workspace as workspace_tools

    return workspace_tools._resolve(relative_path)


def _staging_prefix(target: Path, token: str) -> str:
    safe_token = "".join(character for character in token if character.isalnum())[:64]
    return f".{target.name}.{safe_token}."


def _atomic_replace_text(target: Path, content: str, staging_token: str) -> None:
    """Publish UTF-8 bytes atomically and fsync both file and parent entry."""

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=_staging_prefix(target, staging_token),
        suffix=".outbox-tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _durable_unlink(target: Path) -> None:
    target.unlink(missing_ok=True)
    if not target.parent.exists():
        return
    directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def publish_workspace_file(
    target: Path,
    content: str,
    *,
    staging_token: str,
) -> None:
    """Injectable filesystem publisher used only after the durable fence."""

    await asyncio.to_thread(_atomic_replace_text, target, content, staging_token)


async def delete_workspace_file(target: Path) -> None:
    """Injectable filesystem deletion used only after the durable fence."""

    await asyncio.to_thread(_durable_unlink, target)


def _current_file_digest(target: Path) -> str | None:
    if not target.exists():
        return None
    if not target.is_file():
        raise OutboxNeedsReconciliation("workspace target is not a regular file")
    return _sha256_bytes(target.read_bytes())


def _cleanup_workspace_staging(target: Path, staging_token: str) -> None:
    """Delete only temp files whose exact action token owns this target."""

    if not target.parent.exists():
        return
    pattern = f"{_staging_prefix(target, staging_token)}*.outbox-tmp"
    for candidate in target.parent.glob(pattern):
        if candidate.is_file() and candidate.parent == target.parent:
            candidate.unlink(missing_ok=True)


async def _deliver_workspace_file(action: ClaimedAction) -> DeliveryOutcome:
    payload = action.payload
    target = _workspace_target(str(payload["path"]))
    await asyncio.to_thread(_cleanup_workspace_staging, target, action.id)
    current_digest = await asyncio.to_thread(_current_file_digest, target)
    if payload.get("operation", "write") == "delete":
        if current_digest is None:
            return DeliveryOutcome(
                status="reconciled",
                response={"transport": "workspace_file", "deleted": True},
            )
        if current_digest != payload.get("before_sha256"):
            raise OutboxNeedsReconciliation("workspace target changed after enqueue")
        await delete_workspace_file(target)
        if await asyncio.to_thread(_current_file_digest, target) is not None:
            raise OutboxNeedsReconciliation("workspace deletion did not publish")
        return DeliveryOutcome(
            response={"transport": "workspace_file", "deleted": True}
        )

    desired_digest = str(payload["desired_sha256"])
    if current_digest == desired_digest:
        return DeliveryOutcome(
            status="reconciled",
            response={"transport": "workspace_file", "sha256": desired_digest},
        )

    before_exists = bool(payload.get("before_exists"))
    before_digest = payload.get("before_sha256")
    if before_exists:
        if current_digest != before_digest:
            raise OutboxNeedsReconciliation("workspace target changed after enqueue")
    elif current_digest is not None:
        raise OutboxNeedsReconciliation("workspace target appeared after enqueue")

    await publish_workspace_file(
        target,
        str(payload["content"]),
        staging_token=action.id,
    )
    published_digest = await asyncio.to_thread(_current_file_digest, target)
    if published_digest != desired_digest:
        raise OutboxNeedsReconciliation("workspace publication digest mismatch")
    return DeliveryOutcome(
        response={"transport": "workspace_file", "sha256": published_digest}
    )


async def _store_receipt(
    action: ClaimedAction,
    outcome: DeliveryOutcome,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    Notification, Operation, OutboxAction, OutboxReceipt = _models()
    from app.db.uow import run_short_transaction
    from app.models import LearningEvent, PushSubscription, ToolInvocation

    safe_response = redact_data(outcome.response)

    async def persist(db: AsyncSession) -> None:
        row = await db.get(OutboxAction, action.id)
        if row is None:
            raise OutboxNeedsReconciliation("outbox action disappeared")
        if row.status in {"delivered", "cancelled"}:
            return
        if row.claim_token != action.claim_token:
            raise OutboxNeedsReconciliation("outbox claim ownership changed")

        receipt = (
            await db.execute(
                select(OutboxReceipt).where(
                    OutboxReceipt.outbox_action_id == row.id
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            receipt = OutboxReceipt(
                outbox_action_id=row.id,
                action_key=row.action_key,
                status=outcome.status,
                provider_id=outcome.provider_id,
                response=safe_response,
                accepted_at=utc_now(),
            )
            db.add(receipt)
        elif (
            receipt.action_key != row.action_key
            or receipt.status != outcome.status
            or receipt.provider_id != outcome.provider_id
            or dict(receipt.response or {}) != dict(safe_response or {})
        ):
            raise OutboxConflictError("outbox receipt conflicts with claimed outcome")

        row.status = outcome.action_status
        row.completed_at = utc_now()
        row.last_error = ""
        row.updated_at = utc_now()

        notification = (
            await db.get(Notification, row.notification_id)
            if row.notification_id is not None
            else None
        )
        if notification is not None:
            sibling_states = set(
                (
                    await db.execute(
                        select(OutboxAction.status).where(
                            OutboxAction.notification_id == notification.id
                        )
                    )
                ).scalars()
            )
            if sibling_states.intersection({"needs_reconciliation", "delivering"}):
                notification.status = "needs_reconciliation"
            elif sibling_states.intersection({"queued", "retry_pending"}):
                notification.status = "queued"
            elif row.destination == "web_push" and "delivered" in sibling_states:
                notification.status = "pushed"
                notification.sent_at = utc_now()
            elif row.destination == "web_push" and sibling_states == {"cancelled"}:
                notification.status = "skipped"
                notification.sent_at = None
            else:
                notification.status = "sent"
                notification.sent_at = utc_now()

        operation = (
            await db.get(Operation, row.operation_id)
            if row.operation_id is not None
            else None
        )
        if operation is not None:
            previous_operation_status = operation.status
            final_status = str(
                row.payload.get("operation_status_on_delivery", "committed")
            )
            if final_status not in {"committed", "undone"}:
                raise OutboxProtocolError("invalid operation receipt transition")
            operation.status = final_status
            if final_status == "undone":
                operation.undone_at = utc_now()
                if previous_operation_status != "undone":
                    db.add(
                        LearningEvent(
                            owner_id=operation.owner_id,
                            run_id=operation.run_id,
                            event_type="operation.undone",
                            summary=(
                                f"Undid {operation.tool_name} on "
                                f"{operation.entity_type}:{operation.entity_id}"
                            ),
                            payload={
                                "operation_id": operation.id,
                                "tool_name": operation.tool_name,
                                "entity_type": operation.entity_type,
                                "entity_id": operation.entity_id,
                                "outbox_action_id": row.id,
                            },
                            idempotency_key=f"outbox:{row.id}:operation.undone",
                        )
                    )

        if row.invocation_id is not None:
            invocation = await db.get(ToolInvocation, row.invocation_id)
            if invocation is not None:
                sibling_states = set(
                    (
                        await db.execute(
                            select(OutboxAction.status).where(
                                OutboxAction.invocation_id == invocation.id
                            )
                        )
                    ).scalars()
                )
                if sibling_states.intersection({"needs_reconciliation", "delivering"}):
                    invocation.status = "needs_reconciliation"
                elif sibling_states.intersection(
                    {"queued", "retry_pending"}
                ):
                    invocation.status = "pending_delivery"
                elif row.destination == "subprocess" and sibling_states == {"cancelled"}:
                    invocation.status = "cancelled"
                    invocation.completed_at = utc_now()
                    invocation.result_payload = safe_response
                else:
                    invocation.status = "committed"
                    invocation.completed_at = utc_now()
                    if row.destination == "subprocess":
                        invocation.result_payload = safe_response
                invocation.version += 1
                invocation.updated_at = utc_now()

        if outcome.dead_subscription_ids:
            await db.execute(
                delete(PushSubscription).where(
                    PushSubscription.id.in_(outcome.dead_subscription_ids),
                    PushSubscription.owner_id == action.owner_id,
                )
            )
    await run_short_transaction(session_factory, persist)


async def reconcile_action(
    *,
    action_key: str,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    recover_inflight: bool = False,
) -> dict[str, Any]:
    """Reconcile one action; only a proven unchanged workspace base may finish."""

    action = await _claim_reconciliation(
        action_key=action_key,
        session_factory=session_factory,
        recover_inflight=recover_inflight,
    )
    if action is None:
        current = await _current_action_result(
            action_key=action_key,
            session_factory=session_factory,
        )
        current["reconciled"] = current.get("status") == "delivered"
        return current

    if action.destination != "workspace_file":
        return {
            "status": "needs_reconciliation",
            "error_code": "needs_reconciliation",
            "reconciled": False,
        }

    target = _workspace_target(str(action.payload["path"]))
    await asyncio.to_thread(_cleanup_workspace_staging, target, action.id)
    current_digest = await asyncio.to_thread(_current_file_digest, target)
    operation = action.payload.get("operation", "write")
    effect_is_visible = (
        current_digest is None
        if operation == "delete"
        else current_digest == action.payload.get("desired_sha256")
    )
    if effect_is_visible:
        outcome = DeliveryOutcome(
            status="reconciled",
            response={
                "transport": "workspace_file",
                **(
                    {"deleted": True}
                    if operation == "delete"
                    else {"sha256": current_digest}
                ),
            },
        )
    else:
        base_is_unchanged = (
            current_digest == action.payload.get("before_sha256")
            if action.payload.get("before_exists")
            else current_digest is None
        )
        if not base_is_unchanged:
            await _record_uncertain_error(
                action,
                "WorkspaceStateConflict",
                session_factory=session_factory,
            )
            return {
                "status": "needs_reconciliation",
                "error_code": "needs_reconciliation",
                "reconciled": False,
            }
        # A workspace hash equal to the captured base proves that this exact
        # deterministic action is not currently visible.  It is safe to finish
        # the already-persisted intent; SMTP/Web Push never use this shortcut.
        try:
            outcome = await _deliver_workspace_file(action)
        except Exception as exc:
            await _record_uncertain_error(
                action,
                type(exc).__name__,
                session_factory=session_factory,
            )
            return {
                "status": "needs_reconciliation",
                "error_code": "needs_reconciliation",
                "reconciled": False,
            }

    await _store_receipt(action, outcome, session_factory=session_factory)
    return {"status": "delivered", "reconciled": True}


async def _claim_reconciliation(
    *,
    action_key: str,
    session_factory: async_sessionmaker[AsyncSession],
    recover_inflight: bool,
) -> ClaimedAction | None:
    """Allow one reconciler to inspect/publish an uncertain workspace action."""

    from app.db.uow import run_short_transaction

    _, _, OutboxAction, _ = _models()
    now = utc_now()
    stale_before = (
        now
        if recover_inflight
        else now - timedelta(seconds=ACTIVE_DELIVERY_STALE_SECONDS)
    )
    claim_token = str(uuid4())

    async def claim(db: AsyncSession) -> ClaimedAction | None:
        result = await db.execute(
            update(OutboxAction)
            .where(
                OutboxAction.action_key == action_key,
                OutboxAction.destination == "workspace_file",
                (
                    (OutboxAction.status == "needs_reconciliation")
                    | (
                        (OutboxAction.status == "delivering")
                        & (OutboxAction.claimed_at <= stale_before)
                    )
                ),
            )
            .values(
                status="delivering",
                claim_token=claim_token,
                claimed_at=now,
                version=OutboxAction.version + 1,
                updated_at=now,
            )
            .returning(
                OutboxAction.id,
                OutboxAction.owner_id,
                OutboxAction.run_id,
                OutboxAction.invocation_id,
                OutboxAction.notification_id,
                OutboxAction.operation_id,
                OutboxAction.action_key,
                OutboxAction.request_digest,
                OutboxAction.destination,
                OutboxAction.payload,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return ClaimedAction(
            id=row["id"],
            owner_id=row["owner_id"],
            run_id=row["run_id"],
            invocation_id=row["invocation_id"],
            notification_id=row["notification_id"],
            operation_id=row["operation_id"],
            action_key=row["action_key"],
            request_digest=row["request_digest"],
            destination=row["destination"],
            payload=dict(row["payload"] or {}),
            claim_token=claim_token,
        )

    return await run_short_transaction(session_factory, claim)


async def reconcile_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    recover_inflight: bool = False,
) -> dict[str, Any]:
    """Inspect one uncertain workspace action; transports remain manual."""

    _, _, OutboxAction, _ = _models()
    stale_before = (
        utc_now()
        if recover_inflight
        else utc_now() - timedelta(seconds=ACTIVE_DELIVERY_STALE_SECONDS)
    )
    async with session_factory() as db:
        action_key = await db.scalar(
            select(OutboxAction.action_key)
            .where(
                OutboxAction.destination == "workspace_file",
                (
                    (OutboxAction.status == "needs_reconciliation")
                    | (
                        (OutboxAction.status == "delivering")
                        & (OutboxAction.claimed_at <= stale_before)
                    )
                ),
            )
            .order_by(OutboxAction.claimed_at, OutboxAction.created_at)
            .limit(1)
        )
    if action_key is None:
        return {"status": "idle", "reconciled": False}
    return await reconcile_action(
        action_key=action_key,
        session_factory=session_factory,
        recover_inflight=recover_inflight,
    )


async def reconcile_all_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    limit: int = 100,
    recover_inflight: bool = False,
) -> dict[str, Any]:
    """Inspect a bounded snapshot of uncertain workspace actions exactly once."""

    _, _, OutboxAction, _ = _models()
    bounded_limit = min(max(int(limit), 1), 500)
    stale_before = (
        utc_now()
        if recover_inflight
        else utc_now() - timedelta(seconds=ACTIVE_DELIVERY_STALE_SECONDS)
    )
    async with session_factory() as db:
        action_keys = list(
            (
                await db.execute(
                    select(OutboxAction.action_key)
                    .where(
                        OutboxAction.destination == "workspace_file",
                        (
                            (OutboxAction.status == "needs_reconciliation")
                            | (
                                (OutboxAction.status == "delivering")
                                & (OutboxAction.claimed_at <= stale_before)
                            )
                        ),
                    )
                    .order_by(OutboxAction.claimed_at, OutboxAction.created_at)
                    .limit(bounded_limit)
                )
            ).scalars()
        )
    reconciled = 0
    still_uncertain = 0
    for action_key in action_keys:
        result = await reconcile_action(
            action_key=action_key,
            session_factory=session_factory,
            recover_inflight=recover_inflight,
        )
        if result.get("reconciled"):
            reconciled += 1
        else:
            still_uncertain += 1
    return {
        "status": "completed" if action_keys else "idle",
        "inspected": len(action_keys),
        "reconciled": reconciled,
        "needs_reconciliation": still_uncertain,
    }


async def recover_interrupted_deliveries(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> dict[str, Any]:
    """Adopt outbox fences once at startup while the runtime lease is exclusive."""

    from app.db.uow import run_short_transaction
    from app.models import ToolInvocation

    Notification, Operation, OutboxAction, _ = _models()

    async def fence_non_reconcilable(db: AsyncSession) -> int:
        actions = list(
            (
                await db.execute(
                    select(OutboxAction).where(
                        OutboxAction.status == "delivering",
                        OutboxAction.destination != "workspace_file",
                    )
                )
            ).scalars()
        )
        for action in actions:
            action.status = "needs_reconciliation"
            action.last_error = "process_interrupted"
            action.updated_at = utc_now()
            if action.invocation_id is not None:
                await db.execute(
                    update(ToolInvocation)
                    .where(ToolInvocation.id == action.invocation_id)
                    .values(
                        status="needs_reconciliation",
                        version=ToolInvocation.version + 1,
                        updated_at=utc_now(),
                    )
                )
            if action.notification_id is not None:
                await db.execute(
                    update(Notification)
                    .where(Notification.id == action.notification_id)
                    .values(status="needs_reconciliation")
                )
            if action.operation_id is not None:
                await db.execute(
                    update(Operation)
                    .where(Operation.id == action.operation_id)
                    .values(status="needs_reconciliation")
                )
        return len(actions)

    fenced = await run_short_transaction(session_factory, fence_non_reconcilable)
    workspace = await reconcile_all_once(
        session_factory=session_factory,
        limit=500,
        recover_inflight=True,
    )
    return {
        "status": "completed",
        "fenced_external": fenced,
        "workspace": workspace,
    }


async def drain_outbox(
    stop_event: asyncio.Event,
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    idle_seconds: float = 1.0,
    reconcile_interval_seconds: float = 30.0,
) -> None:
    """Bounded lifespan worker; cancellation never widens delivery semantics."""

    loop = asyncio.get_running_loop()
    next_reconciliation = 0.0
    while not stop_event.is_set():
        if loop.time() >= next_reconciliation:
            try:
                await reconcile_all_once(session_factory=session_factory, limit=100)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A later bounded cycle can retry DB availability.  Never log
                # action payloads, recipients, code, or workspace content.
                pass
            next_reconciliation = loop.time() + max(
                float(reconcile_interval_seconds), 1.0
            )
        try:
            result = await dispatch_once(session_factory=session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            result = {"status": "idle", "delivered": False}
        if result["status"] == "idle":
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_seconds)
            except TimeoutError:
                pass
