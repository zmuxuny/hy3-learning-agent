#!/usr/bin/env python3
"""Safe backup, reset, verification, and restore entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.db.maintenance import (  # noqa: E402
    DATABASE_PATHS,
    MaintenanceError,
    backup_state,
    maintenance_directory_lease,
    preflight_state,
    recover_state,
    reset_state,
    restore_state,
    runtime_state_lease,
    verify_backup,
)
from app.version import APPLICATION_VERSION  # noqa: E402


class MigrationCommandError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recovery_backup: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recovery_backup = recovery_backup


def _absolute_explicit_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


@contextmanager
def _pinned_managed_database(
    root: Path,
    requested: Path,
) -> Iterator[tuple[Path, str]]:
    """Bind a migration restore target to this root's lifecycle lease.

    A caller-controlled ``--root`` must never be usable to lock an unrelated
    empty directory while replacing another running repository's database.
    Open each parent without following links and keep the directory identity
    pinned for the entire restore.  The migration manifest separately binds
    the lexical managed path while I/O uses the pinned descriptor path.
    """

    requested_path = _absolute_explicit_path(requested)
    relative = next(
        (
            candidate
            for candidate in DATABASE_PATHS
            if root.joinpath(*candidate.parts) == requested_path
        ),
        None,
    )
    if relative is None:
        raise MaintenanceError(
            "unmanaged_database_path",
            "Migration restore target must be a managed database under --root.",
        )

    descriptors: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
        descriptors.append(descriptor)
        for component in relative.parts[:-1]:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        try:
            target_stat = os.stat(
                relative.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and (
            not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1
        ):
            raise MaintenanceError(
                "unsafe_database_path",
                "Migration restore target is not a private regular file.",
            )
        yield (
            Path("/proc/self/fd") / str(descriptor) / relative.name,
            relative.as_posix(),
        )
        reopened = os.open(requested_path.parent, flags)
        try:
            pinned_stat = os.fstat(descriptor)
            reopened_stat = os.fstat(reopened)
            if (pinned_stat.st_dev, pinned_stat.st_ino) != (
                reopened_stat.st_dev,
                reopened_stat.st_ino,
            ):
                raise MaintenanceError(
                    "recovery_required",
                    "Managed database parent changed during migration restore.",
                    exit_code=4,
                )
        finally:
            os.close(reopened)
    except MaintenanceError:
        raise
    except OSError as exc:
        raise MaintenanceError(
            "unsafe_database_path",
            "Migration restore target has a missing or unsafe parent directory.",
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_report(report: dict, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="explicit repository root (defaults to the script repository)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create a verified state backup")
    backup.add_argument("--purpose", default="manual")
    backup.add_argument("--dry-run", action="store_true")

    reset = subparsers.add_parser("reset", help="back up and reset managed state")
    reset.add_argument("--purpose", default="pre_clean")
    reset.add_argument("--dry-run", action="store_true")

    restore = subparsers.add_parser("restore", help="verify and restore a backup")
    selector = restore.add_mutually_exclusive_group(required=True)
    selector.add_argument("--backup")
    selector.add_argument("--latest-purpose")
    restore.add_argument("--expected-purpose")
    restore.add_argument("--dry-run", action="store_true")

    verify = subparsers.add_parser("verify", help="verify a published backup")
    verify.add_argument("--backup", required=True)
    verify.add_argument("--expected-purpose")

    preflight = subparsers.add_parser("preflight", help="run read-only maintenance checks")
    preflight.add_argument("--backup")

    subparsers.add_parser("recover", help="recover an interrupted state swap")

    migration_verify = subparsers.add_parser(
        "migration-backup-verify",
        help="verify one explicit canonical migration backup",
    )
    migration_verify.add_argument("--backup", required=True, type=Path)

    migration_restore = subparsers.add_parser(
        "migration-backup-restore",
        help="restore one explicit migration backup to one explicit SQLite path",
    )
    migration_restore.add_argument("--database", required=True, type=Path)
    migration_restore.add_argument("--backup", required=True, type=Path)
    migration_restore.add_argument("--safety-backup-root", required=True, type=Path)
    return parser


def _run_migration_backup_command(args: argparse.Namespace, root: Path) -> dict:
    # Keep SQLAlchemy/model imports completely outside ordinary stdlib-only
    # maintenance commands.  Migration recovery is an explicit opt-in path.
    try:
        from app.db.migrations import (  # noqa: PLC0415
            MigrationError,
            restore_migration_backup,
            verify_migration_backup,
        )
    except (ImportError, RuntimeError) as exc:
        raise MigrationCommandError(
            "migration_runtime_unavailable",
            "Migration backup support is unavailable in this installation.",
        ) from exc

    try:
        if args.command == "migration-backup-verify":
            with maintenance_directory_lease(root):
                manifest = verify_migration_backup(_absolute_explicit_path(args.backup))
                return {
                    "ok": True,
                    "code": "migration_backup_verified",
                    "backup_id": manifest["backup_id"],
                    "purpose": manifest["purpose"],
                    "schema_version": manifest["source"]["schema_version"],
                }
        with runtime_state_lease(root):
            requested_database = _absolute_explicit_path(args.database)
            with _pinned_managed_database(
                root,
                requested_database,
            ) as (database, database_identity):
                report = restore_migration_backup(
                    database,
                    _absolute_explicit_path(args.backup),
                    safety_backup_root=_absolute_explicit_path(args.safety_backup_root),
                    application_version=APPLICATION_VERSION,
                    database_identity=database_identity,
                )
            report["database_path"] = str(requested_database)
            return report
    except MigrationError as exc:
        raise MigrationCommandError(
            exc.code,
            str(exc),
            recovery_backup=exc.recovery_backup,
        ) from None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        if args.command == "backup":
            report = backup_state(root, args.purpose, dry_run=args.dry_run)
        elif args.command == "reset":
            report = reset_state(root, args.purpose, dry_run=args.dry_run)
        elif args.command == "restore":
            report = restore_state(
                root,
                backup=args.backup,
                latest_purpose=args.latest_purpose,
                expected_purpose=args.expected_purpose,
                dry_run=args.dry_run,
            )
        elif args.command == "verify":
            manifest = verify_backup(
                root,
                args.backup,
                expected_purpose=args.expected_purpose,
            )
            report = {
                "ok": True,
                "code": "backup_verified",
                "backup_id": manifest["backup_id"],
                "purpose": manifest["purpose"],
            }
        elif args.command == "preflight":
            report = preflight_state(root, backup=args.backup)
        elif args.command == "recover":
            report = recover_state(root)
        else:
            report = _run_migration_backup_command(args, root)
    except MigrationCommandError as exc:
        report = {
            "ok": False,
            "code": exc.code,
            "message": str(exc),
        }
        if exc.recovery_backup is not None:
            report["recovery_backup"] = str(exc.recovery_backup)
        _write_report(report, error=True)
        return 4 if exc.code == "recovery_required" else 3
    except MaintenanceError as exc:
        _write_report(
            {
                "ok": False,
                "code": exc.code,
                "message": str(exc),
            },
            error=True,
        )
        return exc.exit_code
    except (KeyboardInterrupt, BrokenPipeError):
        _write_report(
            {
                "ok": False,
                "code": "maintenance_interrupted",
                "message": "Maintenance was interrupted before completion.",
            },
            error=True,
        )
        return 130
    except OSError:
        _write_report(
            {
                "ok": False,
                "code": "invalid_root",
                "message": "The explicit repository root is unavailable.",
            },
            error=True,
        )
        return 3
    _write_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
