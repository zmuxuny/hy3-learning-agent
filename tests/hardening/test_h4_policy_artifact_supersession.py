"""H4 policy, Artifact, supersession, and merge-candidate gates.

The database-backed cases use pytest's disposable SQLite fixture. Damaged
supersession graphs are in-memory objects because the canonical schema should
prevent them from being written; the public audit still has to detect them
when rebuilding a legacy or externally restored ledger.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.database import AsyncSessionLocal
from app.models import Artifact, EvidenceObservation, Owner, Plan, Stage, Task
from app.services import competencies as competency_service
from app.services.competencies import create_competency
from app.services.evidence import (
    append_observation,
    audit_observations,
    build_evidence_state,
    create_artifact,
    resolve_artifact_content,
)


def _fact(
    fact_id: int,
    *,
    source_type: str = "quiz",
    outcome: str = "passed",
    owner_id: str = "local",
    plan_id: int = 1,
    task_id: int = 1,
    occurred_offset: int = 0,
    assistance_level: str = "independent",
    transfer_level: str = "same_task",
    normalized_score: float | None = None,
    fact_kind: str = "observation",
    target_observation_id: int | None = None,
    reason_code: str = "",
    artifact_refs: list[dict[str, Any]] | None = None,
    rubric_snapshot: dict[str, Any] | None = None,
    evaluator: dict[str, Any] | None = None,
) -> SimpleNamespace:
    occurred_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=occurred_offset
    )
    return SimpleNamespace(
        id=fact_id,
        owner_id=owner_id,
        source_type=source_type,
        source_id=f"h4-policy:{fact_id}",
        run_id=None,
        session_id=None,
        plan_id=plan_id,
        task_id=task_id,
        fact_kind=fact_kind,
        target_observation_id=target_observation_id,
        reason_code=reason_code,
        evidence_role=(
            "control"
            if fact_kind in {"invalidation", "reinstatement"}
            else "primary"
        ),
        eligibility_stage=None,
        eligibility_reason="",
        eligibility_policy_version="",
        counts_as_success=False,
        outcome=outcome,
        normalized_score=normalized_score,
        is_correct=outcome in {"accepted", "passed", "verified"},
        assistance_level=assistance_level,
        transfer_level=transfer_level,
        rubric_snapshot=rubric_snapshot or {},
        evaluator=evaluator or {},
        _evidence_artifact_refs=artifact_refs or [],
        _evidence_competency_refs=[],
        payload={},
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        schema_version=2,
        correlation_id=None,
        causation_id=None,
        idempotency_key=f"h4-policy:{fact_id}",
        request_digest=None,
    )


@pytest.mark.parametrize(
    ("name", "facts", "expected"),
    [
        pytest.param(
            "ordinary-chat",
            [_fact(1, source_type="chat", outcome="verified")],
            {
                "evidence_stage": "exposed",
                "success_count": 0,
                "failure_count": 0,
                "latest_outcome": "verified",
            },
            id="ordinary-chat-cannot-demonstrate",
        ),
        pytest.param(
            "hinted-pass",
            [
                _fact(
                    1,
                    assistance_level="hint",
                    normalized_score=0.9,
                    artifact_refs=[{"artifact_id": 1}],
                    rubric_snapshot={"pass_threshold": 0.8},
                    evaluator={"type": "deterministic"},
                )
            ],
            {
                "evidence_stage": "practicing",
                "success_count": 1,
                "failure_count": 0,
                "latest_outcome": "passed",
            },
            id="hinted-pass-is-capped",
        ),
        pytest.param(
            "self-report-conflicts-with-quiz",
            [
                _fact(1, source_type="self_report", outcome="verified"),
                _fact(
                    2,
                    outcome="failed",
                    normalized_score=0.2,
                    occurred_offset=1,
                    artifact_refs=[{"artifact_id": 2}],
                    rubric_snapshot={"pass_threshold": 0.8},
                    evaluator={"type": "deterministic"},
                ),
            ],
            {
                "evidence_stage": "practicing",
                "success_count": 0,
                "failure_count": 1,
                "latest_outcome": "failed",
            },
            id="self-report-versus-failed-quiz",
        ),
        pytest.param(
            "old-success-new-failure",
            [
                _fact(
                    1,
                    normalized_score=0.9,
                    artifact_refs=[{"artifact_id": 1}],
                    rubric_snapshot={"pass_threshold": 0.8},
                    evaluator={"type": "deterministic"},
                ),
                _fact(
                    2,
                    outcome="failed",
                    normalized_score=0.3,
                    occurred_offset=1,
                    artifact_refs=[{"artifact_id": 2}],
                    rubric_snapshot={"pass_threshold": 0.8},
                    evaluator={"type": "deterministic"},
                ),
            ],
            {
                "evidence_stage": "practicing",
                "success_count": 1,
                "failure_count": 1,
                "latest_outcome": "failed",
            },
            id="new-failure-downgrades-current-stage",
        ),
        pytest.param(
            "independent-transfer",
            [
                _fact(
                    1,
                    transfer_level="variant",
                    normalized_score=0.88,
                    artifact_refs=[{"artifact_id": 1}],
                    rubric_snapshot={"pass_threshold": 0.8},
                    evaluator={"type": "deterministic"},
                )
            ],
            {
                "evidence_stage": "demonstrated",
                "success_count": 1,
                "failure_count": 0,
                "latest_outcome": "passed",
            },
            id="independent-variant-transfer-remains-eligible",
        ),
    ],
)
def test_h4_evid_004_policy_boundaries_are_conservative_and_conflict_aware(
    name: str,
    facts: list[SimpleNamespace],
    expected: dict[str, Any],
):
    projection = build_evidence_state(facts)
    task = projection["by_task"][0]

    assert name
    assert {key: task[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_h4_evid_005_binary_snapshot_survives_reopen_and_detects_tampering():
    content = b"\x00\xffbinary\r\nanswer\x00"
    metadata = {"media_type": "application/octet-stream", "nested": {"v": 1}}
    async with AsyncSessionLocal() as db:
        artifact, created = await create_artifact(
            db,
            owner_id="local",
            artifact_type="file",
            source_uri="memory://h4/binary",
            idempotency_key="h4-evid-005:binary-reopen",
            content=content,
            metadata=metadata,
        )
        await db.commit()
        artifact_id = artifact.id

    async with AsyncSessionLocal() as db:
        reopened = await db.get(Artifact, artifact_id)
        assert reopened is not None
        resolved = await resolve_artifact_content(db, artifact_id, owner_id="local")
        actual = {
            "created": created,
            "snapshot": resolved,
            "snapshot_sha256": reopened.snapshot_sha256,
            "storage_state": reopened.storage_state,
            "envelope_version": reopened.envelope_version,
        }

    assert actual == {
        "created": True,
        "snapshot": content,
        "snapshot_sha256": hashlib.sha256(content).hexdigest(),
        "storage_state": "stored",
        "envelope_version": 1,
    }

    tampered = bytes([content[0] ^ 1]) + content[1:]
    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="artifacts are immutable"):
            await db.execute(
                update(Artifact)
                .where(Artifact.id == artifact_id)
                .values(snapshot_bytes=tampered)
            )
        await db.rollback()

    async with AsyncSessionLocal() as db:
        assert await resolve_artifact_content(
            db,
            artifact_id,
            owner_id="local",
        ) == content


@pytest.mark.parametrize(
    ("facts", "expected_code"),
    [
        pytest.param(
            [
                _fact(
                    1,
                    fact_kind="amendment",
                    target_observation_id=2,
                    reason_code="CYCLE_A",
                ),
                _fact(
                    2,
                    fact_kind="amendment",
                    target_observation_id=1,
                    reason_code="CYCLE_B",
                ),
            ],
            "EVIDENCE_SUPERSESSION_CYCLE",
            id="cycle",
        ),
        pytest.param(
            [
                _fact(1),
                _fact(
                    2,
                    fact_kind="amendment",
                    target_observation_id=1,
                    reason_code="BRANCH_A",
                ),
                _fact(
                    3,
                    fact_kind="amendment",
                    target_observation_id=1,
                    reason_code="BRANCH_B",
                ),
            ],
            "EVIDENCE_SUPERSESSION_BRANCH",
            id="multiple-successor-branches",
        ),
    ],
)
def test_h4_evid_002_audit_rejects_damaged_supersession_graphs(
    facts: list[SimpleNamespace],
    expected_code: str,
):
    report = audit_observations(facts)
    projection = build_evidence_state(facts)

    assert report.get("ok") is False
    assert report.get("error_codes") == [expected_code]
    assert projection.get("supersession_conflicts") == [expected_code]


@pytest.mark.parametrize(
    ("scope_patch", "expected_scope"),
    [
        pytest.param({"owner_id": "other"}, "owner", id="cross-owner"),
        pytest.param({"plan_id": 2}, "plan", id="cross-plan"),
        pytest.param({"task_id": 2}, "task", id="cross-task"),
    ],
)
def test_h4_evid_002_audit_rejects_cross_scope_supersession(
    scope_patch: dict[str, Any],
    expected_scope: str,
):
    child_kwargs = {
        "owner_id": "local",
        "plan_id": 1,
        "task_id": 1,
        **scope_patch,
    }
    facts = [
        _fact(1),
        _fact(
            2,
            fact_kind="amendment",
            target_observation_id=1,
            reason_code="CROSS_SCOPE",
            **child_kwargs,
        ),
    ]
    report = audit_observations(facts)

    assert report.get("ok") is False
    assert report.get("error_codes") == [
        f"EVIDENCE_SUPERSESSION_CROSS_{expected_scope.upper()}"
    ]


async def _add_plan(title: str, *, owner_id: str = "local") -> tuple[int, int]:
    async with AsyncSessionLocal() as db:
        task = Task(title=f"{title} task", position=0)
        plan = Plan(
            owner_id=owner_id,
            title=title,
            stages=[Stage(title=f"{title} stage", position=0, tasks=[task])],
        )
        db.add(plan)
        await db.commit()
        return plan.id, task.id


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["owner", "plan", "task"])
async def test_h4_evid_002_writer_rejects_cross_scope_amendments(scope: str):
    if scope == "owner":
        async with AsyncSessionLocal() as db:
            db.add(Owner(id="other", display_name="Other owner", timezone="UTC"))
            await db.commit()
    plan_a, task_a = await _add_plan("amendment target")
    plan_b, task_b = await _add_plan("foreign amendment scope")
    if scope == "task":
        async with AsyncSessionLocal() as db:
            plan = await db.get(Plan, plan_a)
            assert plan is not None
            stage_id = await db.scalar(
                select(Stage.id).where(Stage.plan_id == plan_a).limit(1)
            )
            extra = Task(stage_id=stage_id, title="other task", position=1)
            db.add(extra)
            await db.commit()
            task_b = extra.id

    async with AsyncSessionLocal() as db:
        target, _ = await append_observation(
            db,
            owner_id="local",
            source_type="manual",
            source_id=f"scope-target:{scope}",
            outcome="submitted",
            idempotency_key=f"scope-target:{scope}",
            plan_id=plan_a,
            task_id=task_a,
        )
        await db.commit()
        target_id = target.id

    request_scope = {
        "owner_id": "other" if scope == "owner" else "local",
        "plan_id": plan_b if scope == "plan" else plan_a,
        "task_id": task_b if scope in {"plan", "task"} else task_a,
    }
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="scope|owned|belong|target"):
            await append_observation(
                db,
                source_type="evidence_amendment",
                source_id=f"scope-child:{scope}",
                outcome="amended",
                idempotency_key=f"scope-child:{scope}",
                fact_kind="amendment",
                target_observation_id=target_id,
                reason_code="SCOPE_TEST",
                **request_scope,
            )
        await db.rollback()

    async with AsyncSessionLocal() as db:
        count = await db.scalar(
            select(func.count()).select_from(EvidenceObservation)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_h4_comp_001_same_key_nodes_are_distinct_and_emit_merge_candidate():
    plan_a, _ = await _add_plan("merge plan A")
    plan_b, _ = await _add_plan("merge plan B")
    async with AsyncSessionLocal() as db:
        first, first_created = await create_competency(
            db,
            "local",
            key="python.async",
            title="Async A",
            scope="plan",
            plan_id=plan_a,
            focused_plan_id=plan_a,
        )
        second, second_created = await create_competency(
            db,
            "local",
            key="python.async",
            title="Async B",
            scope="plan",
            plan_id=plan_b,
            focused_plan_id=plan_b,
        )
        await db.commit()
        ids = (first.id, second.id)

    assert first_created is True and second_created is True
    assert ids[0] != ids[1]

    reader = getattr(competency_service, "list_merge_candidates", None)
    assert callable(reader), "H4-COMP-001: merge-candidate reader is unavailable"
    async with AsyncSessionLocal() as db:
        candidates = reader(db, "local")
        if inspect.isawaitable(candidates):
            candidates = await candidates
    matching = [
        item
        for item in candidates
        if item.get("key") == "python.async"
        and set(item.get("competency_ids", [])) == set(ids)
    ]
    assert matching == [
        {
            "key": "python.async",
            "competency_ids": [ids[0], ids[1]],
            "plan_ids": [plan_a, plan_b],
            "status": "proposed",
        }
    ]
