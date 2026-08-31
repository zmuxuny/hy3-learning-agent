from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.execution_policy import (
    code_execution_rejection,
    current_code_execution_policy,
    minimal_subprocess_environment,
)
from app.core.trust import mark_external_untrusted_result
from app.db.uow import flush as flush_uow
from app.models import Operation
from app.outbox import (
    enqueue_subprocess,
    enqueue_workspace_write,
    prepare_workspace_write,
)
from app.tools.base import ToolContext, ToolDefinition, ToolEffectKind


WORKSPACE_ROOT = (settings.RUNTIME_STATE_ROOT / "data" / "workspace").resolve()


class FileListArgs(BaseModel):
    path: str = "."
    recursive: bool = False
    limit: int = Field(default=100, ge=1, le=500)


class FileReadArgs(BaseModel):
    path: str
    max_chars: int = Field(default=12000, ge=100, le=50000)


class FileWriteArgs(BaseModel):
    path: str
    content: str = Field(max_length=100000)
    overwrite: bool = False


class CodeExecuteArgs(BaseModel):
    language: Literal["python", "bash"] = "python"
    code: str = Field(min_length=1, max_length=50000)
    timeout_seconds: int = Field(default=8, ge=1, le=30)


def _resolve(relative_path: str, *, must_exist: bool = False) -> Path:
    candidate = (WORKSPACE_ROOT / relative_path).resolve()
    if candidate != WORKSPACE_ROOT and WORKSPACE_ROOT not in candidate.parents:
        raise ValueError("Path escapes the personal Agent workspace")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(relative_path)
    return candidate


async def file_list(_: ToolContext, args: FileListArgs) -> dict:
    root = _resolve(args.path, must_exist=True)
    iterator = root.rglob("*") if args.recursive else root.glob("*")
    entries = []
    for path in iterator:
        if len(entries) >= args.limit:
            break
        stat = path.stat()
        entries.append({
            "path": str(path.relative_to(WORKSPACE_ROOT)),
            "kind": "directory" if path.is_dir() else "file",
            "size": stat.st_size if path.is_file() else None,
        })
    return mark_external_untrusted_result({
        "workspace": str(WORKSPACE_ROOT),
        "entries": entries,
        "truncated": len(entries) >= args.limit,
    })


async def file_read(_: ToolContext, args: FileReadArgs) -> dict:
    path = _resolve(args.path, must_exist=True)
    if not path.is_file():
        return {"error": "Path is not a file"}
    if path.stat().st_size > 2_000_000:
        return {"error": "File exceeds the 2 MB read limit"}
    raw = await asyncio.to_thread(path.read_bytes)
    if b"\x00" in raw[:4096]:
        return {"error": "Binary files cannot be read as text"}
    content = raw.decode("utf-8", errors="replace")
    return mark_external_untrusted_result({
        "path": str(path.relative_to(WORKSPACE_ROOT)),
        "content": content[: args.max_chars],
        "truncated": len(content) > args.max_chars,
        "size": len(raw),
    })


async def file_write(ctx: ToolContext, args: FileWriteArgs) -> dict:
    if not ctx.action_key or not ctx.request_digest or ctx.invocation_id is None:
        return {
            "error": "file_write requires a durable invocation claim",
            "error_code": "missing_invocation_claim",
        }
    try:
        prepared = await prepare_workspace_write(
            path=args.path,
            content=args.content,
            overwrite=args.overwrite,
        )
    except FileExistsError as exc:
        return {"error": str(exc)}
    path = _resolve(args.path)
    relative = str(path.relative_to(WORKSPACE_ROOT))
    previous = None
    if prepared.before_exists:
        try:
            previous = (prepared.before_content or b"").decode("utf-8")
        except UnicodeDecodeError:
            return {"error": "Existing binary files cannot be overwritten as text"}
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        invocation_id=ctx.invocation_id,
        tool_name="file.write",
        entity_type="workspace_file", entity_id=relative,
        forward_patch={
            "path": relative,
            "size": len(args.content.encode("utf-8")),
            "sha256": prepared.desired_sha256,
        },
        inverse_patch={"path": relative, "previous": previous, "delete": not prepared.before_exists},
        status="pending_delivery",
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    action = await enqueue_workspace_write(
        ctx.db,
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        invocation_id=ctx.invocation_id,
        operation_id=operation.id,
        action_key=ctx.action_key,
        request_digest=ctx.request_digest,
        prepared=prepared,
    )
    return {
        "path": relative,
        "size": len(args.content.encode("utf-8")),
        "operation_id": operation.id,
        "undo_available": True,
        "outbox_action_key": action.action_key,
        "_invocation_status": "pending_delivery",
    }


def _run_code(args: CodeExecuteArgs) -> dict:
    command = (
        ["/usr/bin/python3", "-I", "-c", args.code]
        if args.language == "python"
        else ["/bin/bash", "--noprofile", "--norc", "-c", args.code]
    )
    command = [
        "/usr/bin/prlimit",
        f"--cpu={args.timeout_seconds + 1}",
        f"--as={512 * 1024 * 1024}",
        f"--fsize={10 * 1024 * 1024}",
        "--nofile=64",
        "--",
        *command,
    ]
    env = minimal_subprocess_environment()
    try:
        completed = subprocess.run(
            command,
            cwd=WORKSPACE_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=min(args.timeout_seconds, settings.TOOL_EXECUTION_TIMEOUT_SECONDS),
            check=False,
        )
        limit = settings.TOOL_OUTPUT_LIMIT
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[:limit],
            "stderr": completed.stderr[:limit],
            "truncated": len(completed.stdout) > limit or len(completed.stderr) > limit,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        timeout_message = "Execution timed out"
        if stderr:
            stderr = f"{stderr}\n{timeout_message}"
        else:
            stderr = timeout_message
        limit = settings.TOOL_OUTPUT_LIMIT
        return {
            "exit_code": 124,
            "stdout": stdout[:limit],
            "stderr": stderr[:limit],
            "truncated": len(stdout) > limit or len(stderr) > limit,
            "timed_out": True,
        }


async def code_execute(ctx: ToolContext, args: CodeExecuteArgs) -> dict:
    if not current_code_execution_policy().available:
        rejection = code_execution_rejection()
        rejection.pop("ok", None)
        return rejection
    if not ctx.action_key or not ctx.request_digest or ctx.invocation_id is None:
        return {
            "error": "code_execute requires a durable invocation claim",
            "error_code": "missing_invocation_claim",
        }
    request_payload = args.model_dump(mode="json")
    action = await enqueue_subprocess(
        ctx.db,
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        invocation_id=ctx.invocation_id,
        action_key=ctx.action_key,
        request_digest=ctx.request_digest,
        arguments=request_payload,
    )
    return {
        # Registry commits this placeholder with the intent, then asks the
        # targeted dispatcher for the real receipt outside that transaction.
        "exit_code": -1,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "status": "pending_delivery",
        "outbox_action_key": action.action_key,
        "_dispatch_outbox_action_key": action.action_key,
        "_invocation_status": "pending_delivery",
    }


WORKSPACE_TOOLS = [
    ToolDefinition("file_list", "List files inside the Agent's isolated personal workspace.", FileListArgs, file_list, effect_kind=ToolEffectKind.EXTERNAL_READ, idempotent=True),
    ToolDefinition("file_read", "Read a UTF-8 text artifact from the isolated personal workspace.", FileReadArgs, file_read, effect_kind=ToolEffectKind.EXTERNAL_READ, idempotent=True),
    ToolDefinition("file_write", "Create or intentionally overwrite a text artifact from a durable outbox intent.", FileWriteArgs, file_write, effect_kind=ToolEffectKind.EXTERNAL_WRITE, idempotent=True),
    ToolDefinition("code_execute", "Queue bounded Python or Bash code for durable execution outside database transactions. The process is bounded but not a security sandbox.", CodeExecuteArgs, code_execute, effect_kind=ToolEffectKind.EXTERNAL_WRITE, idempotent=True),
]
