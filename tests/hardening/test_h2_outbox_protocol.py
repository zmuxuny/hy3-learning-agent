from __future__ import annotations

import asyncio
import json
import threading

import pytest
from sqlalchemy import select

import app.outbox as outbox
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.uow import rollback as rollback_uow
from app.models import (
    AgentRun,
    Notification,
    Operation,
    OutboxAction,
    OutboxReceipt,
    PushSubscription,
    ToolInvocation,
)
from app.notifications.service import NotificationService
from app.notifications.push import push_service
from app.outbox import (
    OutboxConflictError,
    dispatch_action,
    dispatch_once,
    enqueue_smtp_diagnostic,
    recover_interrupted_deliveries,
)
from app.tools import ToolContext, execute_tool
import app.tools.workspace as workspace_tools


async def _create_run() -> str:
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="H2 durable outbox protocol",
            status="running",
        )
        db.add(run)
        await db.commit()
        return run.id


@pytest.mark.asyncio
async def test_smtp_action_is_fenced_once_and_exact_retry_does_not_resend(monkeypatch):
    run_id = await _create_run()
    sends: list[tuple[str, str, str]] = []
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)
    monkeypatch.setattr(
        NotificationService,
        "_send_email",
        lambda _self, token, title, body: sends.append((token, title, body)),
    )
    raw = json.dumps(
        {"title": "H2 SMTP", "body": "durable", "channels": ["email"]},
        sort_keys=True,
    )

    async with AsyncSessionLocal() as db:
        first = await execute_tool(
            "notification_send",
            raw,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="smtp-action",
            ),
        )
    assert first["status"] == "pending_delivery"
    assert sends == []

    outcomes = await asyncio.gather(
        dispatch_once(session_factory=AsyncSessionLocal),
        dispatch_once(session_factory=AsyncSessionLocal),
    )
    assert sorted(item["status"] for item in outcomes) == ["delivered", "idle"]
    assert len(sends) == 1

    async with AsyncSessionLocal() as db:
        action = (await db.execute(select(OutboxAction))).scalar_one()
        receipt = (await db.execute(select(OutboxReceipt))).scalar_one()
        invocation = (await db.execute(select(ToolInvocation))).scalar_one()
        email = (
            await db.execute(select(Notification).where(Notification.channel == "email"))
        ).scalar_one()
        assert action.status == "delivered"
        assert action.attempt == 1
        assert receipt.action_key == action.action_key
        assert invocation.status == "committed"
        assert email.status == "sent"

    async with AsyncSessionLocal() as db:
        replay = await execute_tool(
            "notification_send",
            raw,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="smtp-action",
            ),
        )
    assert replay["replayed"] is True
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_web_push_creates_one_action_per_subscription_and_aggregates_receipts(
    monkeypatch,
):
    run_id = await _create_run()
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:test.invalid")
    delivered: list[str] = []
    monkeypatch.setattr(
        push_service,
        "_send_one",
        lambda subscription, _payload: delivered.append(subscription.endpoint),
    )
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                PushSubscription(
                    owner_id="local",
                    endpoint="https://push.invalid/one",
                    keys={"p256dh": "one", "auth": "one"},
                ),
                PushSubscription(
                    owner_id="local",
                    endpoint="https://push.invalid/two",
                    keys={"p256dh": "two", "auth": "two"},
                ),
            ]
        )
        await db.commit()

    async with AsyncSessionLocal() as db:
        result = await execute_tool(
            "notification_send",
            json.dumps(
                {"title": "H2 Push", "body": "fanout", "channels": ["browser"]}
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="push-action",
            ),
        )
    assert result["status"] == "pending_delivery"

    async with AsyncSessionLocal() as db:
        actions = list(
            (
                await db.execute(
                    select(OutboxAction)
                    .where(OutboxAction.destination == "web_push")
                    .order_by(OutboxAction.action_key)
                )
            ).scalars()
        )
        assert len(actions) == 2
        assert len({action.action_key for action in actions}) == 2
        assert len({action.notification_id for action in actions}) == 1
        action_keys = [action.action_key for action in actions]

    for action_key in action_keys:
        outcome = await dispatch_action(
            action_key=action_key,
            session_factory=AsyncSessionLocal,
        )
        assert outcome["status"] == "delivered"

    async with AsyncSessionLocal() as db:
        browser = (
            await db.execute(
                select(Notification).where(Notification.channel == "browser")
            )
        ).scalar_one()
        invocation = (await db.execute(select(ToolInvocation))).scalar_one()
        receipts = list((await db.execute(select(OutboxReceipt))).scalars())
        assert browser.status == "pushed"
        assert invocation.status == "committed"
        assert len(receipts) == 2
    assert sorted(delivered) == [
        "https://push.invalid/one",
        "https://push.invalid/two",
    ]


@pytest.mark.asyncio
async def test_workspace_publish_occurs_only_after_intent_commit(
    isolated_runtime_root,
):
    run_id = await _create_run()
    target = isolated_runtime_root / "data" / "workspace" / "artifact.txt"
    raw = json.dumps(
        {"path": "artifact.txt", "content": "durable bytes", "overwrite": False}
    )
    async with AsyncSessionLocal() as db:
        result = await execute_tool(
            "file_write",
            raw,
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="file-action",
            ),
        )
    assert result["status"] == "pending_delivery"
    assert not target.exists()

    action_key = result["data"]["outbox_action_key"]
    delivered = await dispatch_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
    )
    assert delivered["status"] == "delivered"
    assert target.read_text(encoding="utf-8") == "durable bytes"

    async with AsyncSessionLocal() as db:
        operation = (await db.execute(select(Operation))).scalar_one()
        invocation = (await db.execute(select(ToolInvocation))).scalar_one()
        assert operation.status == "committed"
        assert invocation.status == "committed"


@pytest.mark.asyncio
async def test_revoked_push_subscription_is_cancelled_without_delivery(monkeypatch):
    run_id = await _create_run()
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:test.invalid")
    sends: list[str] = []
    monkeypatch.setattr(
        push_service,
        "_send_one",
        lambda subscription, _payload: sends.append(subscription.endpoint),
    )
    async with AsyncSessionLocal() as db:
        subscription = PushSubscription(
            owner_id="local",
            endpoint="https://push.invalid/revoked",
            keys={"p256dh": "key", "auth": "auth"},
        )
        db.add(subscription)
        await db.commit()

    async with AsyncSessionLocal() as db:
        result = await execute_tool(
            "notification_send",
            json.dumps(
                {"title": "H2 revoke", "body": "cancel", "channels": ["browser"]}
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="revoked-push",
            ),
        )
    action_key = result["data"]["outbox_action_keys"][0]

    async with AsyncSessionLocal() as db:
        subscription = (await db.execute(select(PushSubscription))).scalar_one()
        await db.delete(subscription)
        await db.commit()

    outcome = await dispatch_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
    )
    assert outcome["status"] == "cancelled"
    assert sends == []
    async with AsyncSessionLocal() as db:
        browser = (
            await db.execute(
                select(Notification).where(Notification.channel == "browser")
            )
        ).scalar_one()
        assert browser.status == "skipped"


@pytest.mark.asyncio
async def test_targeted_subprocess_dispatch_reuses_terminal_receipt(monkeypatch):
    run_id = await _create_run()
    calls = 0

    def fake_run(_args):
        nonlocal calls
        calls += 1
        return {
            "exit_code": 0,
            "stdout": "receipt-output",
            "stderr": "",
            "truncated": False,
        }

    monkeypatch.setattr(workspace_tools, "_run_code", fake_run)
    async with AsyncSessionLocal() as db:
        result = await execute_tool(
            "code_execute",
            json.dumps({"language": "python", "code": "print('ignored')"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="subprocess-action",
            ),
        )
    assert result["data"]["stdout"] == "receipt-output"

    async with AsyncSessionLocal() as db:
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.destination == "subprocess")
            )
        ).scalar_one()
    replay = await dispatch_action(
        action_key=action.action_key,
        session_factory=AsyncSessionLocal,
    )
    assert replay["status"] == "delivered"
    assert replay["data"]["stdout"] == "receipt-output"
    assert calls == 1


@pytest.mark.asyncio
async def test_smtp_diagnostic_exact_replay_and_route_drift_are_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(settings, "SMTP_PORT", 465)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@example.invalid")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "test-only")
    monkeypatch.setattr(settings, "SMTP_FROM", "sender@example.invalid")
    monkeypatch.setattr(settings, "SMTP_TO", "recipient@example.invalid")
    monkeypatch.setattr(settings, "SMTP_USE_SSL", True)
    sends = 0

    def fake_send(*_args):
        nonlocal sends
        sends += 1

    monkeypatch.setattr(NotificationService, "_send_email", fake_send)
    async with AsyncSessionLocal() as db:
        first = await enqueue_smtp_diagnostic(
            db,
            owner_id="local",
            action_id="diagnostic-1",
            title="SMTP diagnostic",
            body="test body",
        )
        await db.commit()
        first_id = first.id
    async with AsyncSessionLocal() as db:
        replay = await enqueue_smtp_diagnostic(
            db,
            owner_id="local",
            action_id="diagnostic-1",
            title="SMTP diagnostic",
            body="test body",
        )
        assert replay.id == first_id
        with pytest.raises(OutboxConflictError):
            await enqueue_smtp_diagnostic(
                db,
                owner_id="local",
                action_id="diagnostic-1",
                title="changed request",
                body="test body",
            )

    monkeypatch.setattr(settings, "SMTP_TO", "changed@example.invalid")
    outcome = await dispatch_once(session_factory=AsyncSessionLocal)
    assert outcome["status"] == "retry_pending"
    assert sends == 0


@pytest.mark.asyncio
async def test_uncommitted_outbox_savepoint_cannot_escape_caller_rollback():
    async with AsyncSessionLocal() as db:
        action = await enqueue_smtp_diagnostic(
            db,
            owner_id="local",
            action_id="rolled-back-diagnostic",
            title="Rollback probe",
            body="must remain invisible",
        )
        assert action.id is not None
        await rollback_uow(db)

    async with AsyncSessionLocal() as db:
        persisted = (await db.execute(select(OutboxAction))).scalar_one_or_none()
    assert persisted is None


@pytest.mark.asyncio
async def test_startup_fences_interrupted_transport_without_resend(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@example.invalid")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "test-only")
    monkeypatch.setattr(settings, "SMTP_TO", "recipient@example.invalid")
    sends = 0

    def fake_send(*_args):
        nonlocal sends
        sends += 1

    monkeypatch.setattr(NotificationService, "_send_email", fake_send)
    async with AsyncSessionLocal() as db:
        action = await enqueue_smtp_diagnostic(
            db,
            owner_id="local",
            action_id="interrupted-diagnostic",
            title="Interrupted",
            body="must not replay",
        )
        action.status = "delivering"
        action.claim_token = "dead-worker"
        await db.commit()

    recovered = await recover_interrupted_deliveries(
        session_factory=AsyncSessionLocal
    )
    assert recovered["fenced_external"] == 1
    assert sends == 0
    async with AsyncSessionLocal() as db:
        action = (await db.execute(select(OutboxAction))).scalar_one()
        assert action.status == "needs_reconciliation"


@pytest.mark.asyncio
async def test_targeted_dispatch_waits_for_active_worker_receipt(monkeypatch):
    run_id = await _create_run()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_run(_args):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=3)
        return {"exit_code": 0, "stdout": "winner", "stderr": "", "truncated": False}

    monkeypatch.setattr(workspace_tools, "_run_code", blocked_run)

    async def execute():
        async with AsyncSessionLocal() as db:
            return await execute_tool(
                "code_execute",
                json.dumps({"language": "python", "code": "print('winner')"}),
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="active-subprocess",
                ),
            )

    winner = asyncio.create_task(execute())
    assert await asyncio.to_thread(entered.wait, 3)
    async with AsyncSessionLocal() as db:
        action_key = await db.scalar(
            select(OutboxAction.action_key).where(
                OutboxAction.destination == "subprocess"
            )
        )
    waiter = asyncio.create_task(
        dispatch_action(
            action_key=str(action_key),
            session_factory=AsyncSessionLocal,
            wait_for_active_seconds=3,
        )
    )
    await asyncio.sleep(0.05)
    release.set()
    winner_result, waiter_result = await asyncio.gather(winner, waiter)
    assert winner_result["data"]["stdout"] == "winner"
    assert waiter_result["status"] == "delivered"
    assert waiter_result["data"]["stdout"] == "winner"
    assert calls == 1


@pytest.mark.asyncio
async def test_targeted_dispatch_default_wait_covers_the_active_claim_lease(monkeypatch):
    checks = 0

    async def no_claim(*_args, **_kwargs):
        return None

    async def still_active(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        return {"status": "in_progress", "delivered": False}

    monkeypatch.setattr(outbox, "_claim_next", no_claim)
    monkeypatch.setattr(outbox, "_current_action_result", still_active)
    monkeypatch.setattr(outbox, "ACTIVE_DELIVERY_STALE_SECONDS", 0.05)
    monkeypatch.setattr(outbox, "ACTIVE_DELIVERY_RECEIPT_GRACE_SECONDS", 0.05)

    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(
        dispatch_action(action_key="active-lease"),
        timeout=0.5,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert result["status"] == "in_progress"
    assert elapsed >= 0.09
    assert checks >= 4


@pytest.mark.asyncio
async def test_retrying_push_sibling_cannot_hide_an_ambiguous_delivery(monkeypatch):
    run_id = await _create_run()
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:test.invalid")
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                PushSubscription(
                    owner_id="local",
                    endpoint="https://push.invalid/ambiguous",
                    keys={"p256dh": "one", "auth": "one"},
                ),
                PushSubscription(
                    owner_id="local",
                    endpoint="https://push.invalid/retry",
                    keys={"p256dh": "two", "auth": "two"},
                ),
            ]
        )
        await db.commit()
    async with AsyncSessionLocal() as db:
        result = await execute_tool(
            "notification_send",
            json.dumps(
                {"title": "Sibling states", "body": "fanout", "channels": ["browser"]}
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="sibling-states",
            ),
        )
    action_keys = result["data"]["outbox_action_keys"]

    monkeypatch.setattr(
        push_service,
        "_send_one",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("ambiguous")),
    )
    first = await dispatch_action(
        action_key=action_keys[0], session_factory=AsyncSessionLocal
    )
    assert first["status"] == "needs_reconciliation"

    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "")
    second = await dispatch_action(
        action_key=action_keys[1], session_factory=AsyncSessionLocal
    )
    assert second["status"] == "retry_pending"
    async with AsyncSessionLocal() as db:
        browser = (
            await db.execute(
                select(Notification).where(Notification.channel == "browser")
            )
        ).scalar_one()
        invocation = (await db.execute(select(ToolInvocation))).scalar_one()
        assert browser.status == "needs_reconciliation"
        assert invocation.status == "needs_reconciliation"
