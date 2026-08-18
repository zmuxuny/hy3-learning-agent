"""H1 acceptance tests for the crash-safe SQLite migration protocol.

Every database in this module is created below ``tmp_path`` (or pytest's
module-scoped temporary root).  The checked-in SQL files are immutable source
text; they are never opened by SQLite and no configured/runtime database is
consulted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import app.db.migrations as migration_module
from app.core.paths import lexical_absolute
from app.db.migrations import (
    CANONICAL_SCHEMA_CHECKSUM,
    LEGACY_SCHEMA_REGISTRY,
    MIGRATION_REGISTRY,
    MigrationError,
    MigrationRevision,
    backup_sqlite_database,
    migrate_sqlite_database,
    restore_migration_backup,
    schema_checksum,
    verify_sqlite_database,
    verify_migration_backup,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "databases"
FROZEN_H1_SCHEMA_CHECKSUM = (
    "e7130a9013e7bd4754c3318520d9101f18d18b88c11e8ebbe7c965ead4c5e293"
)

CANONICAL_TABLES = {
    "achievements",
    "activity_days",
    "agent_runs",
    "artifacts",
    "calendar_events",
    "chat_message_revisions",
    "chat_messages",
    "competencies",
    "competency_edges",
    "context_snapshots",
    "evidence_observations",
    "learning_events",
    "learning_resources",
    "memories",
    "notifications",
    "operations",
    "owners",
    "plan_competency_links",
    "plan_proposals",
    "planning_intakes",
    "plans",
    "push_subscriptions",
    "queued_messages",
    "quizzes",
    "resource_competency_links",
    "review_schedules",
    "run_events",
    "run_steer_messages",
    "schema_migrations",
    "session_plan_links",
    "session_summaries",
    "sessions",
    "stages",
    "task_competency_links",
    "task_submissions",
    "tasks",
    "tool_invocations",
    "user_profiles",
}

FROZEN_PARTIAL_M13_OVERLAY_SQL = """
CREATE TABLE evidence_observations (
    id INTEGER PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_id VARCHAR(120) NOT NULL,
    run_id VARCHAR(64),
    session_id VARCHAR(64),
    plan_id INTEGER,
    task_id INTEGER,
    competency_key VARCHAR(160),
    outcome VARCHAR(40) NOT NULL,
    normalized_score FLOAT,
    is_correct BOOLEAN,
    assistance_level VARCHAR(24) NOT NULL DEFAULT 'unknown',
    transfer_level VARCHAR(24) NOT NULL DEFAULT 'unknown',
    rubric_snapshot JSON NOT NULL DEFAULT '{}',
    evaluator JSON NOT NULL DEFAULT '{}',
    artifact_refs JSON NOT NULL DEFAULT '[]',
    payload JSON NOT NULL DEFAULT '{}',
    occurred_at DATETIME NOT NULL,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    schema_version INTEGER NOT NULL DEFAULT 1,
    correlation_id VARCHAR(120),
    causation_id VARCHAR(120),
    idempotency_key VARCHAR(180) NOT NULL UNIQUE,
    supersedes_id INTEGER,
    invalidated_at DATETIME,
    invalidation_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(owner_id) REFERENCES owners(id),
    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE SET NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE SET NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL,
    FOREIGN KEY(supersedes_id) REFERENCES evidence_observations(id) ON DELETE SET NULL
);
INSERT INTO evidence_observations (
    id, owner_id, source_type, source_id, run_id, session_id, plan_id, task_id,
    competency_key, outcome, normalized_score, is_correct, assistance_level,
    transfer_level, rubric_snapshot, evaluator, artifact_refs, payload,
    occurred_at, recorded_at, schema_version, correlation_id, causation_id,
    idempotency_key, invalidation_reason
) VALUES (
    701, 'fixture-owner', 'task_submission', 'fixture-partial-evidence',
    'fixture-run', 'fixture-session', 1, 1, 'fixture.legacy', 'passed', 0.875, 1,
    'hint', 'near', '{"criteria":["synthetic"]}', '{"kind":"fixture"}',
    '[{"kind":"fixture-artifact"}]', '{"attempt":1}',
    '2026-01-03 09:25:00.123456', '2026-01-03 09:25:01.654321', 1,
    'fixture-correlation', 'fixture-causation', 'fixture-partial-evidence-key', ''
);
"""
FROZEN_PARTIAL_M13_OVERLAY_SHA256 = (
    "c541108778e532709301d2273b84564093e4a068177fbf99b8e146364d63737d"
)

KILL_PHASES = (
    "before_create",
    "after_create",
    "before_copy",
    "after_copy",
    "before_indexes",
    "after_indexes",
    "before_history",
    "after_history",
)

KILLPOINT_PROGRAM = r"""
import os
import signal
import sys
from pathlib import Path

from app.db.migrations import migrate_sqlite_database

database_path = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
kill_phase = sys.argv[3]

def terminate_at(point: str) -> None:
    if point == kill_phase:
        os.kill(os.getpid(), signal.SIGKILL)

migrate_sqlite_database(
    database_path,
    backup_root=backup_root,
    application_version="h1-killpoint-test",
    fault_injector=terminate_at,
)
raise SystemExit(97)
"""

BACKUP_KILLPOINT_PROGRAM = r"""
import os
import signal
import sys
from pathlib import Path

from app.db.migrations import backup_sqlite_database

database_path = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
kill_phase = sys.argv[3]

def terminate_at(point: str) -> None:
    if point == kill_phase:
        os.kill(os.getpid(), signal.SIGKILL)

backup_sqlite_database(
    database_path,
    backup_root=backup_root,
    application_version="h1-backup-killpoint-test",
    fault_injector=terminate_at,
)
raise SystemExit(97)
"""

CONCURRENT_PROGRAM = r"""
import json
import sys
from pathlib import Path

from app.db.migrations import MigrationError, migrate_sqlite_database, report_dict

report = migrate_sqlite_database(
    Path(sys.argv[1]),
    backup_root=Path(sys.argv[2]),
    application_version="h1-concurrency-test",
)
print(json.dumps(report_dict(report), sort_keys=True), flush=True)
"""

CRASHED_WAL_WRITER_PROGRAM = r"""
import os
import signal
import sqlite3
import sys
from pathlib import Path

database_path = Path(sys.argv[1])
ready_marker = Path(sys.argv[2])
writer = sqlite3.connect(database_path, isolation_level=None)
assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
writer.execute("PRAGMA wal_autocheckpoint=0")
writer.execute("BEGIN IMMEDIATE")
writer.execute(
    "UPDATE owners SET display_name=? WHERE id=?",
    ("WAL fixture learner", "fixture-owner"),
)
writer.commit()
assert database_path.with_name(database_path.name + "-wal").is_file()
assert database_path.with_name(database_path.name + "-shm").is_file()
ready_marker.write_text("committed-wal-before-crash\n", encoding="ascii")
with ready_marker.open("rb") as stream:
    os.fsync(stream.fileno())
os.kill(os.getpid(), signal.SIGKILL)
"""

CONCURRENT_SQLITE_WRITER_PROGRAM = r"""
import sqlite3
import sys
from pathlib import Path

database_path = Path(sys.argv[1])
display_name = sys.argv[2]
connection = sqlite3.connect(database_path, timeout=0, isolation_level=None)
try:
    connection.execute("PRAGMA busy_timeout=0")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE owners SET display_name=? WHERE id=?",
        (display_name, "fixture-owner"),
    )
    connection.commit()
except sqlite3.OperationalError as exc:
    connection.rollback()
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        print("blocked", flush=True)
        raise SystemExit(75)
    raise
finally:
    connection.close()
print("committed", flush=True)
"""

MIGRATOR_PAUSE_AT_PHASE_PROGRAM = r"""
import json
import os
import sys
import time
from pathlib import Path

from app.db.migrations import MigrationError, migrate_sqlite_database, report_dict

database_path = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
pause_phase = sys.argv[3]
ready_marker = Path(sys.argv[4])
release_marker = Path(sys.argv[5])

def pause_at(point: str) -> None:
    if point != pause_phase:
        return
    ready_marker.write_text(point + "\n", encoding="ascii")
    with ready_marker.open("rb") as stream:
        os.fsync(stream.fileno())
    while not release_marker.exists():
        time.sleep(0.01)

try:
    report = migrate_sqlite_database(
        database_path,
        backup_root=backup_root,
        application_version="h1-paused-migrator-test",
        fault_injector=pause_at,
    )
except MigrationError as exc:
    print(json.dumps({
        "kind": "error",
        "code": exc.code,
        "recovery_backup": (
            str(exc.recovery_backup) if exc.recovery_backup is not None else None
        ),
    }, sort_keys=True), flush=True)
else:
    print(json.dumps({
        "kind": "report",
        "report": report_dict(report),
    }, sort_keys=True), flush=True)
"""

WAL_PUBLICATION_KILL_PHASES = (
    "after_journal_canonicalization",
    "after_main_replace",
    "after_sidecar_cleanup",
    "after_publish_fsync",
    "after_publish_verify",
    "after_publish",
)

ROLLBACK_REPLACE_KILL_PROGRAM = r"""
import os
import signal
import sys
from pathlib import Path

from app.db.migrations import migrate_sqlite_database

database_path = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
kill_phase = sys.argv[3]

def reject_then_kill_rollback(point: str) -> None:
    if point == kill_phase:
        os.kill(os.getpid(), signal.SIGKILL)
    if point == "after_publish":
        raise RuntimeError("synthetic post-publish rejection")

migrate_sqlite_database(
    database_path,
    backup_root=backup_root,
    application_version="h1-rollback-killpoint-test",
    fault_injector=reject_then_kill_rollback,
)
raise SystemExit(97)
"""

RESTORE_ROLLBACK_REPLACE_KILL_PROGRAM = r"""
import os
import signal
import sys
from pathlib import Path

from app.db.migrations import restore_migration_backup

database_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
safety_backup_root = Path(sys.argv[3])
kill_phase = sys.argv[4]

def reject_then_kill_rollback(point: str) -> None:
    if point == kill_phase:
        os.kill(os.getpid(), signal.SIGKILL)
    if point == "after_publish_verify":
        raise RuntimeError("synthetic published-restore rejection")

restore_migration_backup(
    database_path,
    backup_path,
    safety_backup_root=safety_backup_root,
    application_version="h1-restore-rollback-killpoint-test",
    fault_injector=reject_then_kill_rollback,
)
raise SystemExit(97)
"""

RESTORE_DIRECT_KILLPOINT_PROGRAM = r"""
import os
import signal
import sys
from pathlib import Path

from app.db.migrations import restore_migration_backup

database_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
safety_backup_root = Path(sys.argv[3])
kill_phase = sys.argv[4]

def terminate_at(point: str) -> None:
    if point == kill_phase:
        os.kill(os.getpid(), signal.SIGKILL)

restore_migration_backup(
    database_path,
    backup_path,
    safety_backup_root=safety_backup_root,
    application_version="h1-restore-publish-killpoint-test",
    fault_injector=terminate_at,
)
raise SystemExit(97)
"""

RESTORER_PAUSE_AT_PHASE_PROGRAM = r"""
import json
import os
import sys
import time
from pathlib import Path

from app.db.migrations import MigrationError, restore_migration_backup

database_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
safety_backup_root = Path(sys.argv[3])
pause_phase = sys.argv[4]
ready_marker = Path(sys.argv[5])
release_marker = Path(sys.argv[6])

def pause_at(point: str) -> None:
    if point != pause_phase:
        return
    ready_marker.write_text(point + "\n", encoding="ascii")
    with ready_marker.open("rb") as stream:
        os.fsync(stream.fileno())
    while not release_marker.exists():
        time.sleep(0.01)

try:
    result = restore_migration_backup(
        database_path,
        backup_path,
        safety_backup_root=safety_backup_root,
        application_version="h1-paused-restorer-test",
        fault_injector=pause_at,
    )
except MigrationError as exc:
    print(json.dumps({
        "kind": "error",
        "code": exc.code,
        "recovery_backup": (
            str(exc.recovery_backup) if exc.recovery_backup is not None else None
        ),
    }, sort_keys=True), flush=True)
else:
    print(json.dumps({
        "kind": "result",
        "result": result,
    }, sort_keys=True), flush=True)
"""

ABSENT_DATABASE_CREATOR_PROGRAM = r"""
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
try:
    with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
        descriptor = -1
        while chunk := input_stream.read(1024 * 1024):
            output_stream.write(chunk)
        output_stream.flush()
        os.fsync(output_stream.fileno())
finally:
    if descriptor >= 0:
        os.close(descriptor)
directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
print("created", flush=True)
"""

ATOMIC_DATABASE_REPLACER_PROGRAM = r"""
import os
import sys
from pathlib import Path

replacement = Path(sys.argv[1])
target = Path(sys.argv[2])
os.replace(replacement, target)
directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
print("replaced", flush=True)
"""

RESTORE_DIRECT_KILL_PHASES = (
    "after_main_replace",
    "after_sidecar_cleanup",
    "after_publish_fsync",
    "after_publish_verify",
)

BACKUP_PUBLICATION_KILL_PHASES = (
    "after_backup_staging_create",
    "after_backup_payload_fsync",
    "after_backup_metadata_fsync",
    "before_backup_publish",
    "after_backup_publish",
    "after_backup_root_fsync",
    "after_backup_verify",
)


@pytest.fixture(autouse=True)
def clean_database() -> None:
    """Shadow the suite fixture: this module owns only explicit temp files."""

    yield


@dataclass(frozen=True)
class BusinessSnapshot:
    columns: dict[str, tuple[tuple[str, str], ...]]
    cardinalities: dict[str, int]
    content_digests: dict[str, str]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _materialize_sql(name: str, target: Path) -> Path:
    source = FIXTURE_ROOT / f"{name}.sql"
    if not source.is_file():
        raise RuntimeError(f"missing public SQL fixture: {source}")
    before = _sha256_bytes(source.read_bytes())
    if target.exists():
        raise RuntimeError(f"refusing to overwrite fixture target: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as connection:
        connection.executescript(source.read_text(encoding="utf-8"))
    if _sha256_bytes(source.read_bytes()) != before:
        raise RuntimeError(f"public SQL fixture changed while materializing: {source}")
    return target


def _materialize_partial_m13(target: Path) -> Path:
    _materialize_sql("v1_1_1_full", target)
    overlay_bytes = FROZEN_PARTIAL_M13_OVERLAY_SQL.encode("utf-8")
    if _sha256_bytes(overlay_bytes) != FROZEN_PARTIAL_M13_OVERLAY_SHA256:
        raise RuntimeError("frozen partial-M13 SQL overlay changed without review")
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(FROZEN_PARTIAL_M13_OVERLAY_SQL)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"partial-M13 harness fixture has foreign-key violations: {violations}")
    return target


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _canonical_datetime_token(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _value_token(value: Any, declared_type: str) -> list[Any]:
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    normalized_type = declared_type.upper()
    if value is not None and "DATETIME" in normalized_type:
        return ["datetime", _canonical_datetime_token(str(value))]
    if value is not None and "JSON" in normalized_type:
        parsed = json.loads(value) if isinstance(value, str) else value
        return ["json", parsed]
    return [type(value).__name__, value]


def _business_snapshot(
    path: Path,
    *,
    columns: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> BusinessSnapshot:
    with sqlite3.connect(path) as connection:
        table_names = sorted(_table_names(connection).difference({"schema_migrations"}))
        if columns is not None:
            table_names = sorted(columns)
        resolved: dict[str, tuple[tuple[str, str], ...]] = {}
        cardinalities: dict[str, int] = {}
        content_digests: dict[str, str] = {}
        for table_name in table_names:
            table_info = {
                row[1]: row[2]
                for row in connection.execute(f'PRAGMA table_xinfo("{table_name}")')
                if row[6] == 0
            }
            selected = (
                tuple(table_info.items()) if columns is None else columns[table_name]
            )
            missing = [name for name, _type in selected if name not in table_info]
            if missing:
                raise RuntimeError(f"migration dropped legacy columns from {table_name}: {missing}")
            resolved[table_name] = selected
            projection = ", ".join(f'"{name}"' for name, _type in selected)
            rows = connection.execute(
                f'SELECT {projection} FROM "{table_name}"'
            ).fetchall()
            encoded_rows = sorted(
                json.dumps(
                    [
                        _value_token(value, declared_type)
                        for value, (_name, declared_type) in zip(row, selected, strict=True)
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for row in rows
            )
            cardinalities[table_name] = len(rows)
            content_digests[table_name] = _sha256_bytes(
                "\n".join(encoded_rows).encode("utf-8")
            )
    return BusinessSnapshot(resolved, cardinalities, content_digests)


def _file_state(database_path: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = database_path.with_name(database_path.name + suffix)
        if candidate.exists():
            payload = candidate.read_bytes()
            result[suffix] = (len(payload), _sha256_bytes(payload))
    return result


def _filesystem_tree_state(root: Path) -> dict[str, tuple[str, int, str | None]]:
    """Snapshot names, kinds, modes, and file bytes without following links."""

    paths = [root, *sorted(root.rglob("*"))]
    result: dict[str, tuple[str, int, str | None]] = {}
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        stat_result = path.lstat()
        mode = stat_result.st_mode & 0o777
        if path.is_symlink():
            result[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            result[relative] = ("directory", mode, None)
        else:
            assert path.is_file()
            result[relative] = ("file", mode, _sha256_bytes(path.read_bytes()))
    return result


def _backup_directories(backup_root: Path) -> tuple[Path, ...]:
    if not backup_root.exists():
        return ()
    return tuple(
        sorted(
            path
            for path in backup_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    )


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _assert_private_backup_tree(backup_root: Path, backup_path: Path) -> None:
    assert _mode(backup_root) == 0o700
    assert _mode(backup_path) == 0o700
    assert _mode(backup_path / "payload") == 0o700
    assert _mode(backup_path / "payload" / "database.sqlite3") == 0o600
    assert _mode(backup_path / "manifest.json") == 0o600
    assert _mode(backup_path / "COMPLETE") == 0o600


def _assert_private_directory_tree(root: Path) -> None:
    assert root.is_dir() and not root.is_symlink()
    assert _mode(root) == 0o700
    for path in root.rglob("*"):
        assert not path.is_symlink()
        if path.is_dir():
            assert _mode(path) == 0o700
        else:
            assert path.is_file()
            assert _mode(path) == 0o600


def _history_rows(database_path: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT version, name, checksum, applied_at, result "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()


def _rewrite_backup_manifest_for_payload(backup_path: Path) -> dict[str, Any]:
    payload_path = backup_path / "payload" / "database.sqlite3"
    payload_bytes = payload_path.read_bytes()
    payload_hash = _sha256_bytes(payload_bytes)
    with sqlite3.connect(payload_path) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_key_count = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["size_bytes"] = len(payload_bytes)
    manifest["files"][0]["sha256"] = payload_hash
    manifest["files"][0]["integrity"] = integrity
    manifest["files"][0]["foreign_key_violation_count"] = foreign_key_count
    manifest["payload_sha256"] = payload_hash
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    manifest_path.write_bytes(manifest_bytes)
    (backup_path / "COMPLETE").write_text(
        _sha256_bytes(manifest_bytes) + "\n",
        encoding="ascii",
    )
    return manifest


def _health(database_path: Path) -> tuple[list[str], list[tuple[Any, ...]], int]:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    return integrity, foreign_keys, user_version


def _tamper_owner_data_without_changing_schema(
    database_path: Path,
    marker: str,
) -> tuple[str, str]:
    """Change one synthetic business value and prove the mutation is data-only."""

    before_hash = _sha256_bytes(database_path.read_bytes())
    before_schema = schema_checksum(database_path)
    before_business = _business_snapshot(database_path)
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            (marker, "fixture-owner"),
        )
        assert cursor.rowcount == 1
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id=?",
            ("fixture-owner",),
        ).fetchone() == (marker,)
    after_hash = _sha256_bytes(database_path.read_bytes())
    after_business = _business_snapshot(database_path)
    assert after_hash != before_hash
    assert schema_checksum(database_path) == before_schema
    assert after_business.columns == before_business.columns
    assert after_business.cardinalities == before_business.cardinalities
    assert after_business.content_digests["owners"] != (
        before_business.content_digests["owners"]
    )
    assert {
        table_name: digest
        for table_name, digest in after_business.content_digests.items()
        if table_name != "owners"
    } == {
        table_name: digest
        for table_name, digest in before_business.content_digests.items()
        if table_name != "owners"
    }
    assert _health(database_path)[:2] == (["ok"], [])
    return before_hash, after_hash


def _subprocess_environment(config_database: Path) -> dict[str, str]:
    # Do not inherit API keys or mailbox credentials into fault-injection
    # children.  Settings may still read .env, so every secret field is
    # explicitly overridden with a harmless value.
    return {
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{config_database}",
        "OPENAI_API_KEY": "",
        "SMTP_PASSWORD": "",
        "IMAP_PASSWORD": "",
        "VAPID_PRIVATE_KEY": "",
        "VAPID_PUBLIC_KEY": "",
        "ENABLE_SCHEDULER": "false",
        "ENABLE_EMAIL_REPLY_POLLING": "false",
    }


def _migrate(
    database_path: Path,
    backup_root: Path,
    *,
    database_identity: str | None = None,
):
    return migrate_sqlite_database(
        database_path,
        backup_root=backup_root,
        application_version="h1-protocol-test",
        database_identity=database_identity,
    )


def _run_concurrent_writer(
    database_path: Path,
    display_name: str,
    config_database: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            CONCURRENT_SQLITE_WRITER_PROGRAM,
            str(database_path),
            display_name,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(config_database),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _wait_for_marker(
    process: subprocess.Popen[str],
    marker: Path,
    *,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            return
        returncode = process.poll()
        if returncode is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                "subprocess exited before synchronization marker; "
                f"returncode={returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate()
    raise RuntimeError(
        "timed out waiting for subprocess synchronization marker; "
        f"stdout={stdout!r}, stderr={stderr!r}"
    )


def _assert_verified(database_path: Path, expected_checksum: str) -> None:
    verification = verify_sqlite_database(
        database_path,
        expected_schema_checksum=expected_checksum,
    )
    assert verification == {
        "integrity": ["ok"],
        "foreign_key_violation_count": 0,
        "user_version": 1,
        "schema_checksum": expected_checksum,
    }


@pytest.fixture
def canonical_database(tmp_path: Path) -> Path:
    database_path = _materialize_sql("empty", tmp_path / "canonical.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.applied is True
    assert report.source_kind == "empty"
    return database_path


def test_history_records_version_checksum_timestamp_result_and_user_version(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "history.sqlite3")
    backup_root = tmp_path / "migration-backups"
    before = datetime.now(timezone.utc)

    report = _migrate(database_path, backup_root)

    after = datetime.now(timezone.utc)
    rows = _history_rows(database_path)
    assert len(rows) == 1
    version, name, checksum, applied_at, result = rows[0]
    assert version == 1
    assert name == "h1_canonical_schema"
    assert re.fullmatch(r"[0-9a-f]{64}", checksum)
    assert checksum == FROZEN_H1_SCHEMA_CHECKSUM
    assert checksum == CANONICAL_SCHEMA_CHECKSUM
    assert checksum == schema_checksum(database_path)
    assert isinstance(applied_at, str)
    applied_instant = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
    if applied_instant.tzinfo is None:
        applied_instant = applied_instant.replace(tzinfo=timezone.utc)
    assert before <= applied_instant <= after
    assert result == "applied"
    assert [
        (row[0], row[1], row[2], row[4])
        for row in rows
    ] == [
        (revision.version, revision.name, revision.checksum, "applied")
        for revision in MIGRATION_REGISTRY
    ]
    assert _health(database_path) == (["ok"], [], 1)
    assert report.version == 1
    assert report.target_schema_checksum == checksum
    assert report.backup_path is not None
    assert Path(report.backup_path) in _backup_directories(backup_root)


@pytest.mark.parametrize("process_umask", (0o000, 0o022, 0o077))
def test_migration_artifacts_remain_private_under_each_umask(
    process_umask: int,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "private.sqlite3")
    backup_root = tmp_path / "migration-backups"
    previous_umask = os.umask(process_umask)
    try:
        report = _migrate(database_path, backup_root)
    finally:
        os.umask(previous_umask)

    assert report.backup_path is not None
    backup_path = Path(report.backup_path)
    assert _mode(database_path) == 0o600
    lock_path = database_path.with_name(f".{database_path.name}.migration.lock")
    assert _mode(lock_path) == 0o600
    _assert_private_backup_tree(backup_root, backup_path)
    assert not tuple(backup_root.glob(".staging-*"))
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )
    assert not tuple(database_path.parent.glob(f".{database_path.name}.candidate-*"))
    verify_migration_backup(backup_path)


def test_verify_sqlite_database_checks_checksum_integrity_fk_and_version(
    canonical_database: Path,
) -> None:
    checksum = schema_checksum(canonical_database)

    _assert_verified(canonical_database, checksum)

    with pytest.raises(MigrationError) as raised:
        verify_sqlite_database(canonical_database, expected_schema_checksum="0" * 64)
    assert raised.value.code == "schema_verification_failed"


def test_verify_sqlite_database_detects_check_constraint_corruption(
    canonical_database: Path,
) -> None:
    with sqlite3.connect(canonical_database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE schema_migrations SET result='pending'")
    with sqlite3.connect(canonical_database) as connection:
        direct_integrity = [
            row[0]
            for row in connection.execute("PRAGMA integrity_check")
        ]
    assert direct_integrity != ["ok"]
    before = _file_state(canonical_database)

    with pytest.raises(MigrationError) as raised:
        verify_sqlite_database(canonical_database)

    assert raised.value.code == "sqlite_integrity_failed"
    assert _file_state(canonical_database) == before


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    (
        pytest.param("name", "rewritten_revision", id="name"),
        pytest.param("checksum", "0" * 64, id="checksum"),
        pytest.param("result", "pending", id="result"),
    ),
)
def test_each_registry_history_field_tamper_fails_closed_without_backup(
    column: str,
    tampered_value: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "tampered-history.sqlite3")
    backup_root = tmp_path / "migration-backups"
    _migrate(database_path, backup_root)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        statement = {
            "name": "UPDATE schema_migrations SET name = ?",
            "checksum": "UPDATE schema_migrations SET checksum = ?",
            "result": "UPDATE schema_migrations SET result = ?",
        }[column]
        connection.execute(
            statement,
            (tampered_value,),
        )
    before = _file_state(database_path)
    backup_count = len(_backup_directories(backup_root))

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    expected_code = (
        "sqlite_integrity_failed"
        if column == "result"
        else "migration_checksum_mismatch"
    )
    assert raised.value.code == expected_code
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == before
    assert len(_backup_directories(backup_root)) == backup_count
    field_index = {"name": 1, "checksum": 2, "result": 4}[column]
    assert _history_rows(database_path)[0][field_index] == tampered_value


def test_invalid_history_applied_at_fails_closed_before_backup(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "invalid-time.sqlite3")
    backup_root = tmp_path / "migration-backups"
    _migrate(database_path, backup_root)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET applied_at = ?",
            ("not-a-timestamp",),
        )
    before = _file_state(database_path)
    backup_count = len(_backup_directories(backup_root))

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "migration_history_invalid"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == before
    assert len(_backup_directories(backup_root)) == backup_count


def test_schema_changing_v2_revision_preserves_v1_history_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "registry-upgrade.sqlite3")
    first = _migrate(database_path, tmp_path / "v1-backups")
    source_business = _business_snapshot(database_path)
    original_history = _history_rows(database_path)
    assert original_history[0][:3] == (
        1,
        "h1_canonical_schema",
        FROZEN_H1_SCHEMA_CHECKSUM,
    )
    v2_table_sql = (
        "CREATE TABLE registry_v2_markers ("
        "id INTEGER NOT NULL PRIMARY KEY, "
        "marker TEXT NOT NULL UNIQUE CHECK(length(marker) > 0))"
    )
    schema_oracle = tmp_path / "registry-v2-schema-oracle.sqlite3"
    shutil.copyfile(database_path, schema_oracle)
    with sqlite3.connect(schema_oracle) as connection:
        connection.execute(v2_table_sql)
    v2_checksum = schema_checksum(schema_oracle)
    assert v2_checksum != FROZEN_H1_SCHEMA_CHECKSUM
    schema_v2 = MigrationRevision(
        version=2,
        name="schema_changing_v2_probe",
        checksum=v2_checksum,
    )
    expanded_registry = MIGRATION_REGISTRY + (schema_v2,)
    real_create_tables = migration_module._create_tables

    def create_v2_tables(connection: Any) -> None:
        real_create_tables(connection)
        connection.exec_driver_sql(v2_table_sql)

    monkeypatch.setattr(migration_module, "_create_tables", create_v2_tables)
    monkeypatch.setattr(migration_module, "MIGRATION_REGISTRY", expanded_registry)
    monkeypatch.setattr(
        migration_module,
        "_REVISION_BY_VERSION",
        {revision.version: revision for revision in expanded_registry},
    )
    monkeypatch.setattr(migration_module, "CURRENT_REVISION", schema_v2)
    monkeypatch.setattr(migration_module, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        migration_module,
        "CURRENT_MIGRATION_NAME",
        "schema_changing_v2_probe",
    )
    monkeypatch.setattr(migration_module, "CANONICAL_SCHEMA_CHECKSUM", v2_checksum)

    second = migration_module.migrate_sqlite_database(
        database_path,
        backup_root=tmp_path / "v2-backups",
        application_version="h1-registry-evolution-test",
    )

    upgraded_history = _history_rows(database_path)
    assert first.version == 1
    assert second.applied is True
    assert second.source_kind == "versioned"
    assert second.version == 2
    assert upgraded_history[0] == original_history[0]
    assert upgraded_history[1][0:3] == (
        2,
        "schema_changing_v2_probe",
        v2_checksum,
    )
    assert upgraded_history[1][4] == "applied"
    assert _health(database_path) == (["ok"], [], 2)
    verification = verify_sqlite_database(
        database_path,
        expected_schema_checksum=v2_checksum,
    )
    assert verification["schema_checksum"] == v2_checksum
    assert _business_snapshot(
        database_path,
        columns=source_business.columns,
    ) == source_business
    with sqlite3.connect(database_path) as connection:
        columns = connection.execute(
            "PRAGMA table_xinfo('registry_v2_markers')"
        ).fetchall()
        assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
            ("id", "INTEGER", 1, 1),
            ("marker", "TEXT", 1, 0),
        ]
        connection.execute(
            "INSERT INTO registry_v2_markers (id, marker) VALUES (1, 'retained')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO registry_v2_markers (id, marker) VALUES (2, '')"
            )
    before_noop = _file_state(database_path)

    third = migration_module.migrate_sqlite_database(
        database_path,
        backup_root=tmp_path / "v2-backups",
        application_version="h1-registry-evolution-test",
    )

    assert third.applied is False
    assert third.version == 2
    assert _file_state(database_path) == before_noop
    assert _history_rows(database_path) == upgraded_history
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, marker FROM registry_v2_markers"
        ).fetchall() == [(1, "retained")]


def test_future_history_version_fails_closed_without_backup_or_source_change(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "future-history.sqlite3")
    backup_root = tmp_path / "migration-backups"
    _migrate(database_path, backup_root)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_migrations SET version = 2")
        connection.execute("PRAGMA user_version=2")
    before = _file_state(database_path)
    backup_count = len(_backup_directories(backup_root))

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "future_schema_version"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == before
    assert len(_backup_directories(backup_root)) == backup_count
    assert _history_rows(database_path)[0][0] == 2


def test_schema_tamper_is_detected_by_checksum_and_migration_entrypoint(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "tampered-schema.sqlite3")
    backup_root = tmp_path / "migration-backups"
    _migrate(database_path, backup_root)
    expected_checksum = _history_rows(database_path)[0][2]
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX ix_learning_events_occurred_at")
    tampered_checksum = schema_checksum(database_path)
    before = _file_state(database_path)
    backup_count = len(_backup_directories(backup_root))
    assert tampered_checksum != expected_checksum

    with pytest.raises(MigrationError) as verify_error:
        verify_sqlite_database(
            database_path,
            expected_schema_checksum=expected_checksum,
        )
    assert verify_error.value.code == "schema_verification_failed"

    with pytest.raises(MigrationError) as migration_error:
        _migrate(database_path, backup_root)
    assert migration_error.value.code == "migration_checksum_mismatch"
    assert migration_error.value.recovery_backup is None
    assert _file_state(database_path) == before
    assert len(_backup_directories(backup_root)) == backup_count


def test_empty_database_migrates_once_and_second_run_is_byte_stable(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "empty.sqlite3")
    backup_root = tmp_path / "migration-backups"

    first = _migrate(database_path, backup_root)
    first_state = _file_state(database_path)
    first_history = _history_rows(database_path)
    first_backups = _backup_directories(backup_root)
    second = _migrate(database_path, backup_root)

    assert first.applied is True
    assert first.source_kind == "empty"
    assert first.source_schema_checksum == (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    assert second.applied is False
    assert second.source_kind == "versioned"
    assert second.backup_path is None
    assert second.target_schema_checksum == first.target_schema_checksum
    assert _file_state(database_path) == first_state
    assert _history_rows(database_path) == first_history
    assert _backup_directories(backup_root) == first_backups
    _assert_verified(database_path, first.target_schema_checksum)


def test_v111_full_database_preserves_every_legacy_row_and_second_run_is_noop(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "v1.sqlite3")
    backup_root = tmp_path / "migration-backups"
    before_business = _business_snapshot(database_path)
    assert len(before_business.cardinalities) == 30
    assert set(before_business.cardinalities.values()) == {1}

    first = _migrate(database_path, backup_root)
    after_business = _business_snapshot(
        database_path,
        columns=before_business.columns,
    )
    first_state = _file_state(database_path)
    first_history = _history_rows(database_path)
    first_backups = _backup_directories(backup_root)
    second = _migrate(database_path, backup_root)

    assert first.applied is True
    assert first.source_kind == "v1_1_1"
    assert before_business.cardinalities == after_business.cardinalities
    assert before_business.content_digests == after_business.content_digests
    assert second.applied is False
    assert second.source_kind == "versioned"
    assert second.backup_path is None
    assert _file_state(database_path) == first_state
    assert _history_rows(database_path) == first_history
    assert _backup_directories(backup_root) == first_backups
    _assert_verified(database_path, first.target_schema_checksum)


def test_partial_m13_database_preserves_evidence_and_second_run_is_noop(
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "partial-m13.sqlite3")
    backup_root = tmp_path / "migration-backups"
    before_business = _business_snapshot(database_path)
    assert before_business.cardinalities["evidence_observations"] == 1

    first = _migrate(database_path, backup_root)
    after_business = _business_snapshot(
        database_path,
        columns=before_business.columns,
    )
    first_state = _file_state(database_path)
    first_history = _history_rows(database_path)
    first_backups = _backup_directories(backup_root)
    second = _migrate(database_path, backup_root)

    assert first.applied is True
    assert first.source_kind == "partial_v2"
    assert before_business.cardinalities == after_business.cardinalities
    assert before_business.content_digests == after_business.content_digests
    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            "SELECT id, competency_id, competency_key, idempotency_key "
            "FROM evidence_observations"
        ).fetchone()
    assert migrated == (
        701,
        None,
        "fixture.legacy",
        "fixture-partial-evidence-key",
    )
    assert second.applied is False
    assert second.source_kind == "versioned"
    assert second.backup_path is None
    assert _file_state(database_path) == first_state
    assert _history_rows(database_path) == first_history
    assert _backup_directories(backup_root) == first_backups
    _assert_verified(database_path, first.target_schema_checksum)


def test_partial_m13_harness_is_frozen_v111_plus_literal_overlay(
    tmp_path: Path,
) -> None:
    v111_path = _materialize_sql("v1_1_1_full", tmp_path / "v1.sqlite3")
    partial_path = _materialize_partial_m13(tmp_path / "partial.sqlite3")
    assert _sha256_bytes(FROZEN_PARTIAL_M13_OVERLAY_SQL.encode("utf-8")) == (
        FROZEN_PARTIAL_M13_OVERLAY_SHA256
    )

    with sqlite3.connect(v111_path) as v111, sqlite3.connect(partial_path) as partial:
        assert _table_names(partial) == _table_names(v111) | {"evidence_observations"}
        evidence_columns = [
            row[1]
            for row in partial.execute(
                "PRAGMA table_xinfo(evidence_observations)"
            )
        ]
        evidence_row = partial.execute(
            "SELECT id, owner_id, competency_key, idempotency_key "
            "FROM evidence_observations"
        ).fetchone()

    assert evidence_columns == [
        "id",
        "owner_id",
        "source_type",
        "source_id",
        "run_id",
        "session_id",
        "plan_id",
        "task_id",
        "competency_key",
        "outcome",
        "normalized_score",
        "is_correct",
        "assistance_level",
        "transfer_level",
        "rubric_snapshot",
        "evaluator",
        "artifact_refs",
        "payload",
        "occurred_at",
        "recorded_at",
        "schema_version",
        "correlation_id",
        "causation_id",
        "idempotency_key",
        "supersedes_id",
        "invalidated_at",
        "invalidation_reason",
    ]
    assert "competency_id" not in evidence_columns
    assert evidence_row == (
        701,
        "fixture-owner",
        "fixture.legacy",
        "fixture-partial-evidence-key",
    )


def test_v111_with_unknown_unique_index_fails_closed_before_backup(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "unknown-index.sqlite3")
    backup_root = tmp_path / "migration-backups"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE UNIQUE INDEX uq_unknown_owner_display_name "
            "ON owners(display_name)"
        )
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "unknown_legacy_schema"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert _backup_directories(backup_root) == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='uq_unknown_owner_display_name'"
        ).fetchone() == (
            "CREATE UNIQUE INDEX uq_unknown_owner_display_name ON owners(display_name)",
        )


def test_v111_with_unknown_nonunique_index_fails_as_semantic_schema_drift(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "semantic-drift.sqlite3")
    backup_root = tmp_path / "migration-backups"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE INDEX ix_unknown_owner_timezone ON owners(timezone)"
        )
    source_state = _file_state(database_path)

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "unknown_legacy_schema"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _backup_directories(backup_root) == ()


def test_schema_checksum_preserves_whitespace_inside_sql_string_literals(
    tmp_path: Path,
) -> None:
    double_space = tmp_path / "double-space.sqlite3"
    single_space = tmp_path / "single-space.sqlite3"
    for path, literal in (
        (double_space, "a  b"),
        (single_space, "a b"),
    ):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute(
                "CREATE UNIQUE INDEX uq_samples_value_partial "
                f"ON samples(value) WHERE value='{literal}'"
            )
            connection.execute("PRAGMA user_version=1")

    with sqlite3.connect(double_space) as connection:
        connection.execute("INSERT INTO samples(value) VALUES ('a  b')")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute("INSERT INTO samples(value) VALUES ('a  b')")
    with sqlite3.connect(single_space) as connection:
        connection.execute("INSERT INTO samples(value) VALUES ('a  b')")
        connection.execute("INSERT INTO samples(value) VALUES ('a  b')")
        assert connection.execute("SELECT count(*) FROM samples").fetchone() == (2,)

    double_checksum = schema_checksum(double_space)
    single_checksum = schema_checksum(single_space)
    assert double_checksum != single_checksum
    with pytest.raises(MigrationError) as raised:
        verify_sqlite_database(
            double_space,
            expected_schema_checksum=single_checksum,
        )
    assert raised.value.code == "schema_verification_failed"


def test_fk_invalid_v111_source_fails_before_backup_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "fk-invalid.sqlite3")
    backup_root = tmp_path / "migration-backups"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE tasks SET stage_id=999999 WHERE id=1")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(violations) == 1
    assert violations[0][0] == "tasks"
    source_state = _file_state(database_path)

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "foreign_key_failed"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _backup_directories(backup_root) == ()


def test_supported_sources_converge_to_identical_canonical_schema(
    tmp_path: Path,
) -> None:
    empty = _materialize_sql("empty", tmp_path / "empty.sqlite3")
    v111 = _materialize_sql("v1_1_1_full", tmp_path / "v1.sqlite3")
    partial = _materialize_partial_m13(tmp_path / "partial.sqlite3")

    empty_report = _migrate(empty, tmp_path / "empty-backups")
    v111_report = _migrate(v111, tmp_path / "v1-backups")
    partial_report = _migrate(partial, tmp_path / "partial-backups")

    assert empty_report.source_kind == "empty"
    assert v111_report.source_kind == "v1_1_1"
    assert partial_report.source_kind == "partial_v2"
    assert {
        schema_checksum(empty),
        schema_checksum(v111),
        schema_checksum(partial),
    } == {empty_report.target_schema_checksum}
    assert v111_report.target_schema_checksum == empty_report.target_schema_checksum
    assert partial_report.target_schema_checksum == empty_report.target_schema_checksum
    for database_path in (empty, v111, partial):
        _assert_verified(database_path, empty_report.target_schema_checksum)


def test_canonical_schema_has_exact_h1_table_inventory(
    canonical_database: Path,
) -> None:
    with sqlite3.connect(canonical_database) as connection:
        actual_tables = _table_names(connection)

    assert actual_tables == CANONICAL_TABLES
    assert len(actual_tables) == 38


def test_learning_event_canonical_columns_defaults_and_partial_unique_index(
    canonical_database: Path,
) -> None:
    with sqlite3.connect(canonical_database) as connection:
        columns = connection.execute("PRAGMA table_xinfo(learning_events)").fetchall()
        indexes = connection.execute("PRAGMA index_list(learning_events)").fetchall()
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("uq_learning_events_idempotency_key",),
        ).fetchone()[0]

    assert [row[1] for row in columns] == [
        "id",
        "owner_id",
        "plan_id",
        "task_id",
        "run_id",
        "event_type",
        "summary",
        "payload",
        "schema_version",
        "occurred_at",
        "correlation_id",
        "causation_id",
        "idempotency_key",
        "invalidated_at",
        "invalidation_reason",
        "created_at",
    ]
    column_contract = {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in columns
    }
    assert column_contract["schema_version"] == ("INTEGER", 1, "1", 0)
    assert column_contract["occurred_at"] == ("DATETIME", 1, None, 0)
    assert column_contract["idempotency_key"] == ("VARCHAR(180)", 0, None, 0)
    assert column_contract["invalidation_reason"] == ("TEXT", 1, "''", 0)
    assert column_contract["created_at"] == ("DATETIME", 1, "CURRENT_TIMESTAMP", 0)
    index_contract = {
        row[1]: (row[2], row[3], row[4])
        for row in indexes
    }
    assert set(index_contract) == {
        "ix_learning_events_causation_id",
        "ix_learning_events_correlation_id",
        "ix_learning_events_created_at",
        "ix_learning_events_event_type",
        "ix_learning_events_occurred_at",
        "ix_learning_events_owner_id",
        "ix_learning_events_plan_id",
        "ix_learning_events_task_id",
        "uq_learning_events_idempotency_key",
    }
    assert index_contract["uq_learning_events_idempotency_key"] == (1, "c", 1)
    assert "UNIQUE INDEX uq_learning_events_idempotency_key" in index_sql
    assert "WHERE idempotency_key IS NOT NULL" in index_sql


def test_learning_event_not_null_and_idempotency_constraints_are_enforced(
    canonical_database: Path,
) -> None:
    with sqlite3.connect(canonical_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners (id, display_name, timezone) VALUES (?, ?, ?)",
            ("constraint-owner", "Constraint learner", "UTC"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            connection.execute(
                "INSERT INTO learning_events "
                "(owner_id, event_type, summary, payload, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?)",
                ("constraint-owner", "attempt", "missing occurred_at", "{}", "event-a"),
            )
        connection.execute(
            "INSERT INTO learning_events "
            "(owner_id, event_type, summary, payload, occurred_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "constraint-owner",
                "attempt",
                "first",
                "{}",
                "2026-08-19 01:02:03.000001",
                "event-key",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO learning_events "
                "(owner_id, event_type, summary, payload, occurred_at, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "constraint-owner",
                    "attempt",
                    "duplicate",
                    "{}",
                    "2026-08-19 01:02:04.000001",
                    "event-key",
                ),
            )


def test_evidence_canonical_defaults_indexes_and_foreign_keys_are_literal(
    canonical_database: Path,
) -> None:
    with sqlite3.connect(canonical_database) as connection:
        columns = connection.execute(
            "PRAGMA table_xinfo(evidence_observations)"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA index_list(evidence_observations)"
        ).fetchall()
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(evidence_observations)"
        ).fetchall()

    assert [row[1] for row in columns] == [
        "id",
        "owner_id",
        "source_type",
        "source_id",
        "run_id",
        "session_id",
        "plan_id",
        "task_id",
        "competency_id",
        "competency_key",
        "outcome",
        "normalized_score",
        "is_correct",
        "assistance_level",
        "transfer_level",
        "rubric_snapshot",
        "evaluator",
        "artifact_refs",
        "payload",
        "occurred_at",
        "recorded_at",
        "schema_version",
        "correlation_id",
        "causation_id",
        "idempotency_key",
        "supersedes_id",
        "invalidated_at",
        "invalidation_reason",
    ]
    defaults = {row[1]: row[4] for row in columns}
    assert defaults == {
        "id": None,
        "owner_id": None,
        "source_type": None,
        "source_id": None,
        "run_id": None,
        "session_id": None,
        "plan_id": None,
        "task_id": None,
        "competency_id": None,
        "competency_key": None,
        "outcome": None,
        "normalized_score": None,
        "is_correct": None,
        "assistance_level": "'unknown'",
        "transfer_level": "'unknown'",
        "rubric_snapshot": "'{}'",
        "evaluator": "'{}'",
        "artifact_refs": "'[]'",
        "payload": "'{}'",
        "occurred_at": None,
        "recorded_at": "CURRENT_TIMESTAMP",
        "schema_version": "1",
        "correlation_id": None,
        "causation_id": None,
        "idempotency_key": None,
        "supersedes_id": None,
        "invalidated_at": None,
        "invalidation_reason": "''",
    }
    assert {
        (row[3], row[2], row[4], row[5], row[6])
        for row in foreign_keys
    } == {
        ("owner_id", "owners", "id", "NO ACTION", "NO ACTION"),
        ("run_id", "agent_runs", "id", "NO ACTION", "SET NULL"),
        ("session_id", "sessions", "id", "NO ACTION", "SET NULL"),
        ("plan_id", "plans", "id", "NO ACTION", "SET NULL"),
        ("task_id", "tasks", "id", "NO ACTION", "SET NULL"),
        ("competency_id", "competencies", "id", "NO ACTION", "SET NULL"),
        (
            "supersedes_id",
            "evidence_observations",
            "id",
            "NO ACTION",
            "SET NULL",
        ),
    }
    assert {row[1] for row in indexes} == {
        "ix_evidence_observations_causation_id",
        "ix_evidence_observations_competency_id",
        "ix_evidence_observations_competency_key",
        "ix_evidence_observations_correlation_id",
        "ix_evidence_observations_idempotency_key",
        "ix_evidence_observations_occurred_at",
        "ix_evidence_observations_outcome",
        "ix_evidence_observations_owner_id",
        "ix_evidence_observations_owner_plan",
        "ix_evidence_observations_plan_id",
        "ix_evidence_observations_recorded_at",
        "ix_evidence_observations_run_id",
        "ix_evidence_observations_session_id",
        "ix_evidence_observations_source",
        "ix_evidence_observations_source_id",
        "ix_evidence_observations_source_type",
        "ix_evidence_observations_supersedes_id",
        "ix_evidence_observations_task",
        "ix_evidence_observations_task_id",
    }


def test_evidence_defaults_and_competency_foreign_key_are_enforced(
    canonical_database: Path,
) -> None:
    with sqlite3.connect(canonical_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners (id, display_name, timezone) VALUES (?, ?, ?)",
            ("evidence-owner", "Evidence learner", "UTC"),
        )
        connection.execute(
            "INSERT INTO evidence_observations "
            "(owner_id, source_type, source_id, outcome, occurred_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "evidence-owner",
                "quiz",
                "quiz-1",
                "passed",
                "2026-08-19 02:03:04.000005",
                "evidence-defaults",
            ),
        )
        defaulted = connection.execute(
            "SELECT assistance_level, transfer_level, rubric_snapshot, evaluator, "
            "artifact_refs, payload, recorded_at, schema_version, invalidation_reason "
            "FROM evidence_observations WHERE idempotency_key=?",
            ("evidence-defaults",),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO evidence_observations "
                "(owner_id, source_type, source_id, competency_id, outcome, "
                "occurred_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "evidence-owner",
                    "quiz",
                    "quiz-invalid-competency",
                    999_999,
                    "passed",
                    "2026-08-19 02:03:05.000006",
                    "evidence-invalid-competency",
                ),
            )

    assert defaulted[:6] == ("unknown", "unknown", "{}", "{}", "[]", "{}")
    assert defaulted[6] is not None
    _canonical_datetime_token(defaulted[6])
    assert defaulted[7:] == (1, "")


@pytest.mark.parametrize(
    "phase",
    BACKUP_PUBLICATION_KILL_PHASES,
    ids=BACKUP_PUBLICATION_KILL_PHASES,
)
def test_sigkill_at_each_backup_publication_phase_is_private_and_retryable(
    phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "backup-kill.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            KILLPOINT_PROGRAM,
            str(database_path),
            str(backup_root),
            phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"backup harness did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    if backup_root.exists():
        assert _mode(backup_root) == 0o700
    staging_paths = tuple(sorted(backup_root.glob(".staging-*")))
    for staging in staging_paths:
        _assert_private_directory_tree(staging)
    published = _backup_directories(backup_root)
    assert len(published) <= 1
    for backup_path in published:
        verify_migration_backup(backup_path)
        _assert_private_backup_tree(backup_root, backup_path)
    work_paths = tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )
    assert len(work_paths) == 1
    _assert_private_directory_tree(work_paths[0])

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    assert len(_backup_directories(backup_root)) == 1
    assert not tuple(backup_root.glob(".staging-*"))
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )
    assert report.backup_path is not None
    _assert_private_backup_tree(backup_root, Path(report.backup_path))
    migrated_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert migrated_business.cardinalities == source_business.cardinalities
    assert migrated_business.content_digests == source_business.content_digests
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


@pytest.mark.parametrize(
    "phase",
    BACKUP_PUBLICATION_KILL_PHASES,
    ids=BACKUP_PUBLICATION_KILL_PHASES,
)
def test_direct_backup_sigkill_residue_is_private_and_retry_cleans_it(
    phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "direct-backup-kill.sqlite3",
    )
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            BACKUP_KILLPOINT_PROGRAM,
            str(database_path),
            str(backup_root),
            phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "backup-child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"direct backup harness did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    work_paths = tuple(
        database_path.parent.glob(f".{database_path.name}.backup-work-*")
    )
    assert len(work_paths) == 1
    _assert_private_directory_tree(work_paths[0])
    staging_paths = tuple(sorted(backup_root.glob(".staging-*")))
    for staging in staging_paths:
        _assert_private_directory_tree(staging)
    published = _backup_directories(backup_root)
    assert len(published) <= 1
    for backup_path in published:
        verify_migration_backup(backup_path)
        _assert_private_backup_tree(backup_root, backup_path)

    backup_path = backup_sqlite_database(
        database_path,
        backup_root=backup_root,
        application_version="h1-direct-backup-retry-test",
    )

    manifest = verify_migration_backup(backup_path)
    assert manifest["source"]["database_identity"] == database_path.name
    assert manifest["source"]["kind"] == "v1_1_1"
    assert len(_backup_directories(backup_root)) == 1
    assert not tuple(backup_root.glob(".staging-*"))
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.backup-work-*")
    )
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)


@pytest.mark.parametrize("phase", KILL_PHASES, ids=KILL_PHASES)
def test_sigkill_at_each_candidate_phase_preserves_source_and_retry_converges(
    phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "killpoint.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    command = [
        sys.executable,
        "-c",
        KILLPOINT_PROGRAM,
        str(database_path),
        str(backup_root),
        phase,
    ]

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"fault harness did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert len(_backup_directories(backup_root)) == 1

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    assert report.source_kind == "partial_v2"
    assert not tuple(database_path.parent.glob(f".{database_path.name}.candidate-*"))
    migrated_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert migrated_business.cardinalities == source_business.cardinalities
    assert migrated_business.content_digests == source_business.content_digests
    assert _history_rows(database_path)[0][:3] == (
        1,
        "h1_canonical_schema",
        report.target_schema_checksum,
    )
    _assert_verified(database_path, report.target_schema_checksum)


def test_caught_post_publish_directory_fsync_failure_restores_source_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "fsync-failure.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_checksum = schema_checksum(database_path)
    source_business = _business_snapshot(database_path)
    source_evidence_digest = source_business.content_digests["evidence_observations"]
    real_fsync_directory = migration_module._fsync_directory
    failure_injected = False

    def fail_first_live_directory_sync(path: Path) -> None:
        nonlocal failure_injected
        if not failure_injected and Path(path).resolve() == database_path.parent.resolve():
            with sqlite3.connect(database_path) as connection:
                is_published = "schema_migrations" in _table_names(connection)
            if is_published:
                failure_injected = True
                raise OSError("synthetic post-publish directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(
        migration_module,
        "_fsync_directory",
        fail_first_live_directory_sync,
    )

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert failure_injected is True
    assert raised.value.code == "migration_failed"
    assert raised.value.recovery_backup is not None
    assert Path(raised.value.recovery_backup).is_dir()
    restored_business = _business_snapshot(database_path)
    assert restored_business == source_business
    assert (
        restored_business.content_digests["evidence_observations"]
        == source_evidence_digest
    )
    restored = verify_sqlite_database(database_path)
    assert restored["schema_checksum"] == source_checksum
    assert restored["integrity"] == ["ok"]
    assert restored["foreign_key_violation_count"] == 0
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)
    assert not tuple(database_path.parent.glob(f".{database_path.name}.candidate-*"))
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    assert report.source_kind == "partial_v2"
    migrated_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert migrated_business.cardinalities == source_business.cardinalities
    assert migrated_business.content_digests == source_business.content_digests
    _assert_verified(database_path, report.target_schema_checksum)


def test_post_publish_validation_failure_rolls_back_source_before_returning_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "validation-failure.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_checksum = schema_checksum(database_path)
    source_business = _business_snapshot(database_path)
    source_evidence_digest = source_business.content_digests["evidence_observations"]
    real_verify = migration_module.verify_sqlite_database
    failure_injected = False

    def fail_live_post_publish(
        path: Path,
        *,
        expected_schema_checksum: str | None = None,
    ) -> dict[str, Any]:
        nonlocal failure_injected
        if not failure_injected and Path(path).resolve() == database_path.resolve():
            with sqlite3.connect(database_path) as connection:
                is_published = "schema_migrations" in _table_names(connection)
            if is_published:
                failure_injected = True
                raise MigrationError(
                    "schema_verification_failed",
                    "synthetic post-publish validation failure",
                )
        return real_verify(
            path,
            expected_schema_checksum=expected_schema_checksum,
        )

    monkeypatch.setattr(
        migration_module,
        "verify_sqlite_database",
        fail_live_post_publish,
    )

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert failure_injected is True
    assert raised.value.code == "schema_verification_failed"
    assert raised.value.recovery_backup is not None
    assert Path(raised.value.recovery_backup).is_dir()
    restored_business = _business_snapshot(database_path)
    assert restored_business == source_business
    assert (
        restored_business.content_digests["evidence_observations"]
        == source_evidence_digest
    )
    restored = verify_sqlite_database(database_path)
    assert restored["schema_checksum"] == source_checksum
    assert restored["integrity"] == ["ok"]
    assert restored["foreign_key_violation_count"] == 0
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    assert report.source_kind == "partial_v2"
    _assert_verified(database_path, report.target_schema_checksum)


@pytest.mark.parametrize(
    "kill_phase",
    ("before_rollback_publish", "after_rollback_publish"),
)
def test_sigkill_at_rollback_replace_leaves_only_complete_old_or_verified_new(
    kill_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "rollback-kill.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_checksum = schema_checksum(database_path)
    source_business = _business_snapshot(database_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            ROLLBACK_REPLACE_KILL_PROGRAM,
            str(database_path),
            str(backup_root),
            kill_phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"rollback harness did not reach {kill_phase}; "
        f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
    )
    observed = verify_sqlite_database(database_path)
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0
    assert observed["schema_checksum"] in {
        source_checksum,
        FROZEN_H1_SCHEMA_CHECKSUM,
    }
    after_kill_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert after_kill_business.cardinalities == source_business.cardinalities
    assert after_kill_business.content_digests == source_business.content_digests
    with sqlite3.connect(database_path) as connection:
        is_new = "schema_migrations" in _table_names(connection)
    assert is_new is (observed["schema_checksum"] == FROZEN_H1_SCHEMA_CHECKSUM)

    report = _migrate(database_path, backup_root)

    assert report.applied is (not is_new)
    assert report.source_kind in {"partial_v2", "versioned"}
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    converged_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert converged_business.cardinalities == source_business.cardinalities
    assert converged_business.content_digests == source_business.content_digests
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )


@pytest.mark.parametrize(
    "kill_phase",
    ("before_rollback_publish", "after_rollback_publish"),
)
def test_migration_rollback_killpoint_when_original_database_was_absent(
    kill_phase: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "absent-rollback.sqlite3"
    backup_root = tmp_path / "migration-backups"
    assert not database_path.exists()

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            ROLLBACK_REPLACE_KILL_PROGRAM,
            str(database_path),
            str(backup_root),
            kill_phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"absent-source rollback harness missed {kill_phase}; "
        f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
    )
    database_existed_after_kill = database_path.exists()
    if database_existed_after_kill:
        _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    assert len(_backup_directories(backup_root)) == 1

    report = _migrate(database_path, backup_root)

    assert report.applied is (not database_existed_after_kill)
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )


def test_sigkill_after_publish_leaves_complete_old_or_new_database_then_converges(
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "after-publish.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_checksum = schema_checksum(database_path)
    source_business = _business_snapshot(database_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            KILLPOINT_PROGRAM,
            str(database_path),
            str(backup_root),
            "after_publish",
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"fault harness did not reach after_publish; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    observed = verify_sqlite_database(database_path)
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0
    after_kill_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert after_kill_business.cardinalities == source_business.cardinalities
    assert after_kill_business.content_digests == source_business.content_digests
    with sqlite3.connect(database_path) as connection:
        tables_after_kill = _table_names(connection)

    if "schema_migrations" in tables_after_kill:
        assert tables_after_kill == CANONICAL_TABLES
        assert observed["schema_checksum"] == FROZEN_H1_SCHEMA_CHECKSUM
        assert observed["user_version"] == 1
        assert _history_rows(database_path)[0][0:3] == (
            1,
            "h1_canonical_schema",
            FROZEN_H1_SCHEMA_CHECKSUM,
        )
    else:
        assert observed["schema_checksum"] == source_checksum
        assert observed["user_version"] == 0
    assert not tuple(database_path.parent.glob(f".{database_path.name}.candidate-*"))

    report = _migrate(database_path, backup_root)

    assert report.applied is ("schema_migrations" not in tables_after_kill)
    assert report.source_kind in {"partial_v2", "versioned"}
    converged_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert converged_business.cardinalities == source_business.cardinalities
    assert converged_business.content_digests == source_business.content_digests
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


def test_active_wal_reader_fails_closed_after_verified_backup_without_publish(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "wal-reader.sqlite3")
    expected_path = _materialize_sql("v1_1_1_full", tmp_path / "wal-expected.sqlite3")
    with sqlite3.connect(expected_path) as connection:
        connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            ("WAL fixture learner", "fixture-owner"),
        )
    expected_business = _business_snapshot(expected_path)
    expected_schema_checksum = schema_checksum(expected_path)
    backup_root = tmp_path / "migration-backups"
    reader = sqlite3.connect(database_path, isolation_level=None)
    try:
        assert reader.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        reader.execute("PRAGMA wal_autocheckpoint=0")
        reader.execute("BEGIN")
        assert reader.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Fixture Learner",)
        writer = sqlite3.connect(database_path, isolation_level=None)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE owners SET display_name=? WHERE id=?",
                ("WAL fixture learner", "fixture-owner"),
            )
            writer.commit()
        finally:
            writer.close()
        assert database_path.with_name(database_path.name + "-wal").is_file()
        assert database_path.with_name(database_path.name + "-shm").is_file()

        with pytest.raises(MigrationError) as raised:
            _migrate(database_path, backup_root)

        assert raised.value.code == "active_sqlite_reader"
        assert raised.value.recovery_backup is not None
        recovery_backup = Path(raised.value.recovery_backup)
        assert recovery_backup in _backup_directories(backup_root)
        verify_migration_backup(recovery_backup)
        probe = sqlite3.connect(database_path)
        try:
            assert "schema_migrations" not in _table_names(probe)
            assert probe.execute(
                "SELECT display_name FROM owners WHERE id='fixture-owner'"
            ).fetchone() == ("WAL fixture learner",)
        finally:
            probe.close()
    finally:
        reader.rollback()
        reader.close()

    observed = verify_sqlite_database(database_path)
    assert observed["schema_checksum"] == expected_schema_checksum
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0
    assert _business_snapshot(database_path) == expected_business

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    converged_business = _business_snapshot(
        database_path,
        columns=expected_business.columns,
    )
    assert converged_business.cardinalities == expected_business.cardinalities
    assert converged_business.content_digests == expected_business.content_digests
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


@pytest.mark.parametrize(
    "phase",
    WAL_PUBLICATION_KILL_PHASES,
    ids=WAL_PUBLICATION_KILL_PHASES,
)
def test_crashed_committed_wal_is_old_or_new_at_each_publication_killpoint(
    phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "wal-source.sqlite3")
    expected_path = _materialize_sql("v1_1_1_full", tmp_path / "wal-expected.sqlite3")
    with sqlite3.connect(expected_path) as connection:
        connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            ("WAL fixture learner", "fixture-owner"),
        )
    expected_business = _business_snapshot(expected_path)
    legacy_checksum = schema_checksum(expected_path)
    backup_root = tmp_path / "migration-backups"
    ready_marker = tmp_path / "wal-ready"

    writer = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASHED_WAL_WRITER_PROGRAM,
            str(database_path),
            str(ready_marker),
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "writer-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert writer.returncode == -signal.SIGKILL, (
        f"WAL writer did not crash as required; stdout={writer.stdout!r}, "
        f"stderr={writer.stderr!r}"
    )
    assert ready_marker.read_text(encoding="ascii") == "committed-wal-before-crash\n"
    assert database_path.with_name(database_path.name + "-wal").is_file()
    assert database_path.with_name(database_path.name + "-shm").is_file()

    migrator = subprocess.run(
        [
            sys.executable,
            "-c",
            KILLPOINT_PROGRAM,
            str(database_path),
            str(backup_root),
            phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "migrator-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert migrator.returncode == -signal.SIGKILL, (
        f"WAL migration harness did not reach {phase}; "
        f"stdout={migrator.stdout!r}, stderr={migrator.stderr!r}"
    )
    observed = verify_sqlite_database(database_path)
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0
    assert observed["schema_checksum"] in {
        legacy_checksum,
        FROZEN_H1_SCHEMA_CHECKSUM,
    }
    after_kill_business = _business_snapshot(
        database_path,
        columns=expected_business.columns,
    )
    assert after_kill_business.cardinalities == expected_business.cardinalities
    assert after_kill_business.content_digests == expected_business.content_digests
    with sqlite3.connect(database_path) as connection:
        is_new = "schema_migrations" in _table_names(connection)
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("WAL fixture learner",)
    assert is_new is (observed["schema_checksum"] == FROZEN_H1_SCHEMA_CHECKSUM)

    report = _migrate(database_path, backup_root)

    assert report.applied is (not is_new)
    assert report.source_kind in {"v1_1_1", "versioned"}
    converged_business = _business_snapshot(
        database_path,
        columns=expected_business.columns,
    )
    assert converged_business.cardinalities == expected_business.cardinalities
    assert converged_business.content_digests == expected_business.content_digests
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )


def test_two_migrator_processes_apply_one_history_revision_and_one_backup(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "concurrent.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_business = _business_snapshot(database_path)
    command = [
        sys.executable,
        "-c",
        CONCURRENT_PROGRAM,
        str(database_path),
        str(backup_root),
    ]
    environment = _subprocess_environment(tmp_path / "child-config.sqlite3")

    first = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=90)
    second_stdout, second_stderr = second.communicate(timeout=90)

    assert first.returncode == 0, first_stderr
    assert second.returncode == 0, second_stderr
    reports = [
        json.loads(first_stdout.strip().splitlines()[-1]),
        json.loads(second_stdout.strip().splitlines()[-1]),
    ]
    assert sorted(report["applied"] for report in reports) == [False, True]
    assert sorted(report["source_kind"] for report in reports) == [
        "v1_1_1",
        "versioned",
    ]
    assert len(_history_rows(database_path)) == 1
    assert _history_rows(database_path)[0][0:2] == (1, "h1_canonical_schema")
    assert len(_backup_directories(backup_root)) == 1
    after_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert after_business.cardinalities == source_business.cardinalities
    assert after_business.content_digests == source_business.content_digests
    checksum = schema_checksum(database_path)
    assert {report["target_schema_checksum"] for report in reports} == {checksum}
    _assert_verified(database_path, checksum)


def test_writer_in_journal_canonicalization_gap_is_detected_without_lost_write(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "gap-writer.sqlite3")
    backup_root = tmp_path / "migration-backups"
    ready_marker = tmp_path / "gap-ready"
    release_marker = tmp_path / "gap-release"
    migrator = subprocess.Popen(
        [
            sys.executable,
            "-c",
            MIGRATOR_PAUSE_AT_PHASE_PROGRAM,
            str(database_path),
            str(backup_root),
            "after_writer_guard_release",
            str(ready_marker),
            str(release_marker),
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "migrator-config.sqlite3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(migrator, ready_marker)
        assert (
            ready_marker.read_text(encoding="ascii")
            == "after_writer_guard_release\n"
        )
        writer = _run_concurrent_writer(
            database_path,
            "Committed during canonicalization gap",
            tmp_path / "gap-writer-config.sqlite3",
        )
    finally:
        release_marker.write_text("release\n", encoding="ascii")
    stdout, stderr = migrator.communicate(timeout=90)

    assert migrator.returncode == 0, stderr
    assert writer.returncode == 0, writer.stderr
    assert writer.stdout.strip() == "committed"
    outcome = json.loads(stdout.strip().splitlines()[-1])
    assert outcome["kind"] == "error", (
        "migration silently published a stale snapshot after a writer committed "
        f"inside the journal canonicalization gap: {outcome!r}"
    )
    assert outcome["code"] == "source_changed_during_migration"
    assert outcome["recovery_backup"] is not None
    verify_migration_backup(Path(outcome["recovery_backup"]))
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Committed during canonicalization gap",)
    observed = verify_sqlite_database(database_path)
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Committed during canonicalization gap",)
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


def test_new_inode_writer_is_blocked_at_after_main_replace(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "new-inode.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_stat = database_path.stat()
    source_inode = (source_stat.st_dev, source_stat.st_ino)
    ready_marker = tmp_path / "new-inode-ready"
    release_marker = tmp_path / "new-inode-release"
    migrator = subprocess.Popen(
        [
            sys.executable,
            "-c",
            MIGRATOR_PAUSE_AT_PHASE_PROGRAM,
            str(database_path),
            str(backup_root),
            "after_main_replace",
            str(ready_marker),
            str(release_marker),
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "migrator-config.sqlite3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(migrator, ready_marker)
        assert ready_marker.read_text(encoding="ascii") == "after_main_replace\n"
        published_stat = database_path.stat()
        assert (published_stat.st_dev, published_stat.st_ino) != source_inode
        writer = _run_concurrent_writer(
            database_path,
            "Forbidden new-inode write",
            tmp_path / "new-inode-writer-config.sqlite3",
        )
    finally:
        release_marker.write_text("release\n", encoding="ascii")
    stdout, stderr = migrator.communicate(timeout=90)

    assert migrator.returncode == 0, stderr
    outcome = json.loads(stdout.strip().splitlines()[-1])
    assert outcome["kind"] == "report", outcome
    assert outcome["report"]["applied"] is True
    assert writer.returncode == 75, (
        "a raw writer acquired the newly published inode before migration "
        f"verification completed; stdout={writer.stdout!r}, stderr={writer.stderr!r}"
    )
    assert writer.stdout.strip() == "blocked"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Fixture Learner",)
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)

    post_migration_writer = _run_concurrent_writer(
        database_path,
        "Post-migration writer succeeds",
        tmp_path / "post-migration-writer-config.sqlite3",
    )

    assert post_migration_writer.returncode == 0, post_migration_writer.stderr
    assert post_migration_writer.stdout.strip() == "committed"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Post-migration writer succeeds",)
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


def test_active_writer_fails_before_backup_and_does_not_touch_source(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "writer.sqlite3")
    backup_root = tmp_path / "migration-backups"
    writer = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    try:
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            ("Uncommitted writer value", "fixture-owner"),
        )
        source_state = _file_state(database_path)

        with pytest.raises(MigrationError) as raised:
            _migrate(database_path, backup_root)

        assert raised.value.code == "active_sqlite_writer"
        assert raised.value.recovery_backup is None
        assert _file_state(database_path) == source_state
        assert _backup_directories(backup_root) == ()
        assert writer.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Uncommitted writer value",)
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as reader:
            assert reader.execute(
                "SELECT display_name FROM owners WHERE id='fixture-owner'"
            ).fetchone() == ("Fixture Learner",)
    finally:
        writer.rollback()
        writer.close()


def test_unknown_nonempty_schema_fails_closed_before_backup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE mystery_records (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO mystery_records (id, payload) VALUES (1, 'synthetic')"
        )
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "unknown_legacy_schema"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _backup_directories(backup_root) == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT * FROM mystery_records").fetchall() == [
            (1, "synthetic")
        ]


def test_stale_unpublished_candidate_is_removed_before_retry(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "stale.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_business = _business_snapshot(database_path)
    stale = database_path.with_name(f".{database_path.name}.candidate-stale")
    stale.write_bytes(b"synthetic incomplete candidate")
    assert stale.is_file()

    report = _migrate(database_path, backup_root)

    assert report.applied is True
    assert report.source_kind == "v1_1_1"
    assert not stale.exists()
    assert not tuple(database_path.parent.glob(f".{database_path.name}.candidate-*"))
    after_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert after_business.cardinalities == source_business.cardinalities
    assert after_business.content_digests == source_business.content_digests
    _assert_verified(database_path, report.target_schema_checksum)


@pytest.mark.parametrize(
    "work_kind",
    ("migration-work", "restore-work", "backup-work"),
)
@pytest.mark.parametrize("path_kind", ("private", "insecure", "symlink", "file"))
def test_stale_work_path_cleanup_is_private_and_fail_closed(
    work_kind: str,
    path_kind: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "stale-work.sqlite3")
    backup_root = tmp_path / "migration-backups"
    stale = database_path.parent / f".{database_path.name}.{work_kind}-synthetic"
    symlink_target = tmp_path / "outside-stale-target"
    symlink_target.mkdir(mode=0o700)
    sentinel = symlink_target / "sentinel.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    if path_kind == "private":
        stale.mkdir(mode=0o700)
        (stale / "private-snapshot.sqlite3").write_bytes(b"synthetic private residue")
    elif path_kind == "insecure":
        stale.mkdir(mode=0o755)
        os.chmod(stale, 0o755)
        (stale / "exposed-snapshot.sqlite3").write_bytes(b"synthetic exposed residue")
    elif path_kind == "symlink":
        stale.symlink_to(symlink_target, target_is_directory=True)
    else:
        stale.write_bytes(b"synthetic non-directory residue")
    source_state = _file_state(database_path)

    if path_kind == "private":
        report = _migrate(database_path, backup_root)
        assert report.applied is True
        assert not stale.exists()
        _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    else:
        with pytest.raises(MigrationError) as raised:
            _migrate(database_path, backup_root)
        assert raised.value.code == "recovery_required"
        assert raised.value.recovery_backup is None
        assert _file_state(database_path) == source_state
        assert os.path.lexists(stale)
        assert _backup_directories(backup_root) == ()
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"


@pytest.mark.parametrize("path_kind", ("private", "insecure", "symlink", "file"))
def test_stale_backup_staging_path_cleanup_is_private_and_fail_closed(
    path_kind: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "stale-backup.sqlite3")
    backup_root = tmp_path / "migration-backups"
    backup_root.mkdir(mode=0o700)
    identity_token = hashlib.sha256(database_path.name.encode("utf-8")).hexdigest()[:16]
    staging = backup_root / (
        f".staging-{database_path.name}-{identity_token}-synthetic"
    )
    symlink_target = tmp_path / "outside-backup-staging"
    symlink_target.mkdir(mode=0o700)
    sentinel = symlink_target / "sentinel.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    if path_kind == "private":
        staging.mkdir(mode=0o700)
        (staging / "snapshot.sqlite3").write_bytes(b"synthetic private staging")
    elif path_kind == "insecure":
        staging.mkdir(mode=0o755)
        os.chmod(staging, 0o755)
        (staging / "snapshot.sqlite3").write_bytes(b"synthetic exposed staging")
    elif path_kind == "symlink":
        staging.symlink_to(symlink_target, target_is_directory=True)
    else:
        staging.write_bytes(b"synthetic staging file")
    source_state = _file_state(database_path)

    if path_kind == "private":
        report = _migrate(database_path, backup_root)
        assert report.applied is True
        assert not staging.exists()
        assert report.backup_path is not None
        _assert_private_backup_tree(backup_root, Path(report.backup_path))
    else:
        with pytest.raises(MigrationError) as raised:
            _migrate(database_path, backup_root)
        assert raised.value.code == "recovery_required"
        assert raised.value.recovery_backup is None
        assert _file_state(database_path) == source_state
        assert os.path.lexists(staging)
        assert _backup_directories(backup_root) == ()
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"


def test_pre_migration_backup_restore_and_reupgrade_preserve_evidence_digest(
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "roundtrip.sqlite3")
    source_business = _business_snapshot(database_path)
    source_evidence_digest = source_business.content_digests["evidence_observations"]

    first = _migrate(database_path, tmp_path / "first-backups")
    assert first.backup_path is not None
    backup_path = Path(first.backup_path)
    manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
    assert verify_migration_backup(backup_path) == manifest
    assert manifest["source"]["database_identity"] == "roundtrip.sqlite3"
    payload_path = backup_path / "payload" / "database.sqlite3"
    payload_bytes = payload_path.read_bytes()
    payload_hash = _sha256_bytes(payload_bytes)
    assert manifest["files"] == [
        {
            "source_path": "roundtrip.sqlite3",
            "archive_path": "payload/database.sqlite3",
            "kind": "sqlite",
            "size_bytes": len(payload_bytes),
            "sha256": payload_hash,
            "integrity": ["ok"],
            "foreign_key_violation_count": 0,
        }
    ]

    restore_result = restore_migration_backup(
        database_path,
        backup_path,
        safety_backup_root=tmp_path / "restore-safety-backups",
        application_version="h1-protocol-test",
    )

    assert restore_result["ok"] is True
    assert restore_result["code"] == "migration_backup_restored"
    assert restore_result["backup_id"] == backup_path.name
    assert Path(restore_result["safety_backup_path"]).is_dir()
    assert _sha256_bytes(database_path.read_bytes()) == payload_hash
    restored_business = _business_snapshot(database_path)
    assert restored_business.cardinalities == source_business.cardinalities
    assert restored_business.content_digests == source_business.content_digests
    assert (
        restored_business.content_digests["evidence_observations"]
        == source_evidence_digest
    )

    second = _migrate(database_path, tmp_path / "second-backups")

    assert second.applied is True
    assert second.source_kind == "partial_v2"
    assert second.target_schema_checksum == first.target_schema_checksum
    reupgraded_business = _business_snapshot(
        database_path,
        columns=source_business.columns,
    )
    assert reupgraded_business.cardinalities == source_business.cardinalities
    assert reupgraded_business.content_digests == source_business.content_digests
    assert (
        reupgraded_business.content_digests["evidence_observations"]
        == source_evidence_digest
    )
    _assert_verified(database_path, first.target_schema_checksum)


def test_same_basename_with_different_managed_identity_is_rejected_before_restore(
    tmp_path: Path,
) -> None:
    source_identity = "data/learning_companion.db"
    target_identity = "backend/data/learning_companion.db"
    source_path = _materialize_partial_m13(
        tmp_path / "checkout-a" / "data" / "learning_companion.db"
    )
    first = _migrate(
        source_path,
        tmp_path / "checkout-a" / "backups",
        database_identity=source_identity,
    )
    assert first.backup_path is not None
    backup_path = Path(first.backup_path)
    manifest = verify_migration_backup(backup_path)
    assert manifest["source"]["database_identity"] == source_identity
    assert manifest["files"][0]["source_path"] == source_identity

    target_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "checkout-b" / "backend" / "data" / "learning_companion.db",
    )
    target_state = _file_state(target_path)
    target_business = _business_snapshot(target_path)
    safety_root = tmp_path / "checkout-b" / "restore-safety"

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            target_path,
            backup_path,
            safety_backup_root=safety_root,
            application_version="h1-identity-test",
            database_identity=target_identity,
        )

    assert raised.value.code == "backup_target_mismatch"
    assert raised.value.recovery_backup is None
    assert _file_state(target_path) == target_state
    assert _business_snapshot(target_path) == target_business
    assert _backup_directories(safety_root) == ()
    assert verify_migration_backup(backup_path) == manifest


def test_managed_identity_backup_is_portable_across_checkout_roots(
    tmp_path: Path,
) -> None:
    database_identity = "data/learning_companion.db"
    source_path = _materialize_partial_m13(
        tmp_path / "checkout-a" / "data" / "learning_companion.db"
    )
    source_business = _business_snapshot(source_path)
    source_evidence_digest = source_business.content_digests["evidence_observations"]
    first = _migrate(
        source_path,
        tmp_path / "checkout-a" / "backups",
        database_identity=database_identity,
    )
    assert first.backup_path is not None
    original_backup = Path(first.backup_path)
    original_manifest = verify_migration_backup(original_backup)
    assert original_manifest["source"]["database_identity"] == database_identity
    assert original_manifest["files"][0]["source_path"] == database_identity

    copied_backup = (
        tmp_path / "checkout-b" / "backups" / original_backup.name
    )
    copied_backup.parent.mkdir(parents=True)
    shutil.copytree(original_backup, copied_backup)
    assert verify_migration_backup(copied_backup) == original_manifest
    payload_path = copied_backup / "payload" / "database.sqlite3"
    payload_hash = _sha256_bytes(payload_path.read_bytes())
    target_path = tmp_path / "checkout-b" / "data" / "learning_companion.db"

    restored = restore_migration_backup(
        target_path,
        copied_backup,
        safety_backup_root=tmp_path / "checkout-b" / "restore-safety",
        application_version="h1-cross-checkout-test",
        database_identity=database_identity,
    )

    assert restored["ok"] is True
    assert _sha256_bytes(target_path.read_bytes()) == payload_hash
    restored_business = _business_snapshot(target_path)
    assert restored_business == source_business
    assert (
        restored_business.content_digests["evidence_observations"]
        == source_evidence_digest
    )
    second = _migrate(
        target_path,
        tmp_path / "checkout-b" / "reupgrade-backups",
        database_identity=database_identity,
    )

    assert second.applied is True
    assert second.source_kind == "partial_v2"
    reupgraded = _business_snapshot(
        target_path,
        columns=source_business.columns,
    )
    assert reupgraded.cardinalities == source_business.cardinalities
    assert reupgraded.content_digests == source_business.content_digests
    assert (
        reupgraded.content_digests["evidence_observations"]
        == source_evidence_digest
    )
    _assert_verified(target_path, FROZEN_H1_SCHEMA_CHECKSUM)


def test_restore_rejects_future_live_history_with_stable_code_and_no_mutation(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "future-live.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE schema_migrations SET version=2")
        connection.execute("PRAGMA user_version=2")
    live_state = _file_state(database_path)
    live_business = _business_snapshot(database_path)
    safety_root = tmp_path / "restore-safety-backups"

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            database_path,
            Path(report.backup_path),
            safety_backup_root=safety_root,
            application_version="h1-protocol-test",
        )

    assert raised.value.code == "future_schema_version"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == live_state
    assert _business_snapshot(database_path) == live_business
    assert _backup_directories(safety_root) == ()


def test_restore_rejects_malformed_live_history_table_without_raw_sqlite_error(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "malformed-live.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
        connection.execute("PRAGMA user_version=1")
    live_state = _file_state(database_path)
    safety_root = tmp_path / "restore-safety-backups"

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            database_path,
            Path(report.backup_path),
            safety_backup_root=safety_root,
            application_version="h1-protocol-test",
        )

    assert raised.value.code == "migration_history_invalid"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == live_state
    assert _backup_directories(safety_root) == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() == (
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)",
        )


def test_restore_rejects_check_invalid_live_as_integrity_failure(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "corrupt-live.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE schema_migrations SET result='pending'")
    with sqlite3.connect(database_path) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    assert integrity != ["ok"]
    live_state = _file_state(database_path)
    safety_root = tmp_path / "restore-safety-backups"

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            database_path,
            Path(report.backup_path),
            safety_backup_root=safety_root,
            application_version="h1-protocol-test",
        )

    assert raised.value.code == "sqlite_integrity_failed"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == live_state
    assert _backup_directories(safety_root) == ()


def test_restore_explicitly_refuses_physically_corrupt_live_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "physical-corrupt.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    corrupt_bytes = b"not a sqlite database\x00synthetic-corrupt-live"
    database_path.write_bytes(corrupt_bytes)
    live_state = _file_state(database_path)
    safety_root = tmp_path / "restore-safety-backups"

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            database_path,
            Path(report.backup_path),
            safety_backup_root=safety_root,
            application_version="h1-protocol-test",
        )

    # A physically unreadable live file cannot be snapshotted for the mandatory
    # before-restore safety backup.  The supported policy is an explicit,
    # non-mutating refusal rather than silently discarding the corrupt artifact.
    assert raised.value.code == "database_open_failed"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == live_state
    assert database_path.read_bytes() == corrupt_bytes
    assert _backup_directories(safety_root) == ()


@pytest.mark.parametrize("live_present", (True, False), ids=("live", "absent"))
@pytest.mark.parametrize(
    "phase",
    RESTORE_DIRECT_KILL_PHASES,
    ids=RESTORE_DIRECT_KILL_PHASES,
)
def test_restore_sigkill_at_each_direct_publish_phase_converges_without_mixing(
    phase: str,
    live_present: bool,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "restore-publish.sqlite3")
    legacy_business = _business_snapshot(database_path)
    first = _migrate(database_path, tmp_path / "migration-backups")
    assert first.backup_path is not None
    backup_path = Path(first.backup_path)
    manifest = verify_migration_backup(backup_path)
    payload_path = backup_path / "payload" / "database.sqlite3"
    payload_hash = _sha256_bytes(payload_path.read_bytes())
    if not live_present:
        database_path.unlink()
    safety_root = tmp_path / "restore-safety-backups"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            RESTORE_DIRECT_KILLPOINT_PROGRAM,
            str(database_path),
            str(backup_path),
            str(safety_root),
            phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"restore harness did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    if database_path.exists():
        observed = verify_sqlite_database(database_path)
        assert observed["integrity"] == ["ok"]
        assert observed["foreign_key_violation_count"] == 0
        assert observed["schema_checksum"] in {
            FROZEN_H1_SCHEMA_CHECKSUM,
            manifest["source"]["schema_checksum"],
        }
        after_kill_business = _business_snapshot(
            database_path,
            columns=legacy_business.columns,
        )
        assert after_kill_business.cardinalities == legacy_business.cardinalities
        assert after_kill_business.content_digests == legacy_business.content_digests
    else:
        assert live_present is False

    restored = restore_migration_backup(
        database_path,
        backup_path,
        safety_backup_root=safety_root,
        application_version="h1-protocol-test",
    )

    assert restored["ok"] is True
    assert _sha256_bytes(database_path.read_bytes()) == payload_hash
    assert database_path.stat().st_mode & 0o777 == 0o600
    assert _business_snapshot(database_path) == legacy_business
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.restore-work-*")
    )
    second = _migrate(database_path, tmp_path / "reupgrade-backups")
    assert second.applied is True
    reupgraded = _business_snapshot(
        database_path,
        columns=legacy_business.columns,
    )
    assert reupgraded.cardinalities == legacy_business.cardinalities
    assert reupgraded.content_digests == legacy_business.content_digests
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


def test_restore_post_publish_validation_failure_restores_live_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "restore-validate.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    live_checksum = schema_checksum(database_path)
    live_business = _business_snapshot(database_path)
    live_evidence_digest = live_business.content_digests["evidence_observations"]
    real_verify = migration_module.verify_sqlite_database
    validation_injected = False

    def fail_restored_live(
        path: Path,
        *,
        expected_schema_checksum: str | None = None,
    ) -> dict[str, Any]:
        nonlocal validation_injected
        if not validation_injected and Path(path).resolve() == database_path.resolve():
            validation_injected = True
            raise MigrationError(
                "schema_verification_failed",
                "synthetic restored-live validation failure",
            )
        return real_verify(
            path,
            expected_schema_checksum=expected_schema_checksum,
        )

    monkeypatch.setattr(
        migration_module,
        "verify_sqlite_database",
        fail_restored_live,
    )
    safety_root = tmp_path / "restore-safety-backups"

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            database_path,
            Path(report.backup_path),
            safety_backup_root=safety_root,
            application_version="h1-protocol-test",
        )

    assert validation_injected is True
    assert raised.value.code == "schema_verification_failed"
    assert raised.value.recovery_backup is not None
    recovery_backup = Path(raised.value.recovery_backup)
    assert recovery_backup in _backup_directories(safety_root)
    verify_migration_backup(recovery_backup)
    restored_business = _business_snapshot(database_path)
    assert restored_business == live_business
    assert (
        restored_business.content_digests["evidence_observations"]
        == live_evidence_digest
    )
    restored = verify_sqlite_database(database_path)
    assert restored["schema_checksum"] == live_checksum
    assert restored["integrity"] == ["ok"]
    assert restored["foreign_key_violation_count"] == 0
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" in _table_names(connection)


@pytest.mark.parametrize(
    "kill_phase",
    ("before_rollback_publish", "after_rollback_publish"),
)
def test_restore_sigkill_at_rollback_replace_is_old_or_verified_payload(
    kill_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "restore-rollback-kill.sqlite3")
    legacy_business = _business_snapshot(database_path)
    first = _migrate(database_path, tmp_path / "migration-backups")
    assert first.backup_path is not None
    backup_path = Path(first.backup_path)
    manifest = verify_migration_backup(backup_path)
    payload_path = backup_path / "payload" / "database.sqlite3"
    payload_hash = _sha256_bytes(payload_path.read_bytes())
    safety_root = tmp_path / "restore-safety-backups"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            RESTORE_ROLLBACK_REPLACE_KILL_PROGRAM,
            str(database_path),
            str(backup_path),
            str(safety_root),
            kill_phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"restore rollback harness did not reach {kill_phase}; "
        f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
    )
    observed = verify_sqlite_database(database_path)
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0
    assert observed["schema_checksum"] in {
        FROZEN_H1_SCHEMA_CHECKSUM,
        manifest["source"]["schema_checksum"],
    }
    after_kill_business = _business_snapshot(
        database_path,
        columns=legacy_business.columns,
    )
    assert after_kill_business.cardinalities == legacy_business.cardinalities
    assert after_kill_business.content_digests == legacy_business.content_digests

    restored = restore_migration_backup(
        database_path,
        backup_path,
        safety_backup_root=safety_root,
        application_version="h1-protocol-test",
    )

    assert restored["ok"] is True
    assert _sha256_bytes(database_path.read_bytes()) == payload_hash
    restored_business = _business_snapshot(database_path)
    assert restored_business == legacy_business
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.restore-work-*")
    )

    second = _migrate(database_path, tmp_path / "reupgrade-backups")

    assert second.applied is True
    reupgraded_business = _business_snapshot(
        database_path,
        columns=legacy_business.columns,
    )
    assert reupgraded_business.cardinalities == legacy_business.cardinalities
    assert reupgraded_business.content_digests == legacy_business.content_digests
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


@pytest.mark.parametrize(
    "kill_phase",
    ("before_rollback_publish", "after_rollback_publish"),
)
def test_restore_rollback_killpoint_when_original_database_was_absent(
    kill_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "absent-restore.sqlite3")
    legacy_business = _business_snapshot(database_path)
    first = _migrate(database_path, tmp_path / "migration-backups")
    assert first.backup_path is not None
    backup_path = Path(first.backup_path)
    payload_path = backup_path / "payload" / "database.sqlite3"
    payload_hash = _sha256_bytes(payload_path.read_bytes())
    database_path.unlink()
    safety_root = tmp_path / "restore-safety-backups"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            RESTORE_ROLLBACK_REPLACE_KILL_PROGRAM,
            str(database_path),
            str(backup_path),
            str(safety_root),
            kill_phase,
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "child-config.sqlite3"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == -signal.SIGKILL, (
        f"absent-live restore rollback harness missed {kill_phase}; "
        f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
    )
    if database_path.exists():
        observed = verify_sqlite_database(database_path)
        assert observed["integrity"] == ["ok"]
        assert observed["foreign_key_violation_count"] == 0
        assert _sha256_bytes(database_path.read_bytes()) == payload_hash
        assert _business_snapshot(database_path) == legacy_business

    restored = restore_migration_backup(
        database_path,
        backup_path,
        safety_backup_root=safety_root,
        application_version="h1-protocol-test",
    )

    assert restored["ok"] is True
    assert _sha256_bytes(database_path.read_bytes()) == payload_hash
    assert _business_snapshot(database_path) == legacy_business
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.restore-work-*")
    )


def test_tampered_migration_backup_is_rejected_before_restore_changes_live_database(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "tamper.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    backup_path = Path(report.backup_path)
    payload_path = backup_path / "payload" / "database.sqlite3"
    payload_path.write_bytes(payload_path.read_bytes() + b"synthetic-tamper")
    live_state = _file_state(database_path)
    live_business = _business_snapshot(database_path)
    safety_backup_root = tmp_path / "restore-safety-backups"

    with pytest.raises(MigrationError) as verify_error:
        verify_migration_backup(backup_path)
    assert verify_error.value.code == "backup_hash_mismatch"

    with pytest.raises(MigrationError) as restore_error:
        restore_migration_backup(
            database_path,
            backup_path,
            safety_backup_root=safety_backup_root,
            application_version="h1-protocol-test",
        )
    assert restore_error.value.code == "backup_hash_mismatch"
    assert _file_state(database_path) == live_state
    assert _business_snapshot(database_path) == live_business
    assert _backup_directories(safety_backup_root) == ()


@pytest.mark.parametrize(
    ("relative_path", "insecure_mode"),
    (
        (".", 0o755),
        ("payload", 0o755),
        ("payload/database.sqlite3", 0o644),
        ("manifest.json", 0o644),
        ("COMPLETE", 0o644),
    ),
)
def test_backup_verifier_rejects_group_or_world_accessible_artifacts(
    relative_path: str,
    insecure_mode: int,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "permissions.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    backup_path = Path(report.backup_path)
    target = backup_path if relative_path == "." else backup_path / relative_path
    os.chmod(target, insecure_mode)
    backup_state = {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(MigrationError) as raised:
        verify_migration_backup(backup_path)

    assert raised.value.code == "unsafe_backup_permissions"
    assert {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    } == backup_state


def test_self_consistent_fk_invalid_backup_is_rejected_even_when_manifest_matches(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "fk-backup.sqlite3")
    report = _migrate(database_path, tmp_path / "migration-backups")
    assert report.backup_path is not None
    backup_path = Path(report.backup_path)
    payload_path = backup_path / "payload" / "database.sqlite3"
    with sqlite3.connect(payload_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE tasks SET stage_id=999999 WHERE id=1")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(violations) == 1
    assert violations[0][0] == "tasks"
    manifest = _rewrite_backup_manifest_for_payload(backup_path)
    assert manifest["files"][0]["foreign_key_violation_count"] == 1
    backup_state = {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(MigrationError) as raised:
        verify_migration_backup(backup_path)

    assert raised.value.code == "foreign_key_failed"
    assert {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    } == backup_state


@pytest.mark.parametrize("lock_kind", ("symlink", "hardlink"))
@pytest.mark.parametrize("lock_scope", ("migration", "backup_root"))
def test_unsafe_lock_paths_fail_closed_without_touching_the_link_target(
    lock_scope: str,
    lock_kind: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "managed" / "locked.sqlite3",
    )
    backup_root = tmp_path / "migration-backups"
    if lock_scope == "migration":
        lock_path = database_path.with_name(f".{database_path.name}.migration.lock")
    else:
        backup_root.mkdir(mode=0o700)
        lock_path = backup_root / ".migration-backup.lock"
    outside_target = tmp_path / f"outside-{lock_scope}-{lock_kind}.sentinel"
    outside_target.write_bytes(b"external lock target must remain unchanged\n")
    os.chmod(outside_target, 0o640)
    if lock_kind == "symlink":
        lock_path.symlink_to(outside_target)
    else:
        os.link(outside_target, lock_path)
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    target_state = (
        outside_target.read_bytes(),
        outside_target.stat().st_mode & 0o777,
        outside_target.stat().st_nlink,
        outside_target.stat().st_ino,
    )

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "unsafe_lock_path"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert os.path.lexists(lock_path)
    assert (
        outside_target.read_bytes(),
        outside_target.stat().st_mode & 0o777,
        outside_target.stat().st_nlink,
        outside_target.stat().st_ino,
    ) == target_state
    assert _backup_directories(backup_root) == ()
    assert not tuple(backup_root.glob(".staging-*"))


@pytest.mark.parametrize("tamper_timing", ("before_replace", "after_replace"))
@pytest.mark.parametrize("validator", ("sha256", "sqlite_health"))
def test_tampered_rollback_staging_never_claims_the_source_was_restored(
    validator: str,
    tamper_timing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "rollback-tamper.sqlite3")
    backup_root = tmp_path / "migration-backups"
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)
    trusted_source_sha256 = migration_module._sha256_file(database_path)
    real_copy = migration_module._copy_file_secure
    real_sha256 = migration_module._sha256_file
    tampered = False

    def corrupt_sqlite_header(path: Path) -> None:
        nonlocal tampered
        with Path(path).open("r+b") as stream:
            stream.seek(0)
            stream.write(b"not a sqlite database\x00rollback-tamper")
            stream.flush()
            os.fsync(stream.fileno())
        tampered = True

    def copy_then_tamper(source: Path, destination: Path, **kwargs: Any) -> None:
        real_copy(source, destination, **kwargs)
        if (
            tamper_timing == "before_replace"
            and ".rollback-publish-" in Path(destination).name
        ):
            corrupt_sqlite_header(destination)

    def synthetic_hash_collision(path: Path) -> str:
        candidate = Path(path)
        if validator == "sqlite_health" and tampered and (
            ".rollback-publish-" in candidate.name
            or candidate.resolve() == database_path.resolve()
        ):
            return trusted_source_sha256
        return real_sha256(candidate)

    def reject_publish_then_tamper_rollback(point: str) -> None:
        if point == "after_publish":
            raise RuntimeError("synthetic post-publish rejection")
        if point == "after_rollback_publish" and tamper_timing == "after_replace":
            corrupt_sqlite_header(database_path)

    monkeypatch.setattr(migration_module, "_copy_file_secure", copy_then_tamper)
    if validator == "sqlite_health":
        monkeypatch.setattr(migration_module, "_sha256_file", synthetic_hash_collision)

    with pytest.raises(MigrationError) as raised:
        migrate_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-rollback-tamper-test",
            fault_injector=reject_publish_then_tamper_rollback,
        )

    assert tampered is True
    assert raised.value.code == "recovery_required"
    assert raised.value.recovery_backup is not None
    recovery_backup = Path(raised.value.recovery_backup)
    manifest = verify_migration_backup(recovery_backup)
    assert manifest["source"]["schema_checksum"] == source_checksum
    recovery_payload = recovery_backup / "payload" / "database.sqlite3"
    assert _business_snapshot(recovery_payload) == source_business
    assert migration_module._sha256_file(recovery_payload) == manifest["payload_sha256"]
    if tamper_timing == "before_replace":
        _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
        migrated_business = _business_snapshot(
            database_path,
            columns=source_business.columns,
        )
        assert migrated_business.cardinalities == source_business.cardinalities
        assert migrated_business.content_digests == source_business.content_digests
    else:
        with pytest.raises(MigrationError) as live_error:
            verify_sqlite_database(database_path)
        assert live_error.value.code == "sqlite_integrity_failed"


def test_shared_backup_root_serializes_same_identity_staging_across_checkouts(
    tmp_path: Path,
) -> None:
    first_database = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "checkout-a" / "shared.sqlite3",
    )
    second_database = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "checkout-b" / "shared.sqlite3",
    )
    first_business = _business_snapshot(first_database)
    second_business = _business_snapshot(second_database)
    backup_root = tmp_path / "shared-backups"
    first_ready = tmp_path / "first-backup-stage-ready"
    first_release = tmp_path / "first-backup-stage-release"
    second_ready = tmp_path / "second-backup-stage-ready"
    second_release = tmp_path / "second-backup-stage-release"
    environment = _subprocess_environment(tmp_path / "shared-config.sqlite3")

    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            MIGRATOR_PAUSE_AT_PHASE_PROGRAM,
            str(first_database),
            str(backup_root),
            "after_backup_staging_create",
            str(first_ready),
            str(first_release),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None
    try:
        _wait_for_marker(first, first_ready)
        first_staging = tuple(backup_root.glob(".staging-shared.sqlite3-*"))
        assert len(first_staging) == 1
        staging_path = first_staging[0]
        staging_inode = (staging_path.stat().st_dev, staging_path.stat().st_ino)
        _assert_private_directory_tree(staging_path)

        second = subprocess.Popen(
            [
                sys.executable,
                "-c",
                MIGRATOR_PAUSE_AT_PHASE_PROGRAM,
                str(second_database),
                str(backup_root),
                "after_backup_staging_create",
                str(second_ready),
                str(second_release),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 20.0
        while second.poll() is None and not second_ready.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("second migrator did not reach backup-root lock outcome")
            time.sleep(0.02)
        assert not second_ready.exists(), (
            "the second migrator entered backup staging while the first process "
            "still owned the shared backup-root lease"
        )
        assert second.poll() == 0
        second_stdout, second_stderr = second.communicate(timeout=30)
        second_outcome = json.loads(second_stdout.strip().splitlines()[-1])
        assert second_outcome["kind"] == "error", second_stderr
        assert second_outcome["code"] == "backup_busy"
        assert second_outcome["recovery_backup"] is None
        assert staging_path.is_dir()
        assert (staging_path.stat().st_dev, staging_path.stat().st_ino) == staging_inode
        _assert_private_directory_tree(staging_path)
        assert _file_state(second_database)
        assert _business_snapshot(second_database) == second_business
    finally:
        first_release.write_text("release\n", encoding="ascii")
        second_release.write_text("release\n", encoding="ascii")
        if second is not None and second.poll() is None:
            second.kill()
            second.communicate()

    first_stdout, first_stderr = first.communicate(timeout=90)
    assert first.returncode == 0, first_stderr
    first_outcome = json.loads(first_stdout.strip().splitlines()[-1])
    assert first_outcome["kind"] == "report", first_outcome
    assert first_outcome["report"]["applied"] is True

    second_report = _migrate(second_database, backup_root)

    assert second_report.applied is True
    assert second_report.backup_path == first_outcome["report"]["backup_path"]
    assert len(_backup_directories(backup_root)) == 1
    backup_path = Path(second_report.backup_path)
    manifest = verify_migration_backup(backup_path)
    assert manifest["source"]["database_identity"] == "shared.sqlite3"
    assert not tuple(backup_root.glob(".staging-*"))
    _assert_verified(first_database, FROZEN_H1_SCHEMA_CHECKSUM)
    _assert_verified(second_database, FROZEN_H1_SCHEMA_CHECKSUM)
    for database_path, original_business in (
        (first_database, first_business),
        (second_database, second_business),
    ):
        migrated = _business_snapshot(database_path, columns=original_business.columns)
        assert migrated.cardinalities == original_business.cardinalities
        assert migrated.content_digests == original_business.content_digests


def test_restore_detects_writer_in_guard_release_gap_and_preserves_commit(
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "restore-gap.sqlite3")
    migration = _migrate(database_path, tmp_path / "migration-backups")
    assert migration.backup_path is not None
    backup_path = Path(migration.backup_path)
    safety_root = tmp_path / "restore-safety-backups"
    ready_marker = tmp_path / "restore-gap-ready"
    release_marker = tmp_path / "restore-gap-release"
    restorer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            RESTORER_PAUSE_AT_PHASE_PROGRAM,
            str(database_path),
            str(backup_path),
            str(safety_root),
            "after_writer_guard_release",
            str(ready_marker),
            str(release_marker),
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "restore-gap-config.sqlite3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(restorer, ready_marker)
        writer = _run_concurrent_writer(
            database_path,
            "Committed during restore guard-release gap",
            tmp_path / "restore-gap-writer-config.sqlite3",
        )
    finally:
        release_marker.write_text("release\n", encoding="ascii")
    stdout, stderr = restorer.communicate(timeout=90)

    assert restorer.returncode == 0, stderr
    assert writer.returncode == 0, writer.stderr
    assert writer.stdout.strip() == "committed"
    outcome = json.loads(stdout.strip().splitlines()[-1])
    assert outcome["kind"] == "error", outcome
    assert outcome["code"] == "source_changed_during_migration"
    assert outcome["recovery_backup"] is not None
    verify_migration_backup(Path(outcome["recovery_backup"]))
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == ("Committed during restore guard-release gap",)
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.restore-work-*")
    )


def test_restore_blocks_new_inode_writer_until_payload_is_verified(
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(tmp_path / "restore-new-inode.sqlite3")
    migration = _migrate(database_path, tmp_path / "migration-backups")
    assert migration.backup_path is not None
    backup_path = Path(migration.backup_path)
    manifest = verify_migration_backup(backup_path)
    payload = backup_path / "payload" / "database.sqlite3"
    payload_sha256 = _sha256_bytes(payload.read_bytes())
    payload_business = _business_snapshot(payload)
    source_stat = database_path.stat()
    source_inode = (source_stat.st_dev, source_stat.st_ino)
    safety_root = tmp_path / "restore-safety-backups"
    ready_marker = tmp_path / "restore-new-inode-ready"
    release_marker = tmp_path / "restore-new-inode-release"
    restorer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            RESTORER_PAUSE_AT_PHASE_PROGRAM,
            str(database_path),
            str(backup_path),
            str(safety_root),
            "after_main_replace",
            str(ready_marker),
            str(release_marker),
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "restore-new-inode-config.sqlite3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(restorer, ready_marker)
        published_stat = database_path.stat()
        assert (published_stat.st_dev, published_stat.st_ino) != source_inode
        writer = _run_concurrent_writer(
            database_path,
            "Forbidden restore new-inode write",
            tmp_path / "restore-new-inode-writer-config.sqlite3",
        )
    finally:
        release_marker.write_text("release\n", encoding="ascii")
    stdout, stderr = restorer.communicate(timeout=90)

    assert restorer.returncode == 0, stderr
    assert writer.returncode == 75, (
        "a writer acquired the restored inode before payload verification; "
        f"stdout={writer.stdout!r}, stderr={writer.stderr!r}"
    )
    assert writer.stdout.strip() == "blocked"
    outcome = json.loads(stdout.strip().splitlines()[-1])
    assert outcome["kind"] == "result", outcome
    assert outcome["result"]["ok"] is True
    assert outcome["result"]["schema_checksum"] == manifest["source"][
        "schema_checksum"
    ]
    assert _sha256_bytes(database_path.read_bytes()) == payload_sha256
    assert _business_snapshot(database_path) == payload_business
    observed = verify_sqlite_database(database_path)
    assert observed["integrity"] == ["ok"]
    assert observed["foreign_key_violation_count"] == 0
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.restore-work-*")
    )


def test_restore_absent_target_creator_wins_before_reservation_without_overwrite(
    tmp_path: Path,
) -> None:
    source = _materialize_partial_m13(
        tmp_path / "backup-source" / "absent-target.sqlite3"
    )
    migration = _migrate(source, tmp_path / "migration-backups")
    assert migration.backup_path is not None
    backup_path = Path(migration.backup_path)
    target = tmp_path / "restore-target" / "absent-target.sqlite3"
    target.parent.mkdir(parents=True)
    external_template = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "external-template.sqlite3",
    )
    with sqlite3.connect(external_template) as connection:
        connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            ("External absent-target creator", "fixture-owner"),
        )
    external_state = _file_state(external_template)
    external_business = _business_snapshot(external_template)
    external_checksum = schema_checksum(external_template)
    safety_root = tmp_path / "restore-safety-backups"
    ready_marker = tmp_path / "restore-absent-ready"
    release_marker = tmp_path / "restore-absent-release"
    restorer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            RESTORER_PAUSE_AT_PHASE_PROGRAM,
            str(target),
            str(backup_path),
            str(safety_root),
            "before_absent_reservation",
            str(ready_marker),
            str(release_marker),
        ],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / "restore-absent-config.sqlite3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(restorer, ready_marker)
        creator = subprocess.run(
            [
                sys.executable,
                "-c",
                ABSENT_DATABASE_CREATOR_PROGRAM,
                str(external_template),
                str(target),
            ],
            cwd=PROJECT_ROOT,
            env=_subprocess_environment(tmp_path / "creator-config.sqlite3"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        release_marker.write_text("release\n", encoding="ascii")
    stdout, stderr = restorer.communicate(timeout=90)

    assert creator.returncode == 0, creator.stderr
    assert creator.stdout.strip() == "created"
    assert restorer.returncode == 0, stderr
    outcome = json.loads(stdout.strip().splitlines()[-1])
    assert outcome["kind"] == "error", outcome
    assert outcome["code"] == "source_changed_during_migration"
    assert outcome["recovery_backup"] is None
    assert _file_state(target) == external_state
    assert _business_snapshot(target) == external_business
    assert schema_checksum(target) == external_checksum
    assert _backup_directories(safety_root) == ()
    assert not tuple(target.parent.glob(f".{target.name}.restore-work-*"))


@pytest.mark.parametrize(
    "replacement_phase",
    (
        "after_backup_staging_create",
        "after_journal_canonicalization",
        "after_main_replace",
    ),
    ids=("guard-held", "late-after-journal", "post-publish-path"),
)
@pytest.mark.parametrize("operation", ("migrate", "restore"))
def test_guarded_source_inode_replacement_is_detected_without_clobbering_replacer(
    operation: str,
    replacement_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(
        tmp_path / operation / "guard-replaced.sqlite3"
    )
    migration_backups = tmp_path / "migration-backups"
    restore_backup: Path | None = None
    if operation == "restore":
        migrated = _migrate(database_path, migration_backups)
        assert migrated.backup_path is not None
        restore_backup = Path(migrated.backup_path)
    replacement = _materialize_sql(
        "v1_1_1_full",
        tmp_path / f"{operation}-replacement.sqlite3",
    )
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            (f"External {operation} inode replacement", "fixture-owner"),
        )
    replacement_state = _file_state(replacement)
    replacement_business = _business_snapshot(replacement)
    replacement_checksum = schema_checksum(replacement)
    operation_backups = tmp_path / f"{operation}-operation-backups"
    ready_marker = tmp_path / f"{operation}-guard-replace-ready"
    release_marker = tmp_path / f"{operation}-guard-replace-release"
    if operation == "migrate":
        command = [
            sys.executable,
            "-c",
            MIGRATOR_PAUSE_AT_PHASE_PROGRAM,
            str(database_path),
            str(operation_backups),
            replacement_phase,
            str(ready_marker),
            str(release_marker),
        ]
    else:
        assert restore_backup is not None
        command = [
            sys.executable,
            "-c",
            RESTORER_PAUSE_AT_PHASE_PROGRAM,
            str(database_path),
            str(restore_backup),
            str(operation_backups),
            replacement_phase,
            str(ready_marker),
            str(release_marker),
        ]
    coordinator = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(tmp_path / f"{operation}-replace-config.sqlite3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(coordinator, ready_marker)
        replacer = subprocess.run(
            [
                sys.executable,
                "-c",
                ATOMIC_DATABASE_REPLACER_PROGRAM,
                str(replacement),
                str(database_path),
            ],
            cwd=PROJECT_ROOT,
            env=_subprocess_environment(tmp_path / f"{operation}-replacer-config.sqlite3"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        release_marker.write_text("release\n", encoding="ascii")
    stdout, stderr = coordinator.communicate(timeout=90)

    assert replacer.returncode == 0, replacer.stderr
    assert replacer.stdout.strip() == "replaced"
    assert coordinator.returncode == 0, stderr
    outcome = json.loads(stdout.strip().splitlines()[-1])
    assert outcome["kind"] == "error", outcome
    assert outcome["code"] == "source_changed_during_migration"
    assert outcome["recovery_backup"] is not None
    verify_migration_backup(Path(outcome["recovery_backup"]))
    assert _file_state(database_path) == replacement_state
    assert _business_snapshot(database_path) == replacement_business
    assert schema_checksum(database_path) == replacement_checksum
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == (f"External {operation} inode replacement",)


def test_view_only_database_is_not_misclassified_as_empty(tmp_path: Path) -> None:
    database_path = tmp_path / "view-only.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE VIEW synthetic_marker AS SELECT 41 + 1 AS value")
    source_state = _file_state(database_path)
    source_checksum = schema_checksum(database_path)
    backup_root = tmp_path / "migration-backups"

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "unknown_legacy_schema"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert schema_checksum(database_path) == source_checksum
    assert _backup_directories(backup_root) == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM synthetic_marker").fetchone() == (42,)


@pytest.mark.parametrize("original_state", ("existing", "absent"))
@pytest.mark.parametrize("operation", ("migrate", "restore"))
def test_rollback_path_ownership_change_preserves_external_database(
    operation: str,
    original_state: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / operation / "rollback-owned.sqlite3"
    backup_root = tmp_path / f"{operation}-backups"
    restore_backup: Path | None = None
    if operation == "restore" or original_state == "existing":
        _materialize_partial_m13(database_path)
    if operation == "restore":
        migrated = _migrate(database_path, tmp_path / "restore-source-backups")
        assert migrated.backup_path is not None
        restore_backup = Path(migrated.backup_path)
        if original_state == "absent":
            database_path.unlink()
    external_template = _materialize_sql(
        "v1_1_1_full",
        tmp_path / f"{operation}-{original_state}-external.sqlite3",
    )
    with sqlite3.connect(external_template) as connection:
        connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            (
                f"External {operation} {original_state} rollback owner",
                "fixture-owner",
            ),
        )
    os.chmod(external_template, 0o600)
    external_state = _file_state(external_template)
    external_business = _business_snapshot(external_template)
    external_checksum = schema_checksum(external_template)
    external_inode = (
        external_template.stat().st_dev,
        external_template.stat().st_ino,
    )
    actor_result: subprocess.CompletedProcess[str] | None = None

    def reject_publish_and_change_rollback_owner(point: str) -> None:
        nonlocal actor_result
        should_replace = (
            original_state == "existing" and point == "before_rollback_publish"
        )
        should_recreate = (
            original_state == "absent" and point == "after_rollback_publish"
        )
        if (should_replace or should_recreate) and actor_result is None:
            program = (
                ATOMIC_DATABASE_REPLACER_PROGRAM
                if should_replace
                else ABSENT_DATABASE_CREATOR_PROGRAM
            )
            actor_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(external_template),
                    str(database_path),
                ],
                cwd=PROJECT_ROOT,
                env=_subprocess_environment(
                    tmp_path / f"{operation}-{original_state}-actor-config.sqlite3"
                ),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if operation == "migrate" and point == "after_publish":
            raise RuntimeError("synthetic migration publication rejection")
        if operation == "restore" and point == "after_publish_verify":
            raise RuntimeError("synthetic restore publication rejection")

    with pytest.raises(MigrationError) as raised:
        if operation == "migrate":
            migrate_sqlite_database(
                database_path,
                backup_root=backup_root,
                application_version="h1-rollback-owner-test",
                fault_injector=reject_publish_and_change_rollback_owner,
            )
        else:
            assert restore_backup is not None
            restore_migration_backup(
                database_path,
                restore_backup,
                safety_backup_root=backup_root,
                application_version="h1-rollback-owner-test",
                fault_injector=reject_publish_and_change_rollback_owner,
            )

    assert actor_result is not None
    assert actor_result.returncode == 0, actor_result.stderr
    assert actor_result.stdout.strip() in {"created", "replaced"}
    assert raised.value.code == "recovery_required"
    if raised.value.recovery_backup is not None:
        verify_migration_backup(Path(raised.value.recovery_backup))
    assert _file_state(database_path) == external_state
    assert _business_snapshot(database_path) == external_business
    assert schema_checksum(database_path) == external_checksum
    target_stat = database_path.stat()
    if original_state == "existing":
        assert (target_stat.st_dev, target_stat.st_ino) == external_inode
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM owners WHERE id='fixture-owner'"
        ).fetchone() == (
            f"External {operation} {original_state} rollback owner",
        )


def test_current_schema_noop_revalidates_guarded_path_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_sql("empty", tmp_path / "noop-replaced.sqlite3")
    _migrate(database_path, tmp_path / "initial-backups")
    replacement = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "noop-external.sqlite3",
    )
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "UPDATE owners SET display_name=? WHERE id=?",
            ("External no-op path replacement", "fixture-owner"),
        )
    os.chmod(replacement, 0o600)
    replacement_state = _file_state(replacement)
    replacement_business = _business_snapshot(replacement)
    replacement_checksum = schema_checksum(replacement)
    replacement_inode = (replacement.stat().st_dev, replacement.stat().st_ino)
    real_verify = migration_module.verify_sqlite_database
    actor_result: subprocess.CompletedProcess[str] | None = None

    def verify_then_replace_live_path(
        path: Path,
        *,
        expected_schema_checksum: str | None = None,
    ) -> dict[str, Any]:
        nonlocal actor_result
        result = real_verify(
            path,
            expected_schema_checksum=expected_schema_checksum,
        )
        if Path(path).name == "source-snapshot.sqlite3" and actor_result is None:
            actor_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    ATOMIC_DATABASE_REPLACER_PROGRAM,
                    str(replacement),
                    str(database_path),
                ],
                cwd=PROJECT_ROOT,
                env=_subprocess_environment(tmp_path / "noop-actor-config.sqlite3"),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        return result

    monkeypatch.setattr(
        migration_module,
        "verify_sqlite_database",
        verify_then_replace_live_path,
    )
    backup_root = tmp_path / "noop-backups"

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert actor_result is not None
    assert actor_result.returncode == 0, actor_result.stderr
    assert actor_result.stdout.strip() == "replaced"
    assert raised.value.code == "source_changed_during_migration"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == replacement_state
    assert _business_snapshot(database_path) == replacement_business
    assert schema_checksum(database_path) == replacement_checksum
    assert (database_path.stat().st_dev, database_path.stat().st_ino) == replacement_inode
    assert _backup_directories(backup_root) == ()


@pytest.mark.parametrize(
    "replacement_phase",
    ("after_backup_publish", "after_backup_verify"),
)
@pytest.mark.parametrize("operation", ("direct_backup", "migrate"))
def test_backup_final_directory_replacement_is_never_reported_as_success(
    operation: str,
    replacement_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / operation / "backup-final-owner.sqlite3",
    )
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    backup_root = tmp_path / f"{operation}-backups"
    external_directory = tmp_path / f"{operation}-{replacement_phase}-external"
    external_directory.mkdir(mode=0o700)
    sentinel = external_directory / "external-owner.txt"
    sentinel.write_text("external backup directory owner\n", encoding="utf-8")
    os.chmod(sentinel, 0o600)
    external_inode = (
        external_directory.stat().st_dev,
        external_directory.stat().st_ino,
    )
    quarantine_root = tmp_path / f"{operation}-{replacement_phase}-quarantine"
    quarantine_root.mkdir(mode=0o700)
    quarantined: Path | None = None
    replaced_final: Path | None = None

    def replace_published_backup(point: str) -> None:
        nonlocal quarantined, replaced_final
        if point != replacement_phase or replaced_final is not None:
            return
        finals = _backup_directories(backup_root)
        assert len(finals) == 1
        replaced_final = finals[0]
        quarantined = quarantine_root / replaced_final.name
        os.replace(replaced_final, quarantined)
        os.replace(external_directory, replaced_final)

    with pytest.raises(MigrationError) as raised:
        if operation == "direct_backup":
            backup_sqlite_database(
                database_path,
                backup_root=backup_root,
                application_version="h1-backup-final-owner-test",
                fault_injector=replace_published_backup,
            )
        else:
            migrate_sqlite_database(
                database_path,
                backup_root=backup_root,
                application_version="h1-backup-final-owner-test",
                fault_injector=replace_published_backup,
            )

    assert replaced_final is not None
    assert raised.value.code == "recovery_required"
    assert raised.value.recovery_backup is None
    assert replaced_final.is_dir()
    assert (replaced_final.stat().st_dev, replaced_final.stat().st_ino) == external_inode
    assert (replaced_final / "external-owner.txt").read_text(encoding="utf-8") == (
        "external backup directory owner\n"
    )
    assert quarantined is not None
    verify_migration_backup(quarantined)
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)


def test_candidate_builder_exception_is_wrapped_with_verified_recovery_backup(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "candidate-exception.sqlite3",
    )
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)

    def reject_candidate(point: str) -> None:
        if point == "before_create":
            raise RuntimeError("synthetic candidate builder exception")

    with pytest.raises(MigrationError) as raised:
        migrate_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-caught-exception-test",
            fault_injector=reject_candidate,
        )

    assert raised.value.code == "migration_failed"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.recovery_backup is not None
    verify_migration_backup(Path(raised.value.recovery_backup))
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)


def test_guard_release_exception_is_wrapped_as_journal_failure_with_backup(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "guard-release-exception.sqlite3",
    )
    backup_root = tmp_path / "migration-backups"
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)

    def reject_guard_release(point: str) -> None:
        if point == "after_writer_guard_release":
            raise RuntimeError("synthetic guard-release exception")

    with pytest.raises(MigrationError) as raised:
        migrate_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-caught-exception-test",
            fault_injector=reject_guard_release,
        )

    assert raised.value.code == "journal_canonicalization_failed"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.recovery_backup is not None
    verify_migration_backup(Path(raised.value.recovery_backup))
    assert _business_snapshot(database_path) == source_business
    assert schema_checksum(database_path) == source_checksum
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    (("migrate", "migration_failed"), ("restore", "restore_failed")),
)
def test_after_journal_exception_is_stable_and_attaches_recovery_backup(
    operation: str,
    expected_code: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(
        tmp_path / operation / "after-journal-exception.sqlite3"
    )
    operation_backup_root = tmp_path / f"{operation}-operation-backups"
    restore_backup: Path | None = None
    if operation == "restore":
        migrated = _migrate(database_path, tmp_path / "restore-source-backups")
        assert migrated.backup_path is not None
        restore_backup = Path(migrated.backup_path)
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)

    def reject_after_journal(point: str) -> None:
        if point == "after_journal_canonicalization":
            raise RuntimeError("synthetic after-journal exception")

    with pytest.raises(MigrationError) as raised:
        if operation == "migrate":
            migrate_sqlite_database(
                database_path,
                backup_root=operation_backup_root,
                application_version="h1-caught-exception-test",
                fault_injector=reject_after_journal,
            )
        else:
            assert restore_backup is not None
            restore_migration_backup(
                database_path,
                restore_backup,
                safety_backup_root=operation_backup_root,
                application_version="h1-caught-exception-test",
                fault_injector=reject_after_journal,
            )

    assert raised.value.code == expected_code
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.recovery_backup is not None
    verify_migration_backup(Path(raised.value.recovery_backup))
    assert _business_snapshot(database_path) == source_business
    assert schema_checksum(database_path) == source_checksum


@pytest.mark.parametrize(
    ("path_name", "expected_checksum"),
    (
        (
            "metadata_only",
            "fb12a30b50595c4e61a06424eb27d4ad586a047a1f9d917fb0bcf7ecb3236b3c",
        ),
        (
            "fresh_create_schema",
            "40063b72061ee0a42dd4f7fccdd4e8d9e64a346ca70452041b579e05d2805d89",
        ),
        (
            "v1_additive_upgrade",
            "21150d5fff8e32807d38e1902f2257cf41154ab47a44bc497a2436002cd9f04e",
        ),
    ),
)
def test_all_pre_h1_schema_paths_have_frozen_registry_provenance(
    path_name: str,
    expected_checksum: str,
    tmp_path: Path,
) -> None:
    manifest = json.loads(
        (FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    provenance = manifest["pre_h1_schema_fixtures"]
    assert provenance["source_revision"] == "baf2564"
    assert provenance["source_artifact_sha256"] == {
        "backend/app/models/entities.py": (
            "b59ab54b70fa054476bc2c4c817bf2a82eee7f0b5a98e6da1584b6cb91bab47f"
        ),
        "backend/app/db/migrations.py": (
            "26c9b02be53701a32750c782b830207d49498960df8dacb7bb9203ff4792b745"
        ),
        "tests/fixtures/databases/v1_1_1_full.sql": (
            "117ebeb7db3f9d1446faf768d40d4153d97acb68108e72afff1d44cb0740f972"
        ),
    }
    assert _sha256_bytes((FIXTURE_ROOT / "v1_1_1_full.sql").read_bytes()) == (
        provenance["source_artifact_sha256"]
        ["tests/fixtures/databases/v1_1_1_full.sql"]
    )
    assert set(provenance["schemas"]) == {
        "metadata_only",
        "fresh_create_schema",
        "v1_additive_upgrade",
    }
    registered = {
        revision.checksum
        for revision in LEGACY_SCHEMA_REGISTRY
        if revision.kind == "pre_h1_current"
    }
    frozen = provenance["schemas"][path_name]
    source = FIXTURE_ROOT / frozen["file"]
    ddl_bytes = source.read_bytes()
    ddl = ddl_bytes.decode("utf-8")
    assert source.is_relative_to(FIXTURE_ROOT / "pre_h1")
    assert source.stat().st_size == frozen["size_bytes"]
    assert _sha256_bytes(ddl_bytes) == frozen["ddl_sha256"]
    assert re.search(r"\bINSERT\b", ddl, flags=re.IGNORECASE) is None
    assert frozen["semantic_checksum"] == expected_checksum
    assert expected_checksum in registered
    database_path = tmp_path / f"{path_name}.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript("PRAGMA foreign_keys=OFF;\n" + ddl)
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        table_names = _table_names(connection)
        assert all(
            connection.execute(
                f'SELECT count(*) FROM "{table_name}"'
            ).fetchone() == (0,)
            for table_name in table_names
        )
    assert {"owners", "learning_events", "evidence_observations"} <= table_names
    assert "schema_migrations" not in table_names
    assert schema_checksum(database_path) == expected_checksum
    assert migration_module._classify_source(
        database_path,
        source_history=[],
    ) == "pre_h1_current"
    backup_root = tmp_path / "migration-backups"

    first = _migrate(database_path, backup_root)

    assert first.applied is True
    assert first.source_kind == "pre_h1_current"
    assert first.source_schema_checksum == expected_checksum
    assert first.backup_path is not None
    verify_migration_backup(Path(first.backup_path))
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    before_noop = _file_state(database_path)
    history_before_noop = _history_rows(database_path)

    second = _migrate(database_path, backup_root)

    assert second.applied is False
    assert second.source_kind == "versioned"
    assert second.backup_path is None
    assert _file_state(database_path) == before_noop
    assert _history_rows(database_path) == history_before_noop
    assert len(_backup_directories(backup_root)) == 1


def test_v111_with_unknown_check_constraint_fails_as_semantic_schema_drift(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "unknown-check.sqlite3",
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN IMMEDIATE;
            CREATE TABLE owners_with_unknown_check (
                id VARCHAR(64) NOT NULL,
                display_name VARCHAR(120) NOT NULL,
                timezone VARCHAR(64) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT ck_unknown_timezone CHECK (length(timezone) > 0)
            );
            INSERT INTO owners_with_unknown_check
                (id, display_name, timezone, created_at)
            SELECT id, display_name, timezone, created_at FROM owners;
            DROP TABLE owners;
            ALTER TABLE owners_with_unknown_check RENAME TO owners;
            COMMIT;
            PRAGMA foreign_keys=ON;
            """
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        owner_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='owners'"
        ).fetchone()[0]
    assert "ck_unknown_timezone CHECK" in owner_sql
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)

    with pytest.raises(MigrationError) as raised:
        _migrate(database_path, backup_root)

    assert raised.value.code == "unknown_legacy_schema"
    assert raised.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert _backup_directories(backup_root) == ()


@pytest.mark.parametrize(
    ("field_path", "boolean_value"),
    (
        (("manifest_version",), True),
        (("source", "schema_version"), False),
        (("source", "sqlite_user_version"), False),
        (("files", 0, "size_bytes"), True),
        (("files", 0, "foreign_key_violation_count"), False),
    ),
    ids=(
        "manifest-version-true",
        "schema-version-false",
        "sqlite-user-version-false",
        "size-bytes-true",
        "foreign-key-count-false",
    ),
)
def test_backup_manifest_rejects_boolean_values_for_integer_fields(
    field_path: tuple[str | int, ...],
    boolean_value: bool,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "manifest-bool.sqlite3",
    )
    backup_path = backup_sqlite_database(
        database_path,
        backup_root=tmp_path / "migration-backups",
        application_version="h1-manifest-type-test",
    )
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target: Any = manifest
    for component in field_path[:-1]:
        target = target[component]
    target[field_path[-1]] = boolean_value
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    manifest_path.write_bytes(manifest_bytes)
    (backup_path / "COMPLETE").write_text(
        _sha256_bytes(manifest_bytes) + "\n",
        encoding="ascii",
    )
    backup_state = {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(MigrationError) as raised:
        verify_migration_backup(backup_path)

    assert raised.value.code == "invalid_backup_manifest"
    assert {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    } == backup_state


def test_backup_manifest_rejects_non_string_purpose_before_restore(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "manifest-purpose.sqlite3",
    )
    backup_root = tmp_path / "migration-backups"
    backup_path = backup_sqlite_database(
        database_path,
        backup_root=backup_root,
        application_version="h1-manifest-purpose-test",
    )
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["purpose"] = ["pre_migration"]
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    manifest_path.write_bytes(manifest_bytes)
    (backup_path / "COMPLETE").write_text(
        _sha256_bytes(manifest_bytes) + "\n",
        encoding="ascii",
    )
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    backup_state = {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(MigrationError) as verify_error:
        verify_migration_backup(backup_path)
    assert verify_error.value.code == "invalid_backup_manifest"

    with pytest.raises(MigrationError) as restore_error:
        restore_migration_backup(
            database_path,
            backup_path,
            safety_backup_root=tmp_path / "before-restore",
        )

    assert restore_error.value.code == "invalid_backup_manifest"
    assert restore_error.value.recovery_backup is None
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert {
        path.relative_to(backup_path).as_posix(): _sha256_bytes(path.read_bytes())
        for path in backup_path.rglob("*")
        if path.is_file()
    } == backup_state
    assert not (tmp_path / "before-restore").exists()


@pytest.mark.parametrize("operation", ("direct_backup", "migrate"))
def test_in_place_payload_tamper_after_first_verify_cannot_publish_backup(
    operation: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / operation / "payload-tamper.sqlite3",
    )
    backup_root = tmp_path / f"{operation}-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    tampered_inode: tuple[int, int] | None = None

    def tamper_payload_in_place(point: str) -> None:
        nonlocal tampered_inode
        if point != "after_backup_verify" or tampered_inode is not None:
            return
        backups = _backup_directories(backup_root)
        assert len(backups) == 1
        payload = backups[0] / "payload" / "database.sqlite3"
        before = payload.stat()
        with payload.open("ab") as stream:
            stream.write(b"same-inode synthetic tamper")
            stream.flush()
            os.fsync(stream.fileno())
        after = payload.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        tampered_inode = after.st_dev, after.st_ino

    with pytest.raises(MigrationError) as raised:
        if operation == "direct_backup":
            backup_sqlite_database(
                database_path,
                backup_root=backup_root,
                application_version="h1-payload-tamper-test",
                fault_injector=tamper_payload_in_place,
            )
        else:
            migrate_sqlite_database(
                database_path,
                backup_root=backup_root,
                application_version="h1-payload-tamper-test",
                fault_injector=tamper_payload_in_place,
            )

    assert tampered_inode is not None
    assert raised.value.code == "backup_hash_mismatch"
    assert raised.value.recovery_backup is None
    assert _backup_directories(backup_root) == ()
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    with sqlite3.connect(database_path) as connection:
        assert "schema_migrations" not in _table_names(connection)


def test_migration_rejects_data_only_work_snapshot_tamper_before_candidate(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "candidate-work-tamper.sqlite3",
    )
    backup_root = tmp_path / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    tampered_hashes: tuple[str, str] | None = None

    def tamper_source_snapshot(point: str) -> None:
        nonlocal tampered_hashes
        if point != "after_backup_verify" or tampered_hashes is not None:
            return
        snapshots = tuple(
            database_path.parent.glob(
                f".{database_path.name}.migration-work-*/source-snapshot.sqlite3"
            )
        )
        assert len(snapshots) == 1
        tampered_hashes = _tamper_owner_data_without_changing_schema(
            snapshots[0],
            "Tampered candidate source snapshot",
        )

    with pytest.raises(MigrationError) as raised:
        migrate_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-work-snapshot-tamper-test",
            fault_injector=tamper_source_snapshot,
        )

    assert tampered_hashes is not None
    assert raised.value.code == "migration_failed"
    assert raised.value.recovery_backup is not None
    recovery_backup = Path(raised.value.recovery_backup)
    verify_migration_backup(recovery_backup)
    assert _business_snapshot(
        recovery_backup / "payload" / "database.sqlite3"
    ) == source_business
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.migration-work-*")
    )


@pytest.mark.parametrize(
    ("operation", "tamper_phase"),
    (
        ("migrate", "after_copy"),
        ("restore", "after_journal_canonicalization"),
    ),
)
def test_post_handoff_recovery_backup_tamper_fails_before_source_replace(
    operation: str,
    tamper_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(
        tmp_path / operation / "post-handoff-backup-tamper.sqlite3"
    )
    operation_backup_root = tmp_path / f"{operation}-operation-backups"
    restore_backup: Path | None = None
    if operation == "restore":
        migrated = _migrate(database_path, tmp_path / "restore-source-backups")
        assert migrated.backup_path is not None
        restore_backup = Path(migrated.backup_path)
    source_inode = (database_path.stat().st_dev, database_path.stat().st_ino)
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)
    tampered_backup: Path | None = None
    tampered_hashes: tuple[str, str] | None = None

    def tamper_recovery_payload(point: str) -> None:
        nonlocal tampered_backup, tampered_hashes
        if point != tamper_phase or tampered_backup is not None:
            return
        backups = _backup_directories(operation_backup_root)
        assert len(backups) == 1
        tampered_backup = backups[0]
        payload = tampered_backup / "payload" / "database.sqlite3"
        payload_identity = (payload.stat().st_dev, payload.stat().st_ino)
        tampered_hashes = _tamper_owner_data_without_changing_schema(
            payload,
            f"Tampered {operation} recovery backup",
        )
        assert (payload.stat().st_dev, payload.stat().st_ino) == payload_identity

    with pytest.raises(MigrationError) as raised:
        if operation == "migrate":
            migrate_sqlite_database(
                database_path,
                backup_root=operation_backup_root,
                application_version="h1-post-handoff-backup-tamper-test",
                fault_injector=tamper_recovery_payload,
            )
        else:
            assert restore_backup is not None
            restore_migration_backup(
                database_path,
                restore_backup,
                safety_backup_root=operation_backup_root,
                application_version="h1-post-handoff-backup-tamper-test",
                fault_injector=tamper_recovery_payload,
            )

    assert tampered_backup is not None
    assert tampered_hashes is not None
    assert raised.value.code == "recovery_required"
    assert raised.value.recovery_backup is None
    with pytest.raises(MigrationError) as invalid_backup:
        verify_migration_backup(tampered_backup)
    assert invalid_backup.value.code == "backup_hash_mismatch"
    assert (database_path.stat().st_dev, database_path.stat().st_ino) == source_inode
    assert _business_snapshot(database_path) == source_business
    assert schema_checksum(database_path) == source_checksum


@pytest.mark.parametrize(
    ("operation", "expected_code", "work_name"),
    (
        ("migrate", "migration_failed", "source-snapshot.sqlite3"),
        ("restore", "restore_failed", "live.sqlite3"),
    ),
)
def test_data_only_work_snapshot_tamper_cannot_poison_publication_rollback(
    operation: str,
    expected_code: str,
    work_name: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(
        tmp_path / operation / "rollback-work-tamper.sqlite3"
    )
    operation_backup_root = tmp_path / f"{operation}-operation-backups"
    restore_backup: Path | None = None
    if operation == "restore":
        migrated = _migrate(database_path, tmp_path / "restore-source-backups")
        assert migrated.backup_path is not None
        restore_backup = Path(migrated.backup_path)
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)
    tampered_hashes: tuple[str, str] | None = None

    def tamper_work_then_reject_publication(point: str) -> None:
        nonlocal tampered_hashes
        if point != "after_publish_verify" or tampered_hashes is not None:
            return
        work_kind = "migration" if operation == "migrate" else "restore"
        snapshots = tuple(
            database_path.parent.glob(
                f".{database_path.name}.{work_kind}-work-*/{work_name}"
            )
        )
        assert len(snapshots) == 1
        tampered_hashes = _tamper_owner_data_without_changing_schema(
            snapshots[0],
            f"Tampered {operation} rollback work snapshot",
        )
        raise RuntimeError("synthetic publication failure after work tamper")

    with pytest.raises(MigrationError) as raised:
        if operation == "migrate":
            migrate_sqlite_database(
                database_path,
                backup_root=operation_backup_root,
                application_version="h1-rollback-work-tamper-test",
                fault_injector=tamper_work_then_reject_publication,
            )
        else:
            assert restore_backup is not None
            restore_migration_backup(
                database_path,
                restore_backup,
                safety_backup_root=operation_backup_root,
                application_version="h1-rollback-work-tamper-test",
                fault_injector=tamper_work_then_reject_publication,
            )

    assert tampered_hashes is not None
    assert raised.value.code == expected_code
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.recovery_backup is not None
    recovery_backup = Path(raised.value.recovery_backup)
    verify_migration_backup(recovery_backup)
    assert _business_snapshot(
        recovery_backup / "payload" / "database.sqlite3"
    ) == source_business
    assert _business_snapshot(database_path) == source_business
    assert schema_checksum(database_path) == source_checksum
    assert _health(database_path)[:2] == (["ok"], [])
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.*-work-*")
    )


@pytest.mark.parametrize("operation", ("migrate", "direct_backup", "restore"))
def test_sqlite_maintenance_paths_treat_uri_metacharacters_as_literal_names(
    operation: str,
    tmp_path: Path,
) -> None:
    operation_root = tmp_path / operation
    database_parent = operation_root / "managed parent # ? space %"
    database_path = database_parent / "learning # ? space %.sqlite3"
    backup_root = operation_root / "backups # ? space %"
    allowed_roots = [backup_root]

    if operation == "restore":
        _materialize_partial_m13(database_path)
        original_business = _business_snapshot(database_path)
        original_checksum = schema_checksum(database_path)
        seed_backup_root = operation_root / "seed backups # ? space %"
        allowed_roots.append(seed_backup_root)
        migrated = _migrate(database_path, seed_backup_root)
        assert migrated.backup_path is not None
        restore_source = Path(migrated.backup_path)

        restored = restore_migration_backup(
            database_path,
            restore_source,
            safety_backup_root=backup_root,
            application_version="h1-special-path-test",
        )

        assert restored["ok"] is True
        assert restored["code"] == "migration_backup_restored"
        assert _business_snapshot(database_path) == original_business
        assert schema_checksum(database_path) == original_checksum
        verify_migration_backup(Path(restored["safety_backup_path"]))
    elif operation == "migrate":
        _materialize_sql("v1_1_1_full", database_path)

        migrated = migrate_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-special-path-test",
        )

        assert migrated.applied is True
        assert migrated.backup_path is not None
        verify_migration_backup(Path(migrated.backup_path))
        _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)
    else:
        _materialize_sql("v1_1_1_full", database_path)
        source_state = _file_state(database_path)
        source_business = _business_snapshot(database_path)

        backup_path = backup_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-special-path-test",
        )

        manifest = verify_migration_backup(backup_path)
        assert manifest["source"]["database_identity"] == database_path.name
        assert manifest["files"][0]["source_path"] == database_path.name
        assert _file_state(database_path) == source_state
        assert _business_snapshot(database_path) == source_business

    migration_lock = database_path.with_name(
        f".{database_path.name}.migration.lock"
    )
    unexpected_siblings = {
        path.name
        for path in database_parent.iterdir()
        if path not in {database_path, migration_lock}
    }
    assert unexpected_siblings == set()
    allowed_files = {database_path, migration_lock}
    escaped_files = [
        path
        for path in operation_root.rglob("*")
        if path.is_file()
        and path not in allowed_files
        and not any(path.is_relative_to(root) for root in allowed_roots)
    ]
    assert escaped_files == []
    assert _health(database_path)[:2] == (["ok"], [])


@pytest.mark.parametrize(
    ("mutation_name", "mutation_sql"),
    (
        (
            "nonempty-row",
            "INSERT INTO _write_probe (id) VALUES (1);",
        ),
        (
            "extra-column",
            "ALTER TABLE _write_probe ADD COLUMN note TEXT;",
        ),
        (
            "index",
            "CREATE INDEX ix_unknown_write_probe ON _write_probe (id);",
        ),
        (
            "trigger",
            "CREATE TRIGGER trg_unknown_write_probe "
            "AFTER INSERT ON _write_probe BEGIN SELECT 1; END;",
        ),
        (
            "view",
            "CREATE VIEW unknown_write_probe_view AS "
            "SELECT id FROM _write_probe;",
        ),
    ),
    ids=("nonempty-row", "extra-column", "index", "trigger", "view"),
)
def test_only_exact_empty_legacy_write_probe_is_ignored_before_backup(
    mutation_name: str,
    mutation_sql: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / mutation_name / "unsupported-write-probe.sqlite3",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE _write_probe (id INTEGER)")
        connection.executescript(mutation_sql)
    backup_root = tmp_path / mutation_name / "migration-backups"
    source_state = _file_state(database_path)
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)

    with pytest.raises(MigrationError) as raised:
        migrate_sqlite_database(
            database_path,
            backup_root=backup_root,
            application_version="h1-write-probe-test",
        )

    assert raised.value.code == "unknown_legacy_schema"
    assert raised.value.recovery_backup is None
    assert _backup_directories(backup_root) == ()
    assert _file_state(database_path) == source_state
    assert _business_snapshot(database_path) == source_business
    assert schema_checksum(database_path) == source_checksum


def test_exact_empty_legacy_write_probe_migrates_and_is_removed(
    tmp_path: Path,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "exact-empty-write-probe.sqlite3",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE _write_probe (id INTEGER)")
    source_business_with_probe = _business_snapshot(database_path)
    legacy_columns = {
        table_name: columns
        for table_name, columns in source_business_with_probe.columns.items()
        if table_name != "_write_probe"
    }
    source_business = _business_snapshot(database_path, columns=legacy_columns)
    backup_root = tmp_path / "migration-backups"

    migrated = migrate_sqlite_database(
        database_path,
        backup_root=backup_root,
        application_version="h1-write-probe-test",
    )

    assert migrated.applied is True
    assert migrated.backup_path is not None
    backup_path = Path(migrated.backup_path)
    verify_migration_backup(backup_path)
    with sqlite3.connect(backup_path / "payload" / "database.sqlite3") as connection:
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='_write_probe'"
        ).fetchone() == ("CREATE TABLE _write_probe (id INTEGER)",)
        assert connection.execute("SELECT count(*) FROM _write_probe").fetchone() == (0,)
    with sqlite3.connect(database_path) as connection:
        assert "_write_probe" not in _table_names(connection)
    assert _business_snapshot(database_path, columns=legacy_columns) == source_business
    _assert_verified(database_path, FROZEN_H1_SCHEMA_CHECKSUM)


@pytest.mark.parametrize(
    ("operation", "tamper_phase"),
    (
        ("migrate", "after_publish"),
        ("restore", "after_publish_verify"),
    ),
)
def test_post_publish_backup_tamper_rolls_back_from_trusted_work_snapshot(
    operation: str,
    tamper_phase: str,
    tmp_path: Path,
) -> None:
    database_path = _materialize_partial_m13(
        tmp_path / operation / "post-publish-backup-tamper.sqlite3"
    )
    operation_backup_root = tmp_path / f"{operation}-operation-backups"
    restore_backup: Path | None = None
    if operation == "restore":
        migrated = _migrate(database_path, tmp_path / "restore-source-backups")
        assert migrated.backup_path is not None
        restore_backup = Path(migrated.backup_path)
    source_business = _business_snapshot(database_path)
    source_checksum = schema_checksum(database_path)
    tampered_backup: Path | None = None
    tampered_identity: tuple[int, int] | None = None
    published_checksum: str | None = None

    def tamper_backup_after_live_publish(point: str) -> None:
        nonlocal tampered_backup, tampered_identity, published_checksum
        if point != tamper_phase or tampered_backup is not None:
            return
        published_checksum = schema_checksum(database_path)
        assert published_checksum != source_checksum
        backups = _backup_directories(operation_backup_root)
        assert len(backups) == 1
        tampered_backup = backups[0]
        payload = tampered_backup / "payload" / "database.sqlite3"
        before = payload.stat()
        _tamper_owner_data_without_changing_schema(
            payload,
            f"Tampered {operation} post-publish recovery backup",
        )
        after = payload.stat()
        tampered_identity = after.st_dev, after.st_ino
        assert tampered_identity == (before.st_dev, before.st_ino)

    with pytest.raises(MigrationError) as raised:
        if operation == "migrate":
            migrate_sqlite_database(
                database_path,
                backup_root=operation_backup_root,
                application_version="h1-post-publish-backup-tamper-test",
                fault_injector=tamper_backup_after_live_publish,
            )
        else:
            assert restore_backup is not None
            restore_migration_backup(
                database_path,
                restore_backup,
                safety_backup_root=operation_backup_root,
                application_version="h1-post-publish-backup-tamper-test",
                fault_injector=tamper_backup_after_live_publish,
            )

    assert tampered_backup is not None
    assert tampered_identity is not None
    assert published_checksum is not None
    assert raised.value.code == "recovery_required"
    assert raised.value.recovery_backup is None
    with pytest.raises(MigrationError) as invalid_backup:
        verify_migration_backup(tampered_backup)
    assert invalid_backup.value.code == "backup_hash_mismatch"
    assert _business_snapshot(database_path) == source_business
    assert schema_checksum(database_path) == source_checksum
    assert _health(database_path)[:2] == (["ok"], [])
    assert not tuple(
        database_path.parent.glob(f".{database_path.name}.*-work-*")
    )


@pytest.mark.parametrize(
    "safety_location",
    ("input-backup", "input-backup-descendant", "dot-segment-alias"),
)
def test_restore_rejects_safety_backup_root_overlapping_input_backup_before_lock(
    safety_location: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "managed" / "database.sqlite3",
    )
    backup_path = backup_sqlite_database(
        database_path,
        backup_root=tmp_path / "input-backups",
        application_version="h1-restore-overlap-test",
    )
    verify_migration_backup(backup_path)
    decoy = backup_path.parent / "decoy"
    decoy.mkdir()
    if safety_location == "input-backup":
        safety_backup_root = backup_path
    elif safety_location == "input-backup-descendant":
        safety_backup_root = backup_path / "nested" / "safety-backups"
    else:
        safety_backup_root = decoy / ".." / backup_path.name
    backup_state = _filesystem_tree_state(backup_path)
    workspace_state = _filesystem_tree_state(tmp_path)
    observed_faults: list[str] = []

    def forbid_migration_lease(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("overlap validation entered the migration lease")

    monkeypatch.setattr(migration_module, "_migration_lease", forbid_migration_lease)

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            database_path,
            backup_path,
            safety_backup_root=safety_backup_root,
            application_version="h1-restore-overlap-test",
            fault_injector=observed_faults.append,
        )

    assert raised.value.code == "unsafe_backup_path"
    assert raised.value.recovery_backup is None
    assert observed_faults == []
    assert _filesystem_tree_state(backup_path) == backup_state
    assert _filesystem_tree_state(tmp_path) == workspace_state


@pytest.mark.parametrize("target_location", ("direct", "dot-segment-alias"))
def test_restore_rejects_payload_as_target_before_lock_even_when_identity_matches(
    target_location: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "source" / "database.sqlite3",
    )
    backup_path = backup_sqlite_database(
        source_path,
        backup_root=tmp_path / "input-backups",
        application_version="h1-restore-overlap-test",
    )
    manifest = verify_migration_backup(backup_path)
    decoy = backup_path.parent / "decoy"
    decoy.mkdir()
    target_path = (
        backup_path / "payload" / "database.sqlite3"
        if target_location == "direct"
        else decoy
        / ".."
        / backup_path.name
        / "payload"
        / "database.sqlite3"
    )
    assert manifest["source"]["database_identity"] == target_path.name
    backup_state = _filesystem_tree_state(backup_path)
    workspace_state = _filesystem_tree_state(tmp_path)
    observed_faults: list[str] = []

    def forbid_migration_lease(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("overlap validation entered the migration lease")

    monkeypatch.setattr(migration_module, "_migration_lease", forbid_migration_lease)

    with pytest.raises(MigrationError) as raised:
        restore_migration_backup(
            target_path,
            backup_path,
            safety_backup_root=tmp_path / "restore-safety-backups",
            application_version="h1-restore-overlap-test",
            fault_injector=observed_faults.append,
        )

    assert raised.value.code == "unsafe_backup_path"
    assert raised.value.recovery_backup is None
    assert observed_faults == []
    assert _filesystem_tree_state(backup_path) == backup_state
    assert _filesystem_tree_state(tmp_path) == workspace_state


@pytest.mark.parametrize("operation", ("verify", "restore"))
@pytest.mark.parametrize(
    "replacement_boundary",
    ("during-health", "between-ancestor-check-and-open"),
)
def test_input_backup_directory_replacement_during_verification_is_rejected(
    operation: str,
    replacement_boundary: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "source" / "database.sqlite3",
    )
    backup_path = backup_sqlite_database(
        source_path,
        backup_root=tmp_path / "input-backups",
        application_version="h1-backup-directory-identity-test",
    )
    expected_manifest = verify_migration_backup(backup_path)
    replacement_parent = tmp_path / "replacement"
    replacement_parent.mkdir(mode=0o700)
    replacement_path = replacement_parent / backup_path.name
    shutil.copytree(backup_path, replacement_path, copy_function=shutil.copy2)
    assert verify_migration_backup(replacement_path) == expected_manifest
    original_identity = (backup_path.stat().st_dev, backup_path.stat().st_ino)
    replacement_identity = (
        replacement_path.stat().st_dev,
        replacement_path.stat().st_ino,
    )
    assert replacement_identity != original_identity

    quarantine_parent = tmp_path / "quarantine"
    quarantine_parent.mkdir(mode=0o700)
    quarantined_path = quarantine_parent / backup_path.name
    live_path: Path | None = None
    live_state: dict[str, tuple[int, str]] | None = None
    live_business: BusinessSnapshot | None = None
    live_schema: str | None = None
    live_tree: dict[str, tuple[str, int, str | None]] | None = None
    safety_backup_root = tmp_path / "restore-safety-backups"
    if operation == "restore":
        live_path = _materialize_sql(
            "v1_1_1_full",
            tmp_path / "live" / "database.sqlite3",
        )
        with sqlite3.connect(live_path) as connection:
            connection.execute(
                "UPDATE owners SET display_name=? WHERE id=?",
                ("Live state must survive verifier race", "fixture-owner"),
            )
        live_state = _file_state(live_path)
        live_business = _business_snapshot(live_path)
        live_schema = schema_checksum(live_path)
        live_tree = _filesystem_tree_state(live_path.parent)

    real_health = migration_module._health
    real_ancestor_check = migration_module._reject_symlinked_directory_ancestors
    swapped = False

    def swap_backup_directory() -> None:
        nonlocal swapped
        os.replace(backup_path, quarantined_path)
        os.replace(replacement_path, backup_path)
        swapped = True
        assert (backup_path.stat().st_dev, backup_path.stat().st_ino) == (
            replacement_identity
        )
        assert (
            quarantined_path.stat().st_dev,
            quarantined_path.stat().st_ino,
        ) == original_identity

    def replace_backup_directory_during_health(path: Path) -> dict[str, Any]:
        candidate = Path(path)
        if (
            not swapped
            and replacement_boundary == "during-health"
            and candidate.parts[:4] == ("/", "proc", "self", "fd")
            and len(candidate.parts) == 5
        ):
            swap_backup_directory()
        return real_health(candidate)

    def replace_after_ancestor_check(path: Path, *, label: str) -> None:
        real_ancestor_check(path, label=label)
        if (
            not swapped
            and replacement_boundary == "between-ancestor-check-and-open"
            and lexical_absolute(path) == backup_path
        ):
            swap_backup_directory()

    monkeypatch.setattr(
        migration_module,
        "_health",
        replace_backup_directory_during_health,
    )
    monkeypatch.setattr(
        migration_module,
        "_reject_symlinked_directory_ancestors",
        replace_after_ancestor_check,
    )
    observed_error: MigrationError | None = None
    try:
        if operation == "verify":
            verify_migration_backup(backup_path)
        else:
            assert live_path is not None
            restore_migration_backup(
                live_path,
                backup_path,
                safety_backup_root=safety_backup_root,
                application_version="h1-backup-directory-identity-test",
            )
    except MigrationError as exc:
        observed_error = exc

    assert swapped is True
    assert verify_migration_backup(backup_path) == expected_manifest
    assert verify_migration_backup(quarantined_path) == expected_manifest
    if operation == "restore":
        assert live_path is not None
        assert live_state is not None
        assert live_business is not None
        assert live_schema is not None
        assert live_tree is not None
        assert _file_state(live_path) == live_state
        assert _business_snapshot(live_path) == live_business
        assert schema_checksum(live_path) == live_schema
        assert _filesystem_tree_state(live_path.parent) == live_tree
        assert not safety_backup_root.exists()
    assert observed_error is not None
    assert observed_error.code == "source_changed_during_migration"
    assert observed_error.recovery_backup is None


@pytest.mark.parametrize("operation", ("verify", "restore"))
def test_input_backup_payload_replacement_during_verification_is_rejected(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _materialize_sql(
        "v1_1_1_full",
        tmp_path / "source" / "database.sqlite3",
    )
    backup_path = backup_sqlite_database(
        source_path,
        backup_root=tmp_path / "input-backups",
        application_version="h1-backup-payload-identity-test",
    )
    expected_manifest = verify_migration_backup(backup_path)
    payload_path = backup_path / "payload" / "database.sqlite3"
    original_payload_identity = (
        payload_path.stat().st_dev,
        payload_path.stat().st_ino,
    )
    replacement_payload = tmp_path / "replacement-payload.sqlite3"
    shutil.copy2(payload_path, replacement_payload)
    os.chmod(replacement_payload, 0o600)
    replacement_identity = (
        replacement_payload.stat().st_dev,
        replacement_payload.stat().st_ino,
    )
    assert replacement_identity != original_payload_identity
    quarantined_payload = tmp_path / "quarantined-payload.sqlite3"

    live_path: Path | None = None
    live_state: dict[str, tuple[int, str]] | None = None
    live_business: BusinessSnapshot | None = None
    live_schema: str | None = None
    live_tree: dict[str, tuple[str, int, str | None]] | None = None
    safety_backup_root = tmp_path / "restore-safety-backups"
    if operation == "restore":
        live_path = _materialize_sql(
            "v1_1_1_full",
            tmp_path / "live" / "database.sqlite3",
        )
        with sqlite3.connect(live_path) as connection:
            connection.execute(
                "UPDATE owners SET display_name=? WHERE id=?",
                ("Live state must survive payload race", "fixture-owner"),
            )
        live_state = _file_state(live_path)
        live_business = _business_snapshot(live_path)
        live_schema = schema_checksum(live_path)
        live_tree = _filesystem_tree_state(live_path.parent)

    real_health = migration_module._health
    swapped = False

    def replace_payload_during_health(path: Path) -> dict[str, Any]:
        nonlocal swapped
        candidate = Path(path)
        if (
            not swapped
            and candidate.parts[:4] == ("/", "proc", "self", "fd")
            and len(candidate.parts) == 5
        ):
            os.replace(payload_path, quarantined_payload)
            os.replace(replacement_payload, payload_path)
            swapped = True
            assert (
                payload_path.stat().st_dev,
                payload_path.stat().st_ino,
            ) == replacement_identity
        return real_health(candidate)

    monkeypatch.setattr(migration_module, "_health", replace_payload_during_health)
    with pytest.raises(MigrationError) as raised:
        if operation == "verify":
            verify_migration_backup(backup_path)
        else:
            assert live_path is not None
            restore_migration_backup(
                live_path,
                backup_path,
                safety_backup_root=safety_backup_root,
                application_version="h1-backup-payload-identity-test",
            )

    assert swapped is True
    assert raised.value.code == "source_changed_during_migration"
    assert raised.value.recovery_backup is None
    assert verify_migration_backup(backup_path) == expected_manifest
    if operation == "restore":
        assert live_path is not None
        assert live_state is not None
        assert live_business is not None
        assert live_schema is not None
        assert live_tree is not None
        assert _file_state(live_path) == live_state
        assert _business_snapshot(live_path) == live_business
        assert schema_checksum(live_path) == live_schema
        assert _filesystem_tree_state(live_path.parent) == live_tree
        assert not safety_backup_root.exists()
