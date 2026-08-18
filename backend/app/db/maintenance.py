"""Safe, dependency-free maintenance for the local Learning Agent state.

This module deliberately uses only the Python standard library.  It must be
usable before the application imports its settings, creates a SQLAlchemy
engine, or runs a migration.  Every public operation receives an explicit
repository root and never reads ``.env``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Sequence

from app.version import APPLICATION_VERSION, CURRENT_SCHEMA_VERSION


MANIFEST_FORMAT = "learning-agent-state-backup"
MANIFEST_VERSION = 1
LAYOUT_VERSION = 1

DATABASE_PATHS = (
    PurePosixPath("data/learning_companion.db"),
    PurePosixPath("learning_companion.db"),
    PurePosixPath("backend/learning_companion.db"),
    PurePosixPath("backend/data/learning_companion.db"),
)
TREE_PATHS = (
    PurePosixPath("data/context"),
    PurePosixPath("data/workspace"),
)
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DATABASE_PATH_SET = {path.as_posix() for path in DATABASE_PATHS}
TREE_PATH_PREFIXES = tuple(path.as_posix() for path in TREE_PATHS)
PURPOSE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
BACKUP_ID_PATTERN = re.compile(
    r"^(?P<purpose>[a-z][a-z0-9_]{0,31})-"
    r"(?P<timestamp>[0-9]{8}T[0-9]{6}\.[0-9]{6}Z)-"
    r"(?P<uuid>[0-9a-f]{32})$"
)
SECRET_NAMES = {".env", ".env.local", ".env.development", ".env.production"}

PhaseHook = Callable[[str], None]


class MaintenanceError(RuntimeError):
    """An expected, machine-readable maintenance failure."""

    def __init__(self, code: str, message: str, *, exit_code: int = 3) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class BackupReference:
    backup_id: str
    purpose: str
    path: Path
    manifest: dict[str, Any]


@dataclass
class _OpenedBackup:
    backup_id: str
    directory_descriptor: int
    payload_descriptor: int | None = None

    @property
    def payload_path(self) -> Path:
        if self.payload_descriptor is None:
            raise MaintenanceError("invalid_manifest", "Backup payload is not open.")
        return Path("/proc/self/fd") / str(self.payload_descriptor)

    def open_payload(self) -> None:
        if self.payload_descriptor is not None:
            return
        try:
            descriptor = os.open(
                "payload",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self.directory_descriptor,
            )
            result = os.fstat(descriptor)
            if not stat.S_ISDIR(result.st_mode):
                os.close(descriptor)
                raise MaintenanceError(
                    "invalid_manifest", "Backup payload directory is unsafe."
                )
        except MaintenanceError:
            raise
        except OSError as exc:
            raise MaintenanceError(
                "invalid_manifest", "Backup payload directory is missing or unsafe."
            ) from exc
        self.payload_descriptor = descriptor


@dataclass(frozen=True)
class _SourceFile:
    relative_path: PurePosixPath
    absolute_path: Path


def _emit_phase(hook: PhaseHook | None, phase: str) -> None:
    if hook is not None:
        hook(phase)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        before = _lstat_regular(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MaintenanceError("state_changed", "State file changed while opening.")
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != total
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise MaintenanceError("state_changed", "State file changed while hashing.")
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError("state_read_failed", "Could not hash a state file.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MaintenanceError("unsafe_backup_path", "Backup contains an unsafe path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MaintenanceError("unsafe_backup_path", "Backup contains an unsafe path.")
    return path


def _is_tree_path(path: PurePosixPath) -> bool:
    value = path.as_posix()
    return any(value == prefix or value.startswith(prefix + "/") for prefix in TREE_PATH_PREFIXES)


def _is_secret_path(path: PurePosixPath) -> bool:
    return any(
        part.lower() in SECRET_NAMES or part.lower().startswith(".env.")
        for part in path.parts
    )


def _path_under(root: Path, relative: PurePosixPath) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=False)
    except OSError as exc:
        raise MaintenanceError("unsafe_state_path", "Could not resolve a state path.") from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise MaintenanceError("unsafe_state_path", "State path escapes the repository root.")
    return candidate


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _lstat_regular(path: Path, *, code: str = "unsafe_state_path") -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise MaintenanceError(code, "Could not inspect a state path.") from exc
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISREG(result.st_mode):
        raise MaintenanceError(code, "State contains a symlink or special file.")
    if result.st_nlink != 1:
        raise MaintenanceError(code, "State contains a multiply-linked file.")
    return result


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MaintenanceError("state_sync_failed", "Could not sync maintenance state.") from exc


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MaintenanceError("state_sync_failed", "Could not sync maintenance state.") from exc


def _fsync_tree_directories(root: Path) -> None:
    try:
        directories = [Path(current) for current, _dirs, _files in os.walk(root)]
    except OSError as exc:
        raise MaintenanceError("state_sync_failed", "Could not enumerate backup staging.") from exc
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            result = directory.lstat()
            if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
                raise MaintenanceError(
                    "state_sync_failed", "Maintenance staging directory is unsafe."
                )
            os.chmod(directory, 0o700)
        except MaintenanceError:
            raise
        except OSError as exc:
            raise MaintenanceError(
                "state_sync_failed", "Could not secure maintenance staging."
            ) from exc
        _fsync_directory(directory)


def _write_exclusive(path: Path, value: bytes, mode: int = 0o600) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
    except FileExistsError as exc:
        raise MaintenanceError(
            "maintenance_collision", "Maintenance staging path already exists."
        ) from exc
    except OSError as exc:
        raise MaintenanceError("state_write_failed", "Could not write maintenance state.") from exc


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        if _lexists(temporary):
            temporary.unlink()
        _write_exclusive(temporary, _canonical_json(value))
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError(
            "state_write_failed", "Could not persist an operation journal."
        ) from exc


class _DirectoryLease:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.descriptor: int | None = None

    def __enter__(self) -> "_DirectoryLease":
        data_directory = self.root / "data"
        try:
            self.descriptor = os.open(
                self.root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MaintenanceError(
                    "maintenance_busy",
                    "Runtime state is in use by another process.",
                    exit_code=2,
                ) from exc
            if _lexists(data_directory):
                result = data_directory.lstat()
                if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
                    raise MaintenanceError(
                        "unsafe_state_path", "The runtime data root is not a safe directory."
                    )
        except MaintenanceError:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            raise
        except OSError as exc:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            raise MaintenanceError(
                "maintenance_busy",
                "Could not acquire the runtime-state lease.",
                exit_code=2,
            ) from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.descriptor is not None:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self.descriptor = None


@contextmanager
def maintenance_directory_lease(root: str | Path) -> Iterator[None]:
    """Hold only the lifecycle mutex without opening live SQLite state.

    Immutable backup verification uses this narrower lease so a damaged live
    database cannot prevent an operator from validating recovery media.
    """

    root_path = Path(root).resolve(strict=True)
    with _DirectoryLease(root_path):
        yield


@contextmanager
def runtime_state_lease(root: str | Path) -> Iterator[None]:
    """Hold the runtime-state lease for the complete application lifespan.

    The caller must acquire this before opening a database connection or
    starting any component that can write Context/Workspace state.  The lease
    is the exact lock used by backup/reset/restore, so maintenance fails closed
    with ``maintenance_busy`` while the sanctioned runtime is alive.
    """

    root_path = Path(root).resolve(strict=True)
    with _DirectoryLease(root_path):
        recovered_state = _recover_orphaned_switch_locked(root_path)
        sqlite_recovered = _recover_sqlite_crash_journals_locked(root_path)
        with _writer_guards(root_path):
            recovered_state = _recover_incomplete_locked(root_path) or recovered_state
            _recover_incomplete_backups_locked(root_path)
        if recovered_state:
            # Recovery can replace database inodes.  Confirm the recovered
            # generation and any rollback journal it carried before startup.
            sqlite_recovered = (
                _recover_sqlite_crash_journals_locked(root_path) or sqlite_recovered
            )
            with _writer_guards(root_path):
                pass
        elif sqlite_recovered:
            # The crash rollback can change database pages and remove the
            # journal.  Re-open the recovered inode under the ordinary guard.
            with _writer_guards(root_path):
                pass
        yield


def _sqlite_uri(path: Path, mode: str, *, immutable: bool = False) -> str:
    uri = path.resolve(strict=True).as_uri() + f"?mode={mode}"
    if immutable:
        uri += "&immutable=1"
    return uri


def _recover_sqlite_crash_journals_locked(root: Path) -> bool:
    """Recover genuine hot rollback journals at explicit recovery boundaries.

    Backup/reset/restore and every read-only inspection still reject rollback
    journals before opening SQLite.  Only startup and ``recover`` call this
    helper while holding the lifecycle lease, allowing SQLite itself to roll
    back a transaction left by a crashed process.  Arbitrary or persistent
    journal files remain a fail-closed recovery error.
    """

    recovered = False
    for relative in DATABASE_PATHS:
        database = _path_under(root, relative)
        journal = database.with_name(database.name + "-journal")
        if not _lexists(journal):
            continue
        if not _lexists(database):
            raise MaintenanceError(
                "unsafe_sqlite_state",
                "A rollback journal exists without its database.",
                exit_code=4,
            )
        _lstat_regular(database)
        _lstat_regular(journal)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                _sqlite_uri(database, "rw"),
                uri=True,
                timeout=0,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("BEGIN IMMEDIATE")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            connection.rollback()
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.close()
                connection = None
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise MaintenanceError(
                    "active_sqlite_writer",
                    "An active SQLite writer is using runtime state.",
                    exit_code=2,
                ) from exc
            raise MaintenanceError(
                "sqlite_recovery_failed",
                "SQLite crash recovery could not complete.",
                exit_code=4,
            ) from exc
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.close()
                connection = None
            raise MaintenanceError(
                "sqlite_integrity_failed",
                "SQLite crash recovery found an invalid database.",
                exit_code=4,
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        if integrity != [("ok",)] or foreign_keys:
            raise MaintenanceError(
                "sqlite_integrity_failed",
                "SQLite crash recovery verification failed.",
                exit_code=4,
            )
        if _lexists(journal):
            raise MaintenanceError(
                "sqlite_recovery_failed",
                "SQLite did not consume the rollback journal safely.",
                exit_code=4,
            )
        recovered = True
    return recovered


@contextmanager
def _writer_guards(
    root: Path,
    *,
    allow_orphan_sidecars: bool = False,
) -> Iterator[dict[PurePosixPath, sqlite3.Connection]]:
    connections: dict[PurePosixPath, sqlite3.Connection] = {}
    try:
        database_sources: list[tuple[PurePosixPath, Path, os.stat_result]] = []
        for relative in DATABASE_PATHS:
            database = _path_under(root, relative)
            database_exists = _lexists(database)
            if database_exists:
                identity = _lstat_regular(database)
                database_sources.append((relative, database, identity))
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar = database.with_name(database.name + suffix)
                if _lexists(sidecar):
                    _lstat_regular(sidecar)
                    if not database_exists and allow_orphan_sidecars:
                        continue
                    if suffix == "-journal" or not database_exists:
                        raise MaintenanceError(
                            "unsafe_sqlite_state",
                            "Runtime SQLite state requires recovery before maintenance.",
                            exit_code=2,
                        )
        for relative, database, _identity in database_sources:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    _sqlite_uri(database, "rw"),
                    uri=True,
                    timeout=0,
                    isolation_level=None,
                )
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    if connection is not None:
                        connection.close()
                    raise MaintenanceError(
                        "active_sqlite_writer",
                        "An active SQLite writer is using runtime state.",
                        exit_code=2,
                    ) from exc
                if connection is not None:
                    connection.close()
                raise MaintenanceError(
                    "sqlite_open_failed", "Could not open a runtime database safely."
                ) from exc
            except sqlite3.DatabaseError as exc:
                if connection is not None:
                    connection.close()
                raise MaintenanceError(
                    "sqlite_integrity_failed", "A runtime database is not valid SQLite."
                ) from exc
            connections[relative] = connection
        expected_databases = {relative for relative, _path, _identity in database_sources}
        for relative, database, identity in database_sources:
            current = _lstat_regular(database)
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise MaintenanceError(
                    "state_changed", "Runtime database changed while acquiring writer guards."
                )
        for relative in DATABASE_PATHS:
            database = _path_under(root, relative)
            database_exists = _lexists(database)
            if database_exists != (relative in expected_databases):
                if database_exists:
                    _lstat_regular(database)
                raise MaintenanceError(
                    "state_changed",
                    "Managed database set changed while acquiring writer guards.",
                )
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar = database.with_name(database.name + suffix)
                if not _lexists(sidecar):
                    continue
                _lstat_regular(sidecar)
                if not database_exists and allow_orphan_sidecars:
                    continue
                if suffix == "-journal" or not database_exists:
                    raise MaintenanceError(
                        "unsafe_sqlite_state",
                        "SQLite state changed while acquiring writer guards.",
                        exit_code=2,
                    )
        yield connections
    finally:
        for connection in reversed(tuple(connections.values())):
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()


def _recover_orphaned_switch_locked(root: Path) -> bool:
    """Rollback a journaled state switch that moved DB before its sidecars."""

    has_orphan = any(
        not _lexists(_path_under(root, relative))
        and any(
            _lexists(
                _path_under(root, relative).with_name(relative.name + suffix)
            )
            for suffix in SQLITE_SIDECAR_SUFFIXES
        )
        for relative in DATABASE_PATHS
    )
    if not has_orphan:
        return False
    with _writer_guards(root, allow_orphan_sidecars=True):
        recovered = _recover_incomplete_locked(root)
    if not recovered:
        raise MaintenanceError(
            "unsafe_sqlite_state",
            "Orphan SQLite sidecars have no recoverable maintenance journal.",
            exit_code=4,
        )
    return True


def _validate_read_only_generation(
    root: Path,
    database_sources: Sequence[Path],
    descriptors: Sequence[int],
) -> None:
    for database, descriptor in zip(database_sources, descriptors, strict=True):
        current = _lstat_regular(database)
        opened = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise MaintenanceError(
                "state_changed", "Runtime database changed during inspection."
            )
    expected_databases = set(database_sources)
    for relative in DATABASE_PATHS:
        database = _path_under(root, relative)
        database_exists = _lexists(database)
        if database_exists != (database in expected_databases):
            if database_exists:
                _lstat_regular(database)
            raise MaintenanceError(
                "state_changed",
                "Managed database set changed while acquiring inspection locks.",
            )
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar = database.with_name(database.name + suffix)
            if _lexists(sidecar):
                _lstat_regular(sidecar)
                raise MaintenanceError(
                    "unsafe_sqlite_state",
                    "SQLite sidecar appeared during read-only inspection.",
                    exit_code=2,
                )


@contextmanager
def _read_only_guards(root: Path) -> Iterator[dict[PurePosixPath, sqlite3.Connection]]:
    """Stabilize clean rollback-mode databases without opening SQLite RW.

    WAL, SHM, and rollback journals require recovery-aware SQLite access, which
    is inherently mutating.  Inspection commands therefore reject them before
    opening any database.  Byte-range locks detect and exclude rollback-mode
    connections while preserving every on-disk byte and timestamp.
    """

    descriptors: list[int] = []
    try:
        database_sources: list[Path] = []
        for relative in DATABASE_PATHS:
            database = _path_under(root, relative)
            database_exists = _lexists(database)
            if database_exists:
                _lstat_regular(database)
                database_sources.append(database)
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar = database.with_name(database.name + suffix)
                if _lexists(sidecar):
                    _lstat_regular(sidecar)
                    raise MaintenanceError(
                        "unsafe_sqlite_state",
                        "Read-only inspection cannot recover SQLite sidecars.",
                        exit_code=2,
                    )
        for database in database_sources:
            before = _lstat_regular(database)
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    database,
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                )
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise MaintenanceError(
                        "state_changed", "Runtime database changed during inspection."
                    )
                lock = struct.pack(
                    "hhqqi",
                    fcntl.F_WRLCK,
                    os.SEEK_SET,
                    0x40000000,
                    512,
                    0,
                )
                fcntl.fcntl(descriptor, fcntl.F_OFD_SETLK, lock)
            except BlockingIOError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise MaintenanceError(
                    "active_sqlite_writer",
                    "An active SQLite connection is using runtime state.",
                    exit_code=2,
                ) from exc
            except MaintenanceError:
                if descriptor is not None:
                    os.close(descriptor)
                raise
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise MaintenanceError(
                    "state_read_failed", "Could not lock a database for read-only inspection."
                ) from exc
            descriptors.append(descriptor)
        # A writer can appear after the first sidecar scan and crash just
        # before its OFD lock is acquired.  With every SQLite lock byte now
        # held, revalidate the pinned main-file identities and the absence of
        # sidecars before exposing the supposedly clean generation.
        _validate_read_only_generation(root, database_sources, descriptors)
        yield {}
        _validate_read_only_generation(root, database_sources, descriptors)
    finally:
        for descriptor in reversed(descriptors):
            try:
                unlock = struct.pack(
                    "hhqqi",
                    fcntl.F_UNLCK,
                    os.SEEK_SET,
                    0x40000000,
                    512,
                    0,
                )
                fcntl.fcntl(descriptor, fcntl.F_OFD_SETLK, unlock)
            finally:
                os.close(descriptor)


def _schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, coalesce(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    normalized = [
        [str(row[0]), str(row[1]), str(row[2]), " ".join(str(row[3]).split())]
        for row in rows
    ]
    return _sha256_bytes(_canonical_json(normalized))


def _migration_history(connection: sqlite3.Connection) -> tuple[int, str]:
    table_present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if table_present is None:
        return user_version, _sha256_bytes(b"[]\n")
    columns = [row[1] for row in connection.execute('PRAGMA table_info("schema_migrations")')]
    if "version" not in columns:
        raise MaintenanceError(
            "schema_incompatible", "Migration history has no version column."
        )
    quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
    rows = connection.execute(
        f'SELECT {quoted} FROM "schema_migrations" ORDER BY version'
    ).fetchall()
    encoded = [[_json_scalar(value) for value in row] for row in rows]
    latest = max((int(row[columns.index("version")]) for row in rows), default=user_version)
    return latest, _sha256_bytes(_canonical_json(encoded))


def _json_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _inspect_sqlite(path: Path) -> dict[str, Any]:
    _lstat_regular(path, code="invalid_manifest")
    try:
        connection = sqlite3.connect(
            _sqlite_uri(path, "ro", immutable=True),
            uri=True,
            timeout=1,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise MaintenanceError(
                    "sqlite_integrity_failed", "A backup database failed integrity_check."
                )
            foreign_key_count = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
            if foreign_key_count:
                raise MaintenanceError(
                    "foreign_key_failed", "A backup database failed foreign_key_check."
                )
            schema_version, migration_digest = _migration_history(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version > CURRENT_SCHEMA_VERSION or user_version > CURRENT_SCHEMA_VERSION:
                raise MaintenanceError(
                    "schema_incompatible", "Backup schema is newer than this application supports."
                )
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            return {
                "integrity_check": "ok",
                "foreign_key_check_count": 0,
                "schema_version": schema_version,
                "user_version": user_version,
                "schema_sha256": _schema_digest(connection),
                "migration_history_sha256": migration_digest,
                "page_size": page_size,
                "sqlite_version": sqlite3.sqlite_version,
            }
        finally:
            connection.close()
    except MaintenanceError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError(
            "sqlite_integrity_failed", "A backup database is not valid SQLite."
        ) from exc
    except OSError as exc:
        raise MaintenanceError("state_read_failed", "Could not inspect a backup database.") from exc


def _scan_tree(
    root: Path,
    relative_root: PurePosixPath,
    *,
    destructive: bool,
) -> tuple[list[_SourceFile], list[PurePosixPath], list[str]]:
    absolute_root = _path_under(root, relative_root)
    if not _lexists(absolute_root):
        return [], [], []
    try:
        root_stat = absolute_root.lstat()
    except OSError as exc:
        raise MaintenanceError(
            "state_read_failed", "Could not inspect a runtime directory."
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise MaintenanceError("unsafe_state_path", "Runtime tree is not a safe directory.")
    files: list[_SourceFile] = []
    directories: list[PurePosixPath] = [relative_root]
    exclusions: list[str] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise MaintenanceError(
                "state_read_failed", "Could not enumerate runtime state."
            ) from exc
        for child in children:
            child_relative = relative / child.name
            if _is_secret_path(child_relative):
                exclusions.append(child_relative.as_posix())
                if destructive:
                    raise MaintenanceError(
                        "secret_state_excluded",
                        "A destructive operation would overwrite excluded secret state.",
                    )
                continue
            try:
                result = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise MaintenanceError(
                    "state_read_failed", "Could not inspect runtime state."
                ) from exc
            if stat.S_ISLNK(result.st_mode):
                raise MaintenanceError("unsafe_state_path", "Runtime state contains a symlink.")
            if stat.S_ISDIR(result.st_mode):
                directories.append(child_relative)
                visit(Path(child.path), child_relative)
                continue
            if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
                raise MaintenanceError(
                    "unsafe_state_path", "Runtime state contains a special or linked file."
                )
            files.append(_SourceFile(child_relative, Path(child.path)))

    visit(absolute_root, relative_root)
    return files, directories, exclusions


def _copy_regular(source: Path, destination: Path) -> tuple[int, str, int, int]:
    before = _lstat_regular(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    try:
        source_fd = os.open(source, flags)
        try:
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise MaintenanceError("state_changed", "Runtime state changed during backup.")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                total = 0
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        view = view[written:]
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError("state_copy_failed", "Could not copy runtime state.") from exc
    after = _lstat_regular(source)
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or total != after.st_size
        or _sha256_file(source) != digest.hexdigest()
    ):
        raise MaintenanceError("state_changed", "Runtime state changed during backup.")
    return total, digest.hexdigest(), stat.S_IMODE(before.st_mode), before.st_mtime_ns


def _snapshot_database(
    source: Path,
    destination: Path,
    *,
    read_only_source: bool = False,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if read_only_source:
            # The read-only guard guarantees a clean, sidecar-free main file.
            # Copy bytes without opening SQLite on the live WAL-mode database;
            # even a mode=ro connection can create live -wal/-shm files.
            _copy_regular(source, destination)
            destination_connection = sqlite3.connect(destination)
            try:
                destination_connection.execute("PRAGMA journal_mode=DELETE")
                destination_connection.commit()
            finally:
                destination_connection.close()
            os.chmod(destination, 0o600)
            _fsync_file(destination)
            return _inspect_sqlite(destination)
        with ExitStack() as stack:
            source_connection = sqlite3.connect(
                _sqlite_uri(source, "ro"),
                uri=True,
                timeout=1,
                isolation_level=None,
            )
            stack.callback(source_connection.close)
            destination_connection = sqlite3.connect(destination)
            stack.callback(destination_connection.close)
            source_connection.backup(destination_connection)
            destination_connection.commit()
            destination_connection.execute("PRAGMA journal_mode=DELETE")
        os.chmod(destination, 0o600)
        _fsync_file(destination)
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError(
            "sqlite_backup_failed", "Could not create a consistent SQLite snapshot."
        ) from exc
    except OSError as exc:
        raise MaintenanceError("state_copy_failed", "Could not write a SQLite snapshot.") from exc
    return _inspect_sqlite(destination)


def _payload_digest(entries: Sequence[dict[str, Any]]) -> str:
    tokens = [
        [entry["archive_path"], entry["size_bytes"], entry["sha256"]]
        for entry in sorted(entries, key=lambda item: item["archive_path"])
    ]
    return _sha256_bytes(_canonical_json(tokens))


def _validate_purpose(purpose: str) -> str:
    if not PURPOSE_PATTERN.fullmatch(purpose):
        raise MaintenanceError("invalid_purpose", "Backup purpose is invalid.")
    return purpose


def _parse_backup_id(backup_id: str) -> tuple[str, str, str]:
    match = BACKUP_ID_PATTERN.fullmatch(backup_id)
    if match is None:
        raise MaintenanceError("invalid_manifest", "Backup identity is malformed.")
    compact_timestamp = match.group("timestamp")
    try:
        parsed = datetime.strptime(compact_timestamp, "%Y%m%dT%H%M%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise MaintenanceError("invalid_manifest", "Backup identity timestamp is invalid.") from exc
    return (
        match.group("purpose"),
        compact_timestamp,
        parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )


def _new_backup_id(purpose: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{purpose}-{timestamp}-{uuid.uuid4().hex}"


def _build_backup_stage(
    root: Path,
    stage: Path,
    *,
    purpose: str,
    destructive: bool,
    read_only_source: bool = False,
    phase_hook: PhaseHook | None,
) -> dict[str, Any] | None:
    database_sources: list[tuple[PurePosixPath, Path]] = []
    tree_files: list[_SourceFile] = []
    directories: list[PurePosixPath] = []
    exclusions: list[str] = []

    for relative in DATABASE_PATHS:
        source = _path_under(root, relative)
        if _lexists(source):
            _lstat_regular(source)
            database_sources.append((relative, source))
    for relative_tree in TREE_PATHS:
        files, tree_directories, tree_exclusions = _scan_tree(
            root,
            relative_tree,
            destructive=destructive,
        )
        tree_files.extend(files)
        directories.extend(tree_directories)
        exclusions.extend(tree_exclusions)

    if not database_sources and not tree_files and not directories:
        return None

    try:
        stage.mkdir(mode=0o700, parents=True, exist_ok=False)
        payload = stage / "payload"
        payload.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise MaintenanceError(
            "maintenance_collision", "Backup staging directory already exists."
        ) from exc
    except OSError as exc:
        raise MaintenanceError("state_write_failed", "Could not create backup staging.") from exc

    entries: list[dict[str, Any]] = []
    database_metadata: list[dict[str, Any]] = []
    for relative, source in database_sources:
        archive_relative = PurePosixPath("payload") / relative
        destination = stage.joinpath(*archive_relative.parts)
        sqlite_metadata = _snapshot_database(
            source,
            destination,
            read_only_source=read_only_source,
        )
        entry = {
            "source_path": relative.as_posix(),
            "archive_path": archive_relative.as_posix(),
            "kind": "sqlite",
            "size_bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
            "mode": "0600",
            "sqlite": sqlite_metadata,
        }
        entries.append(entry)
        database_metadata.append(
            {
                "source_path": relative.as_posix(),
                "schema_version": sqlite_metadata["schema_version"],
                "user_version": sqlite_metadata["user_version"],
                "schema_sha256": sqlite_metadata["schema_sha256"],
                "migration_history_sha256": sqlite_metadata[
                    "migration_history_sha256"
                ],
            }
        )
    _emit_phase(phase_hook, "after_sqlite_snapshot")

    copied_tree_paths: set[str] = set()
    for relative_directory in directories:
        try:
            stage.joinpath("payload", *relative_directory.parts).mkdir(
                mode=0o700,
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            raise MaintenanceError(
                "state_copy_failed", "Could not preserve a runtime directory."
            ) from exc
    for source_file in tree_files:
        archive_relative = PurePosixPath("payload") / source_file.relative_path
        destination = stage.joinpath(*archive_relative.parts)
        size, digest, original_mode, _mtime_ns = _copy_regular(
            source_file.absolute_path,
            destination,
        )
        copied_tree_paths.add(source_file.relative_path.as_posix())
        entries.append(
            {
                "source_path": source_file.relative_path.as_posix(),
                "archive_path": archive_relative.as_posix(),
                "kind": "file",
                "size_bytes": size,
                "sha256": digest,
                "mode": "0600",
                "source_mode": f"{original_mode:04o}",
            }
        )

    # Detect directory membership changes after copying.  Individual files are
    # also re-hashed by _copy_regular, so a state tree cannot silently race the
    # snapshot even if an uncooperative process ignores the lifecycle lease.
    rescanned_paths: set[str] = set()
    rescanned_directories: set[str] = set()
    for relative_tree in TREE_PATHS:
        files, tree_directories, _ = _scan_tree(
            root,
            relative_tree,
            destructive=destructive,
        )
        rescanned_paths.update(item.relative_path.as_posix() for item in files)
        rescanned_directories.update(item.as_posix() for item in tree_directories)
    if rescanned_paths != copied_tree_paths or rescanned_directories != {
        item.as_posix() for item in directories
    }:
        raise MaintenanceError("state_changed", "Runtime tree changed during backup.")

    backup_id = stage.name.removeprefix(".incomplete-")
    identity_purpose, _identity_timestamp, identity_created_at = _parse_backup_id(
        backup_id
    )
    if identity_purpose != purpose:
        raise MaintenanceError("invalid_manifest", "Backup identity purpose is inconsistent.")
    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "manifest_version": MANIFEST_VERSION,
        "layout_version": LAYOUT_VERSION,
        "complete": True,
        "backup_id": backup_id,
        "purpose": purpose,
        "created_at": identity_created_at,
        "application_version": APPLICATION_VERSION,
        "schema": {
            "supported_version": CURRENT_SCHEMA_VERSION,
            "databases": sorted(database_metadata, key=lambda item: item["source_path"]),
        },
        "entries": sorted(entries, key=lambda item: item["source_path"]),
        "directories": sorted(item.as_posix() for item in set(directories)),
        "exclusions": {
            "patterns": [".env", ".env.*"],
            "paths": sorted(exclusions),
            "secrets_included": False,
        },
        "payload_sha256": _payload_digest(entries),
    }
    manifest_bytes = _canonical_json(manifest)
    _write_exclusive(stage / "manifest.json", manifest_bytes)
    _write_exclusive(
        stage / "manifest.sha256",
        (_sha256_bytes(manifest_bytes) + "\n").encode("ascii"),
    )
    _fsync_tree_directories(payload)
    _fsync_directory(stage)
    _emit_phase(phase_hook, "after_manifest_fsync")
    return manifest


def _cleanup_private_directory(path: Path, allowed_parent: Path) -> None:
    try:
        resolved_parent = allowed_parent.resolve(strict=True)
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise MaintenanceError(
            "recovery_required", "Could not resolve maintenance cleanup."
        ) from exc
    if resolved == resolved_parent or not resolved.is_relative_to(resolved_parent):
        raise MaintenanceError("recovery_required", "Refusing unsafe maintenance cleanup.")
    if _lexists(path):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            _fsync_directory(allowed_parent)
        except OSError as exc:
            raise MaintenanceError(
                "recovery_required", "Could not clean incomplete maintenance state."
            ) from exc


def _create_backup_locked(
    root: Path,
    purpose: str,
    *,
    destructive: bool,
    phase_hook: PhaseHook | None,
) -> BackupReference | None:
    purpose = _validate_purpose(purpose)
    backups_root = root / "data" / "backups"
    try:
        if _lexists(backups_root):
            result = backups_root.lstat()
            if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
                raise MaintenanceError(
                    "unsafe_state_path", "Backup root is not a safe directory."
                )
        else:
            # A published backup is not durable if only the leaf directory is
            # synced: on first use, a power loss could discard ``data`` or
            # ``backups`` after live state has already been switched.  Create
            # and sync every missing ancestor before staging any payload.
            _ensure_directory_durable(backups_root)
        os.chmod(backups_root, 0o700)
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError(
            "state_write_failed", "Could not prepare the backup directory."
        ) from exc
    backup_id = _new_backup_id(purpose)
    stage = backups_root / f".incomplete-{backup_id}"
    final = backups_root / backup_id
    try:
        manifest = _build_backup_stage(
            root,
            stage,
            purpose=purpose,
            destructive=destructive,
            phase_hook=phase_hook,
        )
        if manifest is None:
            return None
        os.replace(stage, final)
        _fsync_directory(backups_root)
        try:
            verified = verify_backup(root, final, expected_purpose=purpose)
        except BaseException:
            _cleanup_private_directory(final, backups_root)
            raise
        _emit_phase(phase_hook, "after_backup_publish")
        return BackupReference(backup_id, purpose, final, verified)
    except BaseException:
        if _lexists(stage):
            _cleanup_private_directory(stage, backups_root)
        raise


def _read_manifest_file(path: Path, maximum_size: int = 4 * 1024 * 1024) -> bytes:
    before = _lstat_regular(path, code="invalid_manifest")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MaintenanceError("invalid_manifest", "Backup manifest changed while opening.")
        if opened.st_size > maximum_size:
            raise MaintenanceError("invalid_manifest", "Backup manifest is too large.")
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > maximum_size:
            raise MaintenanceError("invalid_manifest", "Backup manifest is too large.")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != len(value)
        ):
            raise MaintenanceError("invalid_manifest", "Backup manifest changed while reading.")
        return value
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError("invalid_manifest", "Could not read backup manifest.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _backup_name(root: Path, backup: str | Path) -> str:
    backups_root = root / "data" / "backups"
    candidate = Path(backup)
    if candidate.is_absolute():
        normalized = Path(os.path.abspath(os.fspath(candidate)))
        if normalized.parent != backups_root:
            raise MaintenanceError(
                "unsafe_backup_path", "Backup source must be directly under data/backups."
            )
        name = normalized.name
    else:
        if len(candidate.parts) != 1 or candidate.parts[0] in {"", ".", ".."}:
            raise MaintenanceError(
                "unsafe_backup_path", "Backup selector must be one directory name."
            )
        name = candidate.parts[0]
    if "/" in name or "\\" in name or "\x00" in name:
        raise MaintenanceError("unsafe_backup_path", "Backup selector is unsafe.")
    return name


@contextmanager
def _open_backup_directory(
    root: Path,
    backup: str | Path,
) -> Iterator[_OpenedBackup]:
    name = _backup_name(root, backup)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    opened: _OpenedBackup | None = None
    try:
        root_descriptor = os.open(root, flags)
        descriptors.append(root_descriptor)
        data_descriptor = os.open("data", flags, dir_fd=root_descriptor)
        descriptors.append(data_descriptor)
        backups_descriptor = os.open("backups", flags, dir_fd=data_descriptor)
        descriptors.append(backups_descriptor)
        backup_descriptor = os.open(name, flags, dir_fd=backups_descriptor)
        descriptors.append(backup_descriptor)
        result = os.fstat(backup_descriptor)
        if not stat.S_ISDIR(result.st_mode):
            raise MaintenanceError("unsafe_backup_path", "Backup source is not a directory.")
        opened = _OpenedBackup(
            backup_id=name,
            directory_descriptor=backup_descriptor,
        )
        yield opened
    except FileNotFoundError as exc:
        raise MaintenanceError("missing_backup", "Backup source does not exist.") from exc
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError(
            "unsafe_backup_path", "Could not open backup directories safely."
        ) from exc
    finally:
        if opened is not None and opened.payload_descriptor is not None:
            os.close(opened.payload_descriptor)
            opened.payload_descriptor = None
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_backup_metadata(
    opened: _OpenedBackup,
    name: str,
    *,
    maximum_size: int,
) -> bytes:
    descriptor: int | None = None
    try:
        before = os.stat(
            name,
            dir_fd=opened.directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MaintenanceError(
                "invalid_manifest", "Backup metadata is not a safe regular file."
            )
        if before.st_size > maximum_size:
            raise MaintenanceError("invalid_manifest", "Backup metadata is too large.")
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=opened.directory_descriptor,
        )
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise MaintenanceError("invalid_manifest", "Backup metadata changed while opening.")
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(value) > maximum_size or after.st_size != len(value):
            raise MaintenanceError("invalid_manifest", "Backup metadata changed while reading.")
        return value
    except FileNotFoundError as exc:
        raise MaintenanceError("manifest_missing", "Backup manifest is missing.") from exc
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError("invalid_manifest", "Could not read backup metadata.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_entry(entry: Any) -> tuple[PurePosixPath, PurePosixPath, str]:
    if not isinstance(entry, dict):
        raise MaintenanceError("invalid_manifest", "Backup entry is malformed.")
    common_keys = {
        "source_path",
        "archive_path",
        "kind",
        "size_bytes",
        "sha256",
        "mode",
    }
    if not common_keys.issubset(entry):
        raise MaintenanceError("invalid_manifest", "Backup entry is incomplete.")
    source = _safe_relative(entry["source_path"])
    archive = _safe_relative(entry["archive_path"])
    if archive != PurePosixPath("payload") / source:
        raise MaintenanceError("unsafe_backup_path", "Backup path identity is inconsistent.")
    kind = entry.get("kind")
    if kind == "sqlite":
        if (
            set(entry) != common_keys | {"sqlite"}
            or source.as_posix() not in DATABASE_PATH_SET
            or not isinstance(entry.get("sqlite"), dict)
        ):
            raise MaintenanceError("invalid_manifest", "SQLite backup entry is invalid.")
    elif kind == "file":
        if set(entry) != common_keys | {"source_mode"}:
            raise MaintenanceError("invalid_manifest", "File backup entry is invalid.")
        if not _is_tree_path(source):
            raise MaintenanceError("unsafe_backup_path", "Backup contains an unmanaged file.")
        if not isinstance(entry.get("source_mode"), str) or not re.fullmatch(
            r"0[0-7]{3}", entry["source_mode"]
        ):
            raise MaintenanceError("invalid_manifest", "Backup source mode is invalid.")
    else:
        raise MaintenanceError("invalid_manifest", "Backup entry kind is unsupported.")
    if _is_secret_path(source):
        raise MaintenanceError("secret_in_backup", "Backup contains an excluded secret path.")
    if (
        not _is_plain_int(entry["size_bytes"])
        or entry["size_bytes"] < 0
        or entry.get("mode") != "0600"
        or not isinstance(entry["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    ):
        raise MaintenanceError("invalid_manifest", "Backup entry metadata is invalid.")
    return source, archive, kind


def _enumerate_payload(payload: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    try:
        for current, directory_names, file_names in os.walk(payload, followlinks=False):
            current_path = Path(current)
            for name in list(directory_names):
                child = current_path / name
                result = child.lstat()
                if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
                    raise MaintenanceError(
                        "unsafe_backup_path", "Backup payload contains an unsafe directory."
                    )
                relative = child.relative_to(payload).as_posix()
                directories.add(relative)
            for name in file_names:
                child = current_path / name
                _lstat_regular(child, code="unsafe_backup_path")
                files.add(child.relative_to(payload).as_posix())
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError("invalid_manifest", "Could not enumerate backup payload.") from exc
    return files, directories


def _verify_opened_backup(
    opened: _OpenedBackup,
    *,
    expected_purpose: str | None = None,
) -> dict[str, Any]:
    manifest_bytes = _read_backup_metadata(
        opened,
        "manifest.json",
        maximum_size=4 * 1024 * 1024,
    )
    checksum_bytes = _read_backup_metadata(
        opened,
        "manifest.sha256",
        maximum_size=256,
    )
    try:
        checksum = checksum_bytes.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise MaintenanceError("invalid_manifest", "Manifest checksum is malformed.") from exc
    if checksum != _sha256_bytes(manifest_bytes):
        raise MaintenanceError("manifest_hash_mismatch", "Manifest checksum does not match.")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError("invalid_manifest", "Backup manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict) or _canonical_json(manifest) != manifest_bytes:
        raise MaintenanceError("invalid_manifest", "Backup manifest is not canonical.")
    expected_manifest_keys = {
        "format",
        "manifest_version",
        "layout_version",
        "complete",
        "backup_id",
        "purpose",
        "created_at",
        "application_version",
        "schema",
        "entries",
        "directories",
        "exclusions",
        "payload_sha256",
    }
    if (
        set(manifest) != expected_manifest_keys
        or manifest.get("format") != MANIFEST_FORMAT
        or not _is_plain_int(manifest.get("manifest_version"))
        or manifest["manifest_version"] != MANIFEST_VERSION
        or not _is_plain_int(manifest.get("layout_version"))
        or manifest["layout_version"] != LAYOUT_VERSION
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("backup_id"), str)
        or manifest.get("backup_id") != opened.backup_id
        or not isinstance(manifest.get("entries"), list)
        or not isinstance(manifest.get("directories"), list)
    ):
        raise MaintenanceError("invalid_manifest", "Backup manifest contract is invalid.")
    if (
        not isinstance(manifest.get("created_at"), str)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
            manifest["created_at"],
        )
        or not isinstance(manifest.get("application_version"), str)
        or not manifest["application_version"]
        or not isinstance(manifest.get("schema"), dict)
        or set(manifest["schema"]) != {"supported_version", "databases"}
        or not isinstance(manifest["schema"].get("databases"), list)
        or not all(isinstance(item, dict) for item in manifest["schema"]["databases"])
        or not isinstance(manifest.get("exclusions"), dict)
        or set(manifest["exclusions"])
        != {"patterns", "paths", "secrets_included"}
        or manifest["exclusions"].get("secrets_included") is not False
        or manifest["exclusions"].get("patterns") != [".env", ".env.*"]
        or not isinstance(manifest["exclusions"].get("paths"), list)
    ):
        raise MaintenanceError("invalid_manifest", "Backup metadata contract is invalid.")
    exclusion_paths = manifest["exclusions"]["paths"]
    if (
        not all(isinstance(value, str) for value in exclusion_paths)
        or exclusion_paths != sorted(set(exclusion_paths))
    ):
        raise MaintenanceError("invalid_manifest", "Backup exclusions are malformed.")
    for value in exclusion_paths:
        try:
            excluded = _safe_relative(value)
        except MaintenanceError as exc:
            raise MaintenanceError(
                "invalid_manifest", "Backup exclusion path is invalid."
            ) from exc
        if not _is_tree_path(excluded) or not _is_secret_path(excluded):
            raise MaintenanceError("invalid_manifest", "Backup exclusion path is invalid.")
    purpose = manifest.get("purpose")
    if not isinstance(purpose, str) or not PURPOSE_PATTERN.fullmatch(purpose):
        raise MaintenanceError("invalid_manifest", "Backup purpose is invalid.")
    identity_purpose, _identity_timestamp, identity_created_at = _parse_backup_id(
        manifest["backup_id"]
    )
    if purpose != identity_purpose or manifest["created_at"] != identity_created_at:
        raise MaintenanceError(
            "invalid_manifest", "Backup identity is not bound to its metadata."
        )
    if expected_purpose is not None and purpose != expected_purpose:
        raise MaintenanceError("purpose_mismatch", "Backup purpose does not match this workflow.")

    expected_files: set[str] = set()
    source_paths: set[str] = set()
    archive_paths: set[str] = set()
    source_casefold: set[str] = set()
    entries: list[dict[str, Any]] = manifest["entries"]
    inspected_database_summaries: list[dict[str, Any]] = []
    opened.open_payload()
    payload = opened.payload_path
    for entry in entries:
        source, archive, kind = _validate_entry(entry)
        source_value = source.as_posix()
        archive_value = archive.as_posix()
        if (
            source_value in source_paths
            or archive_value in archive_paths
            or source_value.casefold() in source_casefold
        ):
            raise MaintenanceError("invalid_manifest", "Backup contains duplicate paths.")
        source_paths.add(source_value)
        archive_paths.add(archive_value)
        source_casefold.add(source_value.casefold())
        expected_files.add(source_value)
        payload_file = payload.joinpath(*source.parts)
        actual = _lstat_regular(payload_file, code="invalid_manifest")
        if actual.st_size != entry["size_bytes"] or _sha256_file(payload_file) != entry["sha256"]:
            raise MaintenanceError("hash_mismatch", "Backup payload hash does not match manifest.")
        if kind == "sqlite":
            inspected = _inspect_sqlite(payload_file)
            recorded = entry["sqlite"]
            expected_sqlite_keys = {
                "integrity_check",
                "foreign_key_check_count",
                "schema_version",
                "user_version",
                "schema_sha256",
                "migration_history_sha256",
                "page_size",
                "sqlite_version",
            }
            if (
                set(recorded) != expected_sqlite_keys
                or recorded.get("integrity_check") != "ok"
                or not _is_plain_int(recorded.get("foreign_key_check_count"))
                or recorded["foreign_key_check_count"] != 0
                or not _is_plain_int(recorded.get("schema_version"))
                or recorded["schema_version"] < 0
                or not _is_plain_int(recorded.get("user_version"))
                or recorded["user_version"] < 0
                or not _is_plain_int(recorded.get("page_size"))
                or recorded["page_size"] <= 0
                or not isinstance(recorded.get("schema_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", recorded["schema_sha256"]) is None
                or not isinstance(recorded.get("migration_history_sha256"), str)
                or re.fullmatch(
                    r"[0-9a-f]{64}", recorded["migration_history_sha256"]
                )
                is None
                or not isinstance(recorded.get("sqlite_version"), str)
                or not recorded["sqlite_version"]
            ):
                raise MaintenanceError(
                    "invalid_manifest", "Backup SQLite metadata is malformed."
                )
            for key in (
                "integrity_check",
                "foreign_key_check_count",
                "schema_version",
                "user_version",
                "schema_sha256",
                "migration_history_sha256",
                "page_size",
            ):
                if recorded.get(key) != inspected[key]:
                    raise MaintenanceError(
                        "schema_incompatible", "Backup SQLite metadata does not match manifest."
                    )
            inspected_database_summaries.append(
                {
                    "source_path": source_value,
                    "schema_version": inspected["schema_version"],
                    "user_version": inspected["user_version"],
                    "schema_sha256": inspected["schema_sha256"],
                    "migration_history_sha256": inspected[
                        "migration_history_sha256"
                    ],
                }
            )

    supported_version = manifest["schema"].get("supported_version")
    recorded_database_summaries = manifest["schema"]["databases"]
    expected_summary_keys = {
        "source_path",
        "schema_version",
        "user_version",
        "schema_sha256",
        "migration_history_sha256",
    }
    if not _is_plain_int(supported_version) or any(
        set(item) != expected_summary_keys
        or not isinstance(item.get("source_path"), str)
        or not _is_plain_int(item.get("schema_version"))
        or item["schema_version"] < 0
        or not _is_plain_int(item.get("user_version"))
        or item["user_version"] < 0
        or not isinstance(item.get("schema_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", item["schema_sha256"]) is None
        or not isinstance(item.get("migration_history_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", item["migration_history_sha256"])
        is None
        for item in recorded_database_summaries
    ):
        raise MaintenanceError("invalid_manifest", "Backup schema summary is malformed.")
    if (
        supported_version < 0
        or supported_version > CURRENT_SCHEMA_VERSION
        or sorted(recorded_database_summaries, key=lambda item: item["source_path"])
        != sorted(inspected_database_summaries, key=lambda item: item["source_path"])
    ):
        raise MaintenanceError("schema_incompatible", "Backup schema summary is inconsistent.")

    if manifest.get("payload_sha256") != _payload_digest(entries):
        raise MaintenanceError("hash_mismatch", "Backup payload digest does not match manifest.")
    actual_files, actual_directories = _enumerate_payload(payload)
    if actual_files != expected_files:
        raise MaintenanceError(
            "invalid_manifest", "Backup payload file set does not match manifest."
        )

    manifest_directories: set[str] = set()
    for value in manifest["directories"]:
        directory = _safe_relative(value)
        if not _is_tree_path(directory) or _is_secret_path(directory):
            raise MaintenanceError("unsafe_backup_path", "Backup directory path is unsafe.")
        manifest_directories.add(directory.as_posix())
    expected_directories: set[str] = set()
    for source_path in source_paths.union(manifest_directories):
        archive = PurePosixPath("payload") / PurePosixPath(source_path)
        for parent in archive.parents:
            if parent.as_posix() in {".", "payload"}:
                continue
            expected_directories.add(parent.relative_to("payload").as_posix())
        if source_path in manifest_directories:
            expected_directories.add(source_path)
    if actual_directories != expected_directories:
        raise MaintenanceError("invalid_manifest", "Backup directory set does not match manifest.")
    return manifest


def verify_backup(
    root: str | Path,
    backup: str | Path,
    *,
    expected_purpose: str | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve(strict=True)
    with _open_backup_directory(root_path, backup) as opened:
        return _verify_opened_backup(
            opened,
            expected_purpose=expected_purpose,
        )


def _select_latest_backup(root: Path, purpose: str) -> Path:
    _validate_purpose(purpose)
    backups_root = root / "data" / "backups"
    if not backups_root.is_dir():
        raise MaintenanceError("missing_backup", "No backup is available.")
    candidates: list[tuple[str, str, Path]] = []
    try:
        children = sorted(backups_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MaintenanceError("missing_backup", "Could not enumerate backups.") from exc
    for child in children:
        try:
            identity_purpose, identity_timestamp, _created_at = _parse_backup_id(
                child.name
            )
        except MaintenanceError:
            continue
        if identity_purpose != purpose:
            continue
        candidates.append((identity_timestamp, child.name, child))
    if not candidates:
        raise MaintenanceError("missing_backup", "No backup matches this purpose.")
    selected = max(candidates)[2]
    # Directory identity chooses the sole candidate first.  Verification is
    # deliberately not part of candidate filtering: a damaged newest backup
    # must fail closed instead of silently rolling back farther than requested.
    verify_backup(root, selected, expected_purpose=purpose)
    return selected


def _maintenance_operations_root(root: Path, *, create: bool) -> Path:
    maintenance_root = root / "data" / ".maintenance"
    operations_root = maintenance_root / "operations"
    if _lexists(maintenance_root):
        result = maintenance_root.lstat()
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
            raise MaintenanceError(
                "recovery_required", "Maintenance control path is unsafe."
            )
    elif not create:
        return operations_root
    if _lexists(operations_root):
        result = operations_root.lstat()
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
            raise MaintenanceError(
                "recovery_required", "Maintenance operations path is unsafe."
            )
    if create:
        try:
            if not _lexists(operations_root):
                # The recovery journal and rollback tree are useful only when
                # every control-directory entry leading to them is durable.
                _ensure_directory_durable(operations_root)
            os.chmod(maintenance_root, 0o700)
            os.chmod(operations_root, 0o700)
            _fsync_directory(maintenance_root)
            _fsync_directory(operations_root)
        except OSError as exc:
            raise MaintenanceError(
                "state_write_failed", "Could not prepare maintenance control state."
            ) from exc
    return operations_root


def _new_operation_directory(root: Path) -> Path:
    operations_root = _maintenance_operations_root(root, create=True)
    operation = operations_root / uuid.uuid4().hex
    try:
        operation.mkdir(mode=0o700, exist_ok=False)
        _fsync_directory(operation)
        _fsync_directory(operations_root)
        (operation / "candidate").mkdir(mode=0o700)
        _fsync_directory(operation / "candidate")
        _fsync_directory(operation)
        (operation / "rollback").mkdir(mode=0o700)
        _fsync_directory(operation / "rollback")
        _fsync_directory(operation)
        _fsync_directory(operations_root)
    except BaseException as exc:
        try:
            if _lexists(operation):
                _cleanup_private_directory(operation, operations_root)
        except BaseException as cleanup_exc:
            raise MaintenanceError(
                "recovery_required",
                "Operation staging failed and could not be cleaned safely.",
                exit_code=4,
            ) from cleanup_exc
        if isinstance(exc, MaintenanceError):
            raise
        raise MaintenanceError(
            "state_write_failed", "Could not create operation staging."
        ) from exc
    return operation


def _managed_live_objects(root: Path) -> list[PurePosixPath]:
    objects: list[PurePosixPath] = []
    for database_relative in DATABASE_PATHS:
        database = _path_under(root, database_relative)
        if _lexists(database):
            _lstat_regular(database)
            objects.append(database_relative)
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar_relative = PurePosixPath(database_relative.as_posix() + suffix)
            sidecar = _path_under(root, sidecar_relative)
            if _lexists(sidecar):
                _lstat_regular(sidecar)
                objects.append(sidecar_relative)
    for tree_relative in TREE_PATHS:
        tree = _path_under(root, tree_relative)
        if not _lexists(tree):
            continue
        result = tree.lstat()
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
            raise MaintenanceError("unsafe_state_path", "Managed runtime tree is unsafe.")
        objects.append(tree_relative)
    return sorted(objects, key=lambda item: item.as_posix())


def _candidate_objects(operation: Path) -> list[PurePosixPath]:
    candidate = operation / "candidate"
    objects: list[PurePosixPath] = []
    for database_relative in DATABASE_PATHS:
        path = candidate.joinpath(*database_relative.parts)
        if _lexists(path):
            _lstat_regular(path, code="recovery_required")
            objects.append(database_relative)
    for tree_relative in TREE_PATHS:
        path = candidate.joinpath(*tree_relative.parts)
        if not _lexists(path):
            continue
        result = path.lstat()
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
            raise MaintenanceError("recovery_required", "Restore candidate is unsafe.")
        objects.append(tree_relative)
    return sorted(objects, key=lambda item: item.as_posix())


def _ensure_directory_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not _lexists(current):
        parent = current.parent
        if parent == current:
            raise MaintenanceError(
                "state_swap_failed", "Could not find a safe swap-directory ancestor."
            )
        missing.append(current)
        current = parent
    result = current.lstat()
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        raise MaintenanceError("state_swap_failed", "Swap-directory ancestor is unsafe.")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise MaintenanceError(
                "state_swap_failed", "Could not prepare a durable swap directory."
            ) from exc
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _replace_path(source: Path, destination: Path) -> None:
    try:
        _ensure_directory_durable(destination.parent)
        if _lexists(destination):
            raise MaintenanceError(
                "recovery_required", "A maintenance swap destination already exists."
            )
        if source.stat(follow_symlinks=False).st_dev != destination.parent.stat().st_dev:
            raise MaintenanceError(
                "restore_cross_device",
                "Runtime state cannot be atomically swapped across filesystems.",
            )
        os.replace(source, destination)
        _fsync_directory(source.parent)
        if destination.parent != source.parent:
            _fsync_directory(destination.parent)
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError(
            "state_swap_failed", "Could not atomically swap runtime state."
        ) from exc


def _load_journal(operation: Path) -> dict[str, Any]:
    journal_path = operation / "journal.json"
    value = _read_manifest_file(journal_path)
    try:
        journal = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError("recovery_required", "Operation journal is malformed.") from exc
    if not isinstance(journal, dict) or _canonical_json(journal) != value:
        raise MaintenanceError("recovery_required", "Operation journal is not canonical.")
    for key in ("operation_id", "operation", "phase", "old_paths", "target_paths"):
        if key not in journal:
            raise MaintenanceError("recovery_required", "Operation journal is incomplete.")
    if journal["operation_id"] != operation.name:
        raise MaintenanceError("recovery_required", "Operation journal identity is invalid.")
    if journal["operation"] not in {"reset", "restore"}:
        raise MaintenanceError("recovery_required", "Operation journal kind is invalid.")
    for key in ("old_paths", "target_paths", "old_moved", "installed"):
        values = journal.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise MaintenanceError("recovery_required", "Operation journal paths are invalid.")
        for value in values:
            relative = _safe_relative(value)
            allowed = (
                relative.as_posix() in DATABASE_PATH_SET
                or any(
                    relative.as_posix() == database + suffix
                    for database in DATABASE_PATH_SET
                    for suffix in SQLITE_SIDECAR_SUFFIXES
                )
                or relative in TREE_PATHS
            )
            if not allowed:
                raise MaintenanceError("recovery_required", "Operation journal path is unmanaged.")
    return journal


def _persist_journal(operation: Path, journal: dict[str, Any]) -> None:
    _atomic_write_json(operation / "journal.json", journal)


def _rollback_operation(root: Path, operation: Path, journal: dict[str, Any]) -> None:
    candidate_root = operation / "candidate"
    rollback_root = operation / "rollback"
    old_paths = {_safe_relative(value) for value in journal["old_paths"]}
    target_paths = [_safe_relative(value) for value in journal["target_paths"]]

    # Pull any installed target back into candidate staging.  Journal updates
    # can lag a rename by one instruction, so filesystem state is authoritative.
    for relative in reversed(target_paths):
        live = _path_under(root, relative)
        candidate = candidate_root.joinpath(*relative.parts)
        rollback = rollback_root.joinpath(*relative.parts)
        if _lexists(rollback):
            if _lexists(live):
                if _lexists(candidate):
                    raise MaintenanceError(
                        "recovery_required", "Restore rollback found ambiguous target state."
                    )
                _replace_path(live, candidate)
            continue
        if relative in old_paths:
            # A previous rollback pass already restored the old object.
            continue
        if _lexists(live) and not _lexists(candidate):
            _replace_path(live, candidate)
        elif _lexists(live) and _lexists(candidate):
            raise MaintenanceError(
                "recovery_required", "Restore rollback found duplicate target state."
            )

    for relative in sorted(old_paths, key=lambda item: item.as_posix(), reverse=True):
        live = _path_under(root, relative)
        rollback = rollback_root.joinpath(*relative.parts)
        if not _lexists(rollback):
            continue
        if _lexists(live):
            raise MaintenanceError(
                "recovery_required", "Restore rollback cannot replace unexpected live state."
            )
        _replace_path(rollback, live)

    journal["phase"] = "rolled_back"
    _persist_journal(operation, journal)


def _recover_incomplete_locked(root: Path) -> bool:
    operations_root = _maintenance_operations_root(root, create=False)
    if not operations_root.is_dir():
        return False
    recovered = False
    try:
        operations = sorted(operations_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MaintenanceError(
            "recovery_required", "Could not enumerate operation journals."
        ) from exc
    for operation in operations:
        if operation.is_symlink() or not operation.is_dir() or not re.fullmatch(
            r"[0-9a-f]{32}", operation.name
        ):
            raise MaintenanceError("recovery_required", "Maintenance operation path is unsafe.")
        journal_path = operation / "journal.json"
        if not _lexists(journal_path):
            candidate = operation / "candidate"
            rollback = operation / "rollback"
            for required in (candidate, rollback):
                if not _lexists(required):
                    raise MaintenanceError(
                        "recovery_required",
                        "Journal-less maintenance state is incomplete.",
                        exit_code=4,
                    )
                result = required.lstat()
                if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
                    raise MaintenanceError(
                        "recovery_required",
                        "Journal-less maintenance staging is unsafe.",
                        exit_code=4,
                    )
            try:
                with os.scandir(rollback) as entries:
                    rollback_is_empty = next(entries, None) is None
            except OSError as exc:
                raise MaintenanceError(
                    "recovery_required", "Could not inspect journal-less rollback state."
                ) from exc
            if not rollback_is_empty:
                raise MaintenanceError(
                    "recovery_required",
                    "Rollback data exists without a durable operation journal.",
                    exit_code=4,
                )
            _cleanup_private_directory(operation, operations_root)
            recovered = True
            continue
        result = journal_path.lstat()
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISREG(result.st_mode):
            raise MaintenanceError(
                "recovery_required", "Operation journal path is unsafe.", exit_code=4
            )
        journal = _load_journal(operation)
        if journal["phase"] in {"committed", "rolled_back"}:
            _cleanup_private_directory(operation, operations_root)
            recovered = True
            continue
        _rollback_operation(root, operation, journal)
        _cleanup_private_directory(operation, operations_root)
        recovered = True
    return recovered


def _recover_incomplete_backups_locked(root: Path) -> bool:
    backups_root = root / "data" / "backups"
    if not _lexists(backups_root):
        return False
    result = backups_root.lstat()
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        raise MaintenanceError("recovery_required", "Backup root is unsafe during recovery.")
    recovered = False
    try:
        children = sorted(backups_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MaintenanceError(
            "recovery_required", "Could not enumerate backup recovery state."
        ) from exc
    for child in children:
        if not child.name.startswith(".incomplete-"):
            continue
        backup_id = child.name.removeprefix(".incomplete-")
        if child.is_symlink() or not child.is_dir() or not BACKUP_ID_PATTERN.fullmatch(
            backup_id
        ):
            raise MaintenanceError("recovery_required", "Incomplete backup path is unsafe.")
        _cleanup_private_directory(child, backups_root)
        recovered = True
    if recovered:
        _fsync_directory(backups_root)
    return recovered


def _require_clean_recovery_state(root: Path) -> None:
    """Fail read-only inspection when an explicit recovery is pending."""

    operations_root = _maintenance_operations_root(root, create=False)
    if _lexists(operations_root):
        result = operations_root.lstat()
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
            raise MaintenanceError(
                "recovery_required", "Maintenance operation state is unsafe."
            )
        try:
            with os.scandir(operations_root) as entries:
                has_pending_operation = next(entries, None) is not None
            if has_pending_operation:
                raise MaintenanceError(
                    "recovery_required",
                    "A pending maintenance operation must be recovered first.",
                    exit_code=4,
                )
        except MaintenanceError:
            raise
        except OSError as exc:
            raise MaintenanceError(
                "recovery_required", "Could not inspect pending maintenance operations."
            ) from exc

    backups_root = root / "data" / "backups"
    if not _lexists(backups_root):
        return
    result = backups_root.lstat()
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        raise MaintenanceError("recovery_required", "Backup recovery state is unsafe.")
    try:
        with os.scandir(backups_root) as entries:
            has_incomplete = any(
                child.name.startswith(".incomplete-") for child in entries
            )
    except OSError as exc:
        raise MaintenanceError(
            "recovery_required", "Could not inspect incomplete backups."
        ) from exc
    if has_incomplete:
        raise MaintenanceError(
            "recovery_required",
            "An incomplete backup must be recovered first.",
            exit_code=4,
        )


@contextmanager
def _inspect_state(root: Path) -> Iterator[dict[PurePosixPath, sqlite3.Connection]]:
    """Hold state stable for a dry run without performing automatic recovery."""

    with _DirectoryLease(root):
        _require_clean_recovery_state(root)
        with _read_only_guards(root) as guards:
            yield guards


@contextmanager
def _exclusive_state(root: Path) -> Iterator[dict[PurePosixPath, sqlite3.Connection]]:
    with _DirectoryLease(root):
        recovered = _recover_orphaned_switch_locked(root)
        with _writer_guards(root) as guards:
            recovered = _recover_incomplete_locked(root) or recovered
            _recover_incomplete_backups_locked(root)
            if not recovered:
                yield guards
                return
        # Recovery may replace a database inode, so guard the recovered files.
        with _writer_guards(root) as guards:
            yield guards


def _prepare_required_directories(candidate_root: Path) -> None:
    for relative in (
        PurePosixPath("data/context/plans"),
        PurePosixPath("data/context/decisions"),
        PurePosixPath("data/workspace"),
    ):
        try:
            candidate_root.joinpath(*relative.parts).mkdir(
                mode=0o700,
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            raise MaintenanceError(
                "state_write_failed", "Could not prepare a runtime-state candidate."
            ) from exc


def _materialize_candidate(
    operation: Path,
    payload_root: Path,
    manifest: dict[str, Any],
) -> None:
    candidate_root = operation / "candidate"
    for value in manifest["directories"]:
        relative = _safe_relative(value)
        try:
            candidate_root.joinpath(*relative.parts).mkdir(
                mode=0o700,
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            raise MaintenanceError(
                "state_copy_failed", "Could not stage backup directories."
            ) from exc
    for entry in manifest["entries"]:
        source_relative, _archive_relative, _kind = _validate_entry(entry)
        source = payload_root.joinpath(*source_relative.parts)
        destination = candidate_root.joinpath(*source_relative.parts)
        size, digest, _mode, _mtime = _copy_regular(source, destination)
        if size != entry["size_bytes"] or digest != entry["sha256"]:
            raise MaintenanceError("hash_mismatch", "Restore candidate hash does not match.")
        if entry["kind"] == "sqlite":
            inspected = _inspect_sqlite(destination)
            for key in (
                "integrity_check",
                "foreign_key_check_count",
                "schema_version",
                "user_version",
                "schema_sha256",
                "migration_history_sha256",
                "page_size",
            ):
                if inspected[key] != entry["sqlite"].get(key):
                    raise MaintenanceError(
                        "schema_incompatible", "Restore candidate schema does not match."
                    )
    _prepare_required_directories(candidate_root)


def _scan_live_file_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for relative in DATABASE_PATHS:
        database = _path_under(root, relative)
        if _lexists(database):
            _lstat_regular(database)
            paths.add(relative.as_posix())
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar = database.with_name(database.name + suffix)
            if _lexists(sidecar):
                raise MaintenanceError(
                    "restore_verify_failed", "Restored database unexpectedly has a sidecar."
                )
    for tree in TREE_PATHS:
        files, _directories, exclusions = _scan_tree(root, tree, destructive=False)
        if exclusions:
            raise MaintenanceError(
                "restore_verify_failed", "Restored state contains an excluded secret path."
            )
        paths.update(item.relative_path.as_posix() for item in files)
    return paths


def _verify_live_against_manifest(root: Path, manifest: dict[str, Any]) -> None:
    expected_paths = {entry["source_path"] for entry in manifest["entries"]}
    if _scan_live_file_paths(root) != expected_paths:
        raise MaintenanceError(
            "restore_verify_failed", "Restored file set does not match backup manifest."
        )
    for entry in manifest["entries"]:
        relative, _archive, kind = _validate_entry(entry)
        live = _path_under(root, relative)
        result = _lstat_regular(live, code="restore_verify_failed")
        if result.st_size != entry["size_bytes"] or _sha256_file(live) != entry["sha256"]:
            raise MaintenanceError("restore_verify_failed", "Restored file hash does not match.")
        if kind == "sqlite":
            inspected = _inspect_sqlite(live)
            for key in (
                "integrity_check",
                "foreign_key_check_count",
                "schema_version",
                "user_version",
                "schema_sha256",
                "migration_history_sha256",
                "page_size",
            ):
                if inspected[key] != entry["sqlite"].get(key):
                    raise MaintenanceError(
                        "restore_verify_failed", "Restored database metadata does not match."
                    )
    for value in manifest["directories"]:
        relative = _safe_relative(value)
        directory = _path_under(root, relative)
        if not directory.is_dir() or directory.is_symlink():
            raise MaintenanceError(
                "restore_verify_failed", "Restored directory set does not match."
            )


def _verify_empty_live(root: Path) -> None:
    if _scan_live_file_paths(root):
        raise MaintenanceError("reset_verify_failed", "Reset left managed runtime files behind.")
    for relative in (
        PurePosixPath("data/context/plans"),
        PurePosixPath("data/context/decisions"),
        PurePosixPath("data/workspace"),
    ):
        directory = _path_under(root, relative)
        if not directory.is_dir() or directory.is_symlink():
            raise MaintenanceError(
                "reset_verify_failed", "Reset runtime directories are incomplete."
            )


def _switch_state(
    root: Path,
    operation: Path,
    *,
    operation_kind: str,
    post_verify: Callable[[], None],
    phase_hook: PhaseHook | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    old_paths = _managed_live_objects(root)
    target_paths = _candidate_objects(operation)
    candidate_root = operation / "candidate"
    rollback_root = operation / "rollback"
    for relative in old_paths:
        _ensure_directory_durable(rollback_root.joinpath(*relative.parts).parent)
    _fsync_tree_directories(candidate_root)
    _fsync_tree_directories(rollback_root)
    _fsync_directory(operation)
    journal: dict[str, Any] = {
        "journal_version": 1,
        "operation_id": operation.name,
        "operation": operation_kind,
        "phase": "prepared",
        "created_at": _utc_now(),
        "old_paths": [path.as_posix() for path in old_paths],
        "target_paths": [path.as_posix() for path in target_paths],
        "old_moved": [],
        "installed": [],
        "metadata": metadata or {},
    }
    _persist_journal(operation, journal)
    _emit_phase(phase_hook, "after_operation_journal")
    committed = False
    try:
        for relative in old_paths:
            source = _path_under(root, relative)
            destination = rollback_root.joinpath(*relative.parts)
            _replace_path(source, destination)
            journal["old_moved"].append(relative.as_posix())
            journal["phase"] = "quarantining"
            _persist_journal(operation, journal)
        _emit_phase(phase_hook, "after_live_quarantine")

        for index, relative in enumerate(target_paths):
            source = candidate_root.joinpath(*relative.parts)
            destination = _path_under(root, relative)
            _replace_path(source, destination)
            journal["installed"].append(relative.as_posix())
            journal["phase"] = "installing"
            _persist_journal(operation, journal)
            if index == 0:
                _emit_phase(phase_hook, "after_first_install")

        journal["phase"] = "verifying"
        _persist_journal(operation, journal)
        post_verify()
        journal["phase"] = "committed"
        _persist_journal(operation, journal)
        committed = True
        _emit_phase(phase_hook, "after_state_commit")
    except BaseException:
        if committed:
            raise
        try:
            _rollback_operation(root, operation, journal)
        except BaseException as rollback_error:
            raise MaintenanceError(
                "recovery_required",
                "Maintenance failed and automatic rollback did not complete.",
                exit_code=4,
            ) from rollback_error
        try:
            _cleanup_private_directory(operation, operation.parent)
        except MaintenanceError:
            pass
        raise
    try:
        _cleanup_private_directory(operation, operation.parent)
    except MaintenanceError as exc:
        raise MaintenanceError(
            "recovery_required", "Committed state requires maintenance cleanup.", exit_code=4
        ) from exc


def _dry_run_backup_locked(
    root: Path,
    purpose: str,
    *,
    destructive: bool,
    phase_hook: PhaseHook | None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="learning-agent-maintenance-") as temporary:
        backup_id = _new_backup_id(_validate_purpose(purpose))
        stage = Path(temporary) / f".incomplete-{backup_id}"
        manifest = _build_backup_stage(
            root,
            stage,
            purpose=purpose,
            destructive=destructive,
            read_only_source=True,
            phase_hook=phase_hook,
        )
        return {
            "ok": True,
            "code": "dry_run_ok" if manifest is not None else "no_state",
            "dry_run": True,
            "entry_count": len(manifest["entries"]) if manifest is not None else 0,
        }


def _preflight_destructive_trees(root: Path) -> None:
    for relative_tree in TREE_PATHS:
        _scan_tree(root, relative_tree, destructive=True)


@contextmanager
def coordinated_state_mutation(
    root: str | Path,
    purpose: str = "pre_mutation",
    *,
    phase_hook: PhaseHook | None = None,
) -> Iterator[dict[str, Any]]:
    """Back up state, then yield an exclusive lifecycle window for mutation.

    Recovery and the verified snapshot run while SQLite writer reservations are
    held.  Those database reservations are released before yielding so the
    caller can write, while the shared lifecycle lease remains held to exclude
    the sanctioned runtime and every maintenance command.
    """

    root_path = Path(root).resolve(strict=True)
    purpose = _validate_purpose(purpose)
    backup: BackupReference | None = None
    with _DirectoryLease(root_path):
        recovered = _recover_orphaned_switch_locked(root_path)
        _recover_sqlite_crash_journals_locked(root_path)
        with _writer_guards(root_path):
            recovered = _recover_incomplete_locked(root_path) or recovered
            _recover_incomplete_backups_locked(root_path)
            if not recovered:
                backup = _create_backup_locked(
                    root_path,
                    purpose,
                    destructive=True,
                    phase_hook=phase_hook,
                )
        if recovered:
            # Recovery can replace a database inode.  Snapshot only after a
            # fresh reservation covers every recovered live database.
            _recover_sqlite_crash_journals_locked(root_path)
            with _writer_guards(root_path):
                backup = _create_backup_locked(
                    root_path,
                    purpose,
                    destructive=True,
                    phase_hook=phase_hook,
                )
        yield {
            "ok": True,
            "code": "mutation_ready",
            "backup_id": backup.backup_id if backup is not None else None,
            "purpose": purpose,
        }


def backup_state(
    root: str | Path,
    purpose: str = "manual",
    *,
    dry_run: bool = False,
    phase_hook: PhaseHook | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve(strict=True)
    state_context = _inspect_state if dry_run else _exclusive_state
    with state_context(root_path):
        if dry_run:
            return _dry_run_backup_locked(
                root_path,
                purpose,
                destructive=False,
                phase_hook=phase_hook,
            )
        backup = _create_backup_locked(
            root_path,
            purpose,
            destructive=False,
            phase_hook=phase_hook,
        )
        if backup is None:
            return {"ok": True, "code": "no_state", "backup_id": None}
        return {
            "ok": True,
            "code": "backup_created",
            "backup_id": backup.backup_id,
            "purpose": backup.purpose,
        }


def reset_state(
    root: str | Path,
    purpose: str = "pre_clean",
    *,
    dry_run: bool = False,
    phase_hook: PhaseHook | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve(strict=True)
    state_context = _inspect_state if dry_run else _exclusive_state
    with state_context(root_path):
        _preflight_destructive_trees(root_path)
        if dry_run:
            return _dry_run_backup_locked(
                root_path,
                purpose,
                destructive=True,
                phase_hook=phase_hook,
            )
        backup = _create_backup_locked(
            root_path,
            purpose,
            destructive=True,
            phase_hook=phase_hook,
        )
        _emit_phase(phase_hook, "before_state_switch")
        operation = _new_operation_directory(root_path)
        try:
            _prepare_required_directories(operation / "candidate")
            _switch_state(
                root_path,
                operation,
                operation_kind="reset",
                post_verify=lambda: _verify_empty_live(root_path),
                phase_hook=phase_hook,
                metadata={"backup_id": backup.backup_id if backup else None},
            )
        except BaseException:
            if _lexists(operation) and not (operation / "journal.json").is_file():
                _cleanup_private_directory(operation, operation.parent)
            raise
        return {
            "ok": True,
            "code": "state_reset",
            "backup_id": backup.backup_id if backup else None,
            "purpose": purpose,
        }


def restore_state(
    root: str | Path,
    *,
    backup: str | Path | None = None,
    latest_purpose: str | None = None,
    expected_purpose: str | None = None,
    dry_run: bool = False,
    phase_hook: PhaseHook | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve(strict=True)
    if (backup is None) == (latest_purpose is None):
        raise MaintenanceError(
            "invalid_arguments", "Specify exactly one backup or latest-purpose selector."
        )
    state_context = _inspect_state if dry_run else _exclusive_state
    with state_context(root_path):
        _preflight_destructive_trees(root_path)
        backup_selector: str | Path = (
            _select_latest_backup(root_path, latest_purpose)
            if latest_purpose is not None
            else (backup if backup is not None else "")
        )
        with _open_backup_directory(root_path, backup_selector) as opened:
            manifest = _verify_opened_backup(
                opened,
                expected_purpose=expected_purpose,
            )
            if dry_run:
                with tempfile.TemporaryDirectory(prefix="learning-agent-restore-") as temporary:
                    operation = Path(temporary) / uuid.uuid4().hex
                    (operation / "candidate").mkdir(parents=True, mode=0o700)
                    (operation / "rollback").mkdir(mode=0o700)
                    _materialize_candidate(operation, opened.payload_path, manifest)
                return {
                    "ok": True,
                    "code": "dry_run_ok",
                    "dry_run": True,
                    "backup_id": manifest["backup_id"],
                }

            operation = _new_operation_directory(root_path)
            try:
                _materialize_candidate(operation, opened.payload_path, manifest)
                _emit_phase(phase_hook, "after_restore_stage")
                before_restore = _create_backup_locked(
                    root_path,
                    "before_restore",
                    destructive=True,
                    phase_hook=phase_hook,
                )
                _emit_phase(phase_hook, "before_restore_switch")
                _switch_state(
                    root_path,
                    operation,
                    operation_kind="restore",
                    post_verify=lambda: _verify_live_against_manifest(root_path, manifest),
                    phase_hook=phase_hook,
                    metadata={
                        "source_backup_id": manifest["backup_id"],
                        "before_restore_backup_id": (
                            before_restore.backup_id if before_restore else None
                        ),
                    },
                )
            except BaseException:
                if _lexists(operation) and not (operation / "journal.json").is_file():
                    _cleanup_private_directory(operation, operation.parent)
                raise
            return {
                "ok": True,
                "code": "state_restored",
                "backup_id": manifest["backup_id"],
                "before_restore_backup_id": (
                    before_restore.backup_id if before_restore else None
                ),
            }


def recover_state(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve(strict=True)
    with _DirectoryLease(root_path):
        recovered = _recover_orphaned_switch_locked(root_path)
        recovered = _recover_sqlite_crash_journals_locked(root_path) or recovered
        with _writer_guards(root_path):
            recovered_state = _recover_incomplete_locked(root_path)
            recovered = _recover_incomplete_backups_locked(root_path) or recovered
        if recovered_state:
            recovered = _recover_sqlite_crash_journals_locked(root_path) or recovered
            with _writer_guards(root_path):
                pass
            recovered = True
    return {
        "ok": True,
        "code": "recovered" if recovered else "nothing_to_recover",
        "recovered": recovered,
    }


def preflight_state(
    root: str | Path,
    *,
    backup: str | Path | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve(strict=True)
    with _inspect_state(root_path):
        if backup is not None:
            manifest = verify_backup(root_path, backup)
            return {
                "ok": True,
                "code": "preflight_ok",
                "backup_id": manifest["backup_id"],
            }
        report = _dry_run_backup_locked(
            root_path,
            "preflight",
            destructive=False,
            phase_hook=None,
        )
        report["code"] = "preflight_ok"
        return report
