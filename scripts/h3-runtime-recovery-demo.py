#!/usr/bin/env python3
"""Run the H3 two-interruption recovery acceptance against the real Hy3 provider.

The demo always creates a temporary database and runtime root.  It never
prints credentials, provider responses, or user data.  The explicit
``--execute-real`` flag prevents an accidental paid request.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _configure_temp_runtime(root: Path) -> None:
    import app.context.assembler as assembler_module
    import app.core.config as config_module

    config_module.PROJECT_ROOT = root
    assembler_module.PROJECT_ROOT = root
    config_module.prepare_runtime_directories(root)


async def _worker(run_id: str, pause: str, marker: Path, runtime_root: Path) -> None:
    _configure_temp_runtime(runtime_root)
    import app.tools as tool_package
    import app.runtime.agent as agent_module
    from app.runtime.agent import AgentRuntime

    # The acceptance exercises real Hy3 inference through AgentRuntime while
    # preventing unrelated web/mail/tool side effects in this synthetic run.
    tool_package.openai_tools = lambda: []

    async def skip_post_terminal_enrichment(*_args, **_kwargs):
        return None

    # Title/memory enrichment is outside the H3 terminal truth and would add
    # unrelated paid provider calls to this focused recovery acceptance.
    agent_module.generate_session_title = skip_post_terminal_enrichment
    agent_module.MemoryManager.compress_session = skip_post_terminal_enrichment

    class PausingRuntime(AgentRuntime):
        async def _call_model(self, *args, **kwargs):
            if pause == "before_model":
                marker.write_text("checkpointed\n", encoding="ascii")
                await asyncio.Event().wait()
            result = await super()._call_model(*args, **kwargs)
            if pause == "after_model":
                marker.write_text("provider-returned\n", encoding="ascii")
                await asyncio.Event().wait()
            return result

    await PausingRuntime().run(run_id)


async def _seed() -> str:
    from app.db.database import AsyncSessionLocal, engine
    from app.db.uow import commit as commit_uow, flush as flush_uow
    from app.models import AgentRun, ChatMessage, Owner, Session, UserProfile

    async with AsyncSessionLocal() as db:
        db.add(Owner(id="local", display_name="H3 recovery demo", timezone="UTC"))
        db.add(UserProfile(owner_id="local"))
        session = Session(owner_id="local", title="H3 real recovery demo")
        db.add(session)
        await flush_uow(db)
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective=(
                "这是临时数据库中的 H3 恢复验收。不要调用工具；"
                "请只回复一句简短的中文确认。"
            ),
        )
        db.add(run)
        await flush_uow(db)
        db.add(ChatMessage(
            session_id=session.id,
            run_id=run.id,
            message_key=f"run:{run.id}:input",
            role="user",
            content=run.objective,
        ))
        await commit_uow(db)
        run_id = run.id
    await engine.dispose()
    return run_id


async def _reconcile(run_id: str) -> None:
    from app.db.database import AsyncSessionLocal, engine
    from app.runtime.state import reconcile_run_after_restart

    if not await reconcile_run_after_restart(AsyncSessionLocal, run_id, scope_valid=True):
        raise RuntimeError("interrupted_run_not_recoverable")
    await engine.dispose()


async def _inspect(run_id: str) -> dict[str, object]:
    from sqlalchemy import select

    from app.db.database import AsyncSessionLocal, engine
    from app.models import AgentRun, ChatMessage, RunEvent

    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        messages = list((await db.execute(select(ChatMessage).where(
            ChatMessage.run_id == run_id,
            ChatMessage.role == "assistant",
        ))).scalars())
        completed_events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "run.completed",
        ))).scalars())
    await engine.dispose()
    if run is None:
        raise RuntimeError("run_disappeared")
    return {
        "status": run.status,
        "attempts": run.attempt,
        "checkpoint_cleared": run.checkpoint is None,
        "final_messages": len(messages),
        "completed_events": len(completed_events),
        "output_sha256": hashlib.sha256((run.output or "").encode()).hexdigest(),
        "has_output": bool(run.output),
    }


def _launch_worker(
    *,
    run_id: str,
    pause: str,
    marker: Path,
    runtime_root: Path,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            run_id,
            pause,
            str(marker),
            str(runtime_root),
        ],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _kill_at_marker(process: subprocess.Popen, marker: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if process.poll() is not None:
            raise RuntimeError("worker_exited_before_killpoint")
        if time.monotonic() >= deadline:
            process.kill()
            await asyncio.to_thread(process.wait)
            raise RuntimeError("killpoint_timeout")
        await asyncio.sleep(0.05)
    process.kill()
    return_code = await asyncio.to_thread(process.wait)
    if return_code != -signal.SIGKILL:
        raise RuntimeError("worker_was_not_sigkilled")


async def _orchestrate(runtime_root: Path, timeout: float) -> dict[str, object]:
    from app.core.config import settings
    from app.db.migrations import migrate_sqlite_database

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("provider_not_configured")
    if settings.MODEL_NAME.strip().lower() != "hy3":
        raise RuntimeError("configured_model_is_not_hy3")
    database = runtime_root / "demo.sqlite3"
    migrate_sqlite_database(
        database,
        backup_root=runtime_root / "migration-backups",
        application_version="h3-real-recovery-demo",
        database_identity=database.name,
    )
    run_id = await _seed()
    for index, pause in enumerate(("before_model", "after_model"), start=1):
        marker = runtime_root / f"killpoint-{index}.marker"
        process = _launch_worker(
            run_id=run_id,
            pause=pause,
            marker=marker,
            runtime_root=runtime_root,
        )
        await _kill_at_marker(process, marker, timeout)
        await _reconcile(run_id)

    final_process = _launch_worker(
        run_id=run_id,
        pause="none",
        marker=runtime_root / "unused.marker",
        runtime_root=runtime_root,
    )
    try:
        return_code = await asyncio.to_thread(final_process.wait, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        final_process.kill()
        await asyncio.to_thread(final_process.wait)
        raise RuntimeError("final_recovery_timeout") from exc
    if return_code != 0:
        raise RuntimeError("final_recovery_worker_failed")
    result = await _inspect(run_id)
    expected = {
        "status": "completed",
        "attempts": 3,
        "checkpoint_cleared": True,
        "final_messages": 1,
        "completed_events": 1,
        "has_output": True,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise RuntimeError(f"acceptance_mismatch:{key}")
    return {"ok": True, "model": "hy3", "interruptions": 2, **result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-real", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--worker", nargs=4, metavar=("RUN_ID", "PAUSE", "MARKER", "ROOT"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        run_id, pause, marker, runtime_root = args.worker
        asyncio.run(_worker(run_id, pause, Path(marker), Path(runtime_root)))
        return 0
    if not args.execute_real:
        print(json.dumps({"ok": False, "error": "pass --execute-real"}, sort_keys=True))
        return 2
    with tempfile.TemporaryDirectory(prefix="learning-agent-h3-real-") as temp:
        runtime_root = Path(temp)
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{runtime_root / 'demo.sqlite3'}"
        _configure_temp_runtime(runtime_root)
        try:
            result = asyncio.run(_orchestrate(runtime_root, max(10.0, args.timeout)))
        except Exception as exc:
            safe_error = (
                str(exc)
                if isinstance(exc, RuntimeError)
                and str(exc).split(":", 1)[0] in {
                    "provider_not_configured",
                    "configured_model_is_not_hy3",
                    "interrupted_run_not_recoverable",
                    "worker_exited_before_killpoint",
                    "killpoint_timeout",
                    "worker_was_not_sigkilled",
                    "final_recovery_timeout",
                    "final_recovery_worker_failed",
                    "acceptance_mismatch",
                }
                else type(exc).__name__
            )
            print(json.dumps({"ok": False, "error": safe_error}, sort_keys=True))
            return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
