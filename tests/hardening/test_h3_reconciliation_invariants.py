"""H3 fail-closed scope and terminal-cleanup invariants."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import AsyncSessionLocal
import app.runtime.scheduler as scheduler_module
from app.models import (
    AgentRun,
    ChatMessage,
    Notification,
    Plan,
    QueuedMessage,
    RunApproval,
    RunEvent,
    Session,
    ToolInvocation,
)
from app.notifications.email import EmailReplyPoller
from app.runtime.scheduler import ProactiveScheduler
from app.runtime.checkpoints import make_checkpoint
from app.runtime.state import (
    RunStateError,
    ensure_root_scope_available,
    reconcile_run_after_restart,
)


@pytest.mark.asyncio
async def test_reconciliation_run_keeps_exclusive_root_scope() -> None:
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="fenced scope")
        db.add(session)
        await db.flush()
        db.add(AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="uncertain legacy work",
            status="needs_reconciliation",
            phase="reconciling",
            status_reason="legacy_checkpoint_ambiguous",
        ))
        await db.commit()
        session_id = session.id

    async with AsyncSessionLocal() as db:
        with pytest.raises(RunStateError, match="root_run_scope_busy"):
            await ensure_root_scope_available(
                db,
                owner_id="local",
                plan_id=None,
                session_id=session_id,
            )


@pytest.mark.asyncio
async def test_email_reply_and_heartbeat_share_one_serialized_root_arbitration(
    monkeypatch,
) -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="email heartbeat race")
        db.add(plan)
        await db.flush()
        session = Session(owner_id="local", plan_id=plan.id, title="race session")
        db.add(session)
        await db.flush()
        notification = Notification(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            channel="email",
            title="continue",
            body="reply here",
            status="sent",
        )
        db.add(notification)
        await db.commit()
        plan_id = plan.id
        token = notification.reply_token

    poller = EmailReplyPoller()
    monkeypatch.setattr(EmailReplyPoller, "configured", property(lambda _self: True))
    monkeypatch.setattr(poller, "_fetch_unseen", lambda: [{
        "uid": "race-1",
        "reply_token": token,
        "subject": "Re: continue",
        "body": "durable email reply",
    }])
    monkeypatch.setattr(poller, "_mark_seen", lambda _uids: None)

    def suppress_runtime_start(_run_id, coroutine):
        coroutine.close()
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(scheduler_module, "start_tracked_task", suppress_runtime_start)
    scheduler = ProactiveScheduler()

    async def poll_email():
        async with AsyncSessionLocal() as db:
            return await poller.poll(db, "local")

    heartbeat_result, email_result = await asyncio.gather(
        scheduler.trigger_now(plan_id=plan_id, objective="serialized heartbeat"),
        poll_email(),
        return_exceptions=True,
    )
    assert not isinstance(email_result, BaseException)
    assert isinstance(heartbeat_result, AgentRun) or isinstance(heartbeat_result, RunStateError)

    async with AsyncSessionLocal() as db:
        roots = list((await db.execute(select(AgentRun).where(
            AgentRun.plan_id == plan_id,
            AgentRun.parent_run_id.is_(None),
        ))).scalars())
        email_messages = [
            message for message in (await db.execute(select(ChatMessage))).scalars()
            if message.message_metadata.get("email_uid")
        ]
        queued_replies = [
            message for message in (await db.execute(select(QueuedMessage))).scalars()
            if message.message_metadata.get("email_uid")
        ]

    assert len(roots) == 1
    assert len(email_messages) + len(queued_replies) == 1
    assert (roots[0].trigger == "email_reply") == bool(email_messages)
    assert (roots[0].trigger == "heartbeat") == bool(queued_replies)


@pytest.mark.asyncio
async def test_scope_invalid_child_terminalizes_and_projects_in_one_recovery() -> None:
    now = utc_now()
    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="already finished parent",
            status="completed",
            phase="terminal",
            completed_at=now,
        )
        db.add(parent)
        await db.flush()
        checkpoint = make_checkpoint(
            kind="subagent",
            phase="awaiting_model",
            step=1,
            messages=[{"role": "user", "content": "durable child state"}],
            state_version=4,
        )
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="orphaned child",
            status="running",
            phase="awaiting_model",
            state_version=4,
            checkpoint_schema_version=1,
            checkpoint=checkpoint,
            lease_token="orphaned-lease",
            lease_owner="dead-worker",
            lease_acquired_at=now - timedelta(seconds=2),
            lease_expires_at=now + timedelta(seconds=30),
            available_at=now + timedelta(seconds=60),
        )
        db.add(child)
        await db.flush()
        invocation = ToolInvocation(
            owner_id="local",
            run_id=child.id,
            idempotency_key="orphaned-approval-invocation",
            tool_name="plan_create",
            tool_call_id="orphaned-call",
            args_hash="a" * 64,
            status="pending_approval",
        )
        db.add(invocation)
        await db.flush()
        approval = RunApproval(
            owner_id="local",
            run_id=child.id,
            invocation_id=invocation.id,
            tool_call_id="orphaned-call",
            tool_name="plan_create",
            tool_call={"id": "orphaned-call", "name": "plan_create", "arguments": "{}"},
            decision="pending",
        )
        db.add(approval)
        await db.flush()
        child.pending_approval = {"approval_id": approval.id}
        await db.commit()
        parent_id = parent.id
        child_id = child.id
        approval_id = approval.id
        invocation_id = invocation.id

    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        child_id,
        scope_valid=False,
    ) is False

    async with AsyncSessionLocal() as db:
        child = await db.get(AgentRun, child_id)
        approval = await db.get(RunApproval, approval_id)
        invocation = await db.get(ToolInvocation, invocation_id)
        projections = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == parent_id,
            RunEvent.event_key == f"child:{child_id}:terminal",
        ))).scalars())

        assert child is not None
        assert child.status == "failed"
        assert child.phase == "terminal"
        assert child.status_reason == "scope_unavailable"
        assert child.checkpoint is None
        assert child.checkpoint_schema_version is None
        assert child.pending_approval is None
        assert child.lease_token is None
        assert child.lease_owner is None
        assert child.lease_acquired_at is None
        assert child.lease_expires_at is None
        assert child.available_at is None
        assert approval is not None and approval.consumed_at is not None
        assert invocation is not None and invocation.status == "cancelled"
        assert len(projections) == 1
        assert projections[0].payload["status"] == "failed"

    # Recovery is idempotent; it must not need a second restart to project the
    # newly terminal child or duplicate the parent's completion event.
    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        child_id,
        scope_valid=False,
    ) is False
    async with AsyncSessionLocal() as db:
        projection_count = len(list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == parent_id,
            RunEvent.event_key == f"child:{child_id}:terminal",
        ))).scalars()))
    assert projection_count == 1
