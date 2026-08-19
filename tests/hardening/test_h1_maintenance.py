from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import app.db.maintenance as maintenance_module
from app.db.maintenance import (
    MaintenanceError,
    backup_state,
    coordinated_state_mutation,
    preflight_state,
    recover_state,
    reset_state,
    restore_state,
    runtime_state_lease,
    verify_backup,
)
from app.version import CURRENT_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATHS = (
    "data/learning_companion.db",
    "learning_companion.db",
    "backend/learning_companion.db",
    "backend/data/learning_companion.db",
)
MANAGED_TREES = ("data/context", "data/workspace")


@pytest.fixture(autouse=True)
def clean_database():
    """These contracts use only repository-shaped roots under ``tmp_path``."""
    yield


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic-repository"
    root.mkdir()
    return root


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_database(
    path: Path,
    marker: str,
    *,
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={schema_version}")
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES (?, '2026-08-19T00:00:00.000000Z')",
            (schema_version,),
        )
        connection.execute(
            "CREATE TABLE fixture_marker "
            "(position INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE fixture_parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE fixture_child ("
            "id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL, "
            "FOREIGN KEY(parent_id) REFERENCES fixture_parent(id))"
        )
        connection.execute("INSERT INTO fixture_parent(id) VALUES (1)")
        connection.executemany(
            "INSERT INTO fixture_marker(position, value) VALUES (?, ?)",
            ((1, marker), (2, f"{marker}:second")),
        )


def _clear_managed_state(root: Path) -> None:
    for relative in DATABASE_PATHS:
        database = root / relative
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = database.with_name(database.name + suffix)
            if os.path.lexists(path):
                path.unlink()
    for relative in MANAGED_TREES:
        tree = root / relative
        if os.path.lexists(tree):
            if tree.is_dir() and not tree.is_symlink():
                shutil.rmtree(tree)
            else:
                tree.unlink()


def _populate_state(
    root: Path,
    marker: str,
    *,
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> None:
    _clear_managed_state(root)
    for index, relative in enumerate(DATABASE_PATHS):
        _write_database(
            root / relative,
            f"{marker}:database:{index}",
            schema_version=schema_version,
        )

    files = {
        "data/context/plans/active.json": f'{{"marker":"{marker}:plan"}}\n',
        "data/context/decisions/accepted.md": f"{marker}:decision\n",
        "data/workspace/course/notes.txt": f"{marker}:workspace\n",
    }
    for relative, value in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    (root / "data/context/empty/nested").mkdir(parents=True)
    (root / "data/workspace/empty").mkdir(parents=True)


def _read_database(path: Path) -> dict[str, Any]:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return {
            "integrity": connection.execute("PRAGMA integrity_check").fetchall(),
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema": connection.execute(
                "SELECT type, name, coalesce(sql, '') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall(),
            "markers": connection.execute(
                "SELECT position, value FROM fixture_marker ORDER BY position"
            ).fetchall(),
            "migrations": connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall(),
        }


def _managed_state_snapshot(root: Path) -> dict[str, Any]:
    databases: dict[str, Any] = {}
    for relative in DATABASE_PATHS:
        path = root / relative
        if path.is_file() and not path.is_symlink():
            databases[relative] = _read_database(path)

    files: dict[str, bytes] = {}
    directories: set[str] = set()
    for relative in MANAGED_TREES:
        tree = root / relative
        if not tree.exists():
            continue
        assert tree.is_dir() and not tree.is_symlink()
        directories.add(relative)
        for path in sorted(tree.rglob("*")):
            item = path.relative_to(root).as_posix()
            assert not path.is_symlink()
            if path.is_dir():
                directories.add(item)
            elif path.is_file():
                files[item] = path.read_bytes()
            else:
                raise AssertionError(f"unexpected managed state object: {item}")
    return {
        "databases": databases,
        "files": files,
        "directories": sorted(directories),
    }


def _backup_payload_snapshot(
    backup_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    databases: dict[str, Any] = {}
    files: dict[str, bytes] = {}
    for entry in manifest["entries"]:
        source_path = entry["source_path"]
        payload_path = backup_path / entry["archive_path"]
        if entry["kind"] == "sqlite":
            databases[source_path] = _read_database(payload_path)
        else:
            files[source_path] = payload_path.read_bytes()
    return {
        "databases": databases,
        "files": files,
        "directories": sorted(manifest["directories"]),
    }


def _physical_file_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    result = path.stat()
    value = path.read_bytes()
    return {
        "inode": result.st_ino,
        "size": result.st_size,
        "mode": result.st_mode,
        "mtime_ns": result.st_mtime_ns,
        "sha256": _sha256_bytes(value),
    }


def _database_family_snapshot(database: Path) -> dict[str, Any]:
    return {
        suffix or "database": _physical_file_snapshot(
            database.with_name(database.name + suffix)
        )
        for suffix in ("", "-wal", "-shm", "-journal")
    }


def _persistent_tree_snapshot(root: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    paths = [root, *sorted(root.rglob("*"))]
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        result = path.lstat()
        common = {
            "mode": result.st_mode,
            "mtime_ns": result.st_mtime_ns,
        }
        if path.is_symlink():
            snapshot[relative] = {**common, "kind": "symlink", "target": os.readlink(path)}
        elif path.is_dir():
            snapshot[relative] = {**common, "kind": "directory"}
        elif path.is_file():
            snapshot[relative] = {
                **common,
                "kind": "file",
                "size": result.st_size,
                "sha256": _sha256_bytes(path.read_bytes()),
            }
        else:
            snapshot[relative] = {**common, "kind": "special"}
    return snapshot


def _backup_path(root: Path, backup_id: str) -> Path:
    return root / "data" / "backups" / backup_id


def _rewrite_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest_bytes = _canonical_json(manifest)
    (path / "manifest.json").write_bytes(manifest_bytes)
    (path / "manifest.sha256").write_text(
        _sha256_bytes(manifest_bytes) + "\n",
        encoding="ascii",
    )


def _refresh_manifest_payload_digest(manifest: dict[str, Any]) -> None:
    tokens = [
        [entry["archive_path"], entry["size_bytes"], entry["sha256"]]
        for entry in sorted(manifest["entries"], key=lambda item: item["archive_path"])
    ]
    manifest["payload_sha256"] = _sha256_bytes(_canonical_json(tokens))


def _clone_backup(root: Path, source: Path, name: str) -> Path:
    source_manifest = json.loads((source / "manifest.json").read_bytes())
    purpose = source_manifest["purpose"]
    identity = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    backup_id = f"{purpose}-20990101T000000.000000Z-{identity}"
    destination = root / "data" / "backups" / backup_id
    shutil.copytree(source, destination)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    manifest["backup_id"] = backup_id
    manifest["created_at"] = "2099-01-01T00:00:00.000000Z"
    _rewrite_manifest(destination, manifest)
    verify_backup(root, destination)
    return destination


def _assert_error_code(expected: str, operation) -> MaintenanceError:
    with pytest.raises(MaintenanceError) as caught:
        operation()
    assert caught.value.code == expected
    return caught.value


def test_backup_manifest_preserves_complete_relative_database_identity(
    state_root: Path,
) -> None:
    _populate_state(state_root, "source")

    report = backup_state(state_root, "manual")
    backup_path = _backup_path(state_root, report["backup_id"])
    manifest = verify_backup(state_root, report["backup_id"], expected_purpose="manual")

    source_paths = {entry["source_path"] for entry in manifest["entries"]}
    archive_paths = {entry["archive_path"] for entry in manifest["entries"]}
    expected_sources = {
        *DATABASE_PATHS,
        "data/context/plans/active.json",
        "data/context/decisions/accepted.md",
        "data/workspace/course/notes.txt",
    }
    assert report["code"] == "backup_created"
    assert manifest["complete"] is True
    assert source_paths == expected_sources
    assert archive_paths == {f"payload/{path}" for path in expected_sources}
    sqlite_archive_paths = {
        entry["archive_path"]
        for entry in manifest["entries"]
        if entry["kind"] == "sqlite"
    }
    assert len(sqlite_archive_paths) == 4
    assert all(
        not PurePosixPath(path).is_absolute()
        and ".." not in PurePosixPath(path).parts
        and "\\" not in path
        for path in source_paths | archive_paths | set(manifest["directories"])
    )
    actual_payload_files = {
        path.relative_to(backup_path).as_posix()
        for path in (backup_path / "payload").rglob("*")
        if path.is_file()
    }
    assert actual_payload_files == archive_paths
    for index, relative in enumerate(DATABASE_PATHS):
        entry = next(item for item in manifest["entries"] if item["source_path"] == relative)
        assert entry["archive_path"] == f"payload/{relative}"
        assert _read_database(backup_path / entry["archive_path"])["markers"][0][1] == (
            f"source:database:{index}"
        )


def test_backup_never_reads_or_archives_repository_env(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_database(state_root / DATABASE_PATHS[0], "public-state")
    env_path = state_root / ".env"
    env_path.write_text("H1_PRIVATE_SENTINEL=must-not-be-read\n", encoding="utf-8")
    env_path.chmod(0)
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == env_path:
            raise AssertionError("maintenance attempted to open the repository .env")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    report = backup_state(state_root, "manual")
    backup_path = _backup_path(state_root, report["backup_id"])
    manifest = verify_backup(state_root, backup_path)

    asserted_paths = {
        entry["source_path"] for entry in manifest["entries"]
    } | {entry["archive_path"] for entry in manifest["entries"]}
    assert ".env" not in asserted_paths
    assert ".env" not in manifest["exclusions"]["paths"]
    assert all(path.name != ".env" for path in (backup_path / "payload").rglob("*"))
    assert env_path.exists()
    assert env_path.stat().st_mode & 0o777 == 0


def test_secret_named_managed_directory_is_excluded_without_being_entered(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate_state(state_root, "secret-directory")
    secret = state_root / "data/context/.env/token"
    secret.parent.mkdir()
    secret.write_bytes(b"synthetic-test-secret")
    before = _managed_state_snapshot(state_root)
    original_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path) == secret.parent:
            raise AssertionError("maintenance entered an excluded secret directory")
        return original_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", guarded_scandir)
        source = backup_state(state_root, "secret_scan")
        source_path = _backup_path(state_root, source["backup_id"])
        manifest = verify_backup(state_root, source_path)
        assert manifest["exclusions"]["paths"] == ["data/context/.env"]
        assert not (source_path / "payload/data/context/.env").exists()
        assert all(
            ".env" not in path.relative_to(source_path).parts
            for path in (source_path / "payload").rglob("*")
        )

        reset_phases: list[str] = []
        restore_phases: list[str] = []
        _assert_error_code(
            "secret_state_excluded",
            lambda: reset_state(
                state_root,
                "secret_reset",
                phase_hook=reset_phases.append,
            ),
        )
        _assert_error_code(
            "secret_state_excluded",
            lambda: restore_state(
                state_root,
                backup=source["backup_id"],
                expected_purpose="secret_scan",
                phase_hook=restore_phases.append,
            ),
        )
        assert reset_phases == []
        assert restore_phases == []

    assert _managed_state_snapshot(state_root) == before
    assert not any(
        path.name.startswith(".incomplete-")
        for path in (state_root / "data/backups").iterdir()
    )
    operations_root = state_root / "data/.maintenance/operations"
    assert not operations_root.exists() or not any(operations_root.iterdir())
    assert secret.read_bytes() == b"synthetic-test-secret"


def test_backup_rejects_managed_source_symlink(state_root: Path) -> None:
    outside = state_root.parent / "outside-private-state"
    outside.write_bytes(b"must-not-be-copied")
    managed_tree = state_root / "data" / "context"
    managed_tree.mkdir(parents=True)
    (managed_tree / "escape").symlink_to(outside)
    before = _sha256_bytes(outside.read_bytes())

    _assert_error_code(
        "unsafe_state_path",
        lambda: backup_state(state_root, "manual"),
    )

    backups = state_root / "data" / "backups"
    assert not backups.exists() or not any(backups.iterdir())
    assert _sha256_bytes(outside.read_bytes()) == before


def test_verify_backup_rejects_payload_symlink_without_touching_live_state(
    state_root: Path,
) -> None:
    _populate_state(state_root, "live")
    report = backup_state(state_root, "manual")
    source = _backup_path(state_root, report["backup_id"])
    mutant = _clone_backup(state_root, source, "payload-symlink-mutant")
    payload_file = mutant / "payload/data/context/plans/active.json"
    outside = state_root.parent / "outside-payload"
    outside.write_bytes(b"outside")
    payload_file.unlink()
    payload_file.symlink_to(outside)
    live_before = _managed_state_snapshot(state_root)

    _assert_error_code("invalid_manifest", lambda: verify_backup(state_root, mutant))

    assert _managed_state_snapshot(state_root) == live_before
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("metadata_name", ("manifest.json", "manifest.sha256"))
def test_verify_backup_rejects_metadata_symlink(
    state_root: Path,
    metadata_name: str,
) -> None:
    _populate_state(state_root, "metadata-symlink")
    report = backup_state(state_root, "metadata_source")
    source = _backup_path(state_root, report["backup_id"])
    outside = state_root.parent / f"outside-{metadata_name.replace('.', '-')}"
    outside.write_bytes(b"outside metadata")
    metadata = source / metadata_name
    metadata.unlink()
    metadata.symlink_to(outside)
    live_before = _managed_state_snapshot(state_root)

    _assert_error_code("invalid_manifest", lambda: verify_backup(state_root, source))

    assert _managed_state_snapshot(state_root) == live_before
    assert outside.read_bytes() == b"outside metadata"


def test_restore_pins_backup_directory_identity_across_parent_symlink_swap(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate_state(state_root, "pinned-source")
    target = _managed_state_snapshot(state_root)
    report = backup_state(state_root, "pinned_restore")
    source = _backup_path(state_root, report["backup_id"])
    _populate_state(state_root, "live-before-pinned-restore")

    parked = state_root.parent / "parked-original-backup"
    external = state_root.parent / "external-untrusted-backup"
    (external / "payload").mkdir(parents=True)
    (external / "manifest.json").write_bytes(b"{not-valid-json}\n")
    (external / "manifest.sha256").write_text("0" * 64 + "\n", encoding="ascii")
    original_read = maintenance_module._read_backup_metadata
    swapped = False

    def swap_parent_then_read(opened, name, *, maximum_size):
        nonlocal swapped
        if not swapped:
            source.rename(parked)
            source.symlink_to(external, target_is_directory=True)
            swapped = True
        return original_read(opened, name, maximum_size=maximum_size)

    monkeypatch.setattr(
        maintenance_module,
        "_read_backup_metadata",
        swap_parent_then_read,
    )
    try:
        restored = restore_state(
            state_root,
            backup=source,
            expected_purpose="pinned_restore",
        )
    finally:
        if source.is_symlink():
            source.unlink()
        if parked.exists():
            parked.rename(source)

    assert swapped is True
    assert restored["code"] == "state_restored"
    assert _managed_state_snapshot(state_root) == target
    verify_backup(state_root, source, expected_purpose="pinned_restore")


def test_active_sqlite_writer_leaves_database_wal_shm_and_backups_unchanged(
    state_root: Path,
) -> None:
    database = state_root / DATABASE_PATHS[0]
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE fixture_marker(value TEXT NOT NULL)")
        writer.execute("INSERT INTO fixture_marker(value) VALUES ('committed')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO fixture_marker(value) VALUES ('uncommitted')")
        before = _database_family_snapshot(database)
        assert before["database"] is not None
        assert before["-wal"] is not None
        assert before["-shm"] is not None

        error = _assert_error_code(
            "active_sqlite_writer",
            lambda: reset_state(state_root, "pre_clean"),
        )

        assert error.exit_code == 2
        assert _database_family_snapshot(database) == before
        assert not (state_root / "data" / "backups").exists()
    finally:
        writer.rollback()
        writer.close()


def test_rollback_journal_is_rejected_before_sqlite_open_without_side_effects(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate_state(state_root, "rollback-journal")
    source = backup_state(state_root, "journal_source")
    database = state_root / DATABASE_PATHS[0]
    journal = database.with_name(database.name + "-journal")
    journal.write_bytes(b"synthetic interrupted rollback journal")
    before = _persistent_tree_snapshot(state_root)

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("SQLite was opened before rollback-journal preflight")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    operations = (
        lambda: backup_state(state_root, "blocked_journal_backup"),
        lambda: reset_state(state_root, "blocked_journal_reset"),
        lambda: restore_state(
            state_root,
            backup=source["backup_id"],
            expected_purpose="journal_source",
        ),
    )
    for operation in operations:
        error = _assert_error_code("unsafe_sqlite_state", operation)
        assert error.exit_code == 2
        assert _persistent_tree_snapshot(state_root) == before


@pytest.mark.parametrize("entrypoint", ("runtime", "recover"))
def test_explicit_startup_recovery_consumes_real_hot_rollback_journal(
    state_root: Path,
    entrypoint: str,
) -> None:
    _populate_state(state_root, "before-process-crash")
    database = state_root / DATABASE_PATHS[0]
    program = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
connection.execute("PRAGMA synchronous=FULL")
connection.execute("PRAGMA cache_size=1")
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "UPDATE fixture_marker SET value = 'must-rollback' WHERE position = 1"
)
connection.execute("CREATE TABLE crash_spill(id INTEGER PRIMARY KEY, payload BLOB)")
for _index in range(128):
    connection.execute("INSERT INTO crash_spill(payload) VALUES (?)", (b"x" * 4096,))
os._exit(77)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", program, str(database)],
        cwd=PROJECT_ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert crashed.returncode == 77, crashed.stderr
    journal = database.with_name(database.name + "-journal")
    assert journal.is_file()

    if entrypoint == "runtime":
        with runtime_state_lease(state_root):
            assert not journal.exists()
    else:
        assert recover_state(state_root) == {
            "ok": True,
            "code": "recovered",
            "recovered": True,
        }

    assert not journal.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM fixture_marker WHERE position = 1"
        ).fetchone() == ("before-process-crash:database:0",)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'crash_spill'"
        ).fetchone() == (0,)


def test_crash_wal_sidecars_make_all_read_only_inspection_fail_without_mutation(
    state_root: Path,
) -> None:
    _populate_state(state_root, "before-wal-crash")
    source = backup_state(state_root, "wal_source")
    database = state_root / DATABASE_PATHS[0]
    program = """
import os
import sqlite3
import sys

database = sys.argv[1]
connection = sqlite3.connect(database)
assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute(
    "UPDATE fixture_marker SET value = 'committed-before-crash' WHERE position = 1"
)
connection.commit()
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(database)],
        cwd=PROJECT_ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert database.with_name(database.name + "-wal").is_file()
    assert database.with_name(database.name + "-shm").is_file()
    before = _persistent_tree_snapshot(state_root)

    operations = (
        lambda: backup_state(state_root, "wal_dry_backup", dry_run=True),
        lambda: reset_state(state_root, "wal_dry_reset", dry_run=True),
        lambda: restore_state(
            state_root,
            backup=source["backup_id"],
            expected_purpose="wal_source",
            dry_run=True,
        ),
        lambda: preflight_state(state_root, backup=source["backup_id"]),
    )
    for operation in operations:
        error = _assert_error_code("unsafe_sqlite_state", operation)
        assert error.exit_code == 2
        assert _persistent_tree_snapshot(state_root) == before


def test_clean_wal_mode_database_dry_run_never_creates_live_sidecars(
    state_root: Path,
) -> None:
    _populate_state(state_root, "clean-wal-dry-run")
    database = state_root / DATABASE_PATHS[0]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()
    before = _persistent_tree_snapshot(state_root)

    report = backup_state(state_root, "clean_wal_dry_run", dry_run=True)

    assert report["code"] == "dry_run_ok"
    assert _persistent_tree_snapshot(state_root) == before
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_read_only_guard_survives_snapshot_source_descriptor_close(
    state_root: Path,
) -> None:
    _populate_state(state_root, "ofd-lock")
    database = state_root / DATABASE_PATHS[0]
    snapshot = state_root.parent / "read-only-snapshot.sqlite3"
    program = """
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], timeout=0)
try:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE fixture_marker SET value = 'external-write' WHERE position = 1"
    )
    connection.commit()
except sqlite3.OperationalError as exc:
    connection.close()
    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
        raise
    print("locked")
else:
    connection.close()
    print("wrote")
"""

    def external_write() -> str:
        result = subprocess.run(
            [sys.executable, "-c", program, str(database)],
            cwd=PROJECT_ROOT,
            env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    with maintenance_module._read_only_guards(state_root):
        assert external_write() == "locked"
        maintenance_module._snapshot_database(
            database,
            snapshot,
            read_only_source=True,
        )
        assert external_write() == "locked"
    assert external_write() == "wrote"


@pytest.mark.parametrize(
    ("crash_kind", "entrypoint", "expected_suffix", "expected_returncode"),
    (
        ("wal", "backup", "-wal", 0),
        ("journal", "preflight", "-journal", 77),
    ),
)
def test_read_only_guard_rechecks_sidecars_after_lock_acquisition_race(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_kind: str,
    entrypoint: str,
    expected_suffix: str,
    expected_returncode: int,
) -> None:
    _populate_state(state_root, f"{crash_kind}-lock-race")
    database = state_root / DATABASE_PATHS[0]
    programs = {
        "wal": """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
connection.execute(
    "UPDATE fixture_marker SET value = 'committed-race' WHERE position = 1"
)
connection.commit()
os._exit(0)
""",
        "journal": """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA cache_size=1")
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "UPDATE fixture_marker SET value = 'uncommitted-race' WHERE position = 1"
)
connection.execute("CREATE TABLE race_spill(id INTEGER PRIMARY KEY, payload BLOB)")
for _index in range(128):
    connection.execute("INSERT INTO race_spill(payload) VALUES (?)", (b"x" * 4096,))
os._exit(77)
""",
    }
    original_fcntl = maintenance_module.fcntl.fcntl
    injected = False

    def inject_crash_before_first_lock(
        descriptor: int,
        command: int,
        argument: bytes,
    ) -> Any:
        nonlocal injected
        if not injected and command == maintenance_module.fcntl.F_OFD_SETLK:
            injected = True
            crashed = subprocess.run(
                [sys.executable, "-c", programs[crash_kind], str(database)],
                cwd=PROJECT_ROOT,
                env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert crashed.returncode == expected_returncode, crashed.stderr
        return original_fcntl(descriptor, command, argument)

    monkeypatch.setattr(maintenance_module.fcntl, "fcntl", inject_crash_before_first_lock)
    operation = (
        (lambda: backup_state(state_root, "sidecar_race", dry_run=True))
        if entrypoint == "backup"
        else (lambda: preflight_state(state_root))
    )

    error = _assert_error_code("unsafe_sqlite_state", operation)
    assert error.exit_code == 2
    assert injected is True
    assert database.with_name(database.name + expected_suffix).is_file()


def test_read_only_guard_rechecks_managed_database_set_after_lock_race(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = state_root / DATABASE_PATHS[0]
    _write_database(database, "existing-before-lock")
    appeared = state_root / DATABASE_PATHS[3]
    appeared.parent.mkdir(parents=True)
    program = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
connection.execute("CREATE TABLE appeared(value TEXT NOT NULL)")
connection.execute("INSERT INTO appeared(value) VALUES ('committed-race')")
connection.commit()
os._exit(0)
"""
    original_fcntl = maintenance_module.fcntl.fcntl
    injected = False

    def create_missing_database_before_first_lock(
        descriptor: int,
        command: int,
        argument: bytes,
    ) -> Any:
        nonlocal injected
        if not injected and command == maintenance_module.fcntl.F_OFD_SETLK:
            injected = True
            crashed = subprocess.run(
                [sys.executable, "-c", program, str(appeared)],
                cwd=PROJECT_ROOT,
                env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert crashed.returncode == 0, crashed.stderr
        return original_fcntl(descriptor, command, argument)

    monkeypatch.setattr(
        maintenance_module.fcntl,
        "fcntl",
        create_missing_database_before_first_lock,
    )

    error = _assert_error_code(
        "state_changed",
        lambda: backup_state(state_root, "database_set_race", dry_run=True),
    )
    assert error.exit_code == 3
    assert injected is True
    assert appeared.is_file()
    assert appeared.with_name(appeared.name + "-wal").is_file()
    assert appeared.with_name(appeared.name + "-shm").is_file()


def test_writer_guard_rechecks_managed_database_set_after_connect_race(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = state_root / DATABASE_PATHS[0]
    _write_database(existing, "writer-guard-existing")
    appeared = state_root / DATABASE_PATHS[3]
    appeared.parent.mkdir(parents=True)
    program = """
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("CREATE TABLE appeared(value TEXT NOT NULL)")
connection.execute("INSERT INTO appeared(value) VALUES ('committed-race')")
connection.commit()
connection.close()
"""
    original_connect = maintenance_module.sqlite3.connect
    injected = False

    def create_missing_database_before_first_connect(*args: Any, **kwargs: Any):
        nonlocal injected
        if not injected:
            injected = True
            writer = subprocess.run(
                [sys.executable, "-c", program, str(appeared)],
                cwd=PROJECT_ROOT,
                env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert writer.returncode == 0, writer.stderr
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(
        maintenance_module.sqlite3,
        "connect",
        create_missing_database_before_first_connect,
    )
    before_existing = _database_family_snapshot(existing)

    error = _assert_error_code(
        "state_changed",
        lambda: reset_state(state_root, "writer_guard_set_race"),
    )

    assert error.exit_code == 3
    assert injected is True
    assert _database_family_snapshot(existing) == before_existing
    with sqlite3.connect(appeared) as connection:
        assert connection.execute("SELECT value FROM appeared").fetchone() == (
            "committed-race",
        )
    assert not (state_root / "data/backups").exists()


def test_read_only_inspection_detects_delete_mode_writer_without_sidecars(
    state_root: Path,
) -> None:
    _populate_state(state_root, "delete-writer")
    source = backup_state(state_root, "delete_writer_source")
    database = state_root / DATABASE_PATHS[0]
    program = """
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], timeout=0)
assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
connection.execute("BEGIN IMMEDIATE")
print("ready", flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", program, str(database)],
        cwd=PROJECT_ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "ready"
        assert not database.with_name(database.name + "-journal").exists()
        before = _persistent_tree_snapshot(state_root)

        operations = (
            lambda: backup_state(state_root, "delete_writer_backup", dry_run=True),
            lambda: reset_state(state_root, "delete_writer_reset", dry_run=True),
            lambda: restore_state(
                state_root,
                backup=source["backup_id"],
                expected_purpose="delete_writer_source",
                dry_run=True,
            ),
            lambda: preflight_state(state_root),
        )
        for operation in operations:
            error = _assert_error_code("active_sqlite_writer", operation)
            assert error.exit_code == 2
            assert _persistent_tree_snapshot(state_root) == before
    finally:
        if writer.stdin is not None:
            writer.stdin.write("release\n")
            writer.stdin.flush()
        _, stderr = writer.communicate(timeout=10)
        assert writer.returncode == 0, stderr


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("corrupt_json", "invalid_manifest"),
        ("payload_hash", "hash_mismatch"),
        ("sqlite_schema", "schema_incompatible"),
    ),
)
def test_verify_backup_rejects_corrupt_hash_and_schema_manifest_mutants(
    state_root: Path,
    mutation: str,
    expected_code: str,
) -> None:
    _populate_state(state_root, "valid")
    report = backup_state(state_root, "manual")
    source = _backup_path(state_root, report["backup_id"])
    mutant = _clone_backup(state_root, source, f"{mutation}-mutant")
    live_before = _managed_state_snapshot(state_root)

    if mutation == "corrupt_json":
        value = b'{"manifest":\n'
        (mutant / "manifest.json").write_bytes(value)
        (mutant / "manifest.sha256").write_text(
            _sha256_bytes(value) + "\n",
            encoding="ascii",
        )
    else:
        manifest = json.loads((mutant / "manifest.json").read_bytes())
        database_entry = next(
            entry for entry in manifest["entries"] if entry["kind"] == "sqlite"
        )
        if mutation == "payload_hash":
            database_entry["sha256"] = "0" * 64
        else:
            database_entry["sqlite"]["schema_version"] += 1
        _rewrite_manifest(mutant, manifest)

    _assert_error_code(expected_code, lambda: verify_backup(state_root, mutant))
    assert _managed_state_snapshot(state_root) == live_before


@pytest.mark.parametrize(
    "mutation",
    (
        "manifest-version-bool",
        "layout-version-bool",
        "one-byte-file-size-bool",
        "sqlite-foreign-key-count-bool",
        "sqlite-schema-version-bool",
        "sqlite-user-version-bool",
        "sqlite-page-size-bool",
        "summary-schema-version-bool",
        "summary-user-version-bool",
        "missing-source-path",
        "missing-archive-path",
        "missing-size-bytes",
        "missing-sha256",
    ),
)
def test_manifest_integer_types_and_entry_shape_are_strict_before_restore(
    state_root: Path,
    mutation: str,
) -> None:
    _populate_state(state_root, "strict-manifest-types")
    single_byte_path = state_root / "data/workspace/single-byte.bin"
    single_byte_path.write_bytes(b"x")
    report = backup_state(state_root, "strict_types")
    source = _backup_path(state_root, report["backup_id"])
    mutant = _clone_backup(state_root, source, f"strict-types-{mutation}")
    manifest = json.loads((mutant / "manifest.json").read_bytes())
    sqlite_entry = next(entry for entry in manifest["entries"] if entry["kind"] == "sqlite")
    file_entry = next(
        entry
        for entry in manifest["entries"]
        if entry["source_path"] == "data/workspace/single-byte.bin"
    )
    database_summary = next(
        item
        for item in manifest["schema"]["databases"]
        if item["source_path"] == sqlite_entry["source_path"]
    )

    if mutation == "manifest-version-bool":
        manifest["manifest_version"] = True
    elif mutation == "layout-version-bool":
        manifest["layout_version"] = True
    elif mutation == "one-byte-file-size-bool":
        assert file_entry["size_bytes"] == 1
        file_entry["size_bytes"] = True
    elif mutation == "sqlite-foreign-key-count-bool":
        sqlite_entry["sqlite"]["foreign_key_check_count"] = False
    elif mutation == "sqlite-schema-version-bool":
        assert sqlite_entry["sqlite"]["schema_version"] == CURRENT_SCHEMA_VERSION
        sqlite_entry["sqlite"]["schema_version"] = True
    elif mutation == "sqlite-user-version-bool":
        assert sqlite_entry["sqlite"]["user_version"] == CURRENT_SCHEMA_VERSION
        sqlite_entry["sqlite"]["user_version"] = True
    elif mutation == "sqlite-page-size-bool":
        sqlite_entry["sqlite"]["page_size"] = True
    elif mutation == "summary-schema-version-bool":
        assert database_summary["schema_version"] == CURRENT_SCHEMA_VERSION
        database_summary["schema_version"] = True
    elif mutation == "summary-user-version-bool":
        assert database_summary["user_version"] == CURRENT_SCHEMA_VERSION
        database_summary["user_version"] = True
    elif mutation == "missing-source-path":
        file_entry.pop("source_path")
    elif mutation == "missing-archive-path":
        file_entry.pop("archive_path")
    elif mutation == "missing-size-bytes":
        file_entry.pop("size_bytes")
    elif mutation == "missing-sha256":
        file_entry.pop("sha256")
    else:  # pragma: no cover - the parametrization is the closed mutation set
        raise AssertionError(f"unknown mutation: {mutation}")

    if mutation == "one-byte-file-size-bool":
        _refresh_manifest_payload_digest(manifest)
    _rewrite_manifest(mutant, manifest)
    before = _persistent_tree_snapshot(state_root)

    _assert_error_code("invalid_manifest", lambda: verify_backup(state_root, mutant))
    _assert_error_code(
        "invalid_manifest",
        lambda: restore_state(
            state_root,
            backup=mutant,
            expected_purpose="strict_types",
        ),
    )

    assert _persistent_tree_snapshot(state_root) == before


def test_older_backup_supported_version_remains_forward_restorable(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_schema_version = CURRENT_SCHEMA_VERSION - 1
    monkeypatch.setattr(
        maintenance_module,
        "CURRENT_SCHEMA_VERSION",
        older_schema_version,
    )
    _populate_state(
        state_root,
        "older-schema",
        schema_version=older_schema_version,
    )
    report = backup_state(state_root, "older_schema")

    monkeypatch.setattr(
        maintenance_module,
        "CURRENT_SCHEMA_VERSION",
        CURRENT_SCHEMA_VERSION,
    )
    manifest = verify_backup(
        state_root,
        report["backup_id"],
        expected_purpose="older_schema",
    )
    restored = restore_state(
        state_root,
        backup=report["backup_id"],
        expected_purpose="older_schema",
        dry_run=True,
    )

    assert manifest["schema"]["supported_version"] == older_schema_version
    assert restored["code"] == "dry_run_ok"


@pytest.mark.parametrize(
    "invalid_exclusions",
    (
        {
            "patterns": [".env", ".env.*"],
            "paths": ["data/context/public.txt"],
            "secrets_included": False,
        },
        {
            "patterns": [".env", ".env.*"],
            "paths": ["../../.env"],
            "secrets_included": False,
        },
        {
            "patterns": [".env", ".env.*"],
            "paths": [],
            "secrets_included": False,
            "untrusted_policy": True,
        },
    ),
    ids=("non-secret", "traversal", "extra-field"),
)
def test_manifest_exclusion_policy_is_strictly_validated(
    state_root: Path,
    invalid_exclusions: dict[str, Any],
) -> None:
    _populate_state(state_root, "exclusion-policy")
    report = backup_state(state_root, "exclusion_source")
    source = _backup_path(state_root, report["backup_id"])
    mutant = _clone_backup(state_root, source, "invalid-exclusion-policy")
    manifest = json.loads((mutant / "manifest.json").read_bytes())
    manifest["exclusions"] = invalid_exclusions
    _rewrite_manifest(mutant, manifest)

    _assert_error_code("invalid_manifest", lambda: verify_backup(state_root, mutant))


def test_backup_and_restore_enforce_private_file_and_directory_modes(
    state_root: Path,
) -> None:
    _populate_state(state_root, "private-modes")
    report = reset_state(state_root, "private_source")
    backup_path = _backup_path(state_root, report["backup_id"])

    assert (state_root / "data/backups").stat().st_mode & 0o777 == 0o700
    for path in (backup_path, *backup_path.rglob("*")):
        expected = 0o700 if path.is_dir() else 0o600
        assert path.stat().st_mode & 0o777 == expected

    restore_state(
        state_root,
        backup=report["backup_id"],
        expected_purpose="private_source",
    )
    for relative in DATABASE_PATHS:
        assert (state_root / relative).stat().st_mode & 0o777 == 0o600
    for relative in MANAGED_TREES:
        tree = state_root / relative
        for path in (tree, *tree.rglob("*")):
            expected = 0o700 if path.is_dir() else 0o600
            assert path.stat().st_mode & 0o777 == expected


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("corrupt_sqlite", "sqlite_integrity_failed"),
        ("foreign_key_violation", "foreign_key_failed"),
        ("future_schema", "schema_incompatible"),
    ),
)
def test_restore_rejects_hash_valid_but_invalid_sqlite_payload_before_live_mutation(
    state_root: Path,
    mutation: str,
    expected_code: str,
) -> None:
    _populate_state(state_root, "source")
    source_report = backup_state(state_root, "restore_source")
    source = _backup_path(state_root, source_report["backup_id"])
    mutant = _clone_backup(state_root, source, f"{mutation}-payload-mutant")
    _populate_state(state_root, "live")
    live_before = _managed_state_snapshot(state_root)

    manifest = json.loads((mutant / "manifest.json").read_bytes())
    database_entry = next(
        entry
        for entry in manifest["entries"]
        if entry["source_path"] == DATABASE_PATHS[0]
    )
    payload = mutant / database_entry["archive_path"]
    if mutation == "corrupt_sqlite":
        payload.write_bytes(b"not-a-sqlite-database")
    else:
        with sqlite3.connect(payload) as connection:
            if mutation == "foreign_key_violation":
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute(
                    "INSERT INTO fixture_child(id, parent_id) VALUES (2, 999999)"
                )
            else:
                connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
    database_entry["size_bytes"] = payload.stat().st_size
    database_entry["sha256"] = _sha256_bytes(payload.read_bytes())
    _refresh_manifest_payload_digest(manifest)
    _rewrite_manifest(mutant, manifest)

    _assert_error_code(
        expected_code,
        lambda: restore_state(state_root, backup=mutant),
    )
    assert _managed_state_snapshot(state_root) == live_before
    assert not any(
        path.is_dir()
        for path in (state_root / "data" / "backups").iterdir()
        if path.name.startswith("before_restore-")
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("missing_manifest", "manifest_missing"),
        ("path_traversal", "unsafe_backup_path"),
    ),
)
def test_restore_rejects_missing_manifest_and_path_traversal(
    state_root: Path,
    mutation: str,
    expected_code: str,
) -> None:
    _populate_state(state_root, "source")
    source_report = backup_state(state_root, "restore_source")
    source = _backup_path(state_root, source_report["backup_id"])
    mutant = _clone_backup(state_root, source, f"{mutation}-mutant")
    _populate_state(state_root, "live")
    live_before = _managed_state_snapshot(state_root)

    if mutation == "missing_manifest":
        (mutant / "manifest.json").unlink()
    else:
        manifest = json.loads((mutant / "manifest.json").read_bytes())
        entry = manifest["entries"][0]
        entry["source_path"] = "../../.env"
        entry["archive_path"] = "payload/../../.env"
        _rewrite_manifest(mutant, manifest)

    _assert_error_code(
        expected_code,
        lambda: restore_state(state_root, backup=mutant),
    )
    assert _managed_state_snapshot(state_root) == live_before


def test_reset_then_restore_round_trips_state_and_captures_before_restore(
    state_root: Path,
) -> None:
    _populate_state(state_root, "original")
    original = _managed_state_snapshot(state_root)

    reset = reset_state(state_root, "pre_clean")
    source_path = _backup_path(state_root, reset["backup_id"])
    source_manifest = verify_backup(
        state_root,
        reset["backup_id"],
        expected_purpose="pre_clean",
    )
    assert reset["code"] == "state_reset"
    assert _backup_payload_snapshot(source_path, source_manifest) == original
    assert _managed_state_snapshot(state_root) == {
        "databases": {},
        "files": {},
        "directories": [
            "data/context",
            "data/context/decisions",
            "data/context/plans",
            "data/workspace",
        ],
    }

    _populate_state(state_root, "interim")
    interim = _managed_state_snapshot(state_root)
    restored = restore_state(
        state_root,
        backup=reset["backup_id"],
        expected_purpose="pre_clean",
    )

    assert restored["code"] == "state_restored"
    assert restored["backup_id"] == reset["backup_id"]
    assert _managed_state_snapshot(state_root) == original
    before_restore_id = restored["before_restore_backup_id"]
    assert isinstance(before_restore_id, str) and before_restore_id
    before_restore_path = _backup_path(state_root, before_restore_id)
    before_restore_manifest = verify_backup(
        state_root,
        before_restore_id,
        expected_purpose="before_restore",
    )
    assert _backup_payload_snapshot(before_restore_path, before_restore_manifest) == interim


def test_latest_restore_fails_closed_when_newest_named_backup_is_corrupt(
    state_root: Path,
) -> None:
    purpose = "latest_test"
    _populate_state(state_root, "old-backup")
    old_report = backup_state(state_root, purpose)
    old_path = _backup_path(state_root, old_report["backup_id"])
    old_bound = _backup_path(
        state_root,
        f"{purpose}-20980101T000000.000000Z-{'0' * 32}",
    )
    old_path.rename(old_bound)
    old_manifest = json.loads((old_bound / "manifest.json").read_bytes())
    old_manifest["backup_id"] = old_bound.name
    old_manifest["created_at"] = "2098-01-01T00:00:00.000000Z"
    _rewrite_manifest(old_bound, old_manifest)
    verify_backup(state_root, old_bound, expected_purpose=purpose)

    _populate_state(state_root, "new-backup")
    new_report = backup_state(state_root, purpose)
    new_path = _backup_path(state_root, new_report["backup_id"])
    new_bound = _backup_path(
        state_root,
        f"{purpose}-20990101T000000.000000Z-{'1' * 32}",
    )
    new_path.rename(new_bound)
    new_manifest = json.loads((new_bound / "manifest.json").read_bytes())
    new_manifest["backup_id"] = new_bound.name
    new_manifest["created_at"] = "2099-01-01T00:00:00.000000Z"
    _rewrite_manifest(new_bound, new_manifest)
    verify_backup(state_root, new_bound, expected_purpose=purpose)

    corrupt_manifest = b"{not-valid-json}\n"
    (new_bound / "manifest.json").write_bytes(corrupt_manifest)
    (new_bound / "manifest.sha256").write_text(
        _sha256_bytes(corrupt_manifest) + "\n",
        encoding="ascii",
    )
    live_before = _managed_state_snapshot(state_root)

    _assert_error_code(
        "invalid_manifest",
        lambda: restore_state(state_root, latest_purpose=purpose),
    )

    assert _managed_state_snapshot(state_root) == live_before
    verify_backup(state_root, old_bound, expected_purpose=purpose)
    assert (new_bound / "manifest.json").read_bytes() == corrupt_manifest


def test_caught_phase_fault_rolls_back_the_entire_state_switch(
    state_root: Path,
) -> None:
    _populate_state(state_root, "before-fault")
    before = _managed_state_snapshot(state_root)
    observed_phases: list[str] = []

    class InjectedFault(RuntimeError):
        pass

    def fail_after_first_install(phase: str) -> None:
        observed_phases.append(phase)
        if phase == "after_first_install":
            raise InjectedFault("caught fault injection")

    with pytest.raises(InjectedFault, match="caught fault injection"):
        reset_state(
            state_root,
            "fault_backup",
            phase_hook=fail_after_first_install,
        )

    assert "after_live_quarantine" in observed_phases
    assert "after_first_install" in observed_phases
    assert "after_state_commit" not in observed_phases
    assert _managed_state_snapshot(state_root) == before
    operations = state_root / "data" / ".maintenance" / "operations"
    assert operations.is_dir()
    assert not any(operations.iterdir())
    published = [
        path
        for path in (state_root / "data" / "backups").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(published) == 1
    verify_backup(state_root, published[0], expected_purpose="fault_backup")


def test_switch_fsyncs_candidate_and_nested_rollback_parents_before_journal(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate hard-power ordering; os._exit tests below model process loss only."""

    _populate_state(state_root, "durable-directories")
    events: list[tuple[str, str]] = []
    original_fsync = maintenance_module._fsync_directory

    def recording_fsync(path: Path) -> None:
        events.append(("fsync", Path(path).as_posix()))
        original_fsync(path)

    def record_phase(phase: str) -> None:
        events.append(("phase", phase))

    monkeypatch.setattr(maintenance_module, "_fsync_directory", recording_fsync)
    reset_state(state_root, "durable_reset", phase_hook=record_phase)

    journal_index = events.index(("phase", "after_operation_journal"))
    fsynced_before_journal = {
        value for kind, value in events[:journal_index] if kind == "fsync"
    }
    assert any(path.endswith("/candidate/data/context/plans") for path in fsynced_before_journal)
    assert any(path.endswith("/candidate/data/workspace") for path in fsynced_before_journal)
    assert any(path.endswith("/rollback/data") for path in fsynced_before_journal)
    assert any(path.endswith("/rollback/backend/data") for path in fsynced_before_journal)
    assert (state_root / "data").as_posix() in fsynced_before_journal
    assert (state_root / "data/.maintenance").as_posix() in fsynced_before_journal
    assert (state_root / "data/.maintenance/operations").as_posix() in (
        fsynced_before_journal
    )


def test_first_backup_fsyncs_every_new_ancestor_before_success(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_database(state_root / DATABASE_PATHS[1], "first-backup")
    events: list[tuple[str, str]] = []
    original_fsync = maintenance_module._fsync_directory

    def recording_fsync(path: Path) -> None:
        events.append(("fsync", Path(path).as_posix()))
        original_fsync(path)

    def record_phase(phase: str) -> None:
        events.append(("phase", phase))

    monkeypatch.setattr(maintenance_module, "_fsync_directory", recording_fsync)
    backup_state(state_root, "first_backup", phase_hook=record_phase)

    published_index = events.index(("phase", "after_backup_publish"))
    fsynced_before_publish = {
        value for kind, value in events[:published_index] if kind == "fsync"
    }
    assert state_root.as_posix() in fsynced_before_publish
    assert (state_root / "data").as_posix() in fsynced_before_publish
    assert (state_root / "data/backups").as_posix() in fsynced_before_publish


def test_operation_staging_creation_fault_is_cleaned_and_recoverable(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate_state(state_root, "staging-fault")
    before = _managed_state_snapshot(state_root)
    original_mkdir = Path.mkdir
    injected = False

    def fail_first_rollback_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal injected
        if (
            not injected
            and path.name == "rollback"
            and path.parent.parent.name == "operations"
        ):
            injected = True
            raise OSError("injected rollback staging mkdir failure")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_first_rollback_mkdir)
    with pytest.raises(MaintenanceError) as caught:
        reset_state(state_root, "staging_fault")

    assert caught.value.code == "state_write_failed"
    assert injected is True
    assert _managed_state_snapshot(state_root) == before
    operations = state_root / "data/.maintenance/operations"
    assert operations.is_dir()
    assert not any(operations.iterdir())
    assert recover_state(state_root) == {
        "ok": True,
        "code": "nothing_to_recover",
        "recovered": False,
    }


@pytest.mark.parametrize(
    ("kill_phase", "expected_generation"),
    (
        ("after_operation_journal", "old"),
        ("after_live_quarantine", "old"),
        ("after_first_install", "old"),
        ("after_state_commit", "target"),
    ),
)
def test_recover_state_converges_after_real_exit_at_restore_commit_boundaries(
    state_root: Path,
    kill_phase: str,
    expected_generation: str,
) -> None:
    _populate_state(state_root, "restore-source")
    target = _managed_state_snapshot(state_root)
    source_report = backup_state(state_root, "recovery_source")
    _populate_state(state_root, "live-before-crash")
    live_before = _managed_state_snapshot(state_root)
    program = """
import os
import sys
from pathlib import Path
from app.db.maintenance import restore_state

root = Path(sys.argv[1])
backup_id = sys.argv[2]
kill_phase = sys.argv[3]

def crash(phase):
    if phase == kill_phase:
        os._exit(73)

restore_state(
    root,
    backup=backup_id,
    expected_purpose="recovery_source",
    phase_hook=crash,
)
raise SystemExit(91)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(state_root),
            source_report["backup_id"],
            kill_phase,
        ],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 73, result.stderr
    operations = state_root / "data" / ".maintenance" / "operations"
    assert operations.is_dir() and any(operations.iterdir())

    recovery = recover_state(state_root)
    assert recovery == {"ok": True, "code": "recovered", "recovered": True}
    assert _managed_state_snapshot(state_root) == (
        live_before if expected_generation == "old" else target
    )
    assert not any(operations.iterdir())
    assert recover_state(state_root) == {
        "ok": True,
        "code": "nothing_to_recover",
        "recovered": False,
    }


@pytest.mark.parametrize("entrypoint", ("runtime", "recover", "mutation"))
def test_startup_recovers_switch_crash_between_wal_database_and_sidecars(
    state_root: Path,
    entrypoint: str,
) -> None:
    _populate_state(state_root, "wal-switch-crash")
    before = _managed_state_snapshot(state_root)
    database = state_root / DATABASE_PATHS[0]
    program = """
import os
import sqlite3
import sys
from pathlib import Path
import app.db.maintenance as maintenance

root = Path(sys.argv[1])
database = root / sys.argv[2]
keeper = sqlite3.connect(database)
assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
keeper.execute("SELECT count(*) FROM fixture_marker").fetchone()
original_replace = maintenance._replace_path

def crash_after_database_move(source, destination):
    original_replace(source, destination)
    if Path(source) == database and "rollback" in Path(destination).parts:
        os._exit(78)

maintenance._replace_path = crash_after_database_move
maintenance.reset_state(root, "wal_switch_crash")
raise SystemExit(96)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", program, str(state_root), DATABASE_PATHS[0]],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 78, crashed.stderr
    assert not database.exists()
    assert database.with_name(database.name + "-wal").exists()
    assert database.with_name(database.name + "-shm").exists()

    if entrypoint == "runtime":
        with runtime_state_lease(state_root):
            pass
    elif entrypoint == "recover":
        assert recover_state(state_root)["code"] == "recovered"
    else:
        assert backup_state(state_root, "after_wal_switch_crash")["code"] == (
            "backup_created"
        )

    assert _managed_state_snapshot(state_root) == before
    operations = state_root / "data/.maintenance/operations"
    assert operations.is_dir()
    assert not any(operations.iterdir())


def test_runtime_startup_recovers_interrupted_switch_before_yield(
    state_root: Path,
) -> None:
    _populate_state(state_root, "startup-recovery")
    before = _managed_state_snapshot(state_root)
    program = """
import os
import sys
from pathlib import Path
from app.db.maintenance import reset_state

root = Path(sys.argv[1])

def crash(phase):
    if phase == "after_first_install":
        os._exit(75)

reset_state(root, "startup_crash", phase_hook=crash)
raise SystemExit(93)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(state_root)],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 75, result.stderr

    with runtime_state_lease(state_root):
        assert _managed_state_snapshot(state_root) == before
        operations = state_root / "data/.maintenance/operations"
        assert operations.is_dir() and not any(operations.iterdir())

    assert _managed_state_snapshot(state_root) == before


@pytest.mark.parametrize(
    ("kill_phase", "published_count", "recovery_code"),
    (
        ("after_sqlite_snapshot", 0, "recovered"),
        ("after_manifest_fsync", 0, "recovered"),
        ("after_backup_publish", 1, "nothing_to_recover"),
    ),
)
def test_backup_publish_is_all_or_nothing_after_real_exit(
    state_root: Path,
    kill_phase: str,
    published_count: int,
    recovery_code: str,
) -> None:
    _populate_state(state_root, "backup-kill")
    live_before = _managed_state_snapshot(state_root)
    program = """
import os
import sys
from pathlib import Path
from app.db.maintenance import backup_state

root = Path(sys.argv[1])
kill_phase = sys.argv[2]

def crash(phase):
    if phase == kill_phase:
        os._exit(74)

backup_state(root, "kill_backup", phase_hook=crash)
raise SystemExit(92)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(state_root), kill_phase],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 74, result.stderr
    assert _managed_state_snapshot(state_root) == live_before
    backups_root = state_root / "data" / "backups"
    published = [
        path
        for path in backups_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(published) == published_count
    if published:
        verify_backup(state_root, published[0], expected_purpose="kill_backup")

    recovery = recover_state(state_root)
    assert recovery["code"] == recovery_code
    assert recovery["recovered"] is (recovery_code == "recovered")
    assert not any(path.name.startswith(".incomplete-") for path in backups_root.iterdir())
    assert _managed_state_snapshot(state_root) == live_before


def test_concurrent_maintenance_lease_has_exactly_one_winner(
    state_root: Path,
) -> None:
    _write_database(state_root / DATABASE_PATHS[0], "race")
    start = threading.Barrier(3)
    winner_entered = threading.Event()
    release_winner = threading.Event()
    outcomes: queue.Queue[tuple[str, str]] = queue.Queue()

    def hold_winner(phase: str) -> None:
        if phase != "after_sqlite_snapshot":
            return
        winner_entered.set()
        if not release_winner.wait(timeout=10):
            raise RuntimeError("concurrency harness did not release the lease winner")

    def worker(label: str) -> None:
        try:
            start.wait(timeout=10)
            report = backup_state(
                state_root,
                f"race_{label}",
                phase_hook=hold_winner,
            )
        except MaintenanceError as exc:
            outcomes.put(("error", exc.code))
        except BaseException as exc:
            outcomes.put(("unexpected", f"{type(exc).__name__}:{exc}"))
        else:
            outcomes.put(("success", report["code"]))

    threads = [
        threading.Thread(target=worker, args=(label,), daemon=True)
        for label in ("alpha", "beta")
    ]
    for thread in threads:
        thread.start()

    collected: list[tuple[str, str]] = []
    try:
        start.wait(timeout=10)
        assert winner_entered.wait(timeout=10)
        collected.append(outcomes.get(timeout=10))
        assert collected[0] == ("error", "maintenance_busy")
    finally:
        release_winner.set()
        for thread in threads:
            thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    while not outcomes.empty():
        collected.append(outcomes.get_nowait())
    assert sorted(collected) == [
        ("error", "maintenance_busy"),
        ("success", "backup_created"),
    ]
    published = [
        path
        for path in (state_root / "data" / "backups").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(published) == 1
    verify_backup(state_root, published[0])


def test_runtime_state_lease_blocks_all_maintenance_without_side_effects(
    state_root: Path,
) -> None:
    _populate_state(state_root, "idle-runtime")
    source = backup_state(state_root, "runtime_source")
    before = _persistent_tree_snapshot(state_root)

    operations = (
        lambda: backup_state(state_root, "blocked_backup"),
        lambda: reset_state(state_root, "blocked_reset"),
        lambda: restore_state(
            state_root,
            backup=source["backup_id"],
            expected_purpose="runtime_source",
        ),
    )
    with runtime_state_lease(state_root):
        for operation in operations:
            error = _assert_error_code("maintenance_busy", operation)
            assert error.exit_code == 2
            assert _persistent_tree_snapshot(state_root) == before

    report = backup_state(state_root, "after_runtime")
    assert report["code"] == "backup_created"
    verify_backup(
        state_root,
        report["backup_id"],
        expected_purpose="after_runtime",
    )


def test_real_fastapi_lifespan_lease_blocks_cross_process_cli_maintenance(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    _populate_state(state_root, "fastapi-lifespan")
    initial = _persistent_tree_snapshot(state_root)
    events: list[str] = []

    def assert_lifecycle_lease_is_held(stage: str) -> None:
        error = _assert_error_code(
            "maintenance_busy",
            lambda: backup_state(state_root, f"blocked_during_{stage}"),
        )
        assert error.exit_code == 2
        events.append(stage)

    def fake_validate_runtime_database_target(root: Path) -> None:
        assert root == state_root
        assert_lifecycle_lease_is_held("validate")

    def fake_prepare_runtime_directories() -> None:
        assert_lifecycle_lease_is_held("prepare")

    async def fake_create_schema(**kwargs: Any) -> None:
        assert kwargs == {
            "state_lease_held": True,
            "state_root": state_root,
        }
        assert_lifecycle_lease_is_held("create_schema")

    async def fake_verify_database_writable() -> None:
        events.append("verify_writable")

    async def fake_ensure_local_owner() -> None:
        events.append("ensure_owner")

    async def fake_recover_interrupted_deliveries(*, session_factory: Any) -> dict[str, int]:
        assert session_factory is main_module.AsyncSessionLocal
        assert_lifecycle_lease_is_held("recover_outbox")
        return {"fenced_external": 0, "reconciled_workspace": 0}

    async def fake_reconcile_interrupted_runs() -> list[str]:
        events.append("reconcile")
        return []

    async def fake_drain_outbox(stop_event: asyncio.Event, *, session_factory: Any) -> None:
        assert session_factory is main_module.AsyncSessionLocal
        await stop_event.wait()

    class FakeScheduler:
        def start(self) -> None:
            events.append("scheduler_start")

        async def stop(self) -> None:
            events.append("scheduler_stop")

    monkeypatch.setattr(main_module, "PROJECT_ROOT", state_root)
    monkeypatch.setattr(
        main_module,
        "validate_runtime_database_target",
        fake_validate_runtime_database_target,
    )
    monkeypatch.setattr(
        main_module,
        "prepare_runtime_directories",
        fake_prepare_runtime_directories,
    )
    monkeypatch.setattr(main_module, "create_schema", fake_create_schema)
    monkeypatch.setattr(
        main_module,
        "verify_database_writable",
        fake_verify_database_writable,
    )
    monkeypatch.setattr(main_module, "ensure_local_owner", fake_ensure_local_owner)
    monkeypatch.setattr(
        main_module,
        "recover_interrupted_deliveries",
        fake_recover_interrupted_deliveries,
    )
    monkeypatch.setattr(
        main_module,
        "reconcile_interrupted_runs",
        fake_reconcile_interrupted_runs,
    )
    monkeypatch.setattr(main_module, "drain_outbox", fake_drain_outbox)
    monkeypatch.setattr(main_module, "proactive_scheduler", FakeScheduler())

    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts/data-maintenance.py"),
        "--root",
        str(state_root),
        "backup",
        "--purpose",
        "lifespan_probe",
    ]
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        "LC_ALL": "C",
        "TZ": "UTC",
    }

    async def run_during_lifespan() -> subprocess.CompletedProcess[str]:
        async with main_module.app.router.lifespan_context(main_module.app):
            assert events == [
                "validate",
                "prepare",
                "create_schema",
                "verify_writable",
                "ensure_owner",
                "recover_outbox",
                "reconcile",
                "scheduler_start",
            ]
            before = _persistent_tree_snapshot(state_root)
            blocked_result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert _persistent_tree_snapshot(state_root) == before
            return blocked_result

    blocked = asyncio.run(run_during_lifespan())
    assert blocked.returncode == 2, blocked.stderr
    assert json.loads(blocked.stderr)["code"] == "maintenance_busy"
    assert events[-1] == "scheduler_stop"
    assert _persistent_tree_snapshot(state_root) == initial

    released = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert released.returncode == 0, released.stderr
    assert json.loads(released.stdout)["code"] == "backup_created"


def test_runtime_state_lease_recovers_incomplete_backup_before_yield(
    state_root: Path,
) -> None:
    pending = (
        state_root
        / "data/backups"
        / f".incomplete-runtime_recovery-20990101T000000.000000Z-{'2' * 32}"
    )
    pending.mkdir(parents=True)
    (pending / "sentinel").write_bytes(b"interrupted backup")

    with runtime_state_lease(state_root):
        assert not pending.exists()

    assert not pending.exists()


@pytest.mark.parametrize("journal_kind", ("missing", "dangling_symlink"))
def test_startup_never_discards_rollback_data_without_a_durable_journal(
    state_root: Path,
    journal_kind: str,
) -> None:
    _populate_state(state_root, "only-rollback-copy")
    database = state_root / DATABASE_PATHS[0]
    original_hash = _sha256_bytes(database.read_bytes())
    operation = state_root / "data/.maintenance/operations" / ("b" * 32)
    candidate = operation / "candidate"
    rollback_database = operation / "rollback" / DATABASE_PATHS[0]
    candidate.mkdir(parents=True)
    rollback_database.parent.mkdir(parents=True)
    os.replace(database, rollback_database)
    if journal_kind == "dangling_symlink":
        (operation / "journal.json").symlink_to(operation / "missing-journal-target")
    before = _persistent_tree_snapshot(state_root)

    with pytest.raises(MaintenanceError) as caught:
        with runtime_state_lease(state_root):
            raise AssertionError("startup yielded with ambiguous rollback state")

    assert caught.value.code == "recovery_required"
    assert caught.value.exit_code == 4
    assert _persistent_tree_snapshot(state_root) == before
    assert not database.exists()
    assert _sha256_bytes(rollback_database.read_bytes()) == original_hash


def test_coordinated_mutation_backs_up_then_releases_only_sqlite_guards(
    state_root: Path,
) -> None:
    _populate_state(state_root, "before-mutation")
    before = _managed_state_snapshot(state_root)

    with coordinated_state_mutation(state_root, "pre_rebuild") as ready:
        assert ready["code"] == "mutation_ready"
        assert isinstance(ready["backup_id"], str)
        backup_path = _backup_path(state_root, ready["backup_id"])
        manifest = verify_backup(
            state_root,
            ready["backup_id"],
            expected_purpose="pre_rebuild",
        )
        assert _backup_payload_snapshot(backup_path, manifest) == before

        # The snapshot's BEGIN IMMEDIATE guards must be gone before control is
        # yielded, otherwise a coordinated writer could not do its actual job.
        with sqlite3.connect(state_root / DATABASE_PATHS[0], timeout=0) as connection:
            connection.execute(
                "UPDATE fixture_marker SET value = ? WHERE position = 1",
                ("after-mutation",),
            )
        (state_root / "data/context/plans/active.json").write_text(
            '{"marker":"after-mutation"}\n',
            encoding="utf-8",
        )

        after_mutation = _persistent_tree_snapshot(state_root)
        error = _assert_error_code(
            "maintenance_busy",
            lambda: backup_state(state_root, "nested_maintenance"),
        )
        assert error.exit_code == 2
        assert _persistent_tree_snapshot(state_root) == after_mutation

    assert _managed_state_snapshot(state_root) != before
    assert _backup_payload_snapshot(backup_path, manifest) == before
    after = backup_state(state_root, "post_rebuild")
    assert after["code"] == "backup_created"


@pytest.mark.parametrize("pending_kind", ("operation", "backup"))
def test_dry_run_and_preflight_refuse_pending_recovery_without_mutation(
    state_root: Path,
    pending_kind: str,
) -> None:
    _populate_state(state_root, "pending-recovery")
    source = backup_state(state_root, "pending_source")
    if pending_kind == "operation":
        pending = state_root / "data/.maintenance/operations" / ("a" * 32)
        pending.mkdir(parents=True)
        (pending / "journal.json").write_bytes(b"synthetic pending operation\n")
    else:
        pending = state_root / "data/backups/.incomplete-synthetic"
        pending.mkdir()
        (pending / "sentinel").write_bytes(b"synthetic incomplete backup\n")
    before = _persistent_tree_snapshot(state_root)

    operations = (
        lambda: backup_state(state_root, "pending_backup", dry_run=True),
        lambda: reset_state(state_root, "pending_reset", dry_run=True),
        lambda: restore_state(
            state_root,
            backup=source["backup_id"],
            expected_purpose="pending_source",
            dry_run=True,
        ),
        lambda: preflight_state(state_root, backup=source["backup_id"]),
    )
    for operation in operations:
        error = _assert_error_code("recovery_required", operation)
        assert error.exit_code == 4
        assert _persistent_tree_snapshot(state_root) == before


def test_all_dry_runs_have_zero_persistent_side_effects(state_root: Path) -> None:
    _populate_state(state_root, "dry-run")
    source_report = backup_state(state_root, "dry_run_source")
    before = _persistent_tree_snapshot(state_root)

    reports = (
        backup_state(state_root, "dry_run_backup", dry_run=True),
        reset_state(state_root, "dry_run_reset", dry_run=True),
        restore_state(
            state_root,
            backup=source_report["backup_id"],
            expected_purpose="dry_run_source",
            dry_run=True,
        ),
    )

    assert all(report["code"] == "dry_run_ok" for report in reports)
    assert all(report["dry_run"] is True for report in reports)
    assert _persistent_tree_snapshot(state_root) == before


def test_empty_root_dry_runs_do_not_create_runtime_paths(state_root: Path) -> None:
    before = _persistent_tree_snapshot(state_root)

    reports = (
        backup_state(state_root, "empty_backup", dry_run=True),
        reset_state(state_root, "empty_reset", dry_run=True),
        preflight_state(state_root),
    )

    assert [report["code"] for report in reports] == [
        "no_state",
        "no_state",
        "preflight_ok",
    ]
    assert all(report["dry_run"] is True for report in reports)
    assert _persistent_tree_snapshot(state_root) == before


def test_migration_backup_cli_verify_and_restore_use_only_explicit_paths(
    state_root: Path,
) -> None:
    from app.db.migrations import backup_sqlite_database, migrate_sqlite_database

    database = state_root / DATABASE_PATHS[0]
    database.parent.mkdir(parents=True)
    migrate_sqlite_database(
        database,
        backup_root=state_root / "migration-bootstrap-backups",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    backup_path = backup_sqlite_database(
        database,
        backup_root=state_root / "migration-backups",
        purpose="pre_backfill",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES (?, ?, ?)",
            ("post-backup-owner", "Post Backup", "UTC"),
        )

    base_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts/data-maintenance.py"),
        "--root",
        str(state_root),
    ]
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    with runtime_state_lease(state_root):
        blocked = subprocess.run(
            [*base_command, "migration-backup-verify", "--backup", str(backup_path)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert blocked.returncode == 2, blocked.stderr
        assert json.loads(blocked.stderr)["code"] == "maintenance_busy"

    verified = subprocess.run(
        [*base_command, "migration-backup-verify", "--backup", str(backup_path)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["code"] == "migration_backup_verified"

    restored = subprocess.run(
        [
            *base_command,
            "migration-backup-restore",
            "--database",
            str(database),
            "--backup",
            str(backup_path),
            "--safety-backup-root",
            str(state_root / "migration-safety-backups"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr
    restored_report = json.loads(restored.stdout)
    assert restored_report["code"] == "migration_backup_restored"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owners WHERE id = 'post-backup-owner'"
        ).fetchone() == (0,)

    safety_backup = Path(restored_report["safety_backup_path"])
    rollback = subprocess.run(
        [
            *base_command,
            "migration-backup-restore",
            "--database",
            str(database),
            "--backup",
            str(safety_backup),
            "--safety-backup-root",
            str(state_root / "migration-rollback-safety-backups"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rollback.returncode == 0, rollback.stderr
    assert json.loads(rollback.stdout)["code"] == "migration_backup_restored"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owners WHERE id = 'post-backup-owner'"
        ).fetchone() == (1,)

    missing = subprocess.run(
        [
            *base_command,
            "migration-backup-verify",
            "--backup",
            str(state_root / "missing-migration-backup"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert missing.returncode == 3
    assert json.loads(missing.stderr)["code"] == "backup_missing"


def test_migration_restore_cannot_lock_a_decoy_root_and_replace_live_database(
    state_root: Path,
) -> None:
    from app.db.migrations import backup_sqlite_database, migrate_sqlite_database

    database = state_root / DATABASE_PATHS[0]
    database.parent.mkdir(parents=True)
    migrate_sqlite_database(
        database,
        backup_root=state_root / "migration-bootstrap-backups",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    backup_path = backup_sqlite_database(
        database,
        backup_root=state_root / "migration-backups",
        purpose="pre_backfill",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES (?, ?, ?)",
            ("must-survive", "Must Survive", "UTC"),
        )

    decoy_root = state_root.parent / "decoy-repository"
    decoy_root.mkdir()
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts/data-maintenance.py"),
        "--root",
        str(decoy_root),
        "migration-backup-restore",
        "--database",
        str(database),
        "--backup",
        str(backup_path),
        "--safety-backup-root",
        str(state_root / "migration-safety-backups"),
    ]
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    with runtime_state_lease(state_root):
        rejected = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert rejected.returncode == 3, rejected.stderr
    assert json.loads(rejected.stderr)["code"] == "unmanaged_database_path"
    assert not (state_root / "migration-safety-backups").exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owners WHERE id = 'must-survive'"
        ).fetchone() == (1,)


def test_migration_restore_identity_is_portable_across_repository_roots(
    state_root: Path,
) -> None:
    from app.db.migrations import backup_sqlite_database, migrate_sqlite_database

    source_database = state_root / DATABASE_PATHS[0]
    source_database.parent.mkdir(parents=True)
    migrate_sqlite_database(
        source_database,
        backup_root=state_root / "migration-bootstrap-backups",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    with sqlite3.connect(source_database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES (?, ?, ?)",
            ("portable-source", "Portable Source", "UTC"),
        )
    backup_path = backup_sqlite_database(
        source_database,
        backup_root=state_root / "migration-backups",
        purpose="pre_backfill",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )

    target_root = state_root.parent / "second-checkout"
    target_database = target_root / DATABASE_PATHS[0]
    target_database.parent.mkdir(parents=True)
    migrate_sqlite_database(
        target_database,
        backup_root=target_root / "migration-bootstrap-backups",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    with sqlite3.connect(target_database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES (?, ?, ?)",
            ("target-only", "Target Only", "UTC"),
        )

    restored = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/data-maintenance.py"),
            "--root",
            str(target_root),
            "migration-backup-restore",
            "--database",
            str(target_database),
            "--backup",
            str(backup_path),
            "--safety-backup-root",
            str(target_root / "migration-safety-backups"),
        ],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["code"] == "migration_backup_restored"
    with sqlite3.connect(target_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owners WHERE id = 'portable-source'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM owners WHERE id = 'target-only'"
        ).fetchone() == (0,)


def test_migration_restore_rejects_symlink_and_different_managed_target(
    state_root: Path,
) -> None:
    from app.db.migrations import backup_sqlite_database, migrate_sqlite_database

    database = state_root / DATABASE_PATHS[0]
    database.parent.mkdir(parents=True)
    migrate_sqlite_database(
        database,
        backup_root=state_root / "migration-bootstrap-backups",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    backup_path = backup_sqlite_database(
        database,
        backup_root=state_root / "migration-backups",
        purpose="pre_backfill",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        "LC_ALL": "C",
        "TZ": "UTC",
    }

    wrong_target = state_root / DATABASE_PATHS[3]
    wrong_target.parent.mkdir(parents=True)
    shutil.copy2(database, wrong_target)
    wrong_before = _database_family_snapshot(wrong_target)
    wrong_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/data-maintenance.py"),
            "--root",
            str(state_root),
            "migration-backup-restore",
            "--database",
            str(wrong_target),
            "--backup",
            str(backup_path),
            "--safety-backup-root",
            str(state_root / "migration-safety-backups"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert wrong_result.returncode == 3, wrong_result.stderr
    assert json.loads(wrong_result.stderr)["code"] == "backup_target_mismatch"
    assert _database_family_snapshot(wrong_target) == wrong_before
    assert not (state_root / "migration-safety-backups").exists()

    wrong_target.unlink()
    symlink_target = state_root / DATABASE_PATHS[2]
    symlink_target.parent.mkdir(parents=True, exist_ok=True)
    symlink_target.symlink_to(database)
    symlink_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/data-maintenance.py"),
            "--root",
            str(state_root),
            "migration-backup-restore",
            "--database",
            str(symlink_target),
            "--backup",
            str(backup_path),
            "--safety-backup-root",
            str(state_root / "migration-safety-backups"),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert symlink_result.returncode == 3, symlink_result.stderr
    assert json.loads(symlink_result.stderr)["code"] in {
        "unsafe_database_path",
        "unsafe_state_path",
    }
    assert symlink_target.is_symlink()
    assert not (state_root / "migration-safety-backups").exists()


def test_migration_backup_verify_does_not_open_corrupt_live_database(
    state_root: Path,
) -> None:
    from app.db.migrations import backup_sqlite_database, migrate_sqlite_database

    database = state_root / DATABASE_PATHS[0]
    database.parent.mkdir(parents=True)
    migrate_sqlite_database(
        database,
        backup_root=state_root / "migration-bootstrap-backups",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    backup_path = backup_sqlite_database(
        database,
        backup_root=state_root / "migration-backups",
        purpose="pre_backfill",
        application_version="1.1.1",
        database_identity=DATABASE_PATHS[0],
    )
    database.write_bytes(b"physically corrupt live database")
    before = _database_family_snapshot(database)

    verified = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/data-maintenance.py"),
            "--root",
            str(state_root),
            "migration-backup-verify",
            "--backup",
            str(backup_path),
        ],
        cwd=PROJECT_ROOT,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["code"] == "migration_backup_verified"
    assert _database_family_snapshot(database) == before
