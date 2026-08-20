"""H3 real-process interruption, two-process lease and SQLite lock tests."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.migrations import migrate_sqlite_database
from app.db.uow import DatabaseBusyError
from app.models import AgentRun, Owner
from app.runtime.checkpoints import make_checkpoint
from app.runtime.state import (
    claim_run,
    finalize_run,
    prepare_finalization,
    reconcile_run_after_restart,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


KILL_PROGRAM = r"""
import asyncio
import os
import signal
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.runtime.checkpoints import make_checkpoint
from app.runtime.state import claim_run, persist_checkpoint, prepare_finalization

async def main():
    database, run_id, phase = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    lease = await claim_run(factory, run_id, worker_id=f"kill-{phase}")
    assert lease is not None
    if phase == "after_claim":
        os.kill(os.getpid(), signal.SIGKILL)
    checkpoint = make_checkpoint(
        kind="agent",
        phase="awaiting_model",
        step=1,
        messages=[{"role": "user", "content": "durable before kill"}],
    )
    async with factory() as db:
        await persist_checkpoint(db, lease, checkpoint, phase="awaiting_model")
    if phase == "after_checkpoint":
        os.kill(os.getpid(), signal.SIGKILL)
    await prepare_finalization(
        factory,
        lease,
        checkpoint=checkpoint,
        final_text="durable final text",
    )
    os.kill(os.getpid(), signal.SIGKILL)

asyncio.run(main())
"""


CLAIM_PROGRAM = r"""
import asyncio
import sys
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.runtime.state import claim_run

async def main():
    database, run_id, marker = sys.argv[1:]
    while not Path(marker).exists():
        time.sleep(0.005)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", connect_args={"timeout": 0.1})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    lease = await claim_run(factory, run_id, worker_id=f"process-{os.getpid()}")
    print("1" if lease is not None else "0", flush=True)
    await engine.dispose()

import os
asyncio.run(main())
"""


def _factory(path: Path, *, timeout: float = 5.0):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"timeout": timeout},
    )
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_run(path: Path, *, checkpoint: dict | None = None) -> tuple[object, async_sessionmaker, str]:
    migrate_sqlite_database(path, backup_root=path.parent / "migration-backups")
    engine, factory = _factory(path)
    async with factory() as db:
        db.add(Owner(id="local", display_name="H3 process fixture", timezone="UTC"))
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="process fault fixture",
            checkpoint_schema_version=1 if checkpoint is not None else None,
            checkpoint=checkpoint,
        )
        db.add(run)
        await db.commit()
        return engine, factory, run.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "recoverable", "expected_status", "expected_phase"),
    (
        ("after_claim", False, "needs_reconciliation", "reconciling"),
        ("after_checkpoint", True, "queued", "awaiting_model"),
        ("after_finalizing", True, "queued", "finalizing"),
    ),
)
async def test_sigkill_boundaries_leave_only_classifiable_state(
    tmp_path: Path,
    phase: str,
    recoverable: bool,
    expected_status: str,
    expected_phase: str,
) -> None:
    database = tmp_path / f"{phase}.sqlite3"
    engine, factory, run_id = await _seed_run(database)
    await engine.dispose()
    environment = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
    }
    process = subprocess.run(
        [sys.executable, "-c", KILL_PROGRAM, str(database), run_id, phase],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == -signal.SIGKILL
    assert process.stdout == "" and process.stderr == ""

    engine, factory = _factory(database)
    assert await reconcile_run_after_restart(factory, run_id, scope_valid=True) is recoverable
    async with factory() as db:
        run = await db.get(AgentRun, run_id)
    await engine.dispose()
    assert (run.status, run.phase) == (expected_status, expected_phase)
    if recoverable:
        assert run.checkpoint and run.checkpoint["messages"][0]["content"] == "durable before kill"
    else:
        assert run.status_reason == "missing_running_checkpoint"


@pytest.mark.asyncio
async def test_three_consecutive_sigkills_preserve_progress_then_finalize(tmp_path: Path) -> None:
    checkpoint = make_checkpoint(
        kind="agent",
        phase="awaiting_model",
        step=0,
        messages=[{"role": "user", "content": "never lose me"}],
    )
    database = tmp_path / "three-consecutive-sigkills.sqlite3"
    engine, factory, run_id = await _seed_run(database, checkpoint=checkpoint)
    await engine.dispose()
    environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "backend")}

    for interruption in range(3):
        process = subprocess.run(
            [sys.executable, "-c", KILL_PROGRAM, str(database), run_id, "after_checkpoint"],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert process.returncode == -signal.SIGKILL
        assert process.stdout == "" and process.stderr == ""
        engine, factory = _factory(database)
        assert await reconcile_run_after_restart(
            factory,
            run_id,
            scope_valid=True,
        ) is True
        async with factory() as db:
            recovered = await db.get(AgentRun, run_id)
            assert recovered is not None
            assert recovered.status == "queued" and recovered.checkpoint is not None
        await engine.dispose()

    engine, factory = _factory(database)
    lease = await claim_run(factory, run_id, worker_id="final-worker")
    assert lease is not None
    prepared = await prepare_finalization(
        factory,
        lease,
        checkpoint=lease.checkpoint,
        final_text="completed after three restarts",
    )
    assert prepared.action == "finalize"
    completed = await finalize_run(factory, lease)
    await engine.dispose()
    assert completed.status == "completed"
    assert completed.output == "completed after three restarts"


@pytest.mark.asyncio
async def test_two_processes_share_one_sqlite_claim(tmp_path: Path) -> None:
    database = tmp_path / "two-process.sqlite3"
    engine, _, run_id = await _seed_run(database, checkpoint=make_checkpoint(
        kind="agent",
        phase="awaiting_model",
        step=0,
        messages=[{"role": "user", "content": "claim once"}],
    ))
    await engine.dispose()
    marker = tmp_path / "start.marker"
    environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "backend")}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", CLAIM_PROGRAM, str(database), run_id, str(marker)],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    marker.write_text("start\n", encoding="ascii")
    results = [process.communicate(timeout=15) for process in processes]
    assert [process.returncode for process in processes] == [0, 0]
    assert sorted(stdout.strip() for stdout, _ in results) == ["0", "1"]
    assert [stderr for _, stderr in results] == ["", ""]


@pytest.mark.asyncio
async def test_claim_reports_bounded_sqlite_busy_then_recovers(tmp_path: Path) -> None:
    database = tmp_path / "locked-claim.sqlite3"
    engine, factory, run_id = await _seed_run(database)
    await engine.dispose()
    lock = sqlite3.connect(database, timeout=0, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        fast_engine, fast_factory = _factory(database, timeout=0)
        with pytest.raises(DatabaseBusyError) as error:
            await claim_run(fast_factory, run_id, worker_id="locked")
        assert error.value.retryable is True and error.value.attempts == 5
        await fast_engine.dispose()
    finally:
        lock.rollback()
        lock.close()

    engine, factory = _factory(database)
    lease = await claim_run(factory, run_id, worker_id="after-lock")
    await engine.dispose()
    assert lease is not None
