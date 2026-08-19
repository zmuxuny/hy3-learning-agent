import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import Notification, OutboxAction, OutboxReceipt, PushSubscription
from app.notifications.push import push_service
from app.notifications.service import NotificationService
from app.outbox import dispatch_action


ENDPOINT = "https://example.push.test/subscription-1"
SECOND_ENDPOINT = "https://example.push.test/subscription-2"
KEYS = {"p256dh": "abc", "auth": "xyz"}


def _configure_vapid(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:learner@example.com")


@pytest.mark.asyncio
async def test_push_subscription_upsert_and_delete():
    async with AsyncSessionLocal() as db:
        first = await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        second = await push_service.subscribe(
            db,
            "local",
            ENDPOINT,
            {"p256dh": "def", "auth": "xyz"},
        )
        assert first.id == second.id
        assert second.keys == {"p256dh": "def", "auth": "xyz"}
        await db.commit()

    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(select(PushSubscription))).scalars())
        assert len(rows) == 1
        assert rows[0].endpoint == ENDPOINT

        removed = await push_service.unsubscribe(db, "local", ENDPOINT)
        assert removed is True
        await db.commit()

    async with AsyncSessionLocal() as db:
        assert list((await db.execute(select(PushSubscription))).scalars()) == []


@pytest.mark.asyncio
async def test_browser_notification_queues_one_action_and_receipt_per_subscription(monkeypatch):
    sent: list[tuple[str, dict]] = []
    _configure_vapid(monkeypatch)

    def fake_send_one(subscription, payload):
        sent.append((subscription.endpoint, json.loads(payload)))

    monkeypatch.setattr(push_service, "_send_one", fake_send_one)
    async with AsyncSessionLocal() as db:
        await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        await push_service.subscribe(
            db,
            "local",
            SECOND_ENDPOINT,
            {"p256dh": "second", "auth": "keys"},
        )
        await db.commit()

        result = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="复习提醒",
            body="该做异步练习了",
            plan_id=None,
            channels=["browser"],
        )
        browser_result = next(
            item for item in result["notifications"] if item["channel"] == "browser"
        )
        actions = list(
            (
                await db.execute(
                    select(OutboxAction).order_by(OutboxAction.action_key)
                )
            ).scalars()
        )

        assert browser_result["status"] == "queued"
        assert len(result["outbox_action_keys"]) == 2
        assert len(set(result["outbox_action_keys"])) == 2
        assert sent == []
        assert len(actions) == 2
        assert {action.notification_id for action in actions} == {browser_result["id"]}
        assert {action.destination for action in actions} == {"web_push"}
        assert {action.status for action in actions} == {"queued"}
        assert {action.payload["endpoint"] for action in actions} == {
            ENDPOINT,
            SECOND_ENDPOINT,
        }
        assert len(list((await db.execute(select(OutboxReceipt))).scalars())) == 0
        await db.commit()

    first_delivery = await dispatch_action(action_key=result["outbox_action_keys"][0])
    assert first_delivery["status"] == "delivered"
    assert first_delivery["delivered"] is True
    assert len(sent) == 1

    async with AsyncSessionLocal() as db:
        browser = await db.get(Notification, browser_result["id"])
        actions = list((await db.execute(select(OutboxAction))).scalars())
        receipts = list((await db.execute(select(OutboxReceipt))).scalars())
        assert browser.status == "queued"
        assert sorted(action.status for action in actions) == ["delivered", "queued"]
        assert len(receipts) == 1

    second_delivery = await dispatch_action(action_key=result["outbox_action_keys"][1])
    assert second_delivery["status"] == "delivered"
    assert second_delivery["delivered"] is True

    async with AsyncSessionLocal() as db:
        browser = await db.get(Notification, browser_result["id"])
        in_app = (
            await db.execute(
                select(Notification).where(Notification.channel == "in_app")
            )
        ).scalar_one()
        actions = list((await db.execute(select(OutboxAction))).scalars())
        receipts = list((await db.execute(select(OutboxReceipt))).scalars())

        assert browser.status == "pushed"
        assert browser.sent_at is not None
        assert in_app.status == "sent"
        assert {action.status for action in actions} == {"delivered"}
        assert {receipt.action_key for receipt in receipts} == {
            action.action_key for action in actions
        }
        assert len(receipts) == 2

    assert {endpoint for endpoint, _payload in sent} == {ENDPOINT, SECOND_ENDPOINT}
    for _endpoint, payload in sent:
        assert payload == {
            "title": "复习提醒",
            "body": "该做异步练习了",
            "notification_id": browser_result["id"],
            "url": f"/?notification={in_app.id}",
        }


@pytest.mark.asyncio
async def test_confirmed_gone_subscription_is_cancelled_with_receipt(monkeypatch):
    _configure_vapid(monkeypatch)

    class Gone:
        status_code = 410

    def dead_send_one(_subscription, _payload):
        error = RuntimeError("subscription gone")
        error.response = Gone()
        raise error

    monkeypatch.setattr(push_service, "_send_one", dead_send_one)
    async with AsyncSessionLocal() as db:
        subscription = await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        await db.commit()
        subscription_id = subscription.id

        result = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="标题",
            body="正文",
            plan_id=None,
            channels=["browser"],
        )
        browser_id = next(
            item["id"] for item in result["notifications"] if item["channel"] == "browser"
        )
        action_key = result["outbox_action_keys"][0]
        await db.commit()

    delivery = await dispatch_action(action_key=action_key)
    assert delivery["status"] == "cancelled"
    assert delivery["delivered"] is False
    assert delivery["data"] == {
        "transport": "web_push",
        "reason": "subscription_gone",
    }

    async with AsyncSessionLocal() as db:
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
        browser = await db.get(Notification, browser_id)
        assert action.status == "cancelled"
        assert receipt.status == "reconciled"
        assert browser.status == "skipped"
        assert browser.sent_at is None
        assert await db.get(PushSubscription, subscription_id) is None


@pytest.mark.asyncio
async def test_transport_becoming_unconfigured_keeps_subscription_retryable(monkeypatch):
    _configure_vapid(monkeypatch)
    async with AsyncSessionLocal() as db:
        subscription = await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        await db.commit()
        subscription_id = subscription.id

        result = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="标题",
            body="正文",
            plan_id=None,
            channels=["browser"],
        )
        browser_id = next(
            item["id"] for item in result["notifications"] if item["channel"] == "browser"
        )
        action_key = result["outbox_action_keys"][0]
        await db.commit()

    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    delivery = await dispatch_action(action_key=action_key)
    assert delivery == {
        "status": "retry_pending",
        "action_key": action_key,
        "delivered": False,
        "retryable": True,
    }

    async with AsyncSessionLocal() as db:
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one()
        browser = await db.get(Notification, browser_id)
        assert action.status == "retry_pending"
        assert action.last_error == "OutboxPreflightUnavailable"
        assert browser.status == "queued"
        assert await db.get(PushSubscription, subscription_id) is not None
        assert (
            await db.execute(
                select(OutboxReceipt).where(OutboxReceipt.action_key == action_key)
            )
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_ambiguous_push_failure_keeps_subscription_and_requires_reconciliation(monkeypatch):
    _configure_vapid(monkeypatch)

    def ambiguous_send_one(_subscription, _payload):
        raise RuntimeError("provider outcome unavailable")

    monkeypatch.setattr(push_service, "_send_one", ambiguous_send_one)
    async with AsyncSessionLocal() as db:
        subscription = await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        await db.commit()
        subscription_id = subscription.id

        result = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="标题",
            body="正文",
            plan_id=None,
            channels=["browser"],
        )
        browser_id = next(
            item["id"] for item in result["notifications"] if item["channel"] == "browser"
        )
        action_key = result["outbox_action_keys"][0]
        await db.commit()

    delivery = await dispatch_action(action_key=action_key)
    assert delivery == {
        "status": "needs_reconciliation",
        "action_key": action_key,
        "delivered": False,
        "error_code": "needs_reconciliation",
    }

    async with AsyncSessionLocal() as db:
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one()
        browser = await db.get(Notification, browser_id)
        assert action.status == "needs_reconciliation"
        assert action.last_error == "RuntimeError"
        assert browser.status == "needs_reconciliation"
        assert await db.get(PushSubscription, subscription_id) is not None
        assert (
            await db.execute(
                select(OutboxReceipt).where(OutboxReceipt.action_key == action_key)
            )
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_browser_channel_without_vapid_is_skipped_without_deleting_subscription(monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    monkeypatch.setattr(settings, "VAPID_SUBJECT", "mailto:learner@example.com")
    async with AsyncSessionLocal() as db:
        subscription = await push_service.subscribe(db, "local", ENDPOINT, KEYS)
        await db.commit()
        subscription_id = subscription.id

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
        browser_result = next(
            item for item in result["notifications"] if item["channel"] == "browser"
        )
        await db.commit()

    assert browser_result["status"] == "skipped"
    assert result["outbox_action_keys"] == []
    async with AsyncSessionLocal() as db:
        browser = await db.get(Notification, browser_result["id"])
        assert browser.status == "skipped"
        assert browser.sent_at is None
        assert await db.get(PushSubscription, subscription_id) is not None
        assert list((await db.execute(select(OutboxAction))).scalars()) == []
        assert list((await db.execute(select(OutboxReceipt))).scalars()) == []
