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
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.database import AsyncSessionLocal, create_schema  # noqa: E402
from app.models import Artifact, EvidenceObservation  # noqa: E402
from app.services.evidence import (  # noqa: E402
    audit_observations,
    backfill_legacy_observations,
    build_evidence_state,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def run(args: argparse.Namespace) -> int:
    await create_schema()
    async with AsyncSessionLocal() as db:
        backfill = None
        if args.backfill_v1:
            backfill = await backfill_legacy_observations(
                db,
                settings.DEFAULT_OWNER_ID,
                plan_id=args.plan_id,
            )
        query = select(EvidenceObservation).where(EvidenceObservation.owner_id == settings.DEFAULT_OWNER_ID)
        if args.plan_id is not None:
            query = query.where(EvidenceObservation.plan_id == args.plan_id)
        observations = list((await db.execute(query.order_by(EvidenceObservation.id))).scalars())
        artifact_query = select(Artifact).where(Artifact.owner_id == settings.DEFAULT_OWNER_ID)
        if args.plan_id is not None:
            artifact_query = artifact_query.where(Artifact.plan_id == args.plan_id)
        artifacts = list((await db.execute(artifact_query.order_by(Artifact.id))).scalars())
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
            output_dir = PROJECT_ROOT / "data" / "context" / "evidence"
            if args.plan_id is not None:
                _write_json(output_dir / "plans" / f"{args.plan_id}.json", result["projection"])
            else:
                for plan_id, projection in result["plans"].items():
                    _write_json(output_dir / "plans" / f"{plan_id}.json", projection)
            if args.audit:
                _write_json(output_dir / "audit.json", result["audit"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not args.audit or result["audit"]["ok"] else 1


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
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
