#!/usr/bin/env python3
"""Rebuild and audit the V2 evidence projection from SQLite facts.

Examples:
  .venv/bin/python scripts/rebuild-evidence.py --plan-id 3
  .venv/bin/python scripts/rebuild-evidence.py --audit --write

The command never edits evidence rows.  ``--write`` only replaces derived JSON
files under ``data/context/evidence`` atomically, so a projection can be
deleted and regenerated without changing the learning ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.paths import lexical_absolute  # noqa: E402
from app.db.database import AsyncSessionLocal, create_schema  # noqa: E402
from app.db.uow import commit as commit_uow  # noqa: E402
from app.db.maintenance import (  # noqa: E402
    DATABASE_PATHS,
    MaintenanceError,
    coordinated_state_mutation,
)
from app.models import Artifact, EvidenceObservation  # noqa: E402
from app.services.evidence import (  # noqa: E402
    audit_observations,
    backfill_legacy_observations,
    build_evidence_state,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _state_root(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "state_root", PROJECT_ROOT)).resolve(strict=True)


def _require_managed_mutation_scope(args: argparse.Namespace, root: Path) -> None:
    database_url = make_url(settings.DATABASE_URL)
    if database_url.get_backend_name() != "sqlite" or database_url.database in {None, ":memory:"}:
        raise MaintenanceError(
            "unsupported_mutation_database",
            "Evidence write modes require a managed file-backed SQLite database.",
        )
    database_path = lexical_absolute(database_url.database)
    try:
        relative = database_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise MaintenanceError(
            "unmanaged_mutation_target",
            "Configured database is outside the explicit state root.",
        ) from exc
    if relative not in {path.as_posix() for path in DATABASE_PATHS}:
        raise MaintenanceError(
            "unmanaged_mutation_target",
            "Configured database is not one of the managed state paths.",
        )


async def _run_uncoordinated(args: argparse.Namespace) -> int:
    if args.backfill_v1:
        # Backfill is the only database-writing mode.  It first goes through
        # the verified migration/backup coordinator; audit and projection
        # modes never create or alter schema.
        await create_schema(
            state_lease_held=True,
            state_root=_state_root(args),
        )

    table_names: set[str] | None = None
    database_url = make_url(settings.DATABASE_URL)
    if database_url.get_backend_name() == "sqlite" and database_url.database not in {None, ":memory:"}:
        database_path = Path(database_url.database).resolve()
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            connection.close()

    if table_names is not None and "evidence_observations" not in table_names:
        result = {
            "owner_id": settings.DEFAULT_OWNER_ID,
            "plan_id": args.plan_id,
            "backfill": None,
            "projection": build_evidence_state([]) if args.plan_id is not None else None,
            "audit": audit_observations([], []) if args.audit else None,
            "plans": {} if args.plan_id is None else None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    backfill = None
    if args.backfill_v1:
        # Phase 1 owns the only writer transaction.  A failed commit must stop
        # before a projection derived from uncommitted facts can be published.
        async with AsyncSessionLocal() as db:
            backfill = await backfill_legacy_observations(
                db,
                settings.DEFAULT_OWNER_ID,
                plan_id=args.plan_id,
            )
            await commit_uow(db)

    # Phase 2 deliberately uses a fresh session.  It can only observe the
    # committed ledger, and the session is closed before any mkdir/write/fsync
    # or atomic replacement below.
    async with AsyncSessionLocal() as db:
        query = select(EvidenceObservation).where(EvidenceObservation.owner_id == settings.DEFAULT_OWNER_ID)
        if args.plan_id is not None:
            query = query.where(EvidenceObservation.plan_id == args.plan_id)
        observations = list((await db.execute(query.order_by(EvidenceObservation.id))).scalars())
        artifact_query = select(Artifact).where(Artifact.owner_id == settings.DEFAULT_OWNER_ID)
        if args.plan_id is not None:
            artifact_query = artifact_query.where(Artifact.plan_id == args.plan_id)
        artifacts = (
            list((await db.execute(artifact_query.order_by(Artifact.id))).scalars())
            if table_names is None or "artifacts" in table_names
            else []
        )
        result: dict = {
            "owner_id": settings.DEFAULT_OWNER_ID,
            "plan_id": args.plan_id,
            "backfill": backfill,
            "projection": build_evidence_state(observations) if args.plan_id is not None else None,
            "audit": audit_observations(observations, artifacts) if args.audit else None,
        }
        if args.plan_id is None:
            plan_ids = {
                item.plan_id for item in observations if item.plan_id is not None
            }
            result["plans"] = {
                str(plan_id): build_evidence_state([
                    item for item in observations if item.plan_id == plan_id
                ])
                for plan_id in sorted(plan_ids)
            }

    if args.write:
        output_dir = _state_root(args) / "data" / "context" / "evidence"
        if args.plan_id is not None:
            _write_json(output_dir / "plans" / f"{args.plan_id}.json", result["projection"])
        else:
            for plan_id, projection in result["plans"].items():
                _write_json(output_dir / "plans" / f"{plan_id}.json", projection)
        if args.audit:
            _write_json(output_dir / "audit.json", result["audit"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.audit or result["audit"]["ok"] else 1


async def run(args: argparse.Namespace) -> int:
    """Execute the projection, enforcing backup/lease for every write mode."""

    if not (args.backfill_v1 or args.write):
        return await _run_uncoordinated(args)
    root = _state_root(args)
    _require_managed_mutation_scope(args, root)
    with coordinated_state_mutation(root, purpose="pre_evidence_rebuild"):
        return await _run_uncoordinated(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", type=int, help="only rebuild one plan")
    parser.add_argument("--audit", action="store_true", help="run ledger integrity checks")
    parser.add_argument(
        "--backfill-v1",
        action="store_true",
        help="import only explicit v1 submissions, verdicts, quizzes, and evidence-backed task events",
    )
    parser.add_argument("--write", action="store_true", help="atomically write derived JSON snapshots")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=PROJECT_ROOT,
        help="explicit repository-shaped state root for mutating modes",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except MaintenanceError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
