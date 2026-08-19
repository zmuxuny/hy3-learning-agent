"""Crash-safe canonical SQLite schema preparation.

Older releases grew the local database with best-effort ``ALTER TABLE`` calls.
That left fresh and upgraded installs with different constraints and indexes.
H1 instead treats a newly built database as the candidate: the source is read
only until a verified backup exists, every legacy row is copied into the one
canonical schema, and a single ``os.replace`` publishes the result.

The module intentionally has no dependency on application settings. Callers
must pass an explicit database path and backup root, which keeps tests and
maintenance tools away from a user's configured database.
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
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, create_engine, insert
from sqlalchemy.engine import URL
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.paths import lexical_absolute
from app.core.time import canonical_utc, parse_legacy_datetime, utc_now
from app.db.database import Base
from app.db.types import UTCDateTime
from app.version import CURRENT_SCHEMA_VERSION as DECLARED_SCHEMA_VERSION


@dataclass(frozen=True)
class MigrationRevision:
    """One immutable schema revision understood by this application build.

    ``checksum`` is the semantic SQLite schema digest after the revision has
    been applied.  Keeping it as a literal is intentional: later model edits
    must add a new revision instead of silently redefining an installed one.
    """

    version: int
    name: str
    checksum: str


# This checksum is finalized by the H1 schema manifest tests.  Any later schema
# change must append a revision and retain this exact row for existing v1 DBs.
MIGRATION_REGISTRY: tuple[MigrationRevision, ...] = (
    MigrationRevision(
        version=1,
        name="h1_canonical_schema",
        checksum="e7130a9013e7bd4754c3318520d9101f18d18b88c11e8ebbe7c965ead4c5e293",
    ),
    MigrationRevision(
        version=2,
        name="h2_transaction_outbox",
        checksum="7f42435d235b1497a358abc4353ff6b52771514a4b252c5ba74acec6e1493de1",
    ),
)
if [revision.version for revision in MIGRATION_REGISTRY] != list(
    range(1, len(MIGRATION_REGISTRY) + 1)
):
    raise RuntimeError("migration registry versions must be contiguous from 1")
if len({revision.name for revision in MIGRATION_REGISTRY}) != len(MIGRATION_REGISTRY):
    raise RuntimeError("migration registry names must be unique")
if any(
    len(revision.checksum) != 64
    or any(character not in "0123456789abcdef" for character in revision.checksum)
    for revision in MIGRATION_REGISTRY
):
    raise RuntimeError("migration registry checksums must be lowercase SHA-256 values")
_REVISION_BY_VERSION = {revision.version: revision for revision in MIGRATION_REGISTRY}
CURRENT_REVISION = MIGRATION_REGISTRY[-1]
CURRENT_SCHEMA_VERSION = CURRENT_REVISION.version
if CURRENT_SCHEMA_VERSION != DECLARED_SCHEMA_VERSION:
    raise RuntimeError("schema version constant and migration registry disagree")
CURRENT_MIGRATION_NAME = CURRENT_REVISION.name
CANONICAL_SCHEMA_CHECKSUM = CURRENT_REVISION.checksum
EMPTY_SCHEMA_CHECKSUM = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
MIGRATION_BACKUP_FORMAT = "learning-agent-migration-backup"
MIGRATION_BACKUP_VERSION = 1
MIGRATION_BACKUP_PURPOSES = {"pre_migration", "pre_backfill", "before_restore"}
MIGRATION_BACKUP_ID_PATTERN = re.compile(
    r"^(pre-migration|pre-backfill|before-restore)-"
    r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{32}$"
)
IGNORED_LEGACY_TABLES = {"_write_probe"}
FaultInjector = Callable[[str], None]


@dataclass(frozen=True)
class LegacySchemaRevision:
    """One exact unversioned schema emitted by a supported legacy release."""

    kind: str
    checksum: str


# These semantic digests were generated from immutable, public legacy shapes:
# the v1.1.1 SQL fixture, that fixture after the shipped partial-M13 Evidence
# DDL, and all three pre-H1 schema creation paths: metadata-only, a fresh
# ``create_schema`` run, and the additive upgrade from v1.1.1.  They must never
# be derived from today's ORM metadata: doing so would let a future model edit
# silently redefine which unknown databases are accepted.
LEGACY_SCHEMA_REGISTRY: tuple[LegacySchemaRevision, ...] = (
    LegacySchemaRevision(
        kind="v1_1_1",
        checksum="6182f8a7ec5e866286e225cd541cfcbacbde4c717bf1d867812b63f0885ad57b",
    ),
    LegacySchemaRevision(
        kind="partial_v2",
        checksum="bc929623025bab47badecb07572e798d2fd4ba89c898638a273581d897826c98",
    ),
    LegacySchemaRevision(
        kind="pre_h1_current",
        checksum="fb12a30b50595c4e61a06424eb27d4ad586a047a1f9d917fb0bcf7ecb3236b3c",
    ),
    LegacySchemaRevision(
        kind="pre_h1_current",
        checksum="21150d5fff8e32807d38e1902f2257cf41154ab47a44bc497a2436002cd9f04e",
    ),
    LegacySchemaRevision(
        kind="pre_h1_current",
        checksum="40063b72061ee0a42dd4f7fccdd4e8d9e64a346ca70452041b579e05d2805d89",
    ),
)
_LEGACY_SCHEMA_BY_CHECKSUM = {
    revision.checksum: revision for revision in LEGACY_SCHEMA_REGISTRY
}
if len(_LEGACY_SCHEMA_BY_CHECKSUM) != len(LEGACY_SCHEMA_REGISTRY):
    raise RuntimeError("legacy schema registry checksums must be unique")
if any(
    len(revision.checksum) != 64
    or any(character not in "0123456789abcdef" for character in revision.checksum)
    for revision in LEGACY_SCHEMA_REGISTRY
):
    raise RuntimeError("legacy schema checksums must be lowercase SHA-256 values")


class MigrationError(RuntimeError):
    """A fail-closed migration error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, recovery_backup: Path | None = None):
        super().__init__(message)
        self.code = code
        self.recovery_backup = recovery_backup


@dataclass(frozen=True)
class MigrationReport:
    database_path: str
    source_kind: str
    source_schema_checksum: str
    target_schema_checksum: str
    version: int
    applied: bool
    backup_path: str | None
    legacy_naive_timestamps: int
    offset_timestamps_normalized: int


@dataclass(frozen=True)
class _WriterGuardState:
    """SQLite lock plus the pathname identity observed when it was acquired."""

    connection: sqlite3.Connection | None
    path_identity: tuple[int, int] | None
    data_version: int | None


@dataclass(frozen=True)
class _PinnedMigrationBackupState:
    """Open descriptors and immutable observations for one backup tree."""

    directory_fd: int
    payload_directory_fd: int
    manifest_fd: int
    complete_fd: int
    payload_fd: int
    directory_identity: tuple[int, int]
    payload_directory_identity: tuple[int, int]
    manifest_identity: tuple[int, int]
    complete_identity: tuple[int, int]
    payload_identity: tuple[int, int]
    manifest_bytes: bytes
    complete_bytes: bytes
    payload_size: int

    @property
    def payload_path(self) -> Path:
        return Path("/proc/self/fd") / str(self.payload_fd)


def _register_models() -> None:
    # Importing the package registers every mapped table on Base.metadata. It
    # is deliberately explicit so standalone create_schema callers work.
    import app.models  # noqa: F401


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_plain_int(value: Any) -> bool:
    """Return true only for JSON integer values, never booleans.

    ``bool`` subclasses ``int`` in Python.  Backup metadata is an external
    trust boundary, so accepting ``true`` as schema version 1 (or ``false``
    as a zero violation count) would make the manifest contract ambiguous.
    """

    return type(value) is int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_database_identity(identity: str | None, path: Path) -> str:
    """Return a portable, canonical identity for one managed database slot.

    Callers that know a state root pass its repository-relative POSIX path,
    such as ``data/learning_companion.db``.  Standalone callers default to the
    basename, which keeps low-level temporary-database APIs usable without
    binding a backup to an absolute checkout path.
    """

    value = path.name if identity is None else identity
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise MigrationError(
            "invalid_database_identity",
            "database identity must be a non-empty canonical relative POSIX path",
        )
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or parsed.name != path.name
    ):
        raise MigrationError(
            "invalid_database_identity",
            "database identity must name this database through a canonical relative path",
        )
    return value


def _reject_symlinked_directory_ancestors(path: Path, *, label: str) -> None:
    """Reject static parent-symlink escapes while allowing a pinned /proc fd."""

    absolute = lexical_absolute(path)
    parts = absolute.parts
    pinned = (
        len(parts) >= 5
        and parts[:4] == ("/", "proc", "self", "fd")
        and parts[4].isdigit()
    )
    if pinned:
        cursor = Path(*parts[:5])
        try:
            if not cursor.is_dir():
                raise MigrationError("unsafe_backup_path", f"{label} pinned root is invalid")
        except OSError as exc:
            raise MigrationError("unsafe_backup_path", f"{label} pinned root is invalid") from exc
        remaining = parts[5:]
    else:
        cursor = Path(absolute.anchor)
        remaining = parts[1:]
    for component in remaining:
        cursor = cursor / component
        if not cursor.exists() and not cursor.is_symlink():
            break
        try:
            result = cursor.lstat()
        except OSError as exc:
            raise MigrationError("unsafe_backup_path", f"could not inspect {label}") from exc
        if cursor.is_symlink() or not cursor.is_dir():
            raise MigrationError(
                "unsafe_backup_path",
                f"{label} contains a symbolic link or non-directory component",
            )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_file_secure(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    """Create a private destination before copying potentially personal data."""

    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    os.close(descriptor)
    try:
        shutil.copyfile(source, destination)
        os.chmod(destination, mode)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_private_lock(path: Path) -> int:
    """Open one regular, single-link lock file without following symlinks."""

    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or path.is_symlink()
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("lock path is not a stable single-link regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except OSError as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        raise MigrationError(
            "unsafe_lock_path",
            "migration lock path is a symlink, hard link, or non-regular file",
        ) from exc


def _ensure_private_directory(path: Path) -> None:
    """Create missing directory components durably and keep the leaf private."""

    missing: list[Path] = []
    cursor = lexical_absolute(path)
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise MigrationError("unsafe_backup_path", "backup directory ancestry is unsafe")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise MigrationError(
                    "unsafe_backup_path",
                    "backup directory changed to an unsafe path",
                )
        os.chmod(directory, 0o700)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)
    os.chmod(path, 0o700)
    _fsync_directory(path)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_uri(path: Path, *, mode: str) -> str:
    """Build an encoded SQLite URI for an absolute filesystem path."""

    if mode not in {"ro", "rw"}:
        raise ValueError("SQLite URI mode must be ro or rw")
    return f"{lexical_absolute(path).as_uri()}?mode={mode}"


def _normalize_sql(sql: str) -> str:
    """Collapse layout whitespace without changing quoted SQL literals."""

    normalized: list[str] = []
    quote_end: str | None = None
    pending_space = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote_end is not None:
            normalized.append(character)
            if character == quote_end:
                if quote_end != "]" and index + 1 < len(sql) and sql[index + 1] == quote_end:
                    normalized.append(sql[index + 1])
                    index += 1
                else:
                    quote_end = None
            index += 1
            continue
        if character.isspace():
            pending_space = bool(normalized)
            index += 1
            continue
        if pending_space:
            normalized.append(" ")
            pending_space = False
        normalized.append(character)
        if character in {"'", '"', "`"}:
            quote_end = character
        elif character == "[":
            quote_end = "]"
        index += 1
    return "".join(normalized).strip()


def _schema_payload(
    connection: sqlite3.Connection,
    *,
    ignored_tables: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Return a stable semantic SQLite schema representation."""

    objects = connection.execute(
        """
        SELECT type, name, tbl_name, coalesce(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    payload: list[dict[str, Any]] = []
    for object_type, name, table_name, sql in objects:
        if name in ignored_tables or table_name in ignored_tables:
            continue
        item: dict[str, Any] = {
            "type": object_type,
            "name": name,
            "table": table_name,
            "sql": _normalize_sql(sql),
        }
        if object_type == "table":
            quoted = _quote(name)
            item["columns"] = [
                list(row) for row in connection.execute(f"PRAGMA table_xinfo({quoted})")
            ]
            item["foreign_keys"] = sorted(
                [list(row) for row in connection.execute(f"PRAGMA foreign_key_list({quoted})")]
            )
            indexes: list[dict[str, Any]] = []
            for row in connection.execute(f"PRAGMA index_list({quoted})"):
                index_name = row[1]
                indexes.append({
                    "name": index_name,
                    "unique": row[2],
                    "origin": row[3],
                    "partial": row[4],
                    "columns": [
                        list(index_row)
                        for index_row in connection.execute(
                            f"PRAGMA index_xinfo({_quote(index_name)})"
                        )
                    ],
                })
            item["indexes"] = sorted(indexes, key=lambda value: value["name"])
        payload.append(item)
    return payload


def schema_checksum(path: Path) -> str:
    try:
        connection = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
    except sqlite3.Error as exc:
        raise MigrationError("database_open_failed", "could not open SQLite database read-only") from exc
    try:
        return hashlib.sha256(_canonical_json(_schema_payload(connection))).hexdigest()
    finally:
        connection.close()


def _legacy_schema_checksum(path: Path) -> str:
    """Fingerprint a legacy schema while ignoring the obsolete write probe."""

    try:
        connection = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
    except sqlite3.Error as exc:
        raise MigrationError("database_open_failed", "could not open SQLite database read-only") from exc
    try:
        payload = _schema_payload(
            connection,
            ignored_tables=frozenset(IGNORED_LEGACY_TABLES),
        )
        return hashlib.sha256(_canonical_json(payload)).hexdigest()
    finally:
        connection.close()


def _validate_legacy_write_probe(path: Path) -> None:
    """Accept only the exact, empty probe table emitted by old releases.

    The old startup check left ``_write_probe`` behind, so that one harmless
    shape is excluded from legacy schema fingerprints.  Treating every object
    with that name as ignorable would let user data, indexes, or triggers hide
    from classification and then disappear during canonical copy.
    """

    try:
        connection = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
        try:
            objects = connection.execute(
                "SELECT type, name, tbl_name, coalesce(sql, '') "
                "FROM sqlite_master "
                "WHERE name = ? OR tbl_name = ? ORDER BY type, name",
                ("_write_probe", "_write_probe"),
            ).fetchall()
            if not objects:
                return
            if objects != [
                (
                    "table",
                    "_write_probe",
                    "_write_probe",
                    "CREATE TABLE _write_probe (id INTEGER)",
                )
            ]:
                raise MigrationError(
                    "unknown_legacy_schema",
                    "legacy write probe has unsupported schema objects",
                )
            columns = connection.execute(
                f"PRAGMA table_xinfo({_quote('_write_probe')})"
            ).fetchall()
            if columns != [(0, "id", "INTEGER", 0, None, 0, 0)]:
                raise MigrationError(
                    "unknown_legacy_schema",
                    "legacy write probe does not match its frozen shape",
                )
            if connection.execute(
                f"SELECT count(*) FROM {_quote('_write_probe')}"
            ).fetchone() != (0,):
                raise MigrationError(
                    "unknown_legacy_schema",
                    "legacy write probe contains data and cannot be discarded",
                )
        finally:
            connection.close()
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationError(
            "unknown_legacy_schema",
            "legacy write probe could not be verified safely",
        ) from exc


def _health(path: Path) -> dict[str, Any]:
    try:
        # SQLite's read-only URI path can incorrectly report ``integrity_check
        # = ok`` for rows that violate CHECK constraints.  A normal connection
        # placed in query-only mode before the first read performs the complete
        # verifier without permitting application writes.
        connection = sqlite3.connect(_sqlite_uri(path, mode="rw"), uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            return {
                "integrity": integrity,
                "foreign_key_violation_count": len(foreign_keys),
                "user_version": user_version,
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise MigrationError("sqlite_integrity_failed", "SQLite health check could not complete") from exc


def _reject_future_user_version(health: dict[str, Any]) -> None:
    if int(health["user_version"]) > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            "future_schema_version",
            "database was created by a newer application version",
        )


def verify_sqlite_database(
    path: Path,
    *,
    expected_schema_checksum: str | None = None,
) -> dict[str, Any]:
    health = _health(path)
    if health["integrity"] != ["ok"]:
        raise MigrationError("sqlite_integrity_failed", "SQLite integrity_check did not return ok")
    if health["foreign_key_violation_count"]:
        raise MigrationError("foreign_key_failed", "SQLite foreign_key_check found violations")
    _reject_future_user_version(health)
    actual_checksum = schema_checksum(path)
    if expected_schema_checksum is not None and actual_checksum != expected_schema_checksum:
        raise MigrationError("schema_verification_failed", "database schema differs from the canonical schema")
    if expected_schema_checksum is not None and health["user_version"] != CURRENT_SCHEMA_VERSION:
        raise MigrationError("schema_version_mismatch", "PRAGMA user_version disagrees with migration history")
    return {**health, "schema_checksum": actual_checksum}


@contextmanager
def _migration_lease(database_path: Path) -> Iterator[None]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(f".{database_path.name}.migration.lock")
    descriptor = _open_private_lock(lock_path)
    try:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise MigrationError(
                        "migration_busy",
                        "another migration coordinator still owns this database",
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _backup_root_lease(backup_root: Path) -> Iterator[None]:
    """Serialize publication and stale-stage recovery in one backup root."""

    _ensure_private_directory(backup_root)
    lock_path = backup_root / ".migration-backup.lock"
    descriptor = _open_private_lock(lock_path)
    try:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise MigrationError(
                        "backup_busy",
                        "another process is publishing into this backup root",
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _writer_guard(database_path: Path) -> Iterator[_WriterGuardState]:
    if not database_path.exists():
        yield _WriterGuardState(
            connection=None,
            path_identity=None,
            data_version=None,
        )
        return
    try:
        initial_stat = database_path.lstat()
    except OSError as exc:
        raise MigrationError(
            "source_changed_during_migration",
            "SQLite source changed before its writer guard was acquired",
        ) from exc
    initial_identity = (initial_stat.st_dev, initial_stat.st_ino)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _sqlite_uri(database_path, mode="rw"),
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise MigrationError(
                "active_sqlite_writer",
                "database has an active writer; stop the other process and retry",
            ) from exc
        raise MigrationError("database_open_failed", "could not acquire SQLite migration guard") from exc
    try:
        try:
            guarded_stat = database_path.lstat()
        except OSError as exc:
            raise MigrationError(
                "source_changed_during_migration",
                "SQLite source changed while its writer guard was acquired",
            ) from exc
        guarded_identity = (guarded_stat.st_dev, guarded_stat.st_ino)
        if guarded_identity != initial_identity:
            raise MigrationError(
                "source_changed_during_migration",
                "SQLite source inode changed while its writer guard was acquired",
            )
        yield _WriterGuardState(
            connection=connection,
            path_identity=guarded_identity,
            data_version=int(connection.execute("PRAGMA data_version").fetchone()[0]),
        )
    finally:
        connection.rollback()
        connection.close()


def _assert_writer_guard_path_unchanged(
    guard: _WriterGuardState,
    database_path: Path,
) -> None:
    """Ensure the locked inode is still the file named by ``database_path``."""

    if guard.connection is None:
        if database_path.exists() or database_path.is_symlink():
            raise MigrationError(
                "source_changed_during_migration",
                "an absent SQLite target appeared while the operation was preparing",
            )
        return
    try:
        current_stat = database_path.lstat()
    except OSError as exc:
        raise MigrationError(
            "source_changed_during_migration",
            "SQLite source disappeared while the operation was preparing",
        ) from exc
    if (
        database_path.is_symlink()
        or not database_path.is_file()
        or current_stat.st_nlink != 1
        or (current_stat.st_dev, current_stat.st_ino) != guard.path_identity
    ):
        raise MigrationError(
            "source_changed_during_migration",
            "SQLite source inode changed while the operation was preparing",
        )


def _regular_file_identity(path: Path) -> tuple[int, int] | None:
    """Return a safe regular-file identity without following symbolic links."""

    try:
        result = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
        return None
    return result.st_dev, result.st_ino


def _private_directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        result = path.lstat()
    except OSError:
        return None
    if (
        path.is_symlink()
        or not stat.S_ISDIR(result.st_mode)
        or result.st_mode & 0o077
    ):
        return None
    return result.st_dev, result.st_ino


def _assert_private_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    if _private_directory_identity(path) != expected_identity:
        raise MigrationError(
            "recovery_required",
            f"{label} changed ownership while the backup was being published",
        )


def _assert_published_path_unchanged(
    database_path: Path,
    published_identity: tuple[int, int],
) -> None:
    if _regular_file_identity(database_path) != published_identity:
        raise MigrationError(
            "source_changed_during_migration",
            "the published SQLite path was replaced by another process; external state was preserved",
        )


@contextmanager
def _publication_guard(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Hold a SQLite writer lock on a file before and after it is renamed.

    SQLite locks follow the inode, not its pathname.  Opening the complete
    candidate before ``os.replace`` therefore closes the window in which a
    writer could modify the newly published inode while the source guard still
    refers to the old one.
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, timeout=0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise MigrationError(
            "publication_guard_failed",
            "could not lock the prepared SQLite image for publication",
        ) from exc
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def _reserve_absent_database_path(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Atomically reserve an absent target until a candidate is published."""

    descriptor: int | None = None
    connection: sqlite3.Connection | None = None
    created = False
    try:
        descriptor = os.open(
            database_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR,
            0o600,
        )
        created = True
        os.close(descriptor)
        descriptor = None
        connection = sqlite3.connect(database_path, timeout=0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
    except FileExistsError as exc:
        raise MigrationError(
            "source_changed_during_migration",
            "an absent SQLite target was created while the operation was preparing",
        ) from exc
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
        if created:
            database_path.unlink(missing_ok=True)
        raise MigrationError(
            "publication_guard_failed",
            "could not reserve the absent SQLite target for publication",
        ) from exc
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _snapshot_database(source: Path | None, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source is None or not source.exists():
        sqlite3.connect(destination).close()
        os.chmod(destination, 0o600)
        return
    try:
        source_connection = sqlite3.connect(
            _sqlite_uri(source, mode="ro"),
            uri=True,
        )
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
            # SQLite backup copies the source page header, including WAL mode.
            # Canonical backups/candidates must be standalone single files;
            # otherwise a later read-only verification can create -wal/-shm
            # beside the published payload.
            mode = destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise MigrationError(
                    "backup_failed",
                    "could not canonicalize SQLite snapshot journal mode",
                )
        finally:
            destination_connection.close()
            source_connection.close()
        os.chmod(destination, 0o600)
    except sqlite3.Error as exc:
        raise MigrationError("backup_failed", "could not create a consistent SQLite snapshot") from exc


_SQLITE_STATE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _validate_sqlite_path_set(database_path: Path) -> None:
    """Reject unsafe sidecars and sidecars orphaned from their main file."""

    if database_path.exists() or database_path.is_symlink():
        try:
            main_stat = database_path.lstat()
        except OSError as exc:
            raise MigrationError(
                "unsafe_database_path",
                "could not inspect SQLite database path",
            ) from exc
        if (
            database_path.is_symlink()
            or not database_path.is_file()
            or main_stat.st_nlink != 1
        ):
            raise MigrationError(
                "unsafe_database_path",
                "SQLite database is a symlink, hard link, or non-regular file",
            )
    sidecars_present = False
    for suffix in _SQLITE_STATE_SUFFIXES[1:]:
        sidecar = database_path.with_name(database_path.name + suffix)
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        sidecars_present = True
        try:
            stat_result = sidecar.lstat()
        except OSError as exc:
            raise MigrationError(
                "unsafe_database_path",
                "could not inspect SQLite sidecar",
            ) from exc
        if sidecar.is_symlink() or not sidecar.is_file() or stat_result.st_nlink != 1:
            raise MigrationError(
                "unsafe_database_path",
                "SQLite sidecar is a symlink, hard link, or non-regular file",
            )
    if sidecars_present and not database_path.exists():
        raise MigrationError(
            "recovery_required",
            "SQLite sidecars exist without their main database file",
        )


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in _SQLITE_STATE_SUFFIXES[1:]:
        sidecar = database_path.with_name(database_path.name + suffix)
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        if sidecar.is_symlink() or not sidecar.is_file():
            raise MigrationError(
                "unsafe_database_path",
                "SQLite sidecar changed to an unsafe path during publication",
            )
        sidecar.unlink()


def _canonicalize_live_journal(
    guard: _WriterGuardState,
    database_path: Path,
    *,
    fault_injector: FaultInjector | None = None,
) -> None:
    """Make a backed-up source a standalone DELETE-journal database.

    This runs only after a verified backup exists.  It closes the fatal crash
    window where a newly published main file could otherwise be interpreted
    together with the legacy source WAL.  A reader that prevents the journal
    transition is reported explicitly before the candidate is published.
    """

    connection = guard.connection
    if connection is None:
        return
    _assert_writer_guard_path_unchanged(guard, database_path)
    before_version = guard.data_version
    connection.rollback()
    try:
        _call_fault(fault_injector, "after_writer_guard_release")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise MigrationError(
                "active_sqlite_reader",
                "an active SQLite reader prevents a WAL-safe migration",
            )
        mode_row = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode_row is None or str(mode_row[0]).lower() != "delete":
            raise MigrationError(
                "active_sqlite_reader",
                "SQLite journal mode could not be made standalone before publication",
            )
        connection.execute("BEGIN IMMEDIATE")
        after_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
        try:
            after_stat = database_path.stat()
        except OSError as exc:
            raise MigrationError(
                "source_changed_during_migration",
                "SQLite source disappeared during journal canonicalization",
            ) from exc
        if (
            after_version != before_version
            or (after_stat.st_dev, after_stat.st_ino)
            != guard.path_identity
        ):
            raise MigrationError(
                "source_changed_during_migration",
                "SQLite source changed after its backup; the concurrent commit was preserved",
            )
        _remove_sqlite_sidecars(database_path)
        _fsync_file(database_path)
        _fsync_directory(database_path.parent)
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationError(
            "active_sqlite_reader",
            "SQLite journal state could not be made safe for publication",
        ) from exc
    except OSError as exc:
        raise MigrationError(
            "journal_canonicalization_failed",
            "SQLite journal state was backed up but could not be durably canonicalized",
        ) from exc
    except Exception as exc:
        raise MigrationError(
            "journal_canonicalization_failed",
            "SQLite journal canonicalization was interrupted by a recoverable failure",
        ) from exc


def _select_trusted_snapshot(
    expected_sha256: str,
    *candidates: Path | None,
) -> Path:
    """Choose a still-valid copy of one previously verified SQLite state."""

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if _sha256_file(candidate) != expected_sha256:
                continue
            verify_sqlite_database(candidate)
        except (MigrationError, OSError):
            continue
        return candidate
    raise MigrationError(
        "recovery_required",
        "no trusted copy of the pre-publication SQLite state remains",
    )


def _restore_snapshot_atomically(
    database_path: Path,
    snapshot: Path | None,
    *,
    expected_current_identity: tuple[int, int] | None,
    expected_snapshot_sha256: str | None,
    staging_directory: Path,
    mode: int = 0o600,
    fault_injector: FaultInjector | None = None,
) -> None:
    """Publish either a standalone old snapshot or the prior absence.

    Sidecars are removed before the single main-file commit.  A crash during
    this rollback therefore leaves either the already-verified new database or
    the complete standalone old snapshot, never a main/WAL mixture.
    """

    def assert_rollback_target_owned() -> None:
        if _regular_file_identity(database_path) != expected_current_identity:
            raise MigrationError(
                "recovery_required",
                "rollback target changed ownership; external state was preserved",
            )

    assert_rollback_target_owned()
    if snapshot is None:
        if expected_snapshot_sha256 is not None:
            raise MigrationError(
                "recovery_required",
                "rollback absence unexpectedly carried a snapshot digest",
            )
        _call_fault(fault_injector, "before_rollback_publish")
        assert_rollback_target_owned()
        _remove_sqlite_sidecars(database_path)
        assert_rollback_target_owned()
        if database_path.exists():
            database_path.unlink()
        _call_fault(fault_injector, "after_rollback_publish")
        if database_path.exists() or database_path.is_symlink():
            raise MigrationError(
                "recovery_required",
                "rollback target reappeared after restoring the prior absence",
            )
        _fsync_directory(database_path.parent)
        return
    if (
        not isinstance(expected_snapshot_sha256, str)
        or len(expected_snapshot_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_snapshot_sha256
        )
        or _sha256_file(snapshot) != expected_snapshot_sha256
    ):
        raise MigrationError(
            "recovery_required",
            "rollback source no longer matches the verified recovery backup",
        )
    staging_identity = _private_directory_identity(staging_directory)
    if staging_identity is None:
        raise MigrationError(
            "recovery_required",
            "rollback staging directory is not a private owned directory",
        )
    staged = staging_directory / (
        f".{database_path.name}.rollback-publish-{uuid4().hex}"
    )
    try:
        _assert_private_directory_identity(
            staging_directory,
            staging_identity,
            label="rollback staging directory",
        )
        _copy_file_secure(snapshot, staged, mode=mode)
        _fsync_file(staged)
        if _sha256_file(staged) != expected_snapshot_sha256:
            raise MigrationError(
                "recovery_required",
                "rollback staging differs from the verified source snapshot",
            )
        verify_sqlite_database(staged)
        with _publication_guard(staged):
            staged_identity = _regular_file_identity(staged)
            if staged_identity is None:
                raise MigrationError(
                    "recovery_required",
                    "rollback staging became unsafe before publication",
                )
            _call_fault(fault_injector, "before_rollback_publish")
            assert_rollback_target_owned()
            _remove_sqlite_sidecars(database_path)
            assert_rollback_target_owned()
            os.replace(staged, database_path)
            _call_fault(fault_injector, "after_rollback_publish")
            if _regular_file_identity(database_path) != staged_identity:
                raise MigrationError(
                    "recovery_required",
                    "rollback target was replaced before it could be verified",
                )
            _fsync_directory(database_path.parent)
            if _sha256_file(database_path) != expected_snapshot_sha256:
                raise MigrationError(
                    "recovery_required",
                    "published rollback differs from the verified source snapshot",
                )
            verify_sqlite_database(database_path)
    finally:
        staged.unlink(missing_ok=True)


def _cleanup_stale_work_directories(database_path: Path) -> None:
    """Remove only private work directories owned by this database lease."""

    removed = False
    prefixes = (
        f".{database_path.name}.migration-work-",
        f".{database_path.name}.restore-work-",
        f".{database_path.name}.backup-work-",
    )
    for path in database_path.parent.iterdir():
        if not any(path.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or not path.is_dir() or stat_result.st_nlink < 1:
            raise MigrationError(
                "recovery_required",
                "a stale migration work path is unsafe to clean automatically",
            )
        if stat_result.st_mode & 0o077:
            raise MigrationError(
                "recovery_required",
                "a stale migration work directory has unsafe permissions",
            )
        shutil.rmtree(path)
        removed = True
    if removed:
        _fsync_directory(database_path.parent)


def _source_tables(path: Path) -> set[str]:
    connection = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def _classify_source(
    path: Path,
    *,
    source_history: list[dict[str, Any]] | None = None,
) -> str:
    _validate_legacy_write_probe(path)
    all_source_tables = _source_tables(path)
    source_tables = all_source_tables.difference(IGNORED_LEGACY_TABLES)
    source_checksum = _legacy_schema_checksum(path)
    if source_checksum == EMPTY_SCHEMA_CHECKSUM:
        return "empty"
    if not source_tables:
        raise MigrationError(
            "unknown_legacy_schema",
            "database contains unsupported schema objects despite having no tables",
        )
    has_history_table = "schema_migrations" in source_tables
    if has_history_table and not source_history:
        raise MigrationError(
            "migration_history_invalid",
            "schema_migrations exists but contains no applied revision",
        )
    if source_history and not has_history_table:
        raise MigrationError(
            "migration_history_invalid",
            "migration history rows exist without schema_migrations",
        )
    if has_history_table:
        # A versioned database is recognized by its frozen revision checksum,
        # not by today's ORM columns.  That permits a later revision to remove
        # or rename a column without treating the older, already-known schema
        # as an unknown legacy database.
        latest_version = int(source_history[-1]["version"])
        if latest_version > CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                "future_schema_version",
                "database was created by a newer application version",
            )
        revision = _REVISION_BY_VERSION.get(latest_version)
        if revision is None:
            raise MigrationError(
                "migration_history_invalid",
                "database refers to an unknown migration revision",
            )
        if schema_checksum(path) != revision.checksum:
            raise MigrationError(
                "migration_checksum_mismatch",
                "versioned database schema does not match its frozen revision",
            )
        return "versioned"

    revision = _LEGACY_SCHEMA_BY_CHECKSUM.get(source_checksum)
    if revision is None:
        raise MigrationError(
            "unknown_legacy_schema",
            "database schema does not match a frozen supported legacy release",
        )
    return revision.kind


def _call_fault(fault_injector: FaultInjector | None, phase: str) -> None:
    if fault_injector is not None:
        fault_injector(phase)


def _create_tables(connection) -> None:
    for table in Base.metadata.sorted_tables:
        connection.execute(CreateTable(table))


def _create_indexes(connection) -> None:
    indexes = sorted(
        (index for table in Base.metadata.sorted_tables for index in table.indexes),
        key=lambda index: index.name or "",
    )
    for index in indexes:
        connection.execute(CreateIndex(index))


def _legacy_columns(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        row[1]
        for row in connection.execute(f"PRAGMA table_xinfo({_quote(table_name)})")
        if row[6] == 0
    )


def _normalize_datetime(value: Any, counters: dict[str, int]) -> Any:
    if value is None:
        return None
    try:
        parsed = parse_legacy_datetime(value)
    except (TypeError, ValueError) as exc:
        raise MigrationError(
            "invalid_legacy_datetime",
            "legacy database contains an invalid timestamp",
        ) from exc
    if isinstance(value, str):
        raw = value.strip()
        if not (raw.endswith("Z") or "+" in raw[10:] or "-" in raw[10:]):
            counters["legacy_naive_timestamps"] += 1
        elif parsed.isoformat() != raw:
            counters["offset_timestamps_normalized"] += 1
    return parsed


def _normalize_json(value: Any, *, table_name: str, column_name: str) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise MigrationError(
            "invalid_legacy_json",
            f"legacy JSON is invalid at {table_name}.{column_name}",
        ) from exc


def _normalize_row(table, raw: sqlite3.Row, counters: dict[str, int]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    raw_keys = set(raw.keys())
    for column in table.columns:
        if column.name not in raw_keys:
            continue
        value = raw[column.name]
        if table.name == "learning_events" and column.name == "occurred_at" and value is None:
            value = raw["created_at"] if "created_at" in raw_keys else None
        if table.name == "evidence_observations":
            if column.name == "occurred_at" and value is None:
                value = raw["recorded_at"] if "recorded_at" in raw_keys else None
            if column.name == "recorded_at" and value is None:
                value = raw["occurred_at"] if "occurred_at" in raw_keys else None
        if value is None and not column.nullable:
            if column.default is not None or column.server_default is not None:
                continue
            raise MigrationError(
                "legacy_null_constraint",
                f"legacy NULL cannot satisfy {table.name}.{column.name}",
            )
        if isinstance(column.type, UTCDateTime):
            try:
                value = _normalize_datetime(value, counters)
            except (TypeError, ValueError) as exc:
                raise MigrationError(
                    "invalid_legacy_datetime",
                    f"legacy datetime is invalid at {table.name}.{column.name}",
                ) from exc
        elif isinstance(column.type, JSON):
            value = _normalize_json(value, table_name=table.name, column_name=column.name)
        values[column.name] = value
    return values


def _copy_legacy_rows(source_path: Path, target_connection, counters: dict[str, int]) -> None:
    source = sqlite3.connect(_sqlite_uri(source_path, mode="ro"), uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_tables = _source_tables(source_path).difference(IGNORED_LEGACY_TABLES)
        for table in Base.metadata.sorted_tables:
            if table.name not in source_tables or table.name == "schema_migrations":
                continue
            columns = _legacy_columns(source, table.name)
            selected = ", ".join(_quote(column) for column in columns)
            rows = source.execute(f"SELECT {selected} FROM {_quote(table.name)}").fetchall()
            normalized = [_normalize_row(table, row, counters) for row in rows]
            # SQLAlchemy executemany derives its bound columns from the first
            # mapping.  Legacy rows can legitimately omit different defaulted
            # columns (for example one NULL followed by one explicit value),
            # so group equal key sets instead of letting the first row erase a
            # later value.
            grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for values in normalized:
                grouped.setdefault(tuple(sorted(values)), []).append(values)
            for values in grouped.values():
                target_connection.execute(insert(table), values)
            actual_count = target_connection.exec_driver_sql(
                f"SELECT count(*) FROM {_quote(table.name)}"
            ).scalar_one()
            if actual_count != len(rows):
                raise MigrationError(
                    "copy_verification_failed",
                    f"row count changed while rebuilding {table.name}",
                )
            primary_keys = [column.name for column in table.primary_key.columns]
            if primary_keys and rows:
                expected_keys = sorted(tuple(row[key] for key in primary_keys) for row in rows)
                key_sql = ", ".join(_quote(key) for key in primary_keys)
                actual_keys = sorted(
                    tuple(row)
                    for row in target_connection.exec_driver_sql(
                        f"SELECT {key_sql} FROM {_quote(table.name)}"
                    ).all()
                )
                if actual_keys != expected_keys:
                    raise MigrationError(
                        "copy_verification_failed",
                        f"primary-key set changed while rebuilding {table.name}",
                    )
    finally:
        source.close()


def _apply_legacy_backfills(connection) -> None:
    # Every statement operates only on the unpublished candidate database.
    connection.exec_driver_sql(
        """
        UPDATE context_snapshots SET run_id = NULL
        WHERE run_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM agent_runs WHERE agent_runs.id = context_snapshots.run_id)
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO session_plan_links
            (owner_id, session_id, plan_id, relation_type, source_run_id, created_at)
        SELECT sessions.owner_id, sessions.id, sessions.plan_id, 'focused', NULL, sessions.created_at
        FROM sessions
        WHERE sessions.plan_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM session_plan_links AS links
              WHERE links.session_id = sessions.id
                AND links.plan_id = sessions.plan_id
                AND links.relation_type = 'focused'
          )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO session_plan_links
            (owner_id, session_id, plan_id, relation_type, source_run_id, created_at)
        SELECT runs.owner_id, runs.session_id,
               CAST(json_extract(events.payload, '$.result.data.plan_id') AS INTEGER),
               'created', runs.id, events.created_at
        FROM run_events AS events
        JOIN agent_runs AS runs ON runs.id = events.run_id
        WHERE runs.session_id IS NOT NULL
          AND events.event_type = 'tool.completed'
          AND json_extract(events.payload, '$.name') = 'plan_create'
          AND json_extract(events.payload, '$.result.ok') = 1
          AND json_extract(events.payload, '$.result.data.plan_id') IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM session_plan_links AS links
              WHERE links.session_id = runs.session_id
                AND links.plan_id = CAST(json_extract(events.payload, '$.result.data.plan_id') AS INTEGER)
                AND links.relation_type = 'created'
          )
        """
    )
    connection.exec_driver_sql(
        """
        UPDATE notifications SET session_id = (
            SELECT agent_runs.session_id FROM agent_runs WHERE agent_runs.id = notifications.run_id
        ) WHERE session_id IS NULL AND run_id IS NOT NULL
        """
    )
    # H1 invocation rows did not retain validated canonical arguments.  A
    # running row can therefore represent an interrupted domain or external
    # write whose outcome is unknowable.  Preserve the absent digest and fence
    # that row for explicit reconciliation; never synthesize request identity
    # from the legacy raw-argument hash.
    connection.exec_driver_sql(
        """
        UPDATE tool_invocations
        SET status = 'needs_reconciliation'
        WHERE request_digest IS NULL AND status = 'running'
        """
    )
    # H1 delivered email/Web Push inline and had no durable external-effect
    # receipt.  A surviving queued row therefore cannot prove that delivery
    # was never attempted.  Keep in-app rows unchanged, but fence external
    # channels for explicit reconciliation instead of leaving an intent that
    # the H2 outbox worker can neither identify nor safely replay.
    connection.exec_driver_sql(
        """
        UPDATE notifications
        SET status = 'needs_reconciliation'
        WHERE channel IN ('email', 'browser')
          AND status = 'queued'
          AND invocation_id IS NULL
        """
    )


def _build_candidate(
    source_path: Path,
    target_path: Path,
    *,
    source_history: list[dict[str, Any]] | None = None,
    fault_injector: FaultInjector | None,
) -> tuple[str, dict[str, int]]:
    _register_models()
    if target_path.exists():
        target_path.unlink()
    counters = {
        "legacy_naive_timestamps": 0,
        "offset_timestamps_normalized": 0,
    }
    engine = create_engine(
        URL.create("sqlite", database=str(lexical_absolute(target_path)))
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            _call_fault(fault_injector, "before_create")
            _create_tables(connection)
            _call_fault(fault_injector, "after_create")
            _call_fault(fault_injector, "before_copy")
            _copy_legacy_rows(source_path, connection, counters)
            _apply_legacy_backfills(connection)
            _call_fault(fault_injector, "after_copy")
            _call_fault(fault_injector, "before_indexes")
            _create_indexes(connection)
            _call_fault(fault_injector, "after_indexes")
        checksum = schema_checksum(target_path)
        if checksum != CANONICAL_SCHEMA_CHECKSUM:
            raise MigrationError(
                "canonical_manifest_mismatch",
                "current ORM metadata does not match the frozen schema revision",
            )
        with engine.begin() as connection:
            _call_fault(fault_injector, "before_history")
            history = Base.metadata.tables["schema_migrations"]
            existing = {
                int(row["version"]): row for row in (source_history or [])
            }
            applied_now = utc_now()
            for revision in MIGRATION_REGISTRY:
                previous = existing.get(revision.version)
                connection.execute(
                    insert(history).values(
                        version=revision.version,
                        name=revision.name,
                        checksum=revision.checksum,
                        applied_at=(
                            parse_legacy_datetime(previous["applied_at"])
                            if previous is not None
                            else applied_now
                        ),
                        result="applied",
                    )
                )
            connection.exec_driver_sql(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
            _call_fault(fault_injector, "after_history")
    finally:
        engine.dispose()
    os.chmod(target_path, 0o600)
    _fsync_file(target_path)
    verify_sqlite_database(target_path, expected_schema_checksum=checksum)
    return checksum, counters


def _read_history(path: Path) -> list[dict[str, Any]]:
    try:
        connection = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
    except sqlite3.Error as exc:
        raise MigrationError(
            "migration_history_invalid",
            "could not open database migration history",
        ) from exc
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if "schema_migrations" not in tables:
            return []
        try:
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at, result "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.Error as exc:
            raise MigrationError(
                "migration_history_invalid",
                "schema_migrations does not match the frozen history contract",
            ) from exc
        return [
            {
                "version": version,
                "name": name,
                "checksum": checksum,
                "applied_at": applied_at,
                "result": result,
            }
            for version, name, checksum, applied_at, result in rows
        ]
    except sqlite3.Error as exc:
        raise MigrationError(
            "migration_history_invalid",
            "could not inspect database migration history",
        ) from exc
    finally:
        connection.close()


def _read_user_version(path: Path) -> int:
    try:
        connection = sqlite3.connect(_sqlite_uri(path, mode="ro"), uri=True)
    except sqlite3.Error as exc:
        raise MigrationError(
            "schema_version_mismatch",
            "could not read SQLite schema version",
        ) from exc
    try:
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as exc:
            raise MigrationError(
                "schema_version_mismatch",
                "could not read SQLite schema version",
            ) from exc
    finally:
        connection.close()


def _validate_history(path: Path, history: list[dict[str, Any]]) -> int:
    """Validate every installed revision against the immutable registry."""

    if not history:
        raise MigrationError(
            "migration_history_invalid",
            "schema_migrations exists but contains no applied revision",
        )
    versions: list[int] = []
    required_keys = {"version", "name", "checksum", "applied_at", "result"}
    for row in history:
        if not isinstance(row, dict) or set(row) != required_keys:
            raise MigrationError(
                "migration_history_invalid",
                "migration history row shape is invalid",
            )
        version = row["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise MigrationError(
                "migration_history_invalid",
                "migration history version must be a positive integer",
            )
        if not all(
            isinstance(row[field], str)
            for field in ("name", "checksum", "result")
        ) or not isinstance(row["applied_at"], str):
            raise MigrationError(
                "migration_history_invalid",
                "migration history metadata types are invalid",
            )
        versions.append(version)
    latest_version = max(versions)
    if latest_version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            "future_schema_version",
            "database was created by a newer application version",
        )
    if versions != list(range(1, latest_version + 1)):
        raise MigrationError(
            "migration_history_invalid",
            "migration history versions are not contiguous",
        )
    if _read_user_version(path) != latest_version:
        raise MigrationError(
            "schema_version_mismatch",
            "PRAGMA user_version disagrees with migration history",
        )
    for row in history:
        revision = _REVISION_BY_VERSION.get(row["version"])
        if revision is None:
            raise MigrationError(
                "future_schema_version",
                "database references an unknown schema revision",
            )
        if (
            row["name"] != revision.name
            or row["checksum"] != revision.checksum
            or row["result"] != "applied"
        ):
            raise MigrationError(
                "migration_checksum_mismatch",
                "applied migration history does not match this build",
            )
        try:
            parse_legacy_datetime(row["applied_at"])
        except (TypeError, ValueError) as exc:
            raise MigrationError(
                "migration_history_invalid",
                "migration history contains an invalid applied_at value",
            ) from exc
    return latest_version


def _publish_backup(
    snapshot_path: Path,
    *,
    backup_root: Path,
    source_path: Path,
    source_kind: str,
    source_schema_version: int,
    source_schema_checksum: str,
    application_version: str,
    purpose: str = "pre_migration",
    database_identity: str | None = None,
    fault_injector: FaultInjector | None = None,
) -> Path:
    if not isinstance(purpose, str) or purpose not in MIGRATION_BACKUP_PURPOSES:
        raise MigrationError("invalid_backup_purpose", "unsupported migration backup purpose")
    _reject_symlinked_directory_ancestors(backup_root, label="migration backup root")
    if backup_root.is_symlink() or (backup_root.exists() and not backup_root.is_dir()):
        raise MigrationError("unsafe_backup_path", "migration backup root is unsafe")
    _ensure_private_directory(backup_root)
    effective_identity = normalize_database_identity(database_identity, source_path)
    identity_token = hashlib.sha256(effective_identity.encode("utf-8")).hexdigest()[:16]
    staging_prefix = f".staging-{source_path.name}-{identity_token}-"
    staging_removed = False
    for stale in backup_root.iterdir():
        if not stale.name.startswith(staging_prefix):
            continue
        try:
            stale_stat = stale.lstat()
        except FileNotFoundError:
            continue
        if (
            stale.is_symlink()
            or not stale.is_dir()
            or stale_stat.st_mode & 0o077
        ):
            raise MigrationError(
                "recovery_required",
                "stale migration-backup staging path is unsafe",
            )
        shutil.rmtree(stale)
        staging_removed = True
    if staging_removed:
        _fsync_directory(backup_root)

    snapshot_sha256 = _sha256_file(snapshot_path)
    for existing in sorted(backup_root.iterdir(), key=lambda path: path.name):
        if existing.name.startswith(".") or not existing.is_dir() or existing.is_symlink():
            continue
        existing_identity = _private_directory_identity(existing)
        if existing_identity is None:
            continue
        try:
            existing_manifest = verify_migration_backup(existing)
        except MigrationError:
            continue
        _assert_private_directory_identity(
            existing,
            existing_identity,
            label="existing migration backup",
        )
        if (
            existing_manifest["purpose"] == purpose
            and existing_manifest["source"]["database_identity"] == effective_identity
            and existing_manifest["source"]["schema_version"] == source_schema_version
            and existing_manifest["source"]["schema_checksum"] == source_schema_checksum
            and existing_manifest["payload_sha256"] == snapshot_sha256
        ):
            return existing

    timestamp = canonical_utc(utc_now()).replace(":", "").replace("-", "")
    backup_id = f"{purpose.replace('_', '-')}-{timestamp}-{uuid4().hex}"
    staging = backup_root / f"{staging_prefix}{backup_id}"
    final = backup_root / backup_id
    payload = staging / "payload"
    published = False
    staging_identity: tuple[int, int] | None = None
    payload_identity: tuple[int, int] | None = None
    published_identity: tuple[int, int] | None = None
    try:
        staging.mkdir(mode=0o700)
        os.chmod(staging, 0o700)
        payload.mkdir(mode=0o700)
        os.chmod(payload, 0o700)
        staging_identity = _private_directory_identity(staging)
        payload_identity = _private_directory_identity(payload)
        if staging_identity is None or payload_identity is None:
            raise MigrationError(
                "unsafe_backup_permissions",
                "migration backup staging directories are not private",
            )
        _call_fault(fault_injector, "after_backup_staging_create")
        _assert_private_directory_identity(
            staging,
            staging_identity,
            label="migration backup staging directory",
        )
        _assert_private_directory_identity(
            payload,
            payload_identity,
            label="migration backup payload directory",
        )
        destination = payload / "database.sqlite3"
        _copy_file_secure(snapshot_path, destination)
        health = _health(destination)
        _reject_future_user_version(health)
        if health["integrity"] != ["ok"]:
            raise MigrationError(
                "sqlite_integrity_failed",
                "migration backup snapshot failed integrity_check",
            )
        if health["foreign_key_violation_count"]:
            raise MigrationError(
                "foreign_key_failed",
                "migration backup snapshot failed foreign_key_check",
            )
        _fsync_file(destination)
        _fsync_directory(payload)
        _call_fault(fault_injector, "after_backup_payload_fsync")
        _assert_private_directory_identity(
            staging,
            staging_identity,
            label="migration backup staging directory",
        )
        _assert_private_directory_identity(
            payload,
            payload_identity,
            label="migration backup payload directory",
        )
        manifest = {
            "format": MIGRATION_BACKUP_FORMAT,
            "manifest_version": MIGRATION_BACKUP_VERSION,
            "backup_id": backup_id,
            "purpose": purpose,
            "created_at": canonical_utc(utc_now()),
            "application_version": application_version,
            "source": {
                "database_identity": effective_identity,
                "kind": source_kind,
                "schema_version": source_schema_version,
                "sqlite_user_version": health["user_version"],
                "schema_checksum": source_schema_checksum,
            },
            "files": [{
                "source_path": effective_identity,
                "archive_path": "payload/database.sqlite3",
                "kind": "sqlite",
                "size_bytes": destination.stat().st_size,
                "sha256": _sha256_file(destination),
                "integrity": health["integrity"],
                "foreign_key_violation_count": health["foreign_key_violation_count"],
            }],
            "payload_sha256": _sha256_file(destination),
            "secret_policy": "env_files_excluded",
            "complete": True,
        }
        manifest_path = staging / "manifest.json"
        manifest_bytes = _canonical_json(manifest) + b"\n"
        manifest_path.write_bytes(manifest_bytes)
        os.chmod(manifest_path, 0o600)
        complete_path = staging / "COMPLETE"
        complete_path.write_text(hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii")
        os.chmod(complete_path, 0o600)
        for path in (manifest_path, complete_path):
            _fsync_file(path)
        _fsync_directory(staging)
        _call_fault(fault_injector, "after_backup_metadata_fsync")
        _assert_private_directory_identity(
            staging,
            staging_identity,
            label="migration backup staging directory",
        )
        _assert_private_directory_identity(
            payload,
            payload_identity,
            label="migration backup payload directory",
        )
        _call_fault(fault_injector, "before_backup_publish")
        _assert_private_directory_identity(
            staging,
            staging_identity,
            label="migration backup staging directory",
        )
        os.replace(staging, final)
        published = True
        published_identity = staging_identity
        _call_fault(fault_injector, "after_backup_publish")
        _assert_private_directory_identity(
            final,
            published_identity,
            label="published migration backup",
        )
        _fsync_directory(backup_root)
        _call_fault(fault_injector, "after_backup_root_fsync")
        _assert_private_directory_identity(
            final,
            published_identity,
            label="published migration backup",
        )
        verify_migration_backup(final)
        _call_fault(fault_injector, "after_backup_verify")
        _assert_private_directory_identity(
            final,
            published_identity,
            label="published migration backup",
        )
        # The final fault boundary may mutate files in place without changing
        # the directory inode.  Re-run the content verifier after it returns;
        # callers must never modify the source unless the backup is still
        # independently recoverable at the hand-off boundary.
        verify_migration_backup(final)
        _assert_private_directory_identity(
            final,
            published_identity,
            label="published migration backup",
        )
        return final
    except Exception as exc:
        cleanup_target = final if published else staging
        expected_cleanup_identity = (
            published_identity if published else staging_identity
        )
        if expected_cleanup_identity is None and (
            cleanup_target.exists() or cleanup_target.is_symlink()
        ):
            raise MigrationError(
                "recovery_required",
                "migration backup path ownership is unknown; it was preserved",
            ) from exc
        if (
            expected_cleanup_identity is not None
            and _private_directory_identity(cleanup_target)
            != expected_cleanup_identity
        ):
            raise MigrationError(
                "recovery_required",
                "migration backup path changed ownership; external state was preserved",
            ) from exc
        try:
            shutil.rmtree(cleanup_target)
            _fsync_directory(backup_root)
        except FileNotFoundError:
            _fsync_directory(backup_root)
        except BaseException as cleanup_exc:
            raise MigrationError(
                "recovery_required",
                "migration backup publication failed and cleanup is incomplete",
                recovery_backup=final if published else None,
            ) from cleanup_exc
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            "backup_publish_failed",
            "migration backup could not be durably published",
        ) from exc


def _open_backup_regular_at(
    directory_fd: int,
    name: str,
    *,
    maximum_size: int | None = None,
) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise MigrationError(
            "invalid_backup_manifest",
            "migration backup file is missing",
        ) from exc
    except OSError as exc:
        raise MigrationError(
            "unsafe_backup_path",
            "migration backup contains an unsafe file",
        ) from exc
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
            raise MigrationError(
                "unsafe_backup_path",
                "migration backup contains a symlink or non-regular file",
            )
        if result.st_mode & 0o077:
            raise MigrationError(
                "unsafe_backup_permissions",
                "migration backup file is group- or world-accessible",
            )
        if maximum_size is not None and result.st_size > maximum_size:
            raise MigrationError(
                "invalid_backup_manifest",
                "migration backup metadata is too large",
            )
        return descriptor, result
    except BaseException:
        os.close(descriptor)
        raise


def _read_open_regular(descriptor: int, *, expected_size: int) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise MigrationError(
            "backup_read_failed",
            "could not read migration backup",
        ) from exc
    if len(data) != expected_size:
        raise MigrationError(
            "source_changed_during_migration",
            "migration backup file changed while it was being read",
        )
    return data


def _verify_migration_backup_at(
    *,
    backup_name: str,
    manifest_bytes: bytes,
    complete_bytes: bytes,
    payload: Path,
    payload_size: int,
) -> dict[str, Any]:
    """Verify one backup through an already pinned directory descriptor."""
    try:
        manifest = json.loads(manifest_bytes)
        complete_digest = complete_bytes.decode("ascii").strip()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("invalid_backup_manifest", "migration backup metadata is invalid") from exc
    if not isinstance(manifest, dict) or manifest_bytes != _canonical_json(manifest) + b"\n":
        raise MigrationError("invalid_backup_manifest", "migration backup manifest is not canonical")
    if complete_digest != hashlib.sha256(manifest_bytes).hexdigest():
        raise MigrationError("backup_hash_mismatch", "migration backup completion digest is invalid")
    expected_manifest_keys = {
        "format",
        "manifest_version",
        "backup_id",
        "purpose",
        "created_at",
        "application_version",
        "source",
        "files",
        "payload_sha256",
        "secret_policy",
        "complete",
    }
    if (
        set(manifest) != expected_manifest_keys
        or manifest.get("format") != MIGRATION_BACKUP_FORMAT
        or not _is_plain_int(manifest.get("manifest_version"))
        or manifest.get("manifest_version") != MIGRATION_BACKUP_VERSION
        or manifest.get("backup_id") != backup_name
        or MIGRATION_BACKUP_ID_PATTERN.fullmatch(backup_name) is None
        or not isinstance(manifest.get("purpose"), str)
        or manifest["purpose"] not in MIGRATION_BACKUP_PURPOSES
        or not backup_name.startswith(
            manifest["purpose"].replace("_", "-") + "-"
        )
        or not isinstance(manifest.get("application_version"), str)
        or not manifest["application_version"]
        or manifest.get("secret_policy") != "env_files_excluded"
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("source"), dict)
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) != 1
    ):
        raise MigrationError("invalid_backup_manifest", "migration backup contract is invalid")
    try:
        parsed_created_at = parse_legacy_datetime(manifest["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationError("invalid_backup_manifest", "migration backup time is invalid") from exc
    if canonical_utc(parsed_created_at) != manifest["created_at"]:
        raise MigrationError(
            "invalid_backup_manifest",
            "migration backup time is not canonical UTC",
        )

    source = manifest["source"]
    expected_source_keys = {
        "database_identity",
        "kind",
        "schema_version",
        "sqlite_user_version",
        "schema_checksum",
    }
    database_identity = source.get("database_identity")
    if (
        set(source) != expected_source_keys
        or not isinstance(database_identity, str)
        or not isinstance(source.get("kind"), str)
        or not _is_plain_int(source.get("schema_version"))
        or not _is_plain_int(source.get("sqlite_user_version"))
        or not isinstance(source.get("schema_checksum"), str)
        or len(source["schema_checksum"]) != 64
        or any(character not in "0123456789abcdef" for character in source["schema_checksum"])
    ):
        raise MigrationError("invalid_backup_manifest", "migration backup source metadata is invalid")
    try:
        normalize_database_identity(
            database_identity,
            Path(PurePosixPath(database_identity).name),
        )
    except MigrationError as exc:
        raise MigrationError(
            "invalid_backup_manifest",
            "migration backup database identity is invalid",
        ) from exc

    entry = manifest["files"][0]
    expected_entry_keys = {
        "source_path",
        "archive_path",
        "kind",
        "size_bytes",
        "sha256",
        "integrity",
        "foreign_key_violation_count",
    }
    if (
        not isinstance(entry, dict)
        or set(entry) != expected_entry_keys
        or entry.get("source_path") != database_identity
        or entry.get("archive_path") != "payload/database.sqlite3"
        or entry.get("kind") != "sqlite"
        or not _is_plain_int(entry.get("size_bytes"))
        or entry["size_bytes"] < 0
        or not isinstance(entry.get("sha256"), str)
        or len(entry["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in entry["sha256"])
        or manifest.get("payload_sha256") != entry["sha256"]
    ):
        raise MigrationError("invalid_backup_manifest", "migration backup file metadata is invalid")
    if entry.get("integrity") != ["ok"]:
        raise MigrationError(
            "sqlite_integrity_failed",
            "migration backup manifest records a failed integrity check",
        )
    if not _is_plain_int(entry.get("foreign_key_violation_count")):
        raise MigrationError(
            "invalid_backup_manifest",
            "migration backup foreign-key count is not an integer",
        )
    if entry["foreign_key_violation_count"] != 0:
        raise MigrationError(
            "foreign_key_failed",
            "migration backup manifest records foreign-key violations",
        )

    if payload_size != entry["size_bytes"] or _sha256_file(payload) != entry["sha256"]:
        raise MigrationError("backup_hash_mismatch", "migration backup payload hash is invalid")
    health = _health(payload)
    _reject_future_user_version(health)
    if health["integrity"] != ["ok"]:
        raise MigrationError("sqlite_integrity_failed", "migration backup failed integrity_check")
    if health["foreign_key_violation_count"]:
        raise MigrationError("foreign_key_failed", "migration backup failed foreign_key_check")
    history = _read_history(payload)
    has_history_table = "schema_migrations" in _source_tables(payload)
    if history:
        latest_version = _validate_history(payload, history)
        classified_kind = _classify_source(payload, source_history=history)
    elif has_history_table:
        raise MigrationError(
            "migration_history_invalid",
            "backup schema_migrations table contains no applied revision",
        )
    else:
        if health["user_version"] > CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                "future_schema_version",
                "migration backup was created by a newer application version",
            )
        if health["user_version"] != 0:
            raise MigrationError(
                "migration_history_invalid",
                "legacy backup declares a version without migration history",
            )
        latest_version = 0
        classified_kind = _classify_source(payload, source_history=[])
    if (
        health["integrity"] != entry.get("integrity")
        or health["foreign_key_violation_count"] != entry.get("foreign_key_violation_count")
        or health["user_version"] != source.get("sqlite_user_version")
        or latest_version != source.get("schema_version")
        or classified_kind != source.get("kind")
        or schema_checksum(payload) != source.get("schema_checksum")
    ):
        raise MigrationError("backup_verification_failed", "migration backup metadata does not match payload")
    return manifest


@contextmanager
def _pinned_migration_backup(
    backup_path: Path,
) -> Iterator[_PinnedMigrationBackupState]:
    """Pin one private backup directory against pathname replacement.

    The descriptor stays open while every metadata and payload read uses its
    ``/proc/self/fd`` view. A final pathname identity check also prevents a
    caller from accepting a backup swapped out during verification.
    """

    try:
        initial_stat = backup_path.lstat()
    except FileNotFoundError as exc:
        raise MigrationError("backup_missing", "migration backup does not exist") from exc
    except OSError as exc:
        raise MigrationError("unsafe_backup_path", "migration backup path is unsafe") from exc
    if backup_path.is_symlink() or not stat.S_ISDIR(initial_stat.st_mode):
        raise MigrationError("unsafe_backup_path", "migration backup path is unsafe")
    if initial_stat.st_mode & 0o077:
        raise MigrationError(
            "unsafe_backup_permissions",
            "migration backup directory is group- or world-accessible",
        )
    initial_identity = (initial_stat.st_dev, initial_stat.st_ino)

    _reject_symlinked_directory_ancestors(backup_path, label="migration backup")
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(backup_path, flags)
    except FileNotFoundError as exc:
        raise MigrationError("backup_missing", "migration backup does not exist") from exc
    except OSError as exc:
        raise MigrationError("unsafe_backup_path", "migration backup path is unsafe") from exc
    opened_descriptors = [directory_fd]
    try:
        directory_stat = os.fstat(directory_fd)
        expected_identity = (directory_stat.st_dev, directory_stat.st_ino)
        if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_nlink < 1:
            raise MigrationError("unsafe_backup_path", "migration backup path is unsafe")
        if directory_stat.st_mode & 0o077:
            raise MigrationError(
                "unsafe_backup_permissions",
                "migration backup directory is group- or world-accessible",
            )
        if (
            expected_identity != initial_identity
            or _private_directory_identity(backup_path) != initial_identity
        ):
            raise MigrationError(
                "source_changed_during_migration",
                "migration backup path changed while it was being opened",
            )
        try:
            root_names = set(os.listdir(directory_fd))
        except OSError as exc:
            raise MigrationError(
                "backup_read_failed",
                "migration backup directory could not be enumerated",
            ) from exc
        if root_names != {"COMPLETE", "manifest.json", "payload"}:
            raise MigrationError(
                "invalid_backup_manifest",
                "migration backup contains unexpected files",
            )

        try:
            payload_directory_fd = os.open(
                "payload",
                flags,
                dir_fd=directory_fd,
            )
        except FileNotFoundError as exc:
            raise MigrationError(
                "invalid_backup_manifest",
                "migration backup payload directory is missing",
            ) from exc
        except OSError as exc:
            raise MigrationError(
                "unsafe_backup_path",
                "migration backup payload directory is unsafe",
            ) from exc
        opened_descriptors.append(payload_directory_fd)
        payload_directory_stat = os.fstat(payload_directory_fd)
        if (
            not stat.S_ISDIR(payload_directory_stat.st_mode)
            or payload_directory_stat.st_mode & 0o077
        ):
            raise MigrationError(
                "unsafe_backup_permissions",
                "migration backup payload directory is not private",
            )
        try:
            payload_names = set(os.listdir(payload_directory_fd))
        except OSError as exc:
            raise MigrationError(
                "backup_read_failed",
                "migration backup payload directory could not be enumerated",
            ) from exc
        if payload_names != {"database.sqlite3"}:
            raise MigrationError(
                "invalid_backup_manifest",
                "migration backup payload contains unexpected files",
            )

        manifest_fd, manifest_stat = _open_backup_regular_at(
            directory_fd,
            "manifest.json",
            maximum_size=1024 * 1024,
        )
        opened_descriptors.append(manifest_fd)
        complete_fd, complete_stat = _open_backup_regular_at(
            directory_fd,
            "COMPLETE",
            maximum_size=256,
        )
        opened_descriptors.append(complete_fd)
        payload_fd, payload_stat = _open_backup_regular_at(
            payload_directory_fd,
            "database.sqlite3",
        )
        opened_descriptors.append(payload_fd)

        state = _PinnedMigrationBackupState(
            directory_fd=directory_fd,
            payload_directory_fd=payload_directory_fd,
            manifest_fd=manifest_fd,
            complete_fd=complete_fd,
            payload_fd=payload_fd,
            directory_identity=expected_identity,
            payload_directory_identity=(
                payload_directory_stat.st_dev,
                payload_directory_stat.st_ino,
            ),
            manifest_identity=(manifest_stat.st_dev, manifest_stat.st_ino),
            complete_identity=(complete_stat.st_dev, complete_stat.st_ino),
            payload_identity=(payload_stat.st_dev, payload_stat.st_ino),
            manifest_bytes=_read_open_regular(
                manifest_fd,
                expected_size=manifest_stat.st_size,
            ),
            complete_bytes=_read_open_regular(
                complete_fd,
                expected_size=complete_stat.st_size,
            ),
            payload_size=payload_stat.st_size,
        )
        yield state
    finally:
        for descriptor in reversed(opened_descriptors):
            os.close(descriptor)


def _descriptor_entry_identity(
    directory_fd: int,
    name: str,
) -> tuple[int, int] | None:
    try:
        result = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    return result.st_dev, result.st_ino


def _assert_pinned_backup_entries_unchanged(
    state: _PinnedMigrationBackupState,
    *,
    expected_payload_sha256: str,
) -> None:
    try:
        root_names = set(os.listdir(state.directory_fd))
        payload_names = set(os.listdir(state.payload_directory_fd))
        current_manifest_stat = os.fstat(state.manifest_fd)
        current_complete_stat = os.fstat(state.complete_fd)
        current_payload_stat = os.fstat(state.payload_fd)
    except OSError as exc:
        raise MigrationError(
            "source_changed_during_migration",
            "migration backup could not be rechecked after verification",
        ) from exc
    if (
        root_names != {"COMPLETE", "manifest.json", "payload"}
        or payload_names != {"database.sqlite3"}
        or _descriptor_entry_identity(state.directory_fd, "payload")
        != state.payload_directory_identity
        or _descriptor_entry_identity(state.directory_fd, "manifest.json")
        != state.manifest_identity
        or _descriptor_entry_identity(state.directory_fd, "COMPLETE")
        != state.complete_identity
        or _descriptor_entry_identity(
            state.payload_directory_fd,
            "database.sqlite3",
        )
        != state.payload_identity
        or current_manifest_stat.st_size != len(state.manifest_bytes)
        or current_complete_stat.st_size != len(state.complete_bytes)
        or current_payload_stat.st_size != state.payload_size
        or _read_open_regular(
            state.manifest_fd,
            expected_size=current_manifest_stat.st_size,
        )
        != state.manifest_bytes
        or _read_open_regular(
            state.complete_fd,
            expected_size=current_complete_stat.st_size,
        )
        != state.complete_bytes
        or _sha256_file(state.payload_path) != expected_payload_sha256
    ):
        raise MigrationError(
            "source_changed_during_migration",
            "migration backup entries were replaced during verification",
        )


def _assert_input_backup_unchanged(
    backup_path: Path,
    expected_identity: tuple[int, int],
) -> None:
    if _private_directory_identity(backup_path) != expected_identity:
        raise MigrationError(
            "source_changed_during_migration",
            "migration backup path was replaced during verification",
        )


def _verify_and_materialize_migration_backup(
    backup_path: Path,
    *,
    destination: Path | None = None,
    expected_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a pinned backup and optionally copy that exact payload inode."""

    backup_path = lexical_absolute(backup_path)
    with _pinned_migration_backup(backup_path) as pinned:
        manifest = _verify_migration_backup_at(
            backup_name=backup_path.name,
            manifest_bytes=pinned.manifest_bytes,
            complete_bytes=pinned.complete_bytes,
            payload=pinned.payload_path,
            payload_size=pinned.payload_size,
        )
        if expected_manifest is not None and manifest != expected_manifest:
            raise MigrationError(
                "source_changed_during_migration",
                "migration backup content changed after initial verification",
            )
        if destination is not None:
            _copy_file_secure(pinned.payload_path, destination)
            _fsync_file(destination)
            if _sha256_file(destination) != manifest["payload_sha256"]:
                raise MigrationError(
                    "backup_hash_mismatch",
                    "materialized restore candidate differs from the verified payload",
                )
            verify_sqlite_database(destination)
        _assert_pinned_backup_entries_unchanged(
            pinned,
            expected_payload_sha256=manifest["payload_sha256"],
        )
        _assert_input_backup_unchanged(backup_path, pinned.directory_identity)
        return manifest


def verify_migration_backup(backup_path: Path) -> dict[str, Any]:
    """Verify a published pre-migration backup without changing it."""

    return _verify_and_materialize_migration_backup(backup_path)


def backup_sqlite_database(
    database_path: Path,
    *,
    backup_root: Path | None = None,
    purpose: str = "pre_backfill",
    application_version: str = "unknown",
    database_identity: str | None = None,
    fault_injector: FaultInjector | None = None,
) -> Path:
    """Create and verify a manifest-backed snapshot without altering source."""

    requested_path = Path(database_path)
    if requested_path.is_symlink():
        raise MigrationError("unsafe_database_path", "database backup source may not be a symlink")
    database_path = lexical_absolute(requested_path)
    effective_identity = normalize_database_identity(database_identity, database_path)
    if not database_path.is_file():
        raise MigrationError("database_open_failed", "database backup source does not exist")
    _validate_sqlite_path_set(database_path)
    backup_root = (
        lexical_absolute(backup_root)
        if backup_root is not None
        else database_path.parent / "backups" / "migrations"
    )
    if backup_root.is_symlink():
        raise MigrationError("unsafe_backup_path", "database backup root may not be a symlink")
    _reject_symlinked_directory_ancestors(backup_root, label="migration backup root")

    with _migration_lease(database_path):
        _cleanup_stale_work_directories(database_path)
        with _writer_guard(database_path) as guard:
            if guard.connection is None:
                raise MigrationError(
                    "source_changed_during_migration",
                    "database backup source disappeared before it could be snapshotted",
                )
            _assert_writer_guard_path_unchanged(guard, database_path)
            with tempfile.TemporaryDirectory(
                prefix=f".{database_path.name}.backup-work-",
                dir=database_path.parent,
            ) as temporary:
                snapshot = Path(temporary) / "source.sqlite3"
                _snapshot_database(database_path, snapshot)
                _assert_writer_guard_path_unchanged(guard, database_path)
                health = _health(snapshot)
                _reject_future_user_version(health)
                if health["integrity"] != ["ok"]:
                    raise MigrationError(
                        "sqlite_integrity_failed",
                        "database failed integrity_check before backup",
                    )
                if health["foreign_key_violation_count"]:
                    raise MigrationError(
                        "foreign_key_failed",
                        "database failed foreign_key_check before backup",
                    )
                history = _read_history(snapshot)
                has_history_table = "schema_migrations" in _source_tables(snapshot)
                if history:
                    _validate_history(snapshot, history)
                elif has_history_table:
                    raise MigrationError(
                        "migration_history_invalid",
                        "schema_migrations exists but contains no applied revision",
                    )
                elif health["user_version"] > CURRENT_SCHEMA_VERSION:
                    raise MigrationError(
                        "future_schema_version",
                        "database was created by a newer application version",
                    )
                elif health["user_version"] != 0:
                    raise MigrationError(
                        "migration_history_invalid",
                        "legacy database declares a schema version without migration history",
                    )
                source_kind = _classify_source(snapshot, source_history=history)
                with _backup_root_lease(backup_root):
                    backup_path = _publish_backup(
                        snapshot,
                        backup_root=backup_root,
                        source_path=database_path,
                        source_kind=source_kind,
                        source_schema_version=history[-1]["version"] if history else 0,
                        source_schema_checksum=schema_checksum(snapshot),
                        application_version=application_version,
                        purpose=purpose,
                        database_identity=effective_identity,
                        fault_injector=fault_injector,
                    )
                try:
                    _assert_writer_guard_path_unchanged(guard, database_path)
                except MigrationError as exc:
                    exc.recovery_backup = backup_path
                    raise
                return backup_path


def restore_migration_backup(
    database_path: Path,
    backup_path: Path,
    *,
    safety_backup_root: Path | None = None,
    application_version: str = "unknown",
    fault_injector: FaultInjector | None = None,
    database_identity: str | None = None,
) -> dict[str, Any]:
    """Atomically restore a verified snapshot to an explicit SQLite path.

    The restored snapshot may be an older supported schema.  Normal startup
    will run the forward migration chain again; this function never attempts a
    reverse schema transformation.
    """

    backup_path = lexical_absolute(backup_path)
    manifest = verify_migration_backup(backup_path)
    requested_path = Path(database_path)
    if requested_path.is_symlink():
        raise MigrationError("unsafe_database_path", "restore target may not be a symlink")
    database_path = lexical_absolute(requested_path)
    if database_path == backup_path or database_path.is_relative_to(backup_path):
        raise MigrationError(
            "unsafe_backup_path",
            "restore target must not overlap the immutable input backup",
        )
    effective_identity = normalize_database_identity(database_identity, database_path)
    if database_path.exists() and not database_path.is_file():
        raise MigrationError("unsafe_database_path", "restore target is not a regular file")
    _validate_sqlite_path_set(database_path)
    if manifest["source"]["database_identity"] != effective_identity:
        raise MigrationError(
            "backup_target_mismatch",
            "migration backup belongs to a different managed database identity",
        )
    safety_backup_root = (
        lexical_absolute(safety_backup_root)
        if safety_backup_root is not None
        else database_path.parent / "backups" / "migrations"
    )
    if (
        safety_backup_root == backup_path
        or safety_backup_root.is_relative_to(backup_path)
    ):
        raise MigrationError(
            "unsafe_backup_path",
            "restore safety-backup root must not overlap the input backup",
        )
    if safety_backup_root.is_symlink() or (
        safety_backup_root.exists() and not safety_backup_root.is_dir()
    ):
        raise MigrationError(
            "unsafe_backup_path",
            "restore safety-backup root is not a safe directory",
        )
    _reject_symlinked_directory_ancestors(
        safety_backup_root,
        label="restore safety-backup root",
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with _migration_lease(database_path):
        _cleanup_stale_work_directories(database_path)
        try:
            writer_context = _writer_guard(database_path)
            with writer_context as guard:
                guard_connection = guard.connection
                with tempfile.TemporaryDirectory(
                    prefix=f".{database_path.name}.restore-work-",
                    dir=database_path.parent,
                ) as temporary:
                    work_root = Path(temporary)
                    live_existed = guard_connection is not None
                    live_mode = (
                        database_path.stat().st_mode & 0o777
                        if live_existed
                        else 0o600
                    )
                    live_snapshot: Path | None = None
                    rollback_snapshot: Path | None = None
                    rollback_snapshot_sha256: str | None = None

                    safety_backup: Path | None = None
                    trusted_safety_sha256: str | None = None

                    def assert_safety_backup_unchanged() -> None:
                        if safety_backup is None:
                            return
                        try:
                            current_manifest = verify_migration_backup(safety_backup)
                        except MigrationError as exc:
                            raise MigrationError(
                                "recovery_required",
                                "the verified restore safety backup changed after hand-off",
                            ) from exc
                        if current_manifest["payload_sha256"] != trusted_safety_sha256:
                            raise MigrationError(
                                "recovery_required",
                                "the restore safety-backup payload changed after hand-off",
                            )

                    def safety_backup_reference() -> Path | None:
                        try:
                            assert_safety_backup_unchanged()
                        except MigrationError:
                            return None
                        return safety_backup

                    if live_existed:
                        _assert_writer_guard_path_unchanged(guard, database_path)
                        live_snapshot = work_root / "live.sqlite3"
                        try:
                            _snapshot_database(database_path, live_snapshot)
                        except MigrationError as exc:
                            raise MigrationError(
                                "sqlite_integrity_failed",
                                "live database cannot be snapshotted safely for restore",
                            ) from exc
                        live_health = verify_sqlite_database(live_snapshot)
                        live_history = _read_history(live_snapshot)
                        has_live_history = "schema_migrations" in _source_tables(live_snapshot)
                        if live_history:
                            live_version = _validate_history(live_snapshot, live_history)
                        elif has_live_history:
                            raise MigrationError(
                                "migration_history_invalid",
                                "live schema_migrations table has no valid history",
                            )
                        else:
                            if live_health["user_version"] > CURRENT_SCHEMA_VERSION:
                                raise MigrationError(
                                    "future_schema_version",
                                    "refusing to overwrite a newer live schema",
                                )
                            if live_health["user_version"] != 0:
                                raise MigrationError(
                                    "migration_history_invalid",
                                    "live schema version has no migration history",
                                )
                            live_version = 0
                        live_kind = _classify_source(
                            live_snapshot,
                            source_history=live_history,
                        )
                        with _backup_root_lease(safety_backup_root):
                            safety_backup = _publish_backup(
                                live_snapshot,
                                backup_root=safety_backup_root,
                                source_path=database_path,
                                source_kind=live_kind,
                                source_schema_version=live_version,
                                source_schema_checksum=live_health["schema_checksum"],
                                application_version=application_version,
                                purpose="before_restore",
                                database_identity=effective_identity,
                                fault_injector=fault_injector,
                            )
                        safety_manifest = verify_migration_backup(safety_backup)
                        rollback_snapshot = (
                            safety_backup / "payload" / "database.sqlite3"
                        )
                        rollback_snapshot_sha256 = safety_manifest["payload_sha256"]
                        trusted_safety_sha256 = rollback_snapshot_sha256
                        if _sha256_file(live_snapshot) != rollback_snapshot_sha256:
                            raise MigrationError(
                                "restore_failed",
                                "live snapshot changed after its verified safety backup",
                                recovery_backup=safety_backup_reference(),
                            )
                        assert_safety_backup_unchanged()
                        try:
                            _canonicalize_live_journal(
                                guard,
                                database_path,
                                fault_injector=fault_injector,
                            )
                        except MigrationError as exc:
                            exc.recovery_backup = safety_backup_reference()
                            raise
                        try:
                            _call_fault(
                                fault_injector,
                                "after_journal_canonicalization",
                            )
                        except Exception as exc:
                            raise MigrationError(
                                "restore_failed",
                                "restore stopped after journal canonicalization; the live database was not replaced",
                                recovery_backup=safety_backup_reference(),
                            ) from exc
                        assert_safety_backup_unchanged()

                    candidate = work_root / "restore-candidate.sqlite3"
                    try:
                        _verify_and_materialize_migration_backup(
                            backup_path,
                            destination=candidate,
                            expected_manifest=manifest,
                        )
                        if _sha256_file(candidate) != manifest["files"][0]["sha256"]:
                            raise MigrationError(
                                "backup_hash_mismatch",
                                "restore candidate differs from verified payload",
                            )
                        candidate_health = verify_sqlite_database(candidate)
                        if (
                            candidate_health["schema_checksum"]
                            != manifest["source"]["schema_checksum"]
                        ):
                            raise MigrationError(
                                "backup_verification_failed",
                                "restore candidate schema differs from verified payload",
                            )
                    except Exception as exc:
                        candidate.unlink(missing_ok=True)
                        if isinstance(exc, MigrationError):
                            exc.recovery_backup = safety_backup_reference()
                            raise
                        raise MigrationError(
                            "restore_failed",
                            "restore candidate could not be prepared; live state is unchanged",
                            recovery_backup=safety_backup_reference(),
                        ) from exc
                    assert_safety_backup_unchanged()

                    published = False
                    reserved_absence = False
                    published_identity: tuple[int, int] | None = None
                    reserved_identity: tuple[int, int] | None = None
                    with ExitStack() as publication_guards:
                        try:
                            publication_guards.enter_context(
                                _publication_guard(candidate)
                            )
                            candidate_identity = _regular_file_identity(candidate)
                            if candidate_identity is None:
                                raise MigrationError(
                                    "restore_failed",
                                    "restore candidate became unsafe before publication",
                                )
                            if not live_existed:
                                _call_fault(fault_injector, "before_absent_reservation")
                                publication_guards.enter_context(
                                    _reserve_absent_database_path(database_path)
                                )
                                reserved_absence = True
                                reserved_identity = _regular_file_identity(database_path)
                                if reserved_identity is None:
                                    raise MigrationError(
                                        "publication_guard_failed",
                                        "reserved restore target became unsafe before publication",
                                    )
                            else:
                                _assert_writer_guard_path_unchanged(
                                    guard,
                                    database_path,
                                )
                            os.replace(candidate, database_path)
                            published = True
                            published_identity = candidate_identity
                            _call_fault(fault_injector, "after_main_replace")
                            _assert_published_path_unchanged(
                                database_path,
                                published_identity,
                            )
                            _remove_sqlite_sidecars(database_path)
                            _call_fault(fault_injector, "after_sidecar_cleanup")
                            _assert_published_path_unchanged(
                                database_path,
                                published_identity,
                            )
                            _fsync_directory(database_path.parent)
                            _call_fault(fault_injector, "after_publish_fsync")
                            _assert_published_path_unchanged(
                                database_path,
                                published_identity,
                            )
                            restored = verify_sqlite_database(database_path)
                            if (
                                restored["schema_checksum"]
                                != manifest["source"]["schema_checksum"]
                                or _sha256_file(database_path)
                                != manifest["files"][0]["sha256"]
                            ):
                                raise MigrationError(
                                    "backup_verification_failed",
                                    "published restore differs from verified backup",
                                )
                            _call_fault(fault_injector, "after_publish_verify")
                            _assert_published_path_unchanged(
                                database_path,
                                published_identity,
                            )
                            assert_safety_backup_unchanged()
                        except Exception as exc:
                            if (
                                published
                                and published_identity is not None
                                and _regular_file_identity(database_path)
                                != published_identity
                            ):
                                raise MigrationError(
                                    "source_changed_during_migration",
                                    "restore target was replaced during publication; external state was preserved",
                                    recovery_backup=safety_backup_reference(),
                                ) from exc
                            if published or reserved_absence:
                                try:
                                    trusted_rollback_snapshot = (
                                        _select_trusted_snapshot(
                                            rollback_snapshot_sha256,
                                            rollback_snapshot,
                                            live_snapshot,
                                        )
                                        if live_existed
                                        and rollback_snapshot_sha256 is not None
                                        else None
                                    )
                                    _restore_snapshot_atomically(
                                        database_path,
                                        trusted_rollback_snapshot,
                                        expected_current_identity=(
                                            published_identity
                                            if published
                                            else reserved_identity
                                        ),
                                        expected_snapshot_sha256=(
                                            rollback_snapshot_sha256
                                            if live_existed
                                            else None
                                        ),
                                        staging_directory=work_root,
                                        mode=live_mode,
                                        fault_injector=fault_injector,
                                    )
                                except BaseException as rollback_exc:
                                    raise MigrationError(
                                        "recovery_required",
                                        "restore failed and live rollback could not complete",
                                        recovery_backup=safety_backup_reference(),
                                    ) from rollback_exc
                            if isinstance(exc, MigrationError):
                                exc.recovery_backup = safety_backup_reference()
                                raise
                            raise MigrationError(
                                "restore_failed",
                                "restore failed; the previous live state was restored",
                                recovery_backup=safety_backup_reference(),
                            ) from exc
                        finally:
                            candidate.unlink(missing_ok=True)
        except MigrationError:
            raise
    return {
        "ok": True,
        "code": "migration_backup_restored",
        "database_path": str(database_path),
        "backup_id": manifest["backup_id"],
        "safety_backup_path": str(safety_backup) if safety_backup is not None else None,
        "schema_checksum": restored["schema_checksum"],
    }


def _expected_empty_schema_checksum(work_root: Path) -> str:
    empty_source = work_root / "expected-source.sqlite3"
    sqlite3.connect(empty_source).close()
    os.chmod(empty_source, 0o600)
    target = work_root / "expected-target.sqlite3"
    checksum, _ = _build_candidate(empty_source, target, fault_injector=None)
    return checksum


def migrate_sqlite_database(
    database_path: Path,
    *,
    backup_root: Path | None = None,
    application_version: str = "unknown",
    fault_injector: FaultInjector | None = None,
    database_identity: str | None = None,
) -> MigrationReport:
    """Prepare one SQLite file without mutating its legacy source in place."""

    requested_path = Path(database_path)
    if requested_path.is_symlink():
        raise MigrationError(
            "unsafe_database_path",
            "refusing to migrate a database through a symbolic link",
        )
    database_path = lexical_absolute(requested_path)
    effective_identity = normalize_database_identity(database_identity, database_path)
    if database_path.exists() and not database_path.is_file():
        raise MigrationError(
            "unsafe_database_path",
            "configured database path is not a regular file",
        )
    _validate_sqlite_path_set(database_path)
    backup_root = (
        lexical_absolute(backup_root)
        if backup_root is not None
        else database_path.parent / "backups" / "migrations"
    )
    if backup_root.is_symlink():
        raise MigrationError(
            "unsafe_backup_path",
            "refusing to publish migration backups through a symbolic link",
        )
    _reject_symlinked_directory_ancestors(backup_root, label="migration backup root")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with _migration_lease(database_path):
        _cleanup_stale_work_directories(database_path)
        # A crash can leave an unpublished candidate behind. The source remains
        # authoritative until os.replace, so these files are always disposable.
        candidate_prefix = f".{database_path.name}.candidate-"
        for stale in database_path.parent.iterdir():
            if (
                stale.name.startswith(candidate_prefix)
                and stale.is_file()
                and not stale.is_symlink()
            ):
                stale.unlink()

        with tempfile.TemporaryDirectory(
            prefix=f".{database_path.name}.migration-work-",
            dir=database_path.parent,
        ) as temporary:
            work_root = Path(temporary)
            expected_checksum = _expected_empty_schema_checksum(work_root)
            with _writer_guard(database_path) as guard:
                guard_connection = guard.connection
                source_existed = guard_connection is not None
                source_mode = (
                    database_path.stat().st_mode & 0o777
                    if source_existed
                    else 0o600
                )
                snapshot = work_root / "source-snapshot.sqlite3"
                if source_existed:
                    _assert_writer_guard_path_unchanged(guard, database_path)
                _snapshot_database(database_path if source_existed else None, snapshot)
                _assert_writer_guard_path_unchanged(guard, database_path)
                source_health = _health(snapshot)
                _reject_future_user_version(source_health)
                if source_health["integrity"] != ["ok"]:
                    raise MigrationError("sqlite_integrity_failed", "source database failed integrity_check")
                if source_health["foreign_key_violation_count"]:
                    raise MigrationError("foreign_key_failed", "source database failed foreign_key_check")
                source_history = _read_history(snapshot)
                has_history_table = "schema_migrations" in _source_tables(snapshot)
                if source_history:
                    _validate_history(snapshot, source_history)
                elif has_history_table:
                    raise MigrationError(
                        "migration_history_invalid",
                        "schema_migrations exists but contains no applied revision",
                    )
                elif source_health["user_version"] > CURRENT_SCHEMA_VERSION:
                    raise MigrationError(
                        "future_schema_version",
                        "database was created by a newer application version",
                    )
                elif source_health["user_version"] != 0:
                    raise MigrationError(
                        "migration_history_invalid",
                        "legacy database declares a schema version without migration history",
                    )
                source_kind = _classify_source(
                    snapshot,
                    source_history=source_history,
                )
                source_checksum = schema_checksum(snapshot)
                source_schema_version = source_history[-1]["version"] if source_history else 0
                if (
                    source_kind == "versioned"
                    and source_schema_version == CURRENT_SCHEMA_VERSION
                    and source_checksum == expected_checksum
                ):
                    verify_sqlite_database(
                        snapshot,
                        expected_schema_checksum=expected_checksum,
                    )
                    _assert_writer_guard_path_unchanged(guard, database_path)
                    return MigrationReport(
                        database_path=str(database_path),
                        source_kind="versioned",
                        source_schema_checksum=expected_checksum,
                        target_schema_checksum=expected_checksum,
                        version=CURRENT_SCHEMA_VERSION,
                        applied=False,
                        backup_path=None,
                        legacy_naive_timestamps=0,
                        offset_timestamps_normalized=0,
                    )

                # A full dry-run passes before any discoverable backup or
                # source-side change is created.
                dry_run = work_root / "dry-run.sqlite3"
                dry_checksum, _ = _build_candidate(
                    snapshot,
                    dry_run,
                    source_history=source_history,
                    fault_injector=None,
                )
                if dry_checksum != expected_checksum:
                    raise MigrationError("schema_verification_failed", "dry-run schema is not canonical")

                with _backup_root_lease(backup_root):
                    backup_path = _publish_backup(
                        snapshot,
                        backup_root=backup_root,
                        source_path=database_path,
                        source_kind=source_kind,
                        source_schema_version=source_schema_version,
                        source_schema_checksum=source_checksum,
                        application_version=application_version,
                        database_identity=effective_identity,
                        fault_injector=fault_injector,
                    )
                backup_manifest = verify_migration_backup(backup_path)
                trusted_snapshot_sha256 = backup_manifest["payload_sha256"]
                rollback_snapshot = backup_path / "payload" / "database.sqlite3"

                def assert_recovery_backup_unchanged() -> None:
                    try:
                        current_manifest = verify_migration_backup(backup_path)
                    except MigrationError as exc:
                        raise MigrationError(
                            "recovery_required",
                            "the verified migration backup changed after hand-off",
                        ) from exc
                    if current_manifest["payload_sha256"] != trusted_snapshot_sha256:
                        raise MigrationError(
                            "recovery_required",
                            "the migration backup payload changed after hand-off",
                        )

                def recovery_backup_reference() -> Path | None:
                    try:
                        assert_recovery_backup_unchanged()
                    except MigrationError:
                        return None
                    return backup_path

                if _sha256_file(snapshot) != trusted_snapshot_sha256:
                    raise MigrationError(
                        "migration_failed",
                        "source snapshot changed after its verified backup",
                        recovery_backup=recovery_backup_reference(),
                    )
                candidate = work_root / "candidate.sqlite3"
                try:
                    target_checksum, counters = _build_candidate(
                        snapshot,
                        candidate,
                        source_history=source_history,
                        fault_injector=fault_injector,
                    )
                    if target_checksum != expected_checksum:
                        raise MigrationError(
                            "schema_verification_failed",
                            "candidate schema is not canonical",
                        )
                    if _sha256_file(snapshot) != trusted_snapshot_sha256:
                        raise MigrationError(
                            "migration_failed",
                            "source snapshot changed while the candidate was built",
                            recovery_backup=recovery_backup_reference(),
                        )
                    candidate_sha256 = _sha256_file(candidate)
                except MigrationError as exc:
                    exc.recovery_backup = recovery_backup_reference()
                    raise
                except Exception as exc:
                    raise MigrationError(
                        "migration_failed",
                        "migration candidate preparation failed; a verified recovery backup exists",
                        recovery_backup=recovery_backup_reference(),
                    ) from exc
                assert_recovery_backup_unchanged()
                try:
                    _canonicalize_live_journal(
                        guard,
                        database_path,
                        fault_injector=fault_injector,
                    )
                except MigrationError as exc:
                    exc.recovery_backup = recovery_backup_reference()
                    raise
                try:
                    _call_fault(fault_injector, "after_journal_canonicalization")
                except Exception as exc:
                    raise MigrationError(
                        "migration_failed",
                        "migration stopped after journal canonicalization; the source was not replaced",
                        recovery_backup=recovery_backup_reference(),
                    ) from exc
                if _sha256_file(snapshot) != trusted_snapshot_sha256:
                    raise MigrationError(
                        "migration_failed",
                        "source snapshot changed before candidate publication",
                        recovery_backup=recovery_backup_reference(),
                    )
                assert_recovery_backup_unchanged()

                published = False
                reserved_absence = False
                published_identity: tuple[int, int] | None = None
                reserved_identity: tuple[int, int] | None = None
                with ExitStack() as publication_guards:
                    try:
                        publication_guards.enter_context(_publication_guard(candidate))
                        candidate_identity = _regular_file_identity(candidate)
                        if candidate_identity is None:
                            raise MigrationError(
                                "migration_failed",
                                "migration candidate became unsafe before publication",
                            )
                        if not source_existed:
                            _call_fault(fault_injector, "before_absent_reservation")
                            publication_guards.enter_context(
                                _reserve_absent_database_path(database_path)
                            )
                            reserved_absence = True
                            reserved_identity = _regular_file_identity(database_path)
                            if reserved_identity is None:
                                raise MigrationError(
                                    "publication_guard_failed",
                                    "reserved migration target became unsafe before publication",
                                )
                        else:
                            _assert_writer_guard_path_unchanged(
                                guard,
                                database_path,
                            )
                        os.replace(candidate, database_path)
                        published = True
                        published_identity = candidate_identity
                        _call_fault(fault_injector, "after_main_replace")
                        _assert_published_path_unchanged(
                            database_path,
                            published_identity,
                        )
                        _remove_sqlite_sidecars(database_path)
                        _call_fault(fault_injector, "after_sidecar_cleanup")
                        _assert_published_path_unchanged(
                            database_path,
                            published_identity,
                        )
                        _fsync_directory(database_path.parent)
                        _call_fault(fault_injector, "after_publish_fsync")
                        _assert_published_path_unchanged(
                            database_path,
                            published_identity,
                        )
                        verify_sqlite_database(
                            database_path,
                            expected_schema_checksum=expected_checksum,
                        )
                        if _sha256_file(database_path) != candidate_sha256:
                            raise MigrationError(
                                "schema_verification_failed",
                                "published migration differs from its verified candidate",
                            )
                        _call_fault(fault_injector, "after_publish_verify")
                        _assert_published_path_unchanged(
                            database_path,
                            published_identity,
                        )
                        _call_fault(fault_injector, "after_publish")
                        _assert_published_path_unchanged(
                            database_path,
                            published_identity,
                        )
                        assert_recovery_backup_unchanged()
                    except Exception as exc:
                        if (
                            published
                            and published_identity is not None
                            and _regular_file_identity(database_path)
                            != published_identity
                        ):
                            raise MigrationError(
                                "source_changed_during_migration",
                                "migration target was replaced during publication; external state was preserved",
                                recovery_backup=recovery_backup_reference(),
                            ) from exc
                        if published or reserved_absence:
                            try:
                                trusted_rollback_snapshot = (
                                    _select_trusted_snapshot(
                                        trusted_snapshot_sha256,
                                        rollback_snapshot,
                                        snapshot,
                                    )
                                    if source_existed
                                    else None
                                )
                                _restore_snapshot_atomically(
                                    database_path,
                                    trusted_rollback_snapshot,
                                    expected_current_identity=(
                                        published_identity
                                        if published
                                        else reserved_identity
                                    ),
                                    expected_snapshot_sha256=(
                                        trusted_snapshot_sha256
                                        if source_existed
                                        else None
                                    ),
                                    staging_directory=work_root,
                                    mode=source_mode,
                                    fault_injector=fault_injector,
                                )
                            except BaseException as rollback_exc:
                                raise MigrationError(
                                    "recovery_required",
                                    "migration publish failed and source rollback could not complete",
                                    recovery_backup=recovery_backup_reference(),
                                ) from rollback_exc
                        if isinstance(exc, MigrationError):
                            exc.recovery_backup = recovery_backup_reference()
                            raise
                        raise MigrationError(
                            "migration_failed",
                            "canonical migration failed; source is unchanged and a recovery backup exists",
                            recovery_backup=recovery_backup_reference(),
                        ) from exc
                    finally:
                        candidate.unlink(missing_ok=True)

            return MigrationReport(
                database_path=str(database_path),
                source_kind=source_kind,
                source_schema_checksum=source_checksum,
                target_schema_checksum=expected_checksum,
                version=CURRENT_SCHEMA_VERSION,
                applied=True,
                backup_path=str(backup_path),
                legacy_naive_timestamps=counters["legacy_naive_timestamps"],
                offset_timestamps_normalized=counters["offset_timestamps_normalized"],
            )


def report_dict(report: MigrationReport) -> dict[str, Any]:
    """Return a JSON-safe report without database contents."""

    return asdict(report)
