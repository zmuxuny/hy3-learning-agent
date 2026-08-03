from datetime import datetime, timedelta, timezone

import pytest

from app.db.database import AsyncSessionLocal
from app.models import Notification, UserProfile
from app.notifications.service import NotificationService


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
async def test_email_failure_error_is_returned_to_model(monkeypatch):
    async with AsyncSessionLocal() as db:
        service = NotificationService(db)
        monkeypatch.setattr(service, "_email_configured", lambda: True)

        def fail_send(_reply_token, _title, _body):
            raise RuntimeError("SMTP refused connection")

        monkeypatch.setattr(service, "_send_email", fail_send)
        result = await service.send(
            owner_id="local",
            run_id="run-1",
            session_id=None,
            trigger="user_message",
            title="提醒",
            body="正文",
            plan_id=None,
            channels=["email"],
        )
    assert result["blocked"] is False
    email_result = next(item for item in result["notifications"] if item["channel"] == "email")
    assert email_result["status"] == "failed"
    assert "SMTP refused connection" in email_result["error"]
