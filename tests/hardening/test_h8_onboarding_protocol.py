from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.settings as settings_api
from app.api.settings import (
    ModelConnectionTestRequest,
    ModelSettingsUpdate,
    read_onboarding_status,
    test_model_connection as check_model_connection,
    update_model_settings,
)
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import Session


class _FakeCompletions:
    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.requests = []

    async def create(self, **request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(id="synthetic-connection-check")


class _FakeOpenAI:
    instances = []
    failure: Exception | None = None

    def __init__(self, **configuration):
        self.configuration = configuration
        self.chat = SimpleNamespace(completions=_FakeCompletions(failure=self.failure))
        self.closed = False
        self.instances.append(self)

    async def close(self):
        self.closed = True


def test_model_connection_contract_rejects_blank_identity_fields():
    for payload in (
        {"base_url": "https://tokenhub.tencentmaas.com/v1", "model": " ", "api_key": "key"},
        {"base_url": "https://tokenhub.tencentmaas.com/v1", "model": "hy3", "api_key": "  "},
    ):
        with pytest.raises(ValidationError):
            ModelConnectionTestRequest(**payload)


@pytest.mark.asyncio
async def test_onboarding_status_requires_both_missing_key_and_zero_sessions(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    async with AsyncSessionLocal() as db:
        fresh = await read_onboarding_status(db)
        assert fresh["requires_onboarding"] is True

        db.add(Session(owner_id=settings.DEFAULT_OWNER_ID, title="Existing learning history"))
        await db.commit()
        with_history = await read_onboarding_status(db)
        assert with_history["session_count"] == 1
        assert with_history["requires_onboarding"] is False

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "configured-token")
    async with AsyncSessionLocal() as db:
        configured = await read_onboarding_status(db)
        assert configured["api_key_configured"] is True
        assert configured["requires_onboarding"] is False


@pytest.mark.asyncio
async def test_model_connection_check_is_minimal_and_never_persists_or_echoes_key(monkeypatch):
    _FakeOpenAI.instances = []
    _FakeOpenAI.failure = None
    monkeypatch.setattr(settings_api, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr(
        settings_api,
        "update_env_file",
        lambda _values: pytest.fail("connection check must not persist configuration"),
    )

    result = await check_model_connection(ModelConnectionTestRequest(
        base_url="https://tokenhub.tencentmaas.com/v1",
        model="hy3",
        api_key="synthetic-secret",
    ))

    client = _FakeOpenAI.instances[0]
    assert result == {
        "ok": True,
        "provider": "tencent-tokenhub",
        "model": "hy3",
        "base_url": "https://tokenhub.tencentmaas.com/v1",
    }
    assert client.configuration["max_retries"] == 0
    assert client.chat.completions.requests == [{
        "model": "hy3",
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1,
        "temperature": 0,
    }]
    assert client.closed is True
    assert "synthetic-secret" not in str(result)


@pytest.mark.asyncio
async def test_model_connection_failure_has_stable_redacted_error(monkeypatch):
    _FakeOpenAI.instances = []
    _FakeOpenAI.failure = RuntimeError("provider leaked synthetic-secret")
    monkeypatch.setattr(settings_api, "AsyncOpenAI", _FakeOpenAI)

    with pytest.raises(HTTPException) as captured:
        await check_model_connection(ModelConnectionTestRequest(
            base_url="https://tokenhub.tencentmaas.com/v1",
            model="hy3",
            api_key="synthetic-secret",
        ))

    assert captured.value.status_code == 502
    assert captured.value.detail == {
        "code": "model_connection_failed",
        "error_type": "RuntimeError",
    }
    assert "synthetic-secret" not in str(captured.value.detail)
    assert _FakeOpenAI.instances[0].closed is True


@pytest.mark.asyncio
async def test_verified_model_settings_apply_to_current_process_without_echoing_secret(monkeypatch):
    persisted = {}
    monkeypatch.setattr(settings_api, "update_env_file", lambda values: persisted.update(values))
    monkeypatch.setattr(settings, "OPENAI_API_BASE", "https://old.invalid/v1")
    monkeypatch.setattr(settings, "MODEL_NAME", "old-model")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "MODEL_TEMPERATURE", 0.9)

    result = await update_model_settings(ModelSettingsUpdate(
        base_url="https://tokenhub.tencentmaas.com/v1/",
        model="hy3",
        api_key="synthetic-secret",
        temperature=0.4,
    ))

    assert result == {
        "restart_required": False,
        "model": "hy3",
        "base_url": "https://tokenhub.tencentmaas.com/v1",
        "api_key_configured": True,
        "temperature": 0.4,
    }
    assert settings.OPENAI_API_KEY == "synthetic-secret"
    assert persisted["OPENAI_API_KEY"] == "synthetic-secret"
    assert "synthetic-secret" not in str(result)
