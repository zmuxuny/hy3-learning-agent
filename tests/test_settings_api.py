import asyncio
import os
import stat

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

import app.api.settings as settings_api
from app.api.settings import (
    EmailTestRequest,
    EmailSettingsUpdate,
    ModelSettingsUpdate,
    NotificationPolicyUpdate,
    delete_email_credentials,
    update_email_settings,
    update_model_settings,
    update_notification_policy,
)
from app.core.config import settings
from app.core.envfile import clear_env_keys, update_env_file
from app.db.database import AsyncSessionLocal
from app.models import OutboxAction, OutboxReceipt, UserProfile
from app.notifications.service import NotificationService
from app.outbox import dispatch_action


def test_envfile_updates_and_clears_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=old\nMODEL_NAME=hy3\n", encoding="utf-8")

    update_env_file({"OPENAI_API_KEY": "new-key", "MODEL_TEMPERATURE": "0.7"}, env_path)
    content = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=new-key" in content
    assert "MODEL_TEMPERATURE=0.7" in content
    assert "MODEL_NAME=hy3" in content
    assert stat.S_IMODE(os.stat(env_path).st_mode) == 0o600

    clear_env_keys(["OPENAI_API_KEY"], env_path)
    assert "OPENAI_API_KEY=" in env_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_update_email_settings_writes_env_and_never_echoes_password(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.api.settings.update_env_file", lambda values: captured.update(values))
    result = await update_email_settings(EmailSettingsUpdate(
        smtp_host="smtp.qq.com",
        smtp_username="agent@qq.com",
        smtp_password="top-secret",
        smtp_to="user@163.com",
        imap_host="imap.qq.com",
        enable_email_reply_polling=True,
    ))
    assert captured["SMTP_HOST"] == "smtp.qq.com"
    assert captured["SMTP_PASSWORD"] == "top-secret"
    assert captured["IMAP_HOST"] == "imap.qq.com"
    assert captured["ENABLE_EMAIL_REPLY_POLLING"] == "true"
    assert result["restart_required"] is True
    assert "top-secret" not in str(result)


@pytest.mark.asyncio
async def test_delete_email_credentials_clears_all_keys(monkeypatch):
    cleared = {}
    monkeypatch.setattr("app.api.settings.clear_env_keys", lambda keys: cleared.update({key: "" for key in keys}))
    result = await delete_email_credentials()
    assert "SMTP_PASSWORD" in cleared
    assert "IMAP_PASSWORD" in cleared
    assert "SMTP_HOST" in cleared
    assert result["restart_required"] is True


@pytest.mark.asyncio
async def test_update_model_settings_writes_api_key_without_echoing(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.api.settings.update_env_file", lambda values: captured.update(values))
    result = await update_model_settings(ModelSettingsUpdate(
        base_url="https://tokenhub.tencentmaas.com/v1",
        model="hy3",
        api_key="sk-test-secret",
        temperature=0.5,
    ))
    assert captured["OPENAI_API_KEY"] == "sk-test-secret"
    assert captured["MODEL_TEMPERATURE"] == "0.5"
    assert result["api_key_configured"] is True
    assert "sk-test-secret" not in str(result)


@pytest.mark.asyncio
async def test_update_notification_policy_commits_cooldown_with_profile(monkeypatch):
    monkeypatch.setattr(
        "app.api.settings.update_env_file",
        lambda _values: pytest.fail("notification policy must not write .env"),
    )
    async with AsyncSessionLocal() as db:
        result = await update_notification_policy(NotificationPolicyUpdate(
            quiet_hours={"start": "22:00", "end": "07:00"},
            daily_notification_limit=2,
            cooldown_minutes=90,
        ), db)
        profile = await db.get(UserProfile, "local")
        assert profile.quiet_hours == {"start": "22:00", "end": "07:00"}
        assert profile.daily_notification_limit == 2
        assert profile.preferences["notification_cooldown_minutes"] == 90
    assert result["cooldown_minutes"] == 90
    assert result["restart_required"] is False


@pytest.mark.asyncio
async def test_update_notification_policy_without_cooldown_needs_no_restart(monkeypatch):
    called = []
    monkeypatch.setattr("app.api.settings.update_env_file", lambda values: called.append(values))
    async with AsyncSessionLocal() as db:
        result = await update_notification_policy(NotificationPolicyUpdate(
            daily_notification_limit=4,
        ), db)
    assert called == []
    assert result["restart_required"] is False


def _configure_smtp(monkeypatch) -> None:
    for name, value in {
        "SMTP_HOST": "smtp.test.invalid",
        "SMTP_PORT": 587,
        "SMTP_USERNAME": "agent@test.invalid",
        "SMTP_PASSWORD": "fixture-password",
        "SMTP_FROM": "agent@test.invalid",
        "SMTP_TO": "learner@test.invalid",
        "SMTP_USE_TLS": True,
        "SMTP_USE_SSL": False,
    }.items():
        monkeypatch.setattr(settings, name, value)


def test_smtp_message_diagnostic_requires_a_scoped_client_action_id():
    with pytest.raises(ValidationError, match="action_id is required"):
        EmailTestRequest(channel="smtp", send_message=True)
    with pytest.raises(ValidationError, match="supported only for SMTP"):
        EmailTestRequest(channel="imap", send_message=True, action_id="imap-send")
    with pytest.raises(ValidationError, match="accepted only"):
        EmailTestRequest(channel="smtp", action_id="unused-action")


@pytest.mark.asyncio
async def test_smtp_connection_diagnostic_never_enqueues_a_message(monkeypatch):
    calls = 0

    async def fake_connection_test():
        nonlocal calls
        calls += 1
        return {"ok": True, "channel": "smtp", "status": "connected"}

    monkeypatch.setattr(settings_api, "test_smtp", fake_connection_test)
    async with AsyncSessionLocal() as db:
        result = await settings_api.test_email_configuration(
            EmailTestRequest(channel="smtp", send_message=False),
            db,
        )
        action_count = int(await db.scalar(select(func.count(OutboxAction.id))) or 0)

    assert result["status"] == "connected"
    assert calls == 1
    assert action_count == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_smtp_diagnostic_requests_share_one_action(
    monkeypatch,
):
    _configure_smtp(monkeypatch)
    request = EmailTestRequest(
        channel="smtp",
        send_message=True,
        action_id="smtp-diagnostic-concurrent-001",
    )

    async def enqueue_once():
        async with AsyncSessionLocal() as db:
            return await settings_api.test_email_configuration(request, db)

    first, second = await asyncio.gather(enqueue_once(), enqueue_once())
    assert first["action_key"] == second["action_key"]
    assert first["status"] == second["status"] == "queued"

    async with AsyncSessionLocal() as db:
        action_count = int(await db.scalar(select(func.count(OutboxAction.id))) or 0)
    assert action_count == 1


@pytest.mark.asyncio
async def test_smtp_message_diagnostic_uses_durable_idempotent_outbox(
    monkeypatch,
):
    _configure_smtp(monkeypatch)
    transport_calls: list[tuple[str, str, str]] = []

    def fake_send_email(_service, reply_token: str, title: str, body: str) -> None:
        transport_calls.append((reply_token, title, body))

    monkeypatch.setattr(NotificationService, "_send_email", fake_send_email)
    request = EmailTestRequest(
        channel="smtp",
        send_message=True,
        action_id="smtp-diagnostic-request-001",
    )

    async with AsyncSessionLocal() as db:
        first = await settings_api.test_email_configuration(request, db)
        replay_before_delivery = await settings_api.test_email_configuration(request, db)
        actions = (await db.scalars(select(OutboxAction))).all()

    assert first["status"] == "queued"
    assert first["message_queued"] is True
    assert replay_before_delivery["action_key"] == first["action_key"]
    assert replay_before_delivery["status"] == "queued"
    assert len(actions) == 1
    assert transport_calls == []
    assert settings.SMTP_PASSWORD not in str(actions[0].payload)
    assert request.action_id not in actions[0].action_key

    delivered = await dispatch_action(
        action_key=first["action_key"],
        session_factory=AsyncSessionLocal,
    )
    assert delivered["status"] == "delivered"
    assert len(transport_calls) == 1

    async with AsyncSessionLocal() as db:
        replay_after_delivery = await settings_api.test_email_configuration(request, db)
        action_count = int(await db.scalar(select(func.count(OutboxAction.id))) or 0)
        receipt = await db.scalar(
            select(OutboxReceipt).where(
                OutboxReceipt.action_key == first["action_key"]
            )
        )

    assert replay_after_delivery["status"] == "delivered"
    assert replay_after_delivery["message_queued"] is False
    assert action_count == 1
    assert receipt is not None
    assert receipt.status == "accepted"
    assert receipt.response == {"transport": "smtp"}
    assert len(transport_calls) == 1

    monkeypatch.setattr(settings, "SMTP_TO", "changed-route@test.invalid")
    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as conflict:
            await settings_api.test_email_configuration(request, db)
    assert conflict.value.status_code == 409
    assert "different request" in str(conflict.value.detail)
    assert settings.SMTP_PASSWORD not in str(conflict.value.detail)

    async with AsyncSessionLocal() as db:
        final_count = int(await db.scalar(select(func.count(OutboxAction.id))) or 0)
    assert final_count == 1
    assert len(transport_calls) == 1
