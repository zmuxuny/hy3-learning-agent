from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

import app.core.envfile as envfile
from app.core.config import Settings, settings
from app.core.deployment import (
    AUTH_CSRF_COOKIE,
    AUTH_CSRF_HEADER,
    AUTH_SESSION_COOKIE,
    DeploymentBoundaryMiddleware,
    DeploymentConfigError,
    DeploymentPolicy,
    validate_bind_host,
)
from app.core.envfile import update_env_file
from app.core.redaction import configured_secret_values, redact_text


SERVER_ORIGIN = "https://agent.example.test"
SERVER_TOKEN = "synthetic-server-token-that-is-32-bytes-minimum"


def _policy(
    mode: str = "local",
    *,
    token: str = "",
    public_origin: str = "",
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173",
) -> DeploymentPolicy:
    return DeploymentPolicy.from_settings(
        SimpleNamespace(
            DEPLOYMENT_MODE=mode,
            SERVER_AUTH_TOKEN=token,
            SERVER_PUBLIC_ORIGIN=public_origin,
            SERVER_SESSION_TTL_SECONDS=600,
            CORS_ORIGINS=cors_origins,
        )
    )


def _protocol_app(policy: DeploymentPolicy) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/write")
    async def write() -> dict[str, bool]:
        return {"written": True}

    @app.get("/api/v1/runs/example/events/stream")
    async def stream() -> StreamingResponse:
        async def events():
            yield "data: ready\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(policy.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    )
    app.add_middleware(DeploymentBoundaryMiddleware, policy=policy)
    return app


def test_server_configuration_is_complete_explicit_and_secret_safe() -> None:
    common = {
        "_env_file": None,
        "DEPLOYMENT_MODE": "server",
        "SERVER_PUBLIC_ORIGIN": SERVER_ORIGIN,
        "CORS_ORIGINS": SERVER_ORIGIN,
    }
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(**common, SERVER_AUTH_TOKEN="short")
    with pytest.raises(ValidationError, match="bearer-token characters"):
        Settings(**common, SERVER_AUTH_TOKEN=" " * 32)
    with pytest.raises(ValidationError, match="SERVER_PUBLIC_ORIGIN"):
        Settings(
            _env_file=None,
            DEPLOYMENT_MODE="server",
            SERVER_AUTH_TOKEN=SERVER_TOKEN,
            SERVER_PUBLIC_ORIGIN="",
            CORS_ORIGINS=SERVER_ORIGIN,
        )
    with pytest.raises(ValidationError, match="CORS origin must equal public origin"):
        Settings(
            **{**common, "CORS_ORIGINS": "https://other.example.test"},
            SERVER_AUTH_TOKEN=SERVER_TOKEN,
        )
    with pytest.raises(ValidationError, match=r"required HTTP\(S\) scheme"):
        Settings(
            **{**common, "CORS_ORIGINS": "*"},
            SERVER_AUTH_TOKEN=SERVER_TOKEN,
        )

    configured = Settings(
        **common,
        SERVER_AUTH_TOKEN=SERVER_TOKEN,
        WEB_MAX_WIRE_BYTES=2048,
        WEB_MAX_DECODED_BYTES=4096,
        WEB_TOTAL_DEADLINE_SECONDS=7,
        CODE_SANDBOX_PROVIDER="none",
    )
    assert configured.deployment_policy.public_origin is not None
    assert configured.cors_origins == [SERVER_ORIGIN]
    assert configured.WEB_MAX_WIRE_BYTES == 2048
    assert configured.WEB_MAX_DECODED_BYTES == 4096
    assert configured.WEB_TOTAL_DEADLINE_SECONDS == 7
    assert configured.CODE_SANDBOX_PROVIDER == "none"
    assert SERVER_TOKEN not in repr(configured)
    assert SERVER_TOKEN not in repr(configured.deployment_policy)


def test_configuration_rejects_ambiguous_limits_and_normalized_cors_duplicates() -> None:
    with pytest.raises(ValidationError, match="at least WEB_MAX_WIRE_BYTES"):
        Settings(
            _env_file=None,
            WEB_MAX_WIRE_BYTES=4096,
            WEB_MAX_DECODED_BYTES=2048,
        )


def test_server_auth_token_is_collected_dynamically_by_redaction(monkeypatch) -> None:
    token = "synthetic-dynamic-redaction-token-0123456789"
    monkeypatch.setattr(settings, "SERVER_AUTH_TOKEN", SecretStr(token))

    assert token in configured_secret_values()
    assert token not in redact_text(f"configured credential: {token}")
    with pytest.raises(ValidationError, match="unique after normalization"):
        Settings(
            _env_file=None,
            CORS_ORIGINS="http://LOCALHOST:5173,http://localhost:5173",
        )


def test_local_bind_and_request_scope_both_require_loopback(monkeypatch) -> None:
    policy = _policy()
    validate_bind_host(policy, "127.0.0.1")
    with pytest.raises(DeploymentConfigError, match="only loopback"):
        validate_bind_host(policy, "0.0.0.0")

    def mixed_resolution(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("192.0.2.7", 0)),
        ]

    monkeypatch.setattr("app.core.deployment.socket.getaddrinfo", mixed_resolution)
    with pytest.raises(DeploymentConfigError, match="only loopback"):
        validate_bind_host(policy, "mixed.example.test")

    called = False

    async def inner(_scope, _receive, _send):
        nonlocal called
        called = True

    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/health",
        "raw_path": b"/api/v1/health",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1")],
        "server": ("192.0.2.9", 8000),
    }
    asyncio.run(DeploymentBoundaryMiddleware(inner, policy=policy)(scope, receive, send))
    assert called is False
    assert sent[0]["status"] == 403


def test_host_guard_runs_before_routing_and_testclient_exception_is_scope_bound() -> None:
    client = TestClient(_protocol_app(_policy()), base_url="http://127.0.0.1")
    assert client.get("/api/v1/health").status_code == 200
    assert client.get(
        "/api/v1/health", headers={"host": "attacker.example.test"}
    ).status_code == 400
    assert client.get(
        "/api/v1/health", headers={"host": "127.0.0.1."}
    ).status_code == 400

    # Existing tests use Starlette's synthetic authority. It is accepted only
    # when both Host and the non-network ASGI server tuple say "testserver".
    synthetic = TestClient(_protocol_app(_policy()))
    assert synthetic.get("/api/v1/health").status_code == 200


def test_server_bearer_cookie_csrf_sse_rotation_and_cors_protocol() -> None:
    policy = _policy(
        "server",
        token=SERVER_TOKEN,
        public_origin=SERVER_ORIGIN,
        cors_origins=SERVER_ORIGIN,
    )
    client = TestClient(_protocol_app(policy), base_url=SERVER_ORIGIN)
    assert client.get("/api/v1/health").status_code == 401
    assert client.get(
        "/api/v1/health", headers={"authorization": "Bearer wrong"}
    ).status_code == 401
    assert client.get(
        "/api/v1/health", headers={"authorization": f"Bearer {SERVER_TOKEN}"}
    ).status_code == 200

    insecure = TestClient(_protocol_app(policy), base_url="http://agent.example.test")
    insecure_response = insecure.get(
        "/api/v1/health",
        headers={
            "authorization": f"Bearer {SERVER_TOKEN}",
            "host": "agent.example.test:443",
        },
    )
    assert insecure_response.status_code == 400
    assert insecure_response.json()["detail"] == "server mode requires an HTTPS request scope"

    session_token, csrf_token, _ = policy.issue_session(now=1_000)
    assert policy.verify_session(session_token, now=1_599) is not None
    assert policy.verify_session(session_token, now=1_600) is None
    client.cookies.set(AUTH_SESSION_COOKIE, session_token)
    client.cookies.set(AUTH_CSRF_COOKIE, csrf_token)

    # The live client clock differs from the deterministic expiry probe above.
    session_token, csrf_token, _ = policy.issue_session()
    client.cookies.set(AUTH_SESSION_COOKIE, session_token)
    client.cookies.set(AUTH_CSRF_COOKIE, csrf_token)
    assert client.get("/api/v1/health").status_code == 200
    stream = client.get("/api/v1/runs/example/events/stream")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text == "data: ready\n\n"

    assert client.post("/api/v1/write").status_code == 403
    assert client.post(
        "/api/v1/write",
        headers={AUTH_CSRF_HEADER: csrf_token},
    ).status_code == 403
    assert client.post(
        "/api/v1/write",
        headers={"origin": SERVER_ORIGIN, AUTH_CSRF_HEADER: csrf_token},
    ).status_code == 200
    assert client.post(
        "/api/v1/write",
        headers={
            "origin": "https://attacker.example.test",
            AUTH_CSRF_HEADER: csrf_token,
        },
    ).status_code == 403

    allowed_preflight = client.options(
        "/api/v1/write",
        headers={
            "origin": SERVER_ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "x-csrf-token",
        },
    )
    assert allowed_preflight.status_code == 200
    assert allowed_preflight.headers["access-control-allow-origin"] == SERVER_ORIGIN
    denied_preflight = client.options(
        "/api/v1/write",
        headers={
            "origin": "https://attacker.example.test",
            "access-control-request-method": "POST",
        },
    )
    assert denied_preflight.status_code == 400
    assert "access-control-allow-origin" not in denied_preflight.headers

    rotated = _policy(
        "server",
        token="rotated-server-token-that-is-also-long-enough",
        public_origin=SERVER_ORIGIN,
        cors_origins=SERVER_ORIGIN,
    )
    assert rotated.verify_session(session_token) is None
    rotated_client = TestClient(_protocol_app(rotated), base_url=SERVER_ORIGIN)
    rotated_client.cookies.set(AUTH_SESSION_COOKIE, session_token)
    rotated_client.cookies.set(AUTH_CSRF_COOKIE, csrf_token)
    assert rotated_client.get("/api/v1/health").status_code == 401


def test_main_bearer_exchange_sets_host_only_secure_strict_cookies(monkeypatch) -> None:
    import app.main as main_module

    policy = _policy(
        "server",
        token=SERVER_TOKEN,
        public_origin=SERVER_ORIGIN,
        cors_origins=SERVER_ORIGIN,
    )
    monkeypatch.setattr(main_module, "deployment_policy", policy)
    client = TestClient(main_module.app)
    assert client.post(
        "/api/v1/auth/session", headers={"authorization": "Bearer incorrect"}
    ).status_code == 401
    response = client.post(
        "/api/v1/auth/session",
        headers={"authorization": f"Bearer {SERVER_TOKEN}"},
    )
    assert response.status_code == 200
    set_cookies = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in set_cookies if AUTH_SESSION_COOKIE in value)
    csrf_cookie = next(value for value in set_cookies if AUTH_CSRF_COOKIE in value)
    assert all(
        part in session_cookie.lower()
        for part in ("path=/", "secure", "httponly", "samesite=strict")
    )
    assert all(part in csrf_cookie.lower() for part in ("path=/", "secure", "samesite=strict"))
    assert "httponly" not in csrf_cookie.lower()
    assert "domain=" not in session_cookie.lower()
    assert SERVER_TOKEN not in response.text
    assert SERVER_TOKEN not in "\n".join(set_cookies)


@pytest.mark.parametrize(
    "value",
    [
        "line\nINJECTED=value",
        "line\rINJECTED=value",
        "nul\x00value",
        "tab\tvalue",
        "delete\x7fvalue",
        "c1\x85value",
        "separator\u2028value",
        "separator\u2029value",
    ],
)
def test_envfile_rejects_controls_before_touching_the_target(
    tmp_path: Path,
    value: str,
) -> None:
    env_path = tmp_path / "not-created" / ".env"
    with pytest.raises(ValueError, match="control"):
        update_env_file({"SAFE_KEY": "validated first", "SECRET_KEY": value}, env_path)
    assert not env_path.parent.exists()


def test_envfile_validates_every_key_before_touching_the_target(tmp_path: Path) -> None:
    env_path = tmp_path / "not-created" / ".env"
    with pytest.raises(ValueError, match="valid identifier"):
        update_env_file({"SAFE_KEY": "value", "INVALID-KEY": "value"}, env_path)
    assert not env_path.parent.exists()


@pytest.mark.parametrize("target_kind", ["symlink", "directory", "fifo"])
def test_envfile_rejects_nonregular_targets_without_following_or_replacing_them(
    tmp_path: Path,
    target_kind: str,
) -> None:
    env_path = tmp_path / ".env"
    victim = tmp_path / "victim"
    victim.write_text("preserved", encoding="utf-8")
    if target_kind == "symlink":
        env_path.symlink_to(victim)
    elif target_kind == "directory":
        env_path.mkdir()
    else:
        os.mkfifo(env_path)

    with pytest.raises(ValueError, match="regular file"):
        update_env_file({"SAFE_KEY": "new"}, env_path)

    assert victim.read_text(encoding="utf-8") == "preserved"
    if target_kind == "symlink":
        assert env_path.is_symlink()
    elif target_kind == "directory":
        assert env_path.is_dir()
    else:
        assert stat.S_ISFIFO(env_path.stat().st_mode)


@pytest.mark.parametrize(
    "value",
    [
        "spaces and # comment = equals",
        'double " and single \' quotes',
        "one\\backslash and trailing\\",
        "$HOME ${HOME} ${VALUE:-fallback}",
        "literal ${:-$}{HOME}",
        "你好，学习者",
        "",
    ],
)
def test_envfile_complex_values_round_trip_through_dotenv_and_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# preserved comment\nUNRELATED='literal # value'\nexport SMTP_PASSWORD=old\n",
        encoding="utf-8",
    )
    env_path.chmod(0o644)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    update_env_file({"SMTP_PASSWORD": value}, env_path)

    assert dotenv_values(env_path)["SMTP_PASSWORD"] == value
    assert Settings(_env_file=env_path).SMTP_PASSWORD == value
    persisted = env_path.read_text(encoding="utf-8")
    assert "# preserved comment\n" in persisted
    assert "UNRELATED='literal # value'\n" in persisted
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.env-tmp"))


def test_envfile_atomic_replace_failure_preserves_existing_bytes_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    original = b"SAFE_KEY=before\n"
    env_path.write_bytes(original)
    env_path.chmod(0o640)

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(envfile.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        update_env_file({"SAFE_KEY": "after"}, env_path)

    assert env_path.read_bytes() == original
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o640
    assert not list(tmp_path.glob(".*.env-tmp"))
