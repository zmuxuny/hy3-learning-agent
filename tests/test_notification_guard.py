from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Notification, OutboxAction, OutboxReceipt, UserProfile
from app.notifications.service import NotificationService
from app.outbox import dispatch_action


async def _profile_without_quiet_hours(db):
    profile = await db.get(UserProfile, "local")
    profile.quiet_hours = {"start": "00:00", "end": "00:00"}
    await db.commit()


@pytest.mark.asyncio
async def test_email_notifications_count_toward_daily_limit():
    async with AsyncSessionLocal() as db:
        await _profile_without_quiet_hours(db)
        profile = await db.get(UserProfile, "local")
        profile.daily_notification_limit = 0
        await db.commit()
        db.add(Notification(
            owner_id="local",
            channel="email",
            title="邮件提醒",
            body="正文",
            status="sent",
            sent_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        allowed, reason = await NotificationService(db)._guard("local", "heartbeat", None)
    assert allowed is False
    assert reason == "daily notification limit"


@pytest.mark.asyncio
async def test_email_notifications_count_toward_cooldown(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_DAILY_NOTIFICATION_LIMIT", 10)
    async with AsyncSessionLocal() as db:
        await _profile_without_quiet_hours(db)
        profile = await db.get(UserProfile, "local")
        profile.daily_notification_limit = 10
        await db.commit()
        db.add(Notification(
            owner_id="local",
            channel="email",
            title="邮件提醒",
            body="正文",
            status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        ))
        await db.commit()
        allowed, reason = await NotificationService(db)._guard("local", "heartbeat", None)
    assert allowed is False
    assert reason == "notification cooldown"


@pytest.mark.asyncio
async def test_email_dispatch_transitions_queued_to_sent_with_receipt(monkeypatch):
    delivered: list[tuple[str, str, str]] = []

    def fake_send(_self, reply_token, title, body):
        delivered.append((reply_token, title, body))

    monkeypatch.setattr(NotificationService, "_send_email", fake_send)
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)
    async with AsyncSessionLocal() as db:
        service = NotificationService(db)
        result = await service.send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="邮件提醒",
            body="正文",
            plan_id=None,
            channels=["email"],
        )
        email_result = next(
            item for item in result["notifications"] if item["channel"] == "email"
        )
        action_key = result["outbox_action_keys"][0]
        assert email_result["status"] == "queued"
        assert delivered == []
        await db.commit()

    delivery = await dispatch_action(action_key=action_key)
    assert delivery == {
        "status": "delivered",
        "action_key": action_key,
        "delivered": True,
        "data": {"transport": "smtp"},
    }

    async with AsyncSessionLocal() as db:
        email = await db.get(Notification, email_result["id"])
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one()
        receipt = (
            await db.execute(
                select(OutboxReceipt).where(OutboxReceipt.action_key == action_key)
            )
        ).scalar_one()
        assert email.status == "sent"
        assert email.sent_at is not None
        assert action.status == "delivered"
        assert receipt.status == "accepted"
        assert receipt.response == {"transport": "smtp"}
        assert delivered == [(email.reply_token, "邮件提醒", "正文")]


@pytest.mark.asyncio
async def test_email_transport_failure_leaves_durable_reconciliation_state(monkeypatch):
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="发送一封提醒邮件",
            model="hy3",
            status="running",
        )
        db.add(run)
        await db.commit()
        service = NotificationService(db)

        def fail_send(_self, _reply_token, _title, _body):
            raise RuntimeError("SMTP refused connection")

        monkeypatch.setattr(NotificationService, "_send_email", fail_send)
        result = await service.send(
            owner_id="local",
            run_id=run.id,
            session_id=None,
            trigger="user_message",
            title="提醒",
            body="正文",
            plan_id=None,
            channels=["email"],
        )
        email_result = next(
            item for item in result["notifications"] if item["channel"] == "email"
        )
        action_key = result["outbox_action_keys"][0]
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one()
        assert result["blocked"] is False
        assert email_result["status"] == "queued"
        assert action.status == "queued"
        assert action.destination == "smtp"
        assert list((await db.execute(select(OutboxReceipt))).scalars()) == []
        await db.commit()

    delivery = await dispatch_action(action_key=action_key)
    assert delivery == {
        "status": "needs_reconciliation",
        "action_key": action_key,
        "delivered": False,
        "error_code": "needs_reconciliation",
    }

    async with AsyncSessionLocal() as db:
        email = await db.get(Notification, email_result["id"])
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one()
        assert email.status == "needs_reconciliation"
        assert action.status == "needs_reconciliation"
        assert action.last_error == "RuntimeError"
        assert (
            await db.execute(
                select(OutboxReceipt).where(OutboxReceipt.action_key == action_key)
            )
        ).scalar_one_or_none() is None
