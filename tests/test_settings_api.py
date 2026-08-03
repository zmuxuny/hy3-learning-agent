import os
import stat

import pytest

from app.api.settings import (
    EmailSettingsUpdate,
    ModelSettingsUpdate,
    NotificationPolicyUpdate,
    delete_email_credentials,
    update_email_settings,
    update_model_settings,
    update_notification_policy,
)
from app.core.envfile import clear_env_keys, update_env_file
from app.db.database import AsyncSessionLocal
from app.models import UserProfile


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
async def test_update_notification_policy_updates_profile_and_cooldown(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.api.settings.update_env_file", lambda values: captured.update(values))
    async with AsyncSessionLocal() as db:
        result = await update_notification_policy(NotificationPolicyUpdate(
            quiet_hours={"start": "22:00", "end": "07:00"},
            daily_notification_limit=2,
            cooldown_minutes=90,
        ), db)
        profile = await db.get(UserProfile, "local")
        assert profile.quiet_hours == {"start": "22:00", "end": "07:00"}
        assert profile.daily_notification_limit == 2
    assert captured["AGENT_NOTIFICATION_COOLDOWN_MINUTES"] == "90"
    assert result["restart_required"] is True


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
