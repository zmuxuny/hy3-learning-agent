from __future__ import annotations

import json
import secrets
import subprocess

import pytest
from sqlalchemy import func, select

import app.tools.workspace as workspace_tools
from app.core.execution_policy import (
    CODE_EXECUTION_ERROR_CODE,
    CODE_EXECUTION_REASON_CODE,
    TRUSTED_SUBPROCESS_PATH,
    current_code_execution_policy,
)
from app.core.config import settings
from app.core.redaction import REDACTED
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    OutboxAction,
    OutboxReceipt,
    RunApproval,
    ToolInvocation,
)
from app.outbox import dispatch_action, enqueue_subprocess
from app.runtime.checkpoints import CHECKPOINT_SCHEMA_VERSION, make_checkpoint
from app.tools import ToolContext, execute_tool, openai_tools
from app.tools.base import ToolEffectKind
from app.tools.registry import TOOL_MAP


async def _create_run() -> str:
    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="H6 synthetic execution-policy fixture",
        )
        db.add(run)
        await db.commit()
        return run.id


def test_code_execution_policy_fails_closed_without_a_provider(monkeypatch) -> None:
    local = current_code_execution_policy(deployment_mode="local")
    server = current_code_execution_policy(deployment_mode="server")

    assert local.available is False
    assert server.available is False
    assert local.provider_id is None and server.provider_id is None
    assert local.reason_code == CODE_EXECUTION_REASON_CODE
    assert server.reason_code == CODE_EXECUTION_REASON_CODE
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "server")
    assert current_code_execution_policy().deployment_mode == "server"
    assert current_code_execution_policy().available is False


def test_default_model_tool_surface_omits_code_execute() -> None:
    names = {item["function"]["name"] for item in openai_tools()}
    assert "code_execute" not in names


@pytest.mark.asyncio
async def test_direct_approved_flag_cannot_bypass_missing_sandbox(monkeypatch) -> None:
    run_id = await _create_run()
    calls = 0

    def forbidden_runner(_args):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled code reached the host runner")

    monkeypatch.setattr(workspace_tools, "_run_code", forbidden_runner)
    async with AsyncSessionLocal() as db:
        result = await execute_tool(
            "code_execute",
            json.dumps({"language": "python", "code": "raise SystemExit(99)"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                approval_granted=True,
                tool_call_id="approved-without-provider",
            ),
        )
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
        outbox_count = int(
            await db.scalar(
                select(func.count(OutboxAction.id)).where(
                    OutboxAction.run_id == run_id
                )
            )
            or 0
        )

    assert result["ok"] is False
    assert result["error_code"] == CODE_EXECUTION_ERROR_CODE
    assert result["reason_code"] == CODE_EXECUTION_REASON_CODE
    assert invocation_count == 0
    assert outbox_count == 0
    assert calls == 0


@pytest.mark.asyncio
async def test_stale_subprocess_outbox_is_cancelled_without_host_execution(monkeypatch) -> None:
    run_id = await _create_run()
    request_digest = "a" * 64
    calls = 0

    def forbidden_runner(_args):
        nonlocal calls
        calls += 1
        raise AssertionError("stale subprocess intent reached the host runner")

    monkeypatch.setattr(workspace_tools, "_run_code", forbidden_runner)
    async with AsyncSessionLocal() as db:
        invocation = ToolInvocation(
            owner_id="local",
            run_id=run_id,
            idempotency_key=f"{run_id}:legacy-subprocess",
            tool_name="code_execute",
            tool_call_id="legacy-subprocess",
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
            run_id=run_id,
            invocation_id=invocation.id,
            action_key=f"{run_id}:legacy-subprocess",
            request_digest=request_digest,
            arguments={"language": "python", "code": "pass", "timeout_seconds": 1},
        )
        await db.commit()
        action_key = action.action_key
        invocation_id = invocation.id

    outcome = await dispatch_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
    )
    replay = await dispatch_action(
        action_key=action_key,
        session_factory=AsyncSessionLocal,
    )

    assert outcome["status"] == "cancelled"
    assert outcome["delivered"] is False
    assert outcome["data"]["error_code"] == CODE_EXECUTION_ERROR_CODE
    assert replay["status"] == "cancelled"
    assert replay["data"]["error_code"] == CODE_EXECUTION_ERROR_CODE
    assert calls == 0
    async with AsyncSessionLocal() as db:
        action = await db.scalar(
            select(OutboxAction).where(OutboxAction.action_key == action_key)
        )
        invocation = await db.get(ToolInvocation, invocation_id)
        receipt = await db.scalar(
            select(OutboxReceipt).where(OutboxReceipt.outbox_action_id == action.id)
        )
        assert action.status == "cancelled"
        assert invocation.status == "cancelled"
        assert receipt.status == "reconciled"
        assert receipt.response["reason_code"] == CODE_EXECUTION_REASON_CODE


@pytest.mark.parametrize(
    ("language", "expected_interpreter"),
    [("python", "/usr/bin/python3"), ("bash", "/bin/bash")],
)
def test_host_runner_uses_absolute_executables_and_a_fixed_minimal_environment(
    language: str,
    expected_interpreter: str,
    monkeypatch,
) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = dict(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="fixture\n", stderr="")

    monkeypatch.setenv("PATH", "/synthetic/untrusted/path")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/untrusted/pythonpath")
    monkeypatch.setenv("BASH_ENV", "/synthetic/untrusted/bash-env")
    monkeypatch.setattr(workspace_tools.subprocess, "run", fake_run)

    result = workspace_tools._run_code(
        workspace_tools.CodeExecuteArgs(language=language, code="print('fixture')")
    )

    command = captured["command"]
    separator = command.index("--")
    assert command[0] == "/usr/bin/prlimit"
    assert command[separator + 1] == expected_interpreter
    assert all(item.startswith("/") for item in (command[0], command[separator + 1]))
    assert captured["kwargs"]["env"] == {
        "PATH": TRUSTED_SUBPROCESS_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
    assert result["stdout"] == "fixture\n"


@pytest.mark.asyncio
async def test_workspace_reads_are_durable_external_untrusted_observations(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("synthetic external input", encoding="utf-8")
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)
    run_id = await _create_run()
    async with AsyncSessionLocal() as db:
        listed = await execute_tool(
            "file_list",
            json.dumps({"path": "."}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="external-file-list",
            ),
        )
        read = await execute_tool(
            "file_read",
            json.dumps({"path": "fixture.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="external-file-read",
            ),
        )
        invocations = list(
            (
                await db.execute(
                    select(ToolInvocation).where(ToolInvocation.run_id == run_id)
                )
            ).scalars()
        )

    assert listed["ok"] is True and listed["data"]["external_untrusted"] is True
    assert read["ok"] is True and read["data"]["external_untrusted"] is True
    assert {item.tool_name for item in invocations} == {"file_list", "file_read"}
    assert all(item.effect_kind == ToolEffectKind.EXTERNAL_READ.value for item in invocations)
    assert all(item.status == "committed" for item in invocations)
    assert all(item.result_payload["external_untrusted"] is True for item in invocations)
    assert TOOL_MAP["file_list"].effect_kind == ToolEffectKind.EXTERNAL_READ
    assert TOOL_MAP["file_read"].effect_kind == ToolEffectKind.EXTERNAL_READ


@pytest.mark.asyncio
async def test_workspace_tools_reject_parent_path_escape_before_any_side_effect(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must remain private", encoding="utf-8")
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)
    run_id = await _create_run()

    async with AsyncSessionLocal() as db:
        read = await execute_tool(
            "file_read",
            json.dumps({"path": "../outside.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="parent-escape-read",
            ),
        )
        write = await execute_tool(
            "file_write",
            json.dumps({"path": "../created-outside.txt", "content": "forbidden"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="parent-escape-write",
            ),
        )
        outbox_count = int(
            await db.scalar(
                select(func.count(OutboxAction.id)).where(OutboxAction.run_id == run_id)
            )
            or 0
        )

    assert read["ok"] is False and read["error_code"] == "tool_execution_failed"
    assert write["ok"] is False and write["error_code"] == "tool_execution_failed"
    assert outside.read_text(encoding="utf-8") == "must remain private"
    assert not (tmp_path / "created-outside.txt").exists()
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_workspace_tools_reject_internal_symlinks_to_outside_targets(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside_file = outside_directory / "private.txt"
    outside_file.write_text("must remain private", encoding="utf-8")
    (workspace / "escaped-file.txt").symlink_to(outside_file)
    (workspace / "escaped-directory").symlink_to(
        outside_directory,
        target_is_directory=True,
    )
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)
    run_id = await _create_run()

    async with AsyncSessionLocal() as db:
        read = await execute_tool(
            "file_read",
            json.dumps({"path": "escaped-file.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="symlink-escape-read",
            ),
        )
        write = await execute_tool(
            "file_write",
            json.dumps(
                {
                    "path": "escaped-directory/created.txt",
                    "content": "forbidden",
                }
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="symlink-escape-write",
            ),
        )
        outbox_count = int(
            await db.scalar(
                select(func.count(OutboxAction.id)).where(OutboxAction.run_id == run_id)
            )
            or 0
        )

    assert read["ok"] is False and read["error_code"] == "tool_execution_failed"
    assert write["ok"] is False and write["error_code"] == "tool_execution_failed"
    assert outside_file.read_text(encoding="utf-8") == "must remain private"
    assert not (outside_directory / "created.txt").exists()
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_external_file_read_requires_exact_approval_before_a_write(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "external.txt").write_text("synthetic external input", encoding="utf-8")
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace)
    run_id = await _create_run()

    async with AsyncSessionLocal() as db:
        observed = await execute_tool(
            "file_read",
            json.dumps({"path": "external.txt"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="external-source",
            ),
        )
        attempted = await execute_tool(
            "file_write",
            json.dumps({"path": "must-not-exist.txt", "content": "blocked"}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="write-after-external-source",
            ),
        )
        write_invocation = await db.scalar(
            select(ToolInvocation).where(
                ToolInvocation.run_id == run_id,
                ToolInvocation.tool_call_id == "write-after-external-source",
            )
        )
        outbox_count = int(
            await db.scalar(
                select(func.count(OutboxAction.id)).where(
                    OutboxAction.run_id == run_id
                )
            )
            or 0
        )

    assert observed["ok"] is True
    assert attempted["ok"] is True
    assert attempted["status"] == "pending_approval"
    assert attempted["data"]["approval_required"] is True
    assert attempted["data"]["blocking"] is True
    assert write_invocation.status == "pending_approval"
    assert outbox_count == 0
    assert not (workspace / "must-not-exist.txt").exists()


@pytest.mark.asyncio
async def test_root_and_child_checkpoints_recursively_redact_configured_secrets(
    monkeypatch,
) -> None:
    credential = secrets.token_urlsafe(32)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)

    root_checkpoint = make_checkpoint(
        kind="agent",
        phase="tool_ready",
        step=1,
        messages=[{"role": "user", "content": f"prefix {credential} suffix"}],
        current_tool_call={
            "id": "root-secret-call",
            "name": "file_write",
            "arguments": json.dumps({"content": credential}),
        },
        cards=[{"nested": [{"token": credential}]}],
    )
    child_checkpoint = make_checkpoint(
        kind="subagent",
        phase="awaiting_model",
        step=2,
        messages=[{"role": "assistant", "content": credential}],
        identity={"context": {"credential": credential}},
    )

    async with AsyncSessionLocal() as db:
        root = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="root checkpoint redaction",
            checkpoint=root_checkpoint,
            checkpoint_schema_version=CHECKPOINT_SCHEMA_VERSION,
        )
        db.add(root)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=root.id,
            trigger="subagent",
            objective="child checkpoint redaction",
            checkpoint=child_checkpoint,
            checkpoint_schema_version=CHECKPOINT_SCHEMA_VERSION,
        )
        db.add(child)
        await db.commit()
        root_id = root.id
        child_id = child.id

    async with AsyncSessionLocal() as db:
        stored = [await db.get(AgentRun, root_id), await db.get(AgentRun, child_id)]

    for run in stored:
        encoded = json.dumps(run.checkpoint, ensure_ascii=False)
        assert credential not in encoded
        assert REDACTED in encoded


@pytest.mark.asyncio
async def test_tool_secret_material_is_rejected_before_invocation_or_approval(
    monkeypatch,
) -> None:
    credential = secrets.token_urlsafe(32)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", credential)
    run_id = await _create_run()

    async with AsyncSessionLocal() as db:
        configured_secret = await execute_tool(
            "file_write",
            json.dumps({"path": "secret.txt", "content": credential}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="configured-secret-call",
            ),
        )
        redacted_replay = await execute_tool(
            "file_write",
            json.dumps({"path": "redacted.txt", "content": REDACTED}),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="user_message",
                tool_call_id="redacted-secret-call",
            ),
        )
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
        approval_count = int(
            await db.scalar(
                select(func.count(RunApproval.id)).where(RunApproval.run_id == run_id)
            )
            or 0
        )

    assert configured_secret["error_code"] == "secret_material_forbidden"
    assert configured_secret["reason_code"] == "CONFIGURED_SECRET_IN_TOOL_ARGUMENTS"
    assert redacted_replay["error_code"] == "secret_material_forbidden"
    assert redacted_replay["reason_code"] == "REDACTED_SECRET_REPLAY_FORBIDDEN"
    assert credential not in json.dumps(configured_secret, ensure_ascii=False)
    assert invocation_count == 0
    assert approval_count == 0
