from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.outbox as outbox_module
from app.api.operations import undo_operation
from app.db.database import AsyncSessionLocal
from app.db.uow import commit as commit_uow
from app.models import (
    CalendarEvent,
    LearningEvent,
    Operation,
    OutboxAction,
    OutboxReceipt,
)
from app.outbox import dispatch_once, reconcile_action


@pytest.mark.asyncio
async def test_database_undo_concurrent_exact_replay_applies_inverse_once() -> None:
    """Two stale callers must converge on one inverse transaction and audit event."""

    starts_at = datetime.now(timezone.utc) + timedelta(days=1)
    async with AsyncSessionLocal() as db:
        calendar_event = CalendarEvent(
            owner_id="local",
            title="rescheduled title",
            starts_at=starts_at,
        )
        db.add(calendar_event)
        await db.flush()
        operation = Operation(
            owner_id="local",
            tool_name="calendar.patch",
            entity_type="calendar_event",
            entity_id=str(calendar_event.id),
            forward_patch={"changes": {"title": "rescheduled title"}},
            inverse_patch={"changes": {"title": "original title"}},
            status="committed",
        )
        db.add(operation)
        await commit_uow(db)
        operation_id = operation.id
        calendar_event_id = calendar_event.id

    async with AsyncSessionLocal() as first_db, AsyncSessionLocal() as second_db:
        # Keep both committed snapshots resident so the race exercises the
        # committed -> undo_pending compare-and-swap, not only a later replay.
        first_snapshot = await first_db.get(Operation, operation_id)
        second_snapshot = await second_db.get(Operation, operation_id)
        assert first_snapshot is not None and first_snapshot.status == "committed"
        assert second_snapshot is not None and second_snapshot.status == "committed"

        first_result, second_result = await asyncio.gather(
            undo_operation(operation_id, first_db),
            undo_operation(operation_id, second_db),
        )

    assert first_result.id == second_result.id == operation_id
    assert first_result.status == second_result.status == "undone"

    async with AsyncSessionLocal() as replay_db:
        replay = await undo_operation(operation_id, replay_db)
        assert replay.id == operation_id
        assert replay.status == "undone"

    async with AsyncSessionLocal() as db:
        stored_event = await db.get(CalendarEvent, calendar_event_id)
        stored_operation = await db.get(Operation, operation_id)
        audit_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.event_type == "operation.undone",
                LearningEvent.payload["operation_id"].as_string() == operation_id,
            )
        )

    assert stored_event is not None and stored_event.title == "original title"
    assert stored_operation is not None and stored_operation.status == "undone"
    assert audit_count == 1


@pytest.mark.asyncio
async def test_workspace_undo_rejects_file_changed_after_forward_write(
    isolated_runtime_root,
) -> None:
    """Undo must fail closed when current bytes no longer match its forward digest."""

    workspace_root = isolated_runtime_root / "data" / "workspace"
    target = workspace_root / "drifted.txt"
    forward_content = "bytes written by the original operation"
    later_content = "a later independent edit must be preserved"
    target.write_text(later_content, encoding="utf-8")
    desired_sha256 = hashlib.sha256(forward_content.encode("utf-8")).hexdigest()

    async with AsyncSessionLocal() as db:
        operation = Operation(
            owner_id="local",
            tool_name="file.write",
            entity_type="workspace_file",
            entity_id="drifted.txt",
            forward_patch={
                "path": "drifted.txt",
                "size": len(forward_content.encode("utf-8")),
                "sha256": desired_sha256,
            },
            inverse_patch={
                "path": "drifted.txt",
                "previous": "content before the original operation",
                "delete": False,
            },
            status="committed",
        )
        db.add(operation)
        await commit_uow(db)
        operation_id = operation.id

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await undo_operation(operation_id, db)

    assert exc_info.value.status_code == 409
    assert target.read_text(encoding="utf-8") == later_content

    async with AsyncSessionLocal() as db:
        stored_operation = await db.get(Operation, operation_id)
        outbox_count = await db.scalar(
            select(func.count(OutboxAction.id)).where(
                OutboxAction.operation_id == operation_id
            )
        )
        audit_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.payload["operation_id"].as_string() == operation_id
            )
        )

    assert stored_operation is not None and stored_operation.status == "committed"
    assert outbox_count == 0
    assert audit_count == 0


@pytest.mark.asyncio
async def test_workspace_undo_legacy_row_without_forward_digest_fails_closed(
    isolated_runtime_root,
) -> None:
    """A legacy operation cannot compensate bytes it cannot identify."""

    workspace_root = isolated_runtime_root / "data" / "workspace"
    target = workspace_root / "legacy.txt"
    current_content = "current bytes with unknown forward provenance"
    target.write_text(current_content, encoding="utf-8")

    async with AsyncSessionLocal() as db:
        operation = Operation(
            owner_id="local",
            tool_name="file.write",
            entity_type="workspace_file",
            entity_id="legacy.txt",
            forward_patch={"path": "legacy.txt"},
            inverse_patch={
                "path": "legacy.txt",
                "previous": "untrusted legacy previous bytes",
                "delete": False,
            },
            status="committed",
        )
        db.add(operation)
        await commit_uow(db)
        operation_id = operation.id

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc_info:
            await undo_operation(operation_id, db)

    assert exc_info.value.status_code == 409
    assert "digest" in str(exc_info.value.detail).lower()
    assert target.read_text(encoding="utf-8") == current_content
    async with AsyncSessionLocal() as db:
        stored_operation = await db.get(Operation, operation_id)
        outbox_count = await db.scalar(
            select(func.count(OutboxAction.id)).where(
                OutboxAction.operation_id == operation_id
            )
        )

    assert stored_operation is not None and stored_operation.status == "committed"
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_workspace_undo_is_durable_before_publish_and_exact_after_delivery(
    isolated_runtime_root,
) -> None:
    """A successful file undo is requested, delivered, and replayed exactly once."""

    workspace_root = isolated_runtime_root / "data" / "workspace"
    target = workspace_root / "restored.txt"
    previous_content = "content before the forward write"
    forward_content = "content produced by the forward write"
    target.write_text(forward_content, encoding="utf-8")
    forward_digest = hashlib.sha256(forward_content.encode("utf-8")).hexdigest()

    async with AsyncSessionLocal() as db:
        operation = Operation(
            owner_id="local",
            tool_name="file.write",
            entity_type="workspace_file",
            entity_id="restored.txt",
            forward_patch={
                "path": "restored.txt",
                "size": len(forward_content.encode("utf-8")),
                "sha256": forward_digest,
            },
            inverse_patch={
                "path": "restored.txt",
                "previous": previous_content,
                "delete": False,
            },
            status="committed",
        )
        db.add(operation)
        await commit_uow(db)
        operation_id = operation.id

    async with AsyncSessionLocal() as db:
        requested = await undo_operation(operation_id, db)
        assert requested.status == "undo_pending"

    # The API transaction contains only the durable intent; delivery happens
    # later and therefore cannot leave an unrecorded filesystem mutation.
    assert target.read_text(encoding="utf-8") == forward_content
    async with AsyncSessionLocal() as db:
        action_count = await db.scalar(
            select(func.count(OutboxAction.id)).where(
                OutboxAction.operation_id == operation_id
            )
        )
        requested_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.event_type == "operation.undo_requested",
                LearningEvent.payload["operation_id"].as_string() == operation_id,
            )
        )
    assert action_count == 1
    assert requested_count == 1

    delivered = await dispatch_once(session_factory=AsyncSessionLocal)
    assert delivered["status"] == "delivered"
    assert target.read_text(encoding="utf-8") == previous_content

    async with AsyncSessionLocal() as db:
        replay = await undo_operation(operation_id, db)
        assert replay.status == "undone"

    async with AsyncSessionLocal() as db:
        stored_operation = await db.get(Operation, operation_id)
        final_action_count = await db.scalar(
            select(func.count(OutboxAction.id)).where(
                OutboxAction.operation_id == operation_id
            )
        )
        final_requested_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.event_type == "operation.undo_requested",
                LearningEvent.payload["operation_id"].as_string() == operation_id,
            )
        )
        undone_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.event_type == "operation.undone",
                LearningEvent.payload["operation_id"].as_string() == operation_id,
            )
        )

    assert stored_operation is not None and stored_operation.status == "undone"
    assert stored_operation.undone_at is not None
    assert final_action_count == 1
    assert final_requested_count == 1
    assert undone_count == 1


@pytest.mark.asyncio
async def test_workspace_undo_publish_then_receipt_kill_reconciles_without_rewrite(
    isolated_runtime_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after publish is recovered by hash, never by replaying the write."""

    class InjectedProcessCrash(BaseException):
        pass

    workspace_root = isolated_runtime_root / "data" / "workspace"
    target = workspace_root / "receipt-kill.txt"
    previous_content = "bytes restored by undo"
    forward_content = "bytes from the forward operation"
    target.write_text(forward_content, encoding="utf-8")
    forward_digest = hashlib.sha256(forward_content.encode("utf-8")).hexdigest()

    async with AsyncSessionLocal() as db:
        operation = Operation(
            owner_id="local",
            tool_name="file.write",
            entity_type="workspace_file",
            entity_id="receipt-kill.txt",
            forward_patch={"path": "receipt-kill.txt", "sha256": forward_digest},
            inverse_patch={
                "path": "receipt-kill.txt",
                "previous": previous_content,
                "delete": False,
            },
            status="committed",
        )
        db.add(operation)
        await commit_uow(db)
        operation_id = operation.id

    async with AsyncSessionLocal() as db:
        requested = await undo_operation(operation_id, db)
        assert requested.status == "undo_pending"
        action_key = await db.scalar(
            select(OutboxAction.action_key).where(
                OutboxAction.operation_id == operation_id
            )
        )
    assert action_key is not None

    killpoint_seen = False

    def kill_receipt(_sync_session) -> None:
        nonlocal killpoint_seen
        if target.read_text(encoding="utf-8") == previous_content:
            killpoint_seen = True
            raise InjectedProcessCrash("killed after undo publish before receipt")

    event.listen(AsyncSession.sync_session_class, "before_commit", kill_receipt)
    try:
        with pytest.raises(InjectedProcessCrash):
            await dispatch_once(session_factory=AsyncSessionLocal)
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", kill_receipt)

    assert killpoint_seen
    assert target.read_text(encoding="utf-8") == previous_content

    async def reject_rewrite(*_args, **_kwargs) -> None:
        raise AssertionError("reconciliation attempted to publish an already-visible undo")

    monkeypatch.setattr(outbox_module, "publish_workspace_file", reject_rewrite)
    reconciled = await reconcile_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
        recover_inflight=True,
    )
    assert reconciled == {"status": "delivered", "reconciled": True}

    async with AsyncSessionLocal() as db:
        replay = await undo_operation(operation_id, db)
        receipt_count = await db.scalar(
            select(func.count(OutboxReceipt.id)).join(
                OutboxAction,
                OutboxReceipt.outbox_action_id == OutboxAction.id,
            ).where(OutboxAction.operation_id == operation_id)
        )
        request_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.event_type == "operation.undo_requested",
                LearningEvent.payload["operation_id"].as_string() == operation_id,
            )
        )
        undone_count = await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.event_type == "operation.undone",
                LearningEvent.payload["operation_id"].as_string() == operation_id,
            )
        )

    assert replay.status == "undone"
    assert receipt_count == 1
    assert request_count == 1
    assert undone_count == 1
