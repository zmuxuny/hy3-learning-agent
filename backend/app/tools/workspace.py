from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import PROJECT_ROOT, settings
from app.models import Operation
from app.tools.base import ToolContext, ToolDefinition


WORKSPACE_ROOT = (PROJECT_ROOT / "data" / "workspace").resolve()


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
    return {"workspace": str(WORKSPACE_ROOT), "entries": entries, "truncated": len(entries) >= args.limit}


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
    return {
        "path": str(path.relative_to(WORKSPACE_ROOT)),
        "content": content[: args.max_chars],
        "truncated": len(content) > args.max_chars,
        "size": len(raw),
    }


async def file_write(ctx: ToolContext, args: FileWriteArgs) -> dict:
    path = _resolve(args.path)
    if path.exists() and not args.overwrite:
        return {"error": "File already exists; set overwrite=true to replace it"}
    existed = path.exists()
    previous = path.read_text(encoding="utf-8") if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, args.content, "utf-8")
    relative = str(path.relative_to(WORKSPACE_ROOT))
    operation = Operation(
        owner_id=ctx.owner_id, run_id=ctx.run_id, tool_name="file.write",
        entity_type="workspace_file", entity_id=relative,
        forward_patch={"path": relative, "size": path.stat().st_size},
        inverse_patch={"path": relative, "previous": previous, "delete": not existed},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {"path": relative, "size": path.stat().st_size, "operation_id": operation.id, "undo_available": True}


def _run_code(args: CodeExecuteArgs) -> dict:
    command = ["python", "-I", "-c", args.code] if args.language == "python" else ["bash", "--noprofile", "--norc", "-c", args.code]
    command = [
        "/usr/bin/prlimit",
        f"--cpu={args.timeout_seconds + 1}",
        f"--as={512 * 1024 * 1024}",
        f"--fsize={10 * 1024 * 1024}",
        "--nofile=64",
        "--",
        *command,
    ]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
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
        return {
            "error": "Execution timed out",
            "stdout": (exc.stdout or "")[: settings.TOOL_OUTPUT_LIMIT],
            "stderr": (exc.stderr or "")[: settings.TOOL_OUTPUT_LIMIT],
        }


async def code_execute(_: ToolContext, args: CodeExecuteArgs) -> dict:
    return await asyncio.to_thread(_run_code, args)


WORKSPACE_TOOLS = [
    ToolDefinition("file_list", "List files inside the Agent's isolated personal workspace.", FileListArgs, file_list),
    ToolDefinition("file_read", "Read a UTF-8 text artifact from the isolated personal workspace.", FileReadArgs, file_read),
    ToolDefinition("file_write", "Create or intentionally overwrite a text artifact in the isolated personal workspace.", FileWriteArgs, file_write),
    ToolDefinition("code_execute", "Run short Python or Bash code from the personal Agent workspace with strict time and output limits. The process is bounded but not a security sandbox.", CodeExecuteArgs, code_execute),
]
