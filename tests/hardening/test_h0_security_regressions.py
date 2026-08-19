from __future__ import annotations

import asyncio
import errno
import gzip
import json
import runpy
import socket
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.context.assembler as context_assembler
import app.tools.web as web_tools
import app.tools.workspace as workspace_tools
from app.api import settings as settings_api
from app.context.assembler import ContextAssembler
from app.core.config import settings
from app.core.envfile import update_env_file
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ContextSnapshot, Owner, Plan, RunEvent, UserProfile
from app.runtime.agent import _compact_tool_message, _event_tool_arguments
from app.runtime.events import emit_event
from app.search import security as web_security
from app.tools import ToolContext, execute_tool, openai_tools
from app.tools.web import WebOpenArgs, web_open


SENTINEL = "H0_SYNTHETIC_SENTINEL_71d6f8"


def _xfail(defect_id: str, old_failure: str):
    return pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=f"{defect_id}: {old_failure}",
    )


@contextmanager
def _fixture_boundary():
    """Do not let an assertion inside a helper masquerade as the domain failure."""
    try:
        yield
    except AssertionError as exc:
        raise RuntimeError("security regression fixture raised AssertionError") from exc


def _successful_tool_data(envelope: object, tool_name: str) -> dict:
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        error = envelope.get("error") if isinstance(envelope, dict) else type(envelope).__name__
        raise RuntimeError(f"security regression fixture {tool_name} failed: {error}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"security regression fixture {tool_name} returned no typed data")
    return data


def _code_rejection_is_security_boundary(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return False
    error = str(payload.get("error", "")).lower()
    return any(
        phrase in error
        for phrase in (
            "approval required",
            "permission denied",
            "code execution disabled",
            "code_execute disabled",
            "forbidden",
            "sandbox",
            "provider unavailable",
        )
    )


def _approval_rejection(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return False
    error = str(payload.get("error", "")).lower()
    return any(phrase in error for phrase in ("approval required", "permission denied", "forbidden"))


def _fail_if_subprocess_timed_out(payload: object) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, default=str).lower()
    if "execution timed out" in serialized:
        raise RuntimeError("security regression fixture subprocess timed out")


def _require_fixture(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@_xfail(
    "H6-AUTH-001",
    "backend/run.py accepts an unauthenticated non-loopback bind",
)
def test_h6_auth_default_is_loopback_and_unauthenticated_public_bind_fails_closed(monkeypatch):
    project_root = Path(__file__).resolve().parents[2]
    captured: list[dict] = []
    public_bind_rejected = False

    def fake_uvicorn_run(*_args, **kwargs):
        captured.append(kwargs)

    with _fixture_boundary():
        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
        monkeypatch.setattr(sys, "argv", ["backend/run.py"])
        runpy.run_path(str(project_root / "backend" / "run.py"), run_name="__main__")
        default_host = captured[-1].get("host") if captured else None

        monkeypatch.setattr(sys, "argv", ["backend/run.py", "--host", "0.0.0.0"])
        try:
            runpy.run_path(str(project_root / "backend" / "run.py"), run_name="__main__")
        except SystemExit:
            public_bind_rejected = True

    assert default_host == "127.0.0.1", "H6-AUTH-001: default bind is not loopback-only"
    assert public_bind_rejected, "H6-AUTH-001: unauthenticated public bind was accepted"
    assert len(captured) == 1, "H6-AUTH-001: rejected bind still started the server"


@_xfail(
    "H6-AUTH-001",
    "the ASGI app has no trusted-host boundary before routing",
)
def test_h6_auth_rejects_untrusted_host_header_before_api_routing():
    from app.main import app

    with _fixture_boundary():
        response = TestClient(app).get(
            "/api/v1/health",
            headers={"host": "attacker.invalid"},
        )
    assert response.status_code == 400, "H6-AUTH-001: untrusted Host reached API routing"


@_xfail(
    "H6-CODE-001",
    "code_execute is exposed to the model by default without a sandbox provider",
)
def test_h6_code_execute_is_disabled_in_the_default_tool_surface():
    with _fixture_boundary():
        names = {tool["function"]["name"] for tool in openai_tools()}
    assert "code_execute" not in names, "H6-CODE-001: code_execute is enabled by default"


@_xfail(
    "H6-CODE-001",
    "code_execute runs without a per-invocation approval guard",
)
@pytest.mark.asyncio
async def test_h6_code_execute_without_approval_has_no_side_effect(tmp_path, monkeypatch):
    with _fixture_boundary():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        marker = workspace / "must-not-exist.txt"
        monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)

        async with AsyncSessionLocal() as db:
            run = AgentRun(owner_id="local", trigger="user_message", objective="read-only fixture")
            db.add(run)
            await db.commit()
            result = await execute_tool(
                "code_execute",
                json.dumps(
                    {
                        "language": "python",
                        "code": "from pathlib import Path; Path('must-not-exist.txt').write_text('fixture')",
                    }
                ),
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run.id,
                    trigger="user_message",
                    approval_granted=False,
                ),
            )

    _fail_if_subprocess_timed_out(result)
    if result.get("ok") is True:
        approval_blocked = result.get("data", {}).get("approval_required") is True
    elif _approval_rejection(result):
        approval_blocked = True
    else:
        raise RuntimeError(f"security regression fixture code_execute failed: {result.get('error')}")

    assert not marker.exists(), "H6-CODE-001: unapproved code produced a filesystem side effect"
    assert approval_blocked, "H6-CODE-001: unapproved code invocation was not rejected"


@_xfail(
    "H6-CODE-001",
    "the prlimit wrapper can read host files outside the Agent workspace",
)
@pytest.mark.asyncio
async def test_h6_code_execute_cannot_read_a_synthetic_host_secret(tmp_path, monkeypatch):
    with _fixture_boundary():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "synthetic-host-file.txt"
        outside.write_text("non-secret host fixture", encoding="utf-8")
        host_read_signal = workspace / "host-read-succeeded.txt"
        monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)

        async with AsyncSessionLocal() as db:
            run = AgentRun(owner_id="local", trigger="user_message", objective="read-only fixture")
            db.add(run)
            await db.commit()
            result = await execute_tool(
                "code_execute",
                json.dumps(
                    {
                        "language": "python",
                        "code": (
                            "import sys; from pathlib import Path; "
                            f"outside=Path({str(outside)!r}); "
                            "\ntry: outside.read_bytes()"
                            "\nexcept OSError: raise SystemExit(73)"
                            "\nPath('host-read-succeeded.txt').write_text('read', encoding='utf-8')"
                            "\nraise SystemExit(0)"
                        ),
                    }
                ),
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run.id,
                    trigger="user_message",
                    approval_granted=True,
                ),
            )

    _fail_if_subprocess_timed_out(result)
    if result.get("ok") is True:
        exit_code = result.get("data", {}).get("exit_code")
        if exit_code not in {0, 73}:
            raise RuntimeError(f"security regression fixture process exited unexpectedly: {exit_code}")
        host_read_blocked = exit_code == 73
    elif _code_rejection_is_security_boundary(result):
        host_read_blocked = True
    else:
        raise RuntimeError(f"security regression fixture code_execute failed: {result.get('error')}")

    assert not host_read_signal.exists(), "H6-CODE-001: code read an outside host file"
    assert host_read_blocked, "H6-CODE-001: host file read was not denied"


@_xfail(
    "H6-CODE-001",
    "the prlimit wrapper has unrestricted loopback network access",
)
@pytest.mark.asyncio
async def test_h6_code_execute_cannot_reach_a_loopback_service(tmp_path, monkeypatch):
    reached = asyncio.Event()
    handler_errors: list[BaseException] = []

    async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            reached.set()
            await reader.read(64)
            writer.write(b"loopback-fixture-response")
            await writer.drain()
        except BaseException as exc:
            handler_errors.append(exc)
            raise
        finally:
            writer.close()
            await writer.wait_closed()

    with _fixture_boundary():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)
        server = await asyncio.start_server(handle_connection, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        try:
            async with AsyncSessionLocal() as db:
                run = AgentRun(owner_id="local", trigger="user_message", objective="read-only fixture")
                db.add(run)
                await db.commit()
                result = await execute_tool(
                    "code_execute",
                    json.dumps(
                        {
                            "language": "python",
                            "timeout_seconds": 2,
                            "code": (
                                "import socket; "
                                f"\ntry: s=socket.create_connection(('127.0.0.1',{port}),timeout=1)"
                                "\nexcept OSError: raise SystemExit(74)"
                                "\ns.sendall(b'x'); s.recv(128); raise SystemExit(0)"
                            ),
                        }
                    ),
                    ToolContext(
                        db=db,
                        owner_id="local",
                        run_id=run.id,
                        trigger="user_message",
                        approval_granted=True,
                    ),
                )
        finally:
            server.close()
            await server.wait_closed()

    _fail_if_subprocess_timed_out(result)
    if handler_errors:
        raise RuntimeError("security regression fixture loopback server failed") from handler_errors[0]
    if result.get("ok") is True:
        exit_code = result.get("data", {}).get("exit_code")
        if exit_code not in {0, 74}:
            raise RuntimeError(f"security regression fixture process exited unexpectedly: {exit_code}")
        network_blocked = exit_code == 74
    elif _code_rejection_is_security_boundary(result):
        network_blocked = True
    else:
        raise RuntimeError(f"security regression fixture code_execute failed: {result.get('error')}")

    assert not reached.is_set(), "H6-CODE-001: code reached a loopback service"
    assert network_blocked, "H6-CODE-001: approved code retained loopback network access"


@_xfail(
    "H6-TRUST-001",
    "web_open returns model-visible external content without an external_untrusted marker",
)
@pytest.mark.asyncio
async def test_h6_trust_web_content_is_marked_external_untrusted(monkeypatch):
    with _fixture_boundary():
        request = httpx.Request("GET", "https://fixture.invalid/")
        response = httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Fixture</title><p>untrusted fixture body</p>",
            request=request,
        )

        async def fake_fetch(_client, _url):
            return response, 0

        monkeypatch.setattr(web_tools, "fetch_with_safe_redirects", fake_fetch)
        async with AsyncSessionLocal() as db:
            run = AgentRun(owner_id="local", trigger="user_message", objective="trust fixture")
            db.add(run)
            await db.commit()
            envelope = await execute_tool(
                "web_open",
                json.dumps({"url": "https://fixture.invalid/"}),
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run.id,
                    trigger="user_message",
                ),
            )

    data = _successful_tool_data(envelope, "web_open")
    assert data.get("external_untrusted") is True, (
        "H6-TRUST-001: web data envelope lacks external_untrusted=true"
    )


@_xfail(
    "H6-TRUST-001",
    "file_read returns file content without an external_untrusted marker",
)
@pytest.mark.asyncio
async def test_h6_trust_file_content_is_marked_external_untrusted(tmp_path, monkeypatch):
    with _fixture_boundary():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "fixture.txt").write_text("untrusted fixture body", encoding="utf-8")
        monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)

        async with AsyncSessionLocal() as db:
            run = AgentRun(owner_id="local", trigger="user_message", objective="trust fixture")
            db.add(run)
            await db.commit()
            envelope = await execute_tool(
                "file_read",
                json.dumps({"path": "fixture.txt"}),
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run.id,
                    trigger="user_message",
                ),
            )

    data = _successful_tool_data(envelope, "file_read")
    assert data.get("external_untrusted") is True, (
        "H6-TRUST-001: file data envelope lacks external_untrusted=true"
    )


@_xfail(
    "H6-TRUST-001",
    "email_reply is treated as implicit authority for a persistent plan write",
)
@pytest.mark.asyncio
async def test_h6_trust_email_content_is_not_implicit_write_authority():
    payload = {
        "title": "Synthetic email plan",
        "goal": "Must remain a proposal",
        "current_level": "fixture",
        "weekly_minutes": 60,
        "expected_outcome": "No write without approval",
        "stages": [{"title": "Fixture stage", "tasks": [{"title": "Fixture task"}]}],
    }
    with _fixture_boundary():
        async with AsyncSessionLocal() as db:
            run = AgentRun(owner_id="local", trigger="email_reply", objective="external fixture")
            db.add(run)
            await db.commit()
            result = await execute_tool(
                "plan_create",
                json.dumps(payload),
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run.id,
                    trigger="email_reply",
                    approval_granted=False,
                ),
            )
            plan_count = int((await db.scalar(select(func.count(Plan.id)))) or 0)

    if result.get("ok") is True:
        approval_blocked = result.get("data", {}).get("approval_required") is True
    elif _approval_rejection(result):
        approval_blocked = True
    else:
        raise RuntimeError(f"security regression fixture plan_create failed: {result.get('error')}")

    assert plan_count == 0, "H6-TRUST-001: email content caused a persistent plan write"
    assert approval_blocked, "H6-TRUST-001: email-derived write did not require approval"


@_xfail(
    "H6-WEB-001",
    "the DNS guard checks selected flags instead of rejecting every non-global target",
)
@pytest.mark.parametrize(
    "dns_cases",
    [
        pytest.param(
            [
                ("loopback", [(socket.AF_INET, "127.0.0.2")]),
                ("private", [(socket.AF_INET, "10.23.4.5")]),
                ("link-local", [(socket.AF_INET, "169.254.10.20")]),
                ("multicast", [(socket.AF_INET, "224.0.0.251")]),
                ("unspecified", [(socket.AF_INET, "0.0.0.0")]),
                ("reserved", [(socket.AF_INET, "240.0.0.1")]),
                ("cgnat", [(socket.AF_INET, "100.64.0.1")]),
                ("ipv6-ula", [(socket.AF_INET6, "fd00::1")]),
                (
                    "mixed-public-private",
                    [
                        (socket.AF_INET, "93.184.216.34"),
                        (socket.AF_INET, "10.23.4.5"),
                    ],
                ),
            ],
            id="all-non-global-classes",
        )
    ],
)
@pytest.mark.asyncio
async def test_h6_web_rejects_every_non_global_dns_target(monkeypatch, dns_cases):
    accepted: list[str] = []
    with _fixture_boundary():
        for label, addresses in dns_cases:
            def fixture_result(_host, port, *, _addresses=addresses):
                return [
                    (
                        family,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        (address, port, 0, 0) if family == socket.AF_INET6 else (address, port),
                    )
                    for family, address in _addresses
                ]

            monkeypatch.setattr(web_security.socket, "getaddrinfo", fixture_result)
            try:
                await web_security.validate_public_url("http://fixture.invalid/")
            except ValueError:
                continue
            accepted.append(label)

    assert accepted == [], f"H6-WEB-001: non-global DNS targets were accepted: {accepted}"


class _SyntheticPeerStream:
    def get_extra_info(self, name: str):
        if name in {"peername", "server_addr"}:
            return ("127.0.0.1", 43210)
        return None


class _RebindingClient:
    async def get(self, url, *, params=None, follow_redirects=False):
        del params, follow_redirects
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="loopback fixture",
            request=request,
            extensions={"network_stream": _SyntheticPeerStream()},
        )


@_xfail(
    "H6-WEB-001",
    "the validated DNS address is not pinned or compared with the connected peer",
)
@pytest.mark.asyncio
async def test_h6_web_rejects_dns_rebinding_peer_mismatch(monkeypatch):
    rejected = False

    def public_result(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    with _fixture_boundary():
        monkeypatch.setattr(web_security.socket, "getaddrinfo", public_result)
        try:
            await web_security.fetch_with_safe_redirects(
                _RebindingClient(),
                "http://rebind.invalid/",
            )
        except ValueError:
            rejected = True

    assert rejected, "H6-WEB-001: connected peer was not checked against validated DNS"


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        pytest.param("identity", b"A" * 4096, id="wire-size"),
        pytest.param("gzip", gzip.compress(b"A" * 8192), id="decompressed-size"),
    ],
)
@_xfail(
    "H6-WEB-001",
    "web responses are fully buffered and decompressed without byte ceilings",
)
@pytest.mark.asyncio
async def test_h6_web_enforces_wire_and_decompressed_size_limits(
    monkeypatch,
    encoding,
    body,
):
    rejected = False

    async def allow_fixture_url(_url):
        return None

    async def handler(request):
        headers = {"content-type": "text/plain"}
        if encoding == "gzip":
            headers["content-encoding"] = "gzip"
        return httpx.Response(200, headers=headers, content=body, request=request)

    with _fixture_boundary():
        monkeypatch.setattr(web_security, "validate_public_url", allow_fixture_url)
        monkeypatch.setitem(settings.__dict__, "WEB_MAX_RESPONSE_BYTES", 1024)
        monkeypatch.setitem(settings.__dict__, "WEB_MAX_DECOMPRESSED_BYTES", 2048)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                await web_security.fetch_with_safe_redirects(client, "https://fixture.invalid/")
            except ValueError:
                rejected = True

    limit_kind = "wire" if encoding == "identity" else "decompressed"
    assert rejected, f"H6-WEB-001: {limit_kind} response size limit was not enforced"


@_xfail(
    "H6-WEB-001",
    "web_open uses substring matching and accepts application/notjson",
)
@pytest.mark.asyncio
async def test_h6_web_uses_an_exact_content_type_allowlist(monkeypatch):
    with _fixture_boundary():
        request = httpx.Request("GET", "https://fixture.invalid/")
        response = httpx.Response(
            200,
            headers={"content-type": "application/notjson"},
            text="<p>must not be parsed</p>",
            request=request,
        )

        async def fake_fetch(_client, _url):
            return response, 0

        monkeypatch.setattr(web_tools, "fetch_with_safe_redirects", fake_fetch)
        result = await web_open(None, WebOpenArgs(url="https://fixture.invalid/"))
    assert "error" in result, "H6-WEB-001: non-allowlisted content type was parsed"


@_xfail(
    "H6-ENV-001",
    "the allowlisted PATH is copied from the parent and the interpreter is resolved through it",
)
def test_h6_env_parent_path_cannot_replace_the_code_interpreter(tmp_path, monkeypatch):
    with _fixture_boundary():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        poison_bin = tmp_path / "poison-bin"
        poison_bin.mkdir()
        fake_python = poison_bin / "python"
        fake_python.write_text(f"#!/bin/sh\nprintf '%s' '{SENTINEL}'\n", encoding="utf-8")
        fake_python.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(poison_bin))
        monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)

        result = workspace_tools._run_code(
            workspace_tools.CodeExecuteArgs(
                language="python",
                code="print('trusted-interpreter')",
            )
        )

    _fail_if_subprocess_timed_out(result)
    assert SENTINEL not in result.get(
        "stdout", ""
    ), "H6-ENV-001: parent PATH replaced the code interpreter"
    _require_fixture(
        "trusted-interpreter" in result.get("stdout", ""),
        "trusted interpreter did not execute the fixture",
    )


@_xfail(
    "H6-CONFIG-001",
    ".env values accept newlines that inject additional assignments",
)
def test_h6_config_rejects_newlines_without_changing_the_env_file(tmp_path):
    rejected = False
    with _fixture_boundary():
        env_path = tmp_path / ".env"
        original = "SAFE_FIXTURE=unchanged\n"
        env_path.write_text(original, encoding="utf-8")

        try:
            update_env_file(
                {"OPENAI_API_KEY": f"synthetic-value\nINJECTED_FIXTURE={SENTINEL}"},
                env_path,
            )
        except ValueError:
            rejected = True
        persisted = env_path.read_text(encoding="utf-8")

    assert rejected, "H6-CONFIG-001: newline-bearing .env value was accepted"
    assert persisted == original, "H6-CONFIG-001: rejected update changed the .env file"


def test_h6_config_atomic_temp_file_does_not_follow_symlinks(tmp_path):
    with _fixture_boundary():
        env_path = tmp_path / ".env"
        env_path.write_text("SAFE_FIXTURE=before\n", encoding="utf-8")
        victim = tmp_path / "synthetic-victim.txt"
        victim.write_text(SENTINEL, encoding="utf-8")
        predictable_temp = tmp_path / "..env.tmp"
        predictable_temp.symlink_to(victim)

        try:
            update_env_file({"SAFE_FIXTURE": "after"}, env_path)
        except ValueError:
            pass
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ELOOP}:
                raise
        victim_value = victim.read_text(encoding="utf-8")
        env_is_symlink = env_path.is_symlink()

    assert victim_value == SENTINEL, "H6-CONFIG-001: .env temp path overwrote a symlink target"
    assert not env_is_symlink, "H6-CONFIG-001: .env update replaced the target with a symlink"


@_xfail(
    "H6-CONFIG-001",
    ".env values are written without quoting and do not round-trip safely",
)
def test_h6_config_complex_values_round_trip_exactly(tmp_path):
    with _fixture_boundary():
        env_path = tmp_path / ".env"
        value = f'synthetic value # {SENTINEL} = "quoted" \\ tail'
        update_env_file({"SMTP_PASSWORD": value}, env_path)
        parsed_value = dotenv_values(env_path)["SMTP_PASSWORD"]
    assert parsed_value == value, "H6-CONFIG-001: complex .env value did not round-trip"


@_xfail(
    "H6-REDACT-001",
    "Run tool arguments and model observations have no unified secret redaction",
)
def test_h6_redact_tool_trace_and_model_observation(monkeypatch):
    with _fixture_boundary():
        monkeypatch.setattr(settings, "OPENAI_API_KEY", SENTINEL)
        arguments = _event_tool_arguments(
            json.dumps({"api_key": SENTINEL, "nested": {"password": SENTINEL}})
        )
        observation = _compact_tool_message(
            {"ok": False, "error": f"Authorization: Bearer {SENTINEL}"}
        )
    assert SENTINEL not in json.dumps(
        arguments, ensure_ascii=False
    ), "H6-REDACT-001: tool event arguments exposed a secret"
    assert SENTINEL not in observation, "H6-REDACT-001: model observation exposed a secret"


@_xfail(
    "H6-REDACT-001",
    "RunEvent payloads persist configured secret values verbatim",
)
@pytest.mark.asyncio
async def test_h6_redact_run_event_payload_before_persistence(monkeypatch):
    with _fixture_boundary():
        monkeypatch.setattr(settings, "OPENAI_API_KEY", SENTINEL)
        async with AsyncSessionLocal() as db:
            run = AgentRun(owner_id="local", trigger="user_message", objective="redaction fixture")
            db.add(run)
            await db.commit()
            await emit_event(
                db,
                run.id,
                "fixture.failed",
                "synthetic failure",
                {"technical_error": f"Bearer {SENTINEL}", "nested": {"api_key": SENTINEL}},
            )
            event = (
                await db.execute(select(RunEvent).where(RunEvent.run_id == run.id))
            ).scalars().one()

    assert SENTINEL not in json.dumps(
        event.payload, ensure_ascii=False
    ), "H6-REDACT-001: persisted RunEvent payload exposed a secret"


@_xfail(
    "H6-REDACT-001",
    "ContextSnapshot source manifests and Markdown projections persist configured secrets verbatim",
)
@pytest.mark.asyncio
async def test_h6_redact_context_snapshot_manifest_and_markdown(tmp_path, monkeypatch):
    with _fixture_boundary():
        runtime_root = tmp_path / "context-runtime"
        monkeypatch.setattr(context_assembler, "PROJECT_ROOT", runtime_root)
        monkeypatch.setattr(settings, "OPENAI_API_KEY", SENTINEL)

        async with AsyncSessionLocal() as db:
            owner = Owner(id=SENTINEL, display_name="Synthetic owner")
            profile = UserProfile(
                owner_id=SENTINEL,
                agent_style=SENTINEL,
                preferences={"api_key": SENTINEL},
            )
            run = AgentRun(
                id="context-redaction-fixture-run",
                owner_id=SENTINEL,
                trigger="user_message",
                objective="context redaction fixture",
            )
            db.add(owner)
            await db.commit()
            db.add_all([profile, run])
            await db.commit()
            snapshot = await ContextAssembler(db).build(
                SENTINEL,
                run_id=run.id,
                objective="context redaction fixture",
            )
            snapshot_id = snapshot.id
            await db.commit()
            stored = await db.get(ContextSnapshot, snapshot_id)
            if stored is None:
                raise RuntimeError("security regression fixture ContextSnapshot was not persisted")
            manifest = json.dumps(stored.source_manifest, ensure_ascii=False)
            snapshot_markdown = stored.markdown

        run_markdown = (
            runtime_root / "data" / "context" / "runs" / f"{run.id}.md"
        ).read_text(encoding="utf-8")
        canonical_markdown = (
            runtime_root / "data" / "context" / "global.md"
        ).read_text(encoding="utf-8")

    assert SENTINEL not in manifest, "H6-REDACT-001: source manifest exposed a secret"
    assert SENTINEL not in snapshot_markdown, "H6-REDACT-001: snapshot Markdown exposed a secret"
    assert SENTINEL not in run_markdown, "H6-REDACT-001: run Markdown projection exposed a secret"
    assert SENTINEL not in canonical_markdown, (
        "H6-REDACT-001: canonical Markdown projection exposed a secret"
    )


@_xfail(
    "H6-REDACT-001",
    "settings diagnostic error responses echo exception text without redaction",
)
@pytest.mark.asyncio
async def test_h6_redact_settings_diagnostic_errors(monkeypatch):
    captured: HTTPException | None = None

    async def fail_smtp(*, send_message=False):
        del send_message
        raise RuntimeError(f"synthetic provider error: {SENTINEL}")

    with _fixture_boundary():
        monkeypatch.setattr(settings_api, "test_smtp", fail_smtp)
        try:
            await settings_api.test_email_configuration(
                settings_api.EmailTestRequest(channel="smtp")
            )
        except HTTPException as exc:
            captured = exc

    _require_fixture(captured is not None, "diagnostic provider failure was not surfaced")
    assert SENTINEL not in str(
        captured.detail
    ), "H6-REDACT-001: settings diagnostic response exposed a secret"
