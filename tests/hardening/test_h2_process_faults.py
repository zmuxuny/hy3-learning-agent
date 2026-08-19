"""Real-process H2 crash matrix over migrated temporary SQLite databases.

In-process ``BaseException`` kill points are useful for deterministic boundary
selection, but they still let Python unwind.  These tests start an isolated
worker and have the parent send ``SIGKILL`` after the worker publishes a marker.
Only temporary migrated databases and temporary filesystem targets are used.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.migrations import migrate_sqlite_database
from app.db.uow import commit as commit_uow
from app.models import AgentRun, Owner, UserProfile


OWNER_ID = "local"
RUN_ID = "h2-real-sigkill-run"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"


DATABASE_UOW_WORKER = r"""
import asyncio
import os
from pathlib import Path
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.uow import commit, flush
from app.models import Operation, RunEvent, ToolInvocation


async def main() -> None:
    database_path = Path(sys.argv[1])
    marker_path = Path(sys.argv[2])
    phase = sys.argv[3]
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        invocation = ToolInvocation(
            owner_id="local",
            run_id="h2-real-sigkill-run",
            idempotency_key=f"sigkill-uow:{phase}",
            tool_name="h2.sigkill",
            args_hash="a" * 64,
            request_digest="b" * 64,
            canonical_args={"phase": phase},
            effect_kind="database_write",
            status="committed",
            result_payload={"ok": True, "phase": phase},
        )
        db.add(invocation)
        await flush(db)
        db.add_all(
            [
                Operation(
                    owner_id="local",
                    run_id="h2-real-sigkill-run",
                    invocation_id=invocation.id,
                    tool_name="h2.sigkill",
                    entity_type="probe",
                    entity_id=phase,
                    forward_patch={"phase": phase},
                    inverse_patch={},
                ),
                RunEvent(
                    run_id="h2-real-sigkill-run",
                    sequence=1,
                    event_type="tool.completed",
                    summary=f"SIGKILL {phase}",
                    payload={"phase": phase},
                ),
            ]
        )
        await flush(db)
        if phase == "after_commit":
            await commit(db)
        marker_path.write_text("ready", encoding="utf-8")
        await asyncio.sleep(60)
    await engine.dispose()


asyncio.run(main())
"""


SMTP_ACCEPT_WORKER = r"""
import asyncio
import os
from pathlib import Path
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.outbox as outbox


async def main() -> None:
    database_path = Path(sys.argv[1])
    marker_path = Path(sys.argv[2])
    action_key = sys.argv[3]
    acceptance_ledger = Path(sys.argv[4])
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def accepted_transport(_action) -> None:
        with acceptance_ledger.open("ab") as stream:
            stream.write(b"accepted\n")
            stream.flush()
            os.fsync(stream.fileno())
        marker_path.write_text("accepted", encoding="utf-8")
        await asyncio.sleep(60)

    outbox._deliver_smtp = accepted_transport
    await outbox.dispatch_action(action_key=action_key, session_factory=factory)
    await engine.dispose()


asyncio.run(main())
"""


WORKSPACE_PUBLISH_WORKER = r"""
import asyncio
from pathlib import Path
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.outbox as outbox
import app.tools.workspace as workspace_tools


async def main() -> None:
    database_path = Path(sys.argv[1])
    marker_path = Path(sys.argv[2])
    action_key = sys.argv[3]
    workspace_root = Path(sys.argv[4])
    workspace_tools.WORKSPACE_ROOT = workspace_root
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    publish = outbox.publish_workspace_file

    async def publish_then_wait(
        target: Path,
        content: str,
        *,
        staging_token: str,
    ) -> None:
        await publish(target, content, staging_token=staging_token)
        marker_path.write_text("published", encoding="utf-8")
        await asyncio.sleep(60)

    outbox.publish_workspace_file = publish_then_wait
    await outbox.dispatch_action(action_key=action_key, session_factory=factory)
    await engine.dispose()


asyncio.run(main())
"""


async def _prepare_database(database_path: Path) -> None:
    report = await asyncio.to_thread(
        migrate_sqlite_database,
        database_path,
        backup_root=database_path.parent / "migration-backups",
        application_version="h2-process-fault-test",
        database_identity=database_path.name,
    )
    assert report.database_path == str(database_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(Owner(id=OWNER_ID, display_name="H2 process fault fixture"))
            db.add(UserProfile(owner_id=OWNER_ID))
            db.add(
                AgentRun(
                    id=RUN_ID,
                    owner_id=OWNER_ID,
                    trigger="user_message",
                    objective="real SIGKILL transaction probe",
                    status="running",
                )
            )
            await commit_uow(db)
    finally:
        await engine.dispose()


async def _enqueue_smtp_action(database_path: Path, *, action_key: str) -> None:
    from app.outbox import enqueue_action

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            await enqueue_action(
                db,
                owner_id=OWNER_ID,
                run_id=RUN_ID,
                action_key=action_key,
                request_digest="c" * 64,
                destination="smtp",
                payload={
                    "reply_token": "offline-reply-token",
                    "title": "offline SIGKILL acceptance",
                    "body": "synthetic transport; no network",
                },
            )
            await commit_uow(db)
    finally:
        await engine.dispose()


async def _enqueue_workspace_action(
    database_path: Path,
    *,
    action_key: str,
) -> None:
    from app.outbox import enqueue_workspace_write, prepare_workspace_write

    prepared = await prepare_workspace_write(
        path="artifact.txt",
        content="durable content after SIGKILL",
        overwrite=True,
    )
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            await enqueue_workspace_write(
                db,
                owner_id=OWNER_ID,
                run_id=RUN_ID,
                action_key=action_key,
                request_digest="d" * 64,
                prepared=prepared,
            )
            await commit_uow(db)
    finally:
        await engine.dispose()


async def _dispatch_or_reconcile(
    database_path: Path,
    operation,
    **kwargs,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        return await operation(session_factory=factory, **kwargs)
    finally:
        await engine.dispose()


def _worker_environment(database_path: Path) -> dict[str, str]:
    # Never inherit the parent's environment: it may contain API, SMTP, IMAP,
    # or push credentials.  Settings also load .env, so explicitly overriding
    # every credential field is required even with this minimal allowlist.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(BACKEND_ROOT),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
        # Every application-global engine imported by the worker is forced
        # onto this temporary file; no configured/user database can open.
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
        "OPENAI_API_KEY": "",
        "OPENAI_API_BASE": "http://127.0.0.1:9/disabled",
        "SMTP_HOST": "",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "SMTP_FROM": "",
        "SMTP_TO": "",
        "IMAP_HOST": "",
        "IMAP_USERNAME": "",
        "IMAP_PASSWORD": "",
        "VAPID_PUBLIC_KEY": "",
        "VAPID_PRIVATE_KEY": "",
        "VAPID_SUBJECT": "",
        "ENABLE_SCHEDULER": "false",
        "ENABLE_EMAIL_REPLY_POLLING": "false",
        "WEB_SEARCH_PROVIDER": "disabled",
        "WEB_SEARCH_FALLBACK_PROVIDER": "none",
    }


def _sigkill_at_marker(
    worker_source: str,
    *,
    database_path: Path,
    marker_path: Path,
    arguments: tuple[str, ...],
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", worker_source, str(database_path), str(marker_path), *arguments],
        cwd=PROJECT_ROOT,
        env=_worker_environment(database_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 8
    try:
        while not marker_path.exists():
            return_code = process.poll()
            if return_code is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(
                    "SIGKILL worker exited before its kill marker: "
                    f"code={return_code}; stdout_bytes={len(stdout.encode())}; "
                    f"stderr_bytes={len(stderr.encode())}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("SIGKILL worker did not reach its bounded marker")
            time.sleep(0.01)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _database_health_and_uow_counts(database_path: Path) -> tuple[str, int, tuple[int, int, int]]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        counts = tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("tool_invocations", "operations", "run_events")
        )
        return integrity, foreign_keys, counts
    finally:
        connection.close()


def _outbox_state(database_path: Path, *, action_key: str) -> tuple[str, int, int]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status, attempt FROM outbox_actions WHERE action_key = ?",
            (action_key,),
        ).fetchone()
        assert row is not None
        receipt_count = int(
            connection.execute(
                "SELECT count(*) FROM outbox_receipts WHERE action_key = ?",
                (action_key,),
            ).fetchone()[0]
        )
        return str(row[0]), int(row[1]), receipt_count
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_counts"),
    [
        ("before_commit", (0, 0, 0)),
        ("after_commit", (1, 1, 1)),
    ],
    ids=["kill-before-commit", "kill-after-commit"],
)
async def test_real_sigkill_exposes_only_complete_uow_or_no_uow(
    tmp_path: Path,
    phase: str,
    expected_counts: tuple[int, int, int],
) -> None:
    database_path = tmp_path / f"uow-{phase}.sqlite3"
    marker_path = tmp_path / f"uow-{phase}.ready"
    await _prepare_database(database_path)

    await asyncio.to_thread(
        _sigkill_at_marker,
        DATABASE_UOW_WORKER,
        database_path=database_path,
        marker_path=marker_path,
        arguments=(phase,),
    )

    integrity, foreign_key_violations, counts = await asyncio.to_thread(
        _database_health_and_uow_counts,
        database_path,
    )
    assert (integrity, foreign_key_violations, counts) == (
        "ok",
        0,
        expected_counts,
    )


@pytest.mark.asyncio
async def test_real_sigkill_after_smtp_acceptance_fences_exact_retry_without_resend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.notifications.service import NotificationService
    from app.outbox import dispatch_once, recover_interrupted_deliveries

    database_path = tmp_path / "smtp-acceptance.sqlite3"
    marker_path = tmp_path / "smtp-accepted.ready"
    acceptance_ledger = tmp_path / "smtp-acceptance.ledger"
    action_key = "sigkill:smtp:accepted"
    await _prepare_database(database_path)
    await _enqueue_smtp_action(database_path, action_key=action_key)

    await asyncio.to_thread(
        _sigkill_at_marker,
        SMTP_ACCEPT_WORKER,
        database_path=database_path,
        marker_path=marker_path,
        arguments=(action_key, str(acceptance_ledger)),
    )
    state_after_kill = await asyncio.to_thread(
        _outbox_state,
        database_path,
        action_key=action_key,
    )

    def reject_any_resend(*_args, **_kwargs) -> None:
        raise AssertionError("uncertain SMTP action was sent again")

    monkeypatch.setattr(NotificationService, "_send_email", reject_any_resend)
    recovery_result = await _dispatch_or_reconcile(
        database_path,
        recover_interrupted_deliveries,
    )
    state_after_recovery = await asyncio.to_thread(
        _outbox_state,
        database_path,
        action_key=action_key,
    )
    retry_result = await _dispatch_or_reconcile(database_path, dispatch_once)
    state_after_retry = await asyncio.to_thread(
        _outbox_state,
        database_path,
        action_key=action_key,
    )
    ledger_entries = acceptance_ledger.read_text(encoding="utf-8").splitlines()
    assert (
        state_after_kill == ("delivering", 1, 0)
        and recovery_result["fenced_external"] == 1
        and state_after_recovery == ("needs_reconciliation", 1, 0)
        and retry_result == {"status": "idle", "delivered": False}
        and state_after_retry == state_after_recovery
        and ledger_entries == ["accepted"]
    ), (
        f"after_kill={state_after_kill!r}; recovery={recovery_result!r}; "
        f"after_recovery={state_after_recovery!r}; retry={retry_result!r}; "
        f"after_retry={state_after_retry!r}; ledger_entries={len(ledger_entries)}"
    )


@pytest.mark.asyncio
async def test_real_sigkill_after_file_publish_reconciles_without_rewrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.outbox as outbox
    import app.tools.workspace as workspace_tools

    database_path = tmp_path / "workspace-publish.sqlite3"
    marker_path = tmp_path / "workspace-published.ready"
    workspace_root = tmp_path / "workspace-root"
    workspace_root.mkdir()
    action_key = "sigkill:workspace:published"
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace_root)
    await _prepare_database(database_path)
    await _enqueue_workspace_action(database_path, action_key=action_key)

    await asyncio.to_thread(
        _sigkill_at_marker,
        WORKSPACE_PUBLISH_WORKER,
        database_path=database_path,
        marker_path=marker_path,
        arguments=(action_key, str(workspace_root)),
    )
    target = workspace_root / "artifact.txt"
    content_after_kill = target.read_text(encoding="utf-8")
    state_after_kill = await asyncio.to_thread(
        _outbox_state,
        database_path,
        action_key=action_key,
    )

    async def reject_any_rewrite(
        _target: Path,
        _content: str,
        **_kwargs,
    ) -> None:
        raise AssertionError("reconciliation repeated a published filesystem write")

    monkeypatch.setattr(outbox, "publish_workspace_file", reject_any_rewrite)
    recovery_result = await _dispatch_or_reconcile(
        database_path,
        outbox.recover_interrupted_deliveries,
    )
    state_after_recovery = await asyncio.to_thread(
        _outbox_state,
        database_path,
        action_key=action_key,
    )
    reconcile_result = await _dispatch_or_reconcile(
        database_path,
        outbox.reconcile_action,
        action_key=action_key,
    )
    state_after_reconcile = await asyncio.to_thread(
        _outbox_state,
        database_path,
        action_key=action_key,
    )
    final_content = target.read_text(encoding="utf-8")
    assert (
        content_after_kill == "durable content after SIGKILL"
        and state_after_kill == ("delivering", 1, 0)
        and recovery_result["fenced_external"] == 0
        and recovery_result["workspace"]["reconciled"] == 1
        and state_after_recovery == ("delivered", 1, 1)
        and reconcile_result["status"] == "delivered"
        and reconcile_result["reconciled"] is True
        and reconcile_result["replayed"] is True
        and state_after_reconcile == ("delivered", 1, 1)
        and final_content == content_after_kill
    ), (
        f"content_after_kill={content_after_kill!r}; "
        f"state_after_kill={state_after_kill!r}; recovery={recovery_result!r}; "
        f"state_after_recovery={state_after_recovery!r}; reconcile={reconcile_result!r}; "
        f"state_after_reconcile={state_after_reconcile!r}; final={final_content!r}"
    )
