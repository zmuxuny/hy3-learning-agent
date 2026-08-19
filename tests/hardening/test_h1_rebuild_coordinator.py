from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.time import utc_now
from app.db.maintenance import MaintenanceError, verify_backup
from app.db.migrations import migrate_sqlite_database
from app.models import (
    EvidenceObservation,
    Owner,
    Plan,
    Stage,
    Task,
    TaskSubmission,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "rebuild-evidence.py"
MANAGED_DATABASE = Path("data/learning_companion.db")


@pytest.fixture(autouse=True)
def clean_database() -> None:
    """This module never uses the suite's process-global application DB."""


@pytest.fixture(scope="module")
def canonical_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("h1-rebuild-schema")
    database = root / "canonical.sqlite3"
    migrate_sqlite_database(
        database,
        backup_root=root / "migration-backups",
        application_version="pytest-h1-rebuild",
    )
    return database


@pytest.fixture
def rebuild_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "h1_rebuild_evidence_script",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state_root(tmp_path: Path, canonical_database: Path) -> tuple[Path, Path]:
    root = tmp_path / "repository"
    database = root / MANAGED_DATABASE
    database.parent.mkdir(parents=True)
    shutil.copyfile(canonical_database, database)
    os.chmod(database, 0o600)
    (root / "data/context/plans").mkdir(parents=True)
    (root / "data/context/decisions").mkdir(parents=True)
    (root / "data/workspace/course").mkdir(parents=True)
    (root / "data/context/plans/existing.json").write_bytes(
        b'{"fixture":"context-before"}\n'
    )
    (root / "data/workspace/course/notes.txt").write_bytes(b"workspace-before\n")
    return root, database


def _args(
    root: Path,
    *,
    plan_id: int | None = None,
    audit: bool = False,
    backfill_v1: bool = False,
    write: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_id=plan_id,
        audit=audit,
        backfill_v1=backfill_v1,
        write=write,
        state_root=root,
    )


def _configure_script(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: Path,
    sessions: async_sessionmaker,
) -> None:
    monkeypatch.setattr(
        module,
        "settings",
        SimpleNamespace(
            DATABASE_URL=f"sqlite+aiosqlite:///{database}",
            DEFAULT_OWNER_ID="local",
        ),
    )
    monkeypatch.setattr(module, "AsyncSessionLocal", sessions)

    async def current_schema_is_ready(
        *,
        state_lease_held: bool = False,
        state_root: Path | None = None,
    ) -> None:
        if not state_lease_held:
            raise RuntimeError("backfill schema preparation must reuse the coordinator lease")
        if state_root is None:
            raise RuntimeError("backfill schema preparation must use an explicit state root")
        return None

    monkeypatch.setattr(module, "create_schema", current_schema_is_ready)


def _tree_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    snapshot: dict[str, tuple[Any, ...]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result = path.lstat()
        mode = stat.S_IMODE(result.st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode)
        elif path.is_file():
            snapshot[relative] = ("file", mode, path.read_bytes())
        else:
            snapshot[relative] = ("special", mode)
    return snapshot


def _managed_source_files(root: Path) -> set[str]:
    files = {MANAGED_DATABASE.as_posix()}
    for tree in (root / "data/context", root / "data/workspace"):
        files.update(
            path.relative_to(root).as_posix()
            for path in tree.rglob("*")
            if path.is_file()
        )
    return files


def _backup_for(root: Path, purpose: str = "pre_evidence_rebuild") -> Path:
    candidates = sorted(
        path
        for path in (root / "data/backups").iterdir()
        if path.is_dir() and path.name.startswith(f"{purpose}-")
    )
    assert len(candidates) == 1
    return candidates[0]


def _row_counts(database: Path) -> tuple[int, int]:
    uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return (
            int(connection.execute("SELECT count(*) FROM evidence_observations").fetchone()[0]),
            int(connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]),
        )


async def _seed_owner_and_plan(
    sessions: async_sessionmaker,
    *,
    observation: bool = False,
    legacy_submission: bool = False,
) -> int:
    async with sessions() as db:
        owner = Owner(id="local", display_name="Fixture learner", timezone="Asia/Shanghai")
        plan = Plan(owner_id="local", title="Evidence rebuild fixture")
        db.add_all([owner, plan])
        await db.flush()
        stage = Stage(plan_id=plan.id, title="Fixture stage", position=0)
        db.add(stage)
        await db.flush()
        task = Task(stage_id=stage.id, title="Fixture task", position=0)
        db.add(task)
        await db.flush()
        if observation:
            db.add(
                EvidenceObservation(
                    owner_id="local",
                    source_type="manual",
                    source_id="fixture-observation",
                    plan_id=plan.id,
                    task_id=task.id,
                    outcome="passed",
                    normalized_score=0.9,
                    is_correct=True,
                    occurred_at=utc_now(),
                    idempotency_key="h1-rebuild:existing-observation",
                )
            )
        if legacy_submission:
            db.add(
                TaskSubmission(
                    owner_id="local",
                    plan_id=plan.id,
                    task_id=task.id,
                    submission_type="text",
                    content="A legacy answer with explicit evidence.",
                    artifacts=[],
                    status="accepted",
                    score=91,
                    feedback="accepted fixture",
                    checked_at=utc_now(),
                )
            )
        await db.commit()
        return plan.id


class _ObservedOS:
    """Delegate to os while observing only the script's JSON publication."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.publications: list[tuple[Path, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def replace(self, source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == self.destination:
            assert source_path.parent == destination_path.parent
            assert source_path.name.startswith(f".{destination_path.name}.")
            assert source_path.name.endswith(".tmp")
            assert not destination_path.exists()
            self.publications.append(
                (source_path, json.loads(source_path.read_text(encoding="utf-8")))
            )
        os.replace(source_path, destination_path)


@pytest.mark.asyncio
async def test_audit_and_projection_are_byte_for_byte_read_only(
    tmp_path: Path,
    canonical_database: Path,
    rebuild_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = _state_root(tmp_path, canonical_database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        plan_id = await _seed_owner_and_plan(sessions, observation=True)
        await engine.dispose()
        before_database = database.read_bytes()
        before_tree = _tree_snapshot(root)
        _configure_script(
            rebuild_module,
            monkeypatch,
            database=database,
            sessions=sessions,
        )

        assert await rebuild_module.run(
            _args(root, plan_id=plan_id, audit=True)
        ) == 0
        await engine.dispose()

        assert database.read_bytes() == before_database
        assert _tree_snapshot(root) == before_tree
        assert not (root / "data/backups").exists()
        assert not (root / "data/context/evidence").exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_write_verifies_full_state_backup_before_atomic_json_publish(
    tmp_path: Path,
    canonical_database: Path,
    rebuild_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = _state_root(tmp_path, canonical_database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        plan_id = await _seed_owner_and_plan(sessions, observation=True)
        await engine.dispose()
        target = root / f"data/context/evidence/plans/{plan_id}.json"
        expected_sources = _managed_source_files(root)
        _configure_script(
            rebuild_module,
            monkeypatch,
            database=database,
            sessions=sessions,
        )
        observed_os = _ObservedOS(target)
        monkeypatch.setattr(rebuild_module, "os", observed_os)

        assert await rebuild_module.run(
            _args(root, plan_id=plan_id, write=True)
        ) == 0
        await engine.dispose()

        backup = _backup_for(root)
        manifest = verify_backup(
            root,
            backup.name,
            expected_purpose="pre_evidence_rebuild",
        )
        assert {entry["source_path"] for entry in manifest["entries"]} == expected_sources
        assert target.relative_to(root).as_posix() not in {
            entry["source_path"] for entry in manifest["entries"]
        }
        assert not (backup / "payload" / target.relative_to(root)).exists()
        assert (backup / "payload/data/context/plans/existing.json").read_bytes() == (
            b'{"fixture":"context-before"}\n'
        )
        assert (backup / "payload/data/workspace/course/notes.txt").read_bytes() == (
            b"workspace-before\n"
        )
        assert _row_counts(backup / "payload" / MANAGED_DATABASE) == (1, 0)

        assert len(observed_os.publications) == 1
        temporary, observed_payload = observed_os.publications[0]
        assert not temporary.exists()
        assert target.is_file()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert json.loads(target.read_text(encoding="utf-8")) == observed_payload
        assert observed_payload["observation_count"] == 1
        assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_backup_precedes_legacy_evidence_and_artifact_writes(
    tmp_path: Path,
    canonical_database: Path,
    rebuild_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = _state_root(tmp_path, canonical_database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        plan_id = await _seed_owner_and_plan(sessions, legacy_submission=True)
        await engine.dispose()
        assert _row_counts(database) == (0, 0)
        _configure_script(
            rebuild_module,
            monkeypatch,
            database=database,
            sessions=sessions,
        )

        assert await rebuild_module.run(
            _args(root, plan_id=plan_id, backfill_v1=True)
        ) == 0
        await engine.dispose()

        backup = _backup_for(root)
        verify_backup(
            root,
            backup.name,
            expected_purpose="pre_evidence_rebuild",
        )
        assert _row_counts(backup / "payload" / MANAGED_DATABASE) == (0, 0)
        live_observations, live_artifacts = _row_counts(database)
        assert live_observations >= 1
        assert live_artifacts >= 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_commit_precedes_projection_filesystem_publish(
    tmp_path: Path,
    canonical_database: Path,
    rebuild_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = _state_root(tmp_path, canonical_database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        plan_id = await _seed_owner_and_plan(sessions, legacy_submission=True)
        await engine.dispose()
        target = root / f"data/context/evidence/plans/{plan_id}.json"
        _configure_script(
            rebuild_module,
            monkeypatch,
            database=database,
            sessions=sessions,
        )
        opened_sessions: list[Any] = []

        def tracked_session_factory() -> Any:
            session = sessions()
            opened_sessions.append(session)
            return session

        monkeypatch.setattr(rebuild_module, "AsyncSessionLocal", tracked_session_factory)
        original_write_json = rebuild_module._write_json
        counts_at_publish: list[tuple[int, int]] = []

        def write_only_from_committed_facts(path: Path, value: dict[str, Any]) -> None:
            # This hook runs before _write_json performs mkdir, write, fsync, or
            # os.replace.  An independent read must already see the backfill.
            assert len(opened_sessions) == 2
            assert all(not session.in_transaction() for session in opened_sessions)
            counts = _row_counts(database)
            counts_at_publish.append(counts)
            assert counts[0] >= 2
            assert counts[1] >= 1
            original_write_json(path, value)

        monkeypatch.setattr(rebuild_module, "_write_json", write_only_from_committed_facts)

        assert await rebuild_module.run(
            _args(root, plan_id=plan_id, backfill_v1=True, write=True)
        ) == 0
        await engine.dispose()

        assert counts_at_publish == [_row_counts(database)]
        projection = json.loads(target.read_text(encoding="utf-8"))
        assert projection["observation_count"] == counts_at_publish[0][0]
        assert projection["observation_count"] >= 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_commit_failure_cannot_publish_uncommitted_projection(
    tmp_path: Path,
    canonical_database: Path,
    rebuild_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database = _state_root(tmp_path, canonical_database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        plan_id = await _seed_owner_and_plan(sessions, legacy_submission=True)
        await engine.dispose()
        target = root / f"data/context/evidence/plans/{plan_id}.json"
        _configure_script(
            rebuild_module,
            monkeypatch,
            database=database,
            sessions=sessions,
        )

        async def fail_commit(db: Any) -> None:
            assert db.in_transaction()
            raise RuntimeError("injected backfill commit failure")

        def forbidden_publish(*_: object, **__: object) -> None:
            raise AssertionError("projection publication must follow a successful commit")

        monkeypatch.setattr(rebuild_module, "commit_uow", fail_commit)
        monkeypatch.setattr(rebuild_module, "_write_json", forbidden_publish)

        with pytest.raises(RuntimeError, match="injected backfill commit failure"):
            await rebuild_module.run(
                _args(root, plan_id=plan_id, backfill_v1=True, write=True)
            )
        await engine.dispose()

        assert _row_counts(database) == (0, 0)
        assert not target.exists()
        assert not (root / "data/context/evidence").exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ("inside-unmanaged", "outside"))
async def test_mutation_rejects_unmanaged_or_outside_database_without_side_effects(
    tmp_path: Path,
    rebuild_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "data/context/plans").mkdir(parents=True)
    (root / "data/context/plans/existing.json").write_bytes(b"unchanged\n")
    database = (
        root / "data/unmanaged.sqlite3"
        if target_kind == "inside-unmanaged"
        else tmp_path / "outside.sqlite3"
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")

    async def forbidden_schema_mutation(**_: object) -> None:
        raise AssertionError("scope guard must run before create_schema")

    class ForbiddenSessions:
        def __call__(self) -> None:
            raise AssertionError("scope guard must run before opening a session")

    monkeypatch.setattr(
        rebuild_module,
        "settings",
        SimpleNamespace(
            DATABASE_URL=f"sqlite+aiosqlite:///{database}",
            DEFAULT_OWNER_ID="local",
        ),
    )
    monkeypatch.setattr(rebuild_module, "create_schema", forbidden_schema_mutation)
    monkeypatch.setattr(rebuild_module, "AsyncSessionLocal", ForbiddenSessions())
    before_root = _tree_snapshot(root)
    before_database = database.read_bytes()

    with pytest.raises(MaintenanceError) as captured:
        await rebuild_module.run(
            _args(root, audit=True, backfill_v1=True, write=True)
        )

    assert captured.value.code == "unmanaged_mutation_target"
    assert _tree_snapshot(root) == before_root
    assert database.read_bytes() == before_database
    assert not (root / "data/backups").exists()
    assert not (root / "data/context/evidence").exists()
    assert not list(root.rglob(".incomplete-*"))
    assert not list(root.rglob(".operation-*"))
