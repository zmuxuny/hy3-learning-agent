import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import Notification, PushSubscription
from app.notifications.push import push_service
from app.notifications.service import NotificationService


ENDPOINT = "https://example.push.test/subscription-1"
KEYS = {"p256dh": "abc", "auth": "xyz"}


@pytest.mark.asyncio
async def test_push_subscription_upsert_and_delete():
    async with AsyncSessionLocal() as db:
        first = await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        second = await push_service.subscribe(db, "local", ENDPOINT, {"p256dh": "def", "auth": "xyz"})
        assert first.id == second.id
        assert second.keys == {"p256dh": "def", "auth": "xyz"}
        rows = list((await db.execute(select(PushSubscription))).scalars())
        assert len(rows) == 1

        removed = await push_service.unsubscribe(db, "local", ENDPOINT)
        assert removed is True
        assert list((await db.execute(select(PushSubscription))).scalars()) == []


@pytest.mark.asyncio
async def test_push_send_delivers_to_subscriptions(monkeypatch):
    sent = []
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:learner@example.com")

    def fake_send_one(subscription, payload):
        sent.append((subscription.endpoint, json.loads(payload)))

    monkeypatch.setattr(push_service, "_send_one", fake_send_one)
    async with AsyncSessionLocal() as db:
        await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        delivered = await push_service.send(db, "local", "复习提醒", "该做异步练习了", {"url": "/?view=inbox"})
        assert len(delivered) == 1
        assert sent == [(ENDPOINT, {
            "title": "复习提醒",
            "body": "该做异步练习了",
            "url": "/?view=inbox",
        })]


@pytest.mark.asyncio
async def test_push_send_removes_dead_subscription(monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:learner@example.com")

    class Gone:
        def __init__(self):
            self.status_code = 410

    def dead_send_one(subscription, payload):
        error = RuntimeError("subscription gone")
        error.response = Gone()
        raise error

    monkeypatch.setattr(push_service, "_send_one", dead_send_one)
    async with AsyncSessionLocal() as db:
        await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        delivered = await push_service.send(db, "local", "标题", "正文")
        assert delivered == []
        assert list((await db.execute(select(PushSubscription))).scalars()) == []


@pytest.mark.asyncio
async def test_notification_service_uses_push_for_browser_channel(monkeypatch):
    pushed = []
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:learner@example.com")
    async def fake_send(db, owner, title, body, data=None):
        pushed.append((title, body, data))

    monkeypatch.setattr(push_service, "send", fake_send)

    async with AsyncSessionLocal() as db:
        result = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="站内提醒",
            body="浏览器推送正文",
            plan_id=None,
            channels=["browser"],
        )
        assert result["blocked"] is False
        rows = list((await db.execute(select(Notification))).scalars())
        browser = [row for row in rows if row.channel == "browser"]
        assert browser and browser[0].status == "sent"
        assert pushed and pushed[0][0] == "站内提醒"
        assert pushed[0][2]["url"] == "/?view=inbox"
