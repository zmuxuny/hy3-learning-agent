from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import app.core.config as config_module
import pytest
from app.core.time import frozen_utc, utc_now
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    Intervention,
    Notification,
    OutboxAction,
    OutboxReceipt,
    ProactiveDecision,
    PushSubscription,
    UserProfile,
)
from app.notifications.push import push_service
from app.notifications.service import NotificationService
from app.outbox import (
    ClaimedAction,
    UnsupportedOutboxDestination,
    _deliver,
    dispatch_action,
)
from app.runtime.proactive import capture_proactive_candidate
from app.search import SnapshotResourceUnavailable, use_snapshot_provider
from app.tools.web import (
    ResourceSaveArgs,
    WebOpenArgs,
    WebSearchArgs,
    resource_save,
    web_open,
    web_search,
)
from learning_agent_eval.delivery import RecordingDeliverySink
from learning_agent_eval.resources import EvaluationSnapshotProvider
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = (
    PROJECT_ROOT
    / "evaluation"
    / "datasets"
    / "decisionbench-v1"
    / "resources"
    / "e1-mini"
    / "snapshot.json"
)


def test_evaluation_settings_entry_disables_env_file_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    settings_class = config_module.Settings

    def fake_settings(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(config_module, "Settings", fake_settings)
    monkeypatch.setenv("EVALUATION_MODE", "1")
    config_module.get_settings.cache_clear()
    config_module.get_settings()
    assert calls == [{"_env_file": None}]

    calls.clear()
    monkeypatch.delenv("EVALUATION_MODE")
    config_module.get_settings.cache_clear()
    config_module.get_settings()
    assert calls == [{}]
    assert Path(settings_class.model_config["env_file"]).resolve() == PROJECT_ROOT / ".env"
    config_module.get_settings.cache_clear()


def test_evaluation_settings_reject_database_outside_worker_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside RUNTIME_STATE_ROOT"):
        config_module.Settings(
            _env_file=None,
            EVALUATION_MODE=True,
            RUNTIME_STATE_ROOT=tmp_path / "worker",
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'outside.db'}",
            ENABLE_SCHEDULER=False,
            ENABLE_EMAIL_REPLY_POLLING=False,
        )


def test_evaluation_settings_reject_runtime_root_inside_repository() -> None:
    runtime_root = (
        config_module.PROJECT_ROOT / "data" / "evaluation-must-not-run-here"
    )
    with pytest.raises(ValueError, match="outside the repository"):
        config_module.Settings(
            _env_file=None,
            EVALUATION_MODE=True,
            RUNTIME_STATE_ROOT=runtime_root,
            DATABASE_URL=f"sqlite+aiosqlite:///{runtime_root / 'fixture.db'}",
            ENABLE_SCHEDULER=False,
            ENABLE_EMAIL_REPLY_POLLING=False,
        )


def test_frozen_clock_is_scoped_and_restores_real_utc() -> None:
    instant = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    with frozen_utc(instant):
        assert utc_now() is instant
    assert utc_now() != instant
    assert abs((datetime.now(timezone.utc) - utc_now()).total_seconds()) < 1


@pytest.mark.asyncio
async def test_guard_quiet_hours_use_frozen_time_and_create_no_delivery_rows() -> None:
    instant = datetime(2026, 8, 31, 15, 30, tzinfo=timezone.utc)
    sink = RecordingDeliverySink()
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        profile.quiet_hours = {"start": "23:00", "end": "08:00"}
        run = AgentRun(
            id="run:e1:guard-blocked",
            owner_id="local",
            trigger="heartbeat",
            objective="Synthetic blocked reminder",
            status="running",
        )
        capture_proactive_candidate(
            run,
            {
                "candidate_key": "candidate:e1:guard-blocked",
                "candidate_kind": "study_reminder",
                "candidate_payload": {},
            },
            detected_at=instant,
        )
        db.add(run)
        await db.commit()

        with frozen_utc(instant):
            result = await NotificationService(db).send(
                owner_id="local",
                run_id=run.id,
                session_id=None,
                trigger="heartbeat",
                title="Synthetic reminder",
                body="This should be blocked.",
                plan_id=None,
                channels=["email"],
            )
            await db.commit()

        assert result["blocked"] is True
        assert result["reason"] == "quiet hours"
        assert await db.scalar(select(func.count()).select_from(Notification)) == 0
        assert await db.scalar(select(func.count()).select_from(Intervention)) == 0
        assert await db.scalar(select(func.count()).select_from(OutboxAction)) == 0
        assert await db.scalar(select(func.count()).select_from(OutboxReceipt)) == 0
        decision = await db.scalar(
            select(ProactiveDecision).where(ProactiveDecision.source_run_id == run.id)
        )
        assert decision.outcome == "deferred_quiet_hours"
        assert decision.decided_at == instant
        assert decision.next_eligible_at == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert sink.attempts == []


@pytest.mark.asyncio
async def test_guard_cooldown_uses_frozen_time(monkeypatch: pytest.MonkeyPatch) -> None:
    instant = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(config_module.settings, "AGENT_NOTIFICATION_COOLDOWN_MINUTES", 60)
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        profile.quiet_hours = {"start": "00:00", "end": "00:00"}
        profile.daily_notification_limit = 10
        db.add(
            Notification(
                owner_id="local",
                channel="in_app",
                title="Earlier synthetic reminder",
                body="Public synthetic content.",
                status="sent",
                sent_at=instant - timedelta(minutes=30),
            )
        )
        await db.commit()
        with frozen_utc(instant):
            allowed, reason = await NotificationService(db)._guard(
                "local", "heartbeat", None
            )
    assert allowed is False
    assert reason == "notification cooldown"


def _claimed(destination: str, *, action_key: str) -> ClaimedAction:
    payload = {
        "reply_token": "opaque-synthetic-reply",
        "title": "Synthetic notification",
        "body": "Public synthetic body.",
        "route_digest": "a" * 64,
    }
    if destination == "web_push":
        payload = {
            "subscription_id": 1,
            "endpoint": "https://push.example.test/subscription",
            "keys": {"p256dh": "synthetic", "auth": "synthetic"},
            "title": "Synthetic notification",
            "body": "Public synthetic body.",
            "data": {},
        }
    return ClaimedAction(
        id=f"action:{action_key}",
        owner_id="local",
        run_id=None,
        invocation_id=None,
        notification_id=None,
        operation_id=None,
        action_key=action_key,
        request_digest="b" * 64,
        destination=destination,
        payload=payload,
        claim_token="synthetic-claim",
    )


@pytest.mark.asyncio
async def test_recording_sink_preserves_smtp_and_web_push_receipt_semantics() -> None:
    sink = RecordingDeliverySink()
    smtp = await sink.send_smtp(_claimed("smtp", action_key="sink:smtp"))
    push = await sink.send_web_push(
        _claimed("web_push", action_key="sink:webpush"),
        payload="synthetic payload",
    )

    assert smtp.status == "accepted"
    assert smtp.action_status == "delivered"
    assert push.status == "delivered"
    assert push.action_status == "delivered"
    assert [item["destination"] for item in sink.attempts] == ["smtp", "web_push"]
    encoded = str(sink.attempts)
    assert "opaque-synthetic-reply" not in encoded
    assert "push.example.test" not in encoded
    assert "synthetic payload" not in encoded
    assert "a" * 64 not in encoded
    for outcome, destination in ((smtp, "smtp"), (push, "web_push")):
        assert outcome.response == {
            "transport": "evaluation_sink",
            "emulated_destination": destination,
            "external_side_effect": False,
            "sink_version": "recording-delivery-sink-v1",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["workspace_file", "subprocess", "unknown"])
async def test_explicit_recording_adapter_fails_closed_for_other_destinations(
    destination: str,
) -> None:
    with pytest.raises(UnsupportedOutboxDestination):
        await _deliver(
            _claimed(destination, action_key=f"sink:{destination}"),
            session_factory=AsyncSessionLocal,
            delivery_adapter=RecordingDeliverySink(),
        )


@pytest.mark.asyncio
async def test_smtp_recording_adapter_runs_real_outbox_and_replays_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module.settings, "SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setattr(config_module.settings, "SMTP_USERNAME", "synthetic")
    monkeypatch.setattr(config_module.settings, "SMTP_PASSWORD", "synthetic")
    monkeypatch.setattr(config_module.settings, "SMTP_FROM", "sender@example.test")
    monkeypatch.setattr(config_module.settings, "SMTP_TO", "recipient@example.test")

    def provider_trap(*_: object, **__: object) -> None:
        raise AssertionError("SMTP provider must not be called")

    monkeypatch.setattr(NotificationService, "_send_email", provider_trap)
    instant = datetime(2026, 8, 31, 1, 15, tzinfo=timezone.utc)
    sink = RecordingDeliverySink()
    async with AsyncSessionLocal() as db:
        with frozen_utc(instant):
            result = await NotificationService(db).send(
                owner_id="local",
                run_id=None,
                session_id=None,
                trigger="user_message",
                title="Synthetic SMTP",
                body="Public body.",
                plan_id=None,
                channels=["email"],
            )
            await db.commit()
    action_key = result["outbox_action_keys"][0]
    with frozen_utc(instant):
        delivered = await dispatch_action(action_key=action_key, delivery_adapter=sink)
        replayed = await dispatch_action(action_key=action_key, delivery_adapter=sink)
    assert delivered["status"] == "delivered"
    assert replayed["replayed"] is True
    assert len(sink.attempts) == 1
    async with AsyncSessionLocal() as db:
        receipts = list((await db.execute(select(OutboxReceipt))).scalars())
        invocation_free_receipt = next(item for item in receipts if item.action_key == action_key)
        assert invocation_free_receipt.status == "accepted"
        assert invocation_free_receipt.accepted_at == instant


@pytest.mark.asyncio
async def test_web_push_recording_adapter_runs_real_outbox_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module.settings, "VAPID_PUBLIC_KEY", "synthetic-public")
    monkeypatch.setattr(config_module.settings, "VAPID_PRIVATE_KEY", "synthetic-private")
    monkeypatch.setattr(config_module.settings, "VAPID_SUBJECT", "mailto:push@example.test")

    def provider_trap(*_: object, **__: object) -> None:
        raise AssertionError("Web Push provider must not be called")

    monkeypatch.setattr(push_service, "_send_one", provider_trap)
    sink = RecordingDeliverySink()
    async with AsyncSessionLocal() as db:
        subscription = PushSubscription(
            owner_id="local",
            endpoint="https://push.example.test/subscription",
            keys={"p256dh": "synthetic", "auth": "synthetic"},
        )
        db.add(subscription)
        await db.commit()
        result = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="user_message",
            title="Synthetic Push",
            body="Public body.",
            plan_id=None,
            channels=["browser"],
        )
        browser_id = next(
            item["id"] for item in result["notifications"] if item["channel"] == "browser"
        )
        await db.commit()
    action_key = result["outbox_action_keys"][0]
    delivered = await dispatch_action(action_key=action_key, delivery_adapter=sink)
    assert delivered["status"] == "delivered"
    assert delivered["data"]["emulated_destination"] == "web_push"
    async with AsyncSessionLocal() as db:
        browser = await db.get(Notification, browser_id)
        receipt = await db.scalar(
            select(OutboxReceipt).where(OutboxReceipt.action_key == action_key)
        )
        assert browser.status == "pushed"
        assert receipt.status == "delivered"


@pytest.mark.asyncio
async def test_snapshot_provider_blocks_unknown_resources_without_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls: list[str] = []

    def dns_trap(host: str, *_: object, **__: object) -> None:
        dns_calls.append(host)
        raise AssertionError("live DNS must not be called")

    monkeypatch.setattr(socket, "getaddrinfo", dns_trap)
    provider = EvaluationSnapshotProvider(SNAPSHOT)
    context = SimpleNamespace(plan_id=None)
    with use_snapshot_provider(provider):
        search_result = await web_search(
            context,
            WebSearchArgs(query="synthetic cuda profiling guide"),
        )
        open_result = await web_open(
            context,
            WebOpenArgs(url="https://cuda.example.test/profiling-guide"),
        )
        with pytest.raises(
            SnapshotResourceUnavailable,
            match="snapshot_query_not_registered",
        ) as query_error:
            await web_search(context, WebSearchArgs(query="unknown synthetic query"))
        with pytest.raises(
            SnapshotResourceUnavailable,
            match="snapshot_url_not_registered",
        ) as url_error:
            await resource_save(
                context,
                ResourceSaveArgs(
                    plan_id=1,
                    title="Unknown synthetic resource",
                    url="https://unknown.example.test/resource",
                    resource_type="documentation",
                    summary="A deliberately unregistered synthetic resource.",
                    why_recommended="It must fail before any database or DNS work.",
                ),
            )
    assert query_error.value.code == "snapshot_query_not_registered"
    assert url_error.value.code == "snapshot_url_not_registered"
    assert search_result["provider"] == "decisionbench-e1-snapshot"
    assert open_result["snapshot_version"] == "decisionbench-e1-mini-resources-v1"
    assert provider.calls == {"search": 2, "open": 1, "validate": 1}
    assert dns_calls == []
