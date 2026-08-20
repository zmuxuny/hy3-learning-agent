from __future__ import annotations

import json
import secrets
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.core.execution_policy as execution_policy
import app.tools.workspace as workspace_tools
from app.core.config import settings
from app.core.redaction import REDACTED, redact_data, redact_text
from app.db.database import AsyncSessionLocal
from app.main import app
from app.models import AgentRun, OutboxAction, OutboxReceipt, ToolInvocation
from app.outbox import dispatch_action, enqueue_subprocess
from app.tools.base import ToolEffectKind


def _contains(value: object, secret: str) -> bool:
    return secret in json.dumps(value, ensure_ascii=False, default=str)


def test_configured_short_secret_is_redacted_when_embedded(monkeypatch) -> None:
    short_secret = "q@"
    monkeypatch.setattr(settings, "OPENAI_API_KEY", short_secret)

    safe_text = redact_text(f"before{short_secret}after")
    safe_data = redact_data(
        {"message": f"prefix{short_secret}suffix", "items": [short_secret]}
    )

    assert short_secret not in safe_text
    assert not _contains(safe_data, short_secret)
    assert REDACTED in safe_text
    assert safe_data["items"] == [REDACTED]


@pytest.mark.asyncio
async def test_request_validation_error_does_not_reflect_configured_secret(
    monkeypatch,
) -> None:
    short_secret = "q@"
    monkeypatch.setattr(settings, "OPENAI_API_KEY", short_secret)
    invalid_value = f"prefix{short_secret}suffix" + ("x" * 500)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.put(
            "/api/v1/settings/model",
            json={"api_key": invalid_value},
        )

    assert response.status_code == 422
    assert short_secret not in response.text
    assert REDACTED in response.text


@pytest.mark.parametrize(
    ("path", "field", "length"),
    [
        ("/api/v1/settings/model", "api_key", 501),
        ("/api/v1/settings/email", "smtp_password", 301),
    ],
    ids=["unconfigured-api-key", "unconfigured-password"],
)
@pytest.mark.asyncio
async def test_request_validation_redacts_unconfigured_secret_field_input(
    path: str,
    field: str,
    length: int,
) -> None:
    prefix = secrets.token_urlsafe(24)
    invalid_secret = (prefix + ("x" * length))[:length]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.put(path, json={field: invalid_secret})

    assert response.status_code == 422
    assert invalid_secret not in response.text
    assert REDACTED in response.text
    error = response.json()["detail"][0]
    assert error["input"] == REDACTED
    assert error["msg"] == "Secret field validation failed"


@pytest.mark.asyncio
async def test_pending_approval_api_projection_is_redacted_without_rewriting_fact(
    monkeypatch,
) -> None:
    short_secret = "q@"
    monkeypatch.setattr(settings, "OPENAI_API_KEY", short_secret)
    pending_approval = {
        "approval_id": "h6-redaction-approval",
        "tool_call": {
            "id": "h6-redaction-call",
            "function": {
                "name": "file_write",
                "arguments": json.dumps(
                    {"path": "fixture.txt", "content": f"before{short_secret}after"},
                    ensure_ascii=False,
                ),
            },
        },
        "reason": f"review prefix{short_secret}suffix",
    }
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="approval projection redaction",
            status="waiting_approval",
            phase="waiting_approval",
            pending_approval=pending_approval,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get(f"/api/v1/agent/runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert not _contains(payload["pending_approval"], short_secret)
    assert REDACTED in json.dumps(payload["pending_approval"], ensure_ascii=False)

    async with AsyncSessionLocal() as db:
        stored = await db.get(AgentRun, run_id)
        assert stored is not None
        assert _contains(stored.pending_approval, short_secret)


@pytest.mark.parametrize("exit_code", [0, 9], ids=["success", "failure"])
@pytest.mark.asyncio
async def test_subprocess_dispatch_redacts_return_receipt_and_invocation_result(
    monkeypatch,
    exit_code: int,
) -> None:
    configured_secret = secrets.token_urlsafe(24)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", configured_secret)
    monkeypatch.setattr(
        execution_policy,
        "current_code_execution_policy",
        lambda: SimpleNamespace(available=True),
    )

    def completed_result(_args):
        return {
            "exit_code": exit_code,
            "stdout": f"stdout prefix {configured_secret} suffix",
            "stderr": f"stderr prefix {configured_secret} suffix",
            "nested": {"provider_error": configured_secret},
        }

    monkeypatch.setattr(workspace_tools, "_run_code", completed_result)
    request_digest = "d" * 64
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="outbox result redaction",
        )
        db.add(run)
        await db.flush()
        invocation = ToolInvocation(
            owner_id="local",
            run_id=run.id,
            idempotency_key=f"{run.id}:redacted-subprocess",
            tool_name="code_execute",
            tool_call_id="redacted-subprocess",
            args_hash=request_digest,
            request_digest=request_digest,
            canonical_args={"language": "python", "code": "pass", "timeout_seconds": 1},
            effect_kind=ToolEffectKind.EXTERNAL_WRITE.value,
            status="pending_delivery",
            result_payload={},
        )
        db.add(invocation)
        await db.flush()
        action = await enqueue_subprocess(
            db,
            owner_id="local",
            run_id=run.id,
            invocation_id=invocation.id,
            action_key=f"{run.id}:redacted-subprocess",
            request_digest=request_digest,
            arguments={"language": "python", "code": "pass", "timeout_seconds": 1},
        )
        await db.commit()
        action_key = action.action_key
        action_id = action.id
        invocation_id = invocation.id

    first = await dispatch_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
    )
    replay = await dispatch_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
    )

    async with AsyncSessionLocal() as db:
        action = await db.get(OutboxAction, action_id)
        receipt = await db.scalar(
            select(OutboxReceipt).where(OutboxReceipt.outbox_action_id == action_id)
        )
        invocation = await db.get(ToolInvocation, invocation_id)

    assert action is not None and action.status == "delivered"
    assert receipt is not None
    assert invocation is not None and invocation.status == "committed"
    for value in (first, replay, receipt.response, invocation.result_payload):
        assert not _contains(value, configured_secret)
        assert REDACTED in json.dumps(value, ensure_ascii=False, default=str)
