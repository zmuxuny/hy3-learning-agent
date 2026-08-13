"""Append-only learning evidence and deterministic plan projections.

The v1 operational tables remain the source of truth for plan execution.  V2
adds this deliberately small fact layer so conversations, heartbeat runs, and
future reducers can read the same observations without inferring competence
from a mutable task status or a model-generated summary.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Artifact, EvidenceObservation, LearningEvent, Quiz, TaskSubmission


EVIDENCE_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


async def create_artifact(
    db: AsyncSession,
    *,
    owner_id: str,
    artifact_type: str,
    source_uri: str,
    idempotency_key: str,
    title: str = "",
    content: str | bytes | None = None,
    metadata: dict[str, Any] | None = None,
    size_bytes: int | None = None,
    plan_id: int | None = None,
    task_id: int | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
) -> tuple[Artifact, bool]:
    """Register an immutable source artifact and return a stable reference."""

    existing = await db.scalar(
        select(Artifact).where(
            Artifact.owner_id == owner_id,
            Artifact.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False
    raw = content.encode("utf-8") if isinstance(content, str) else content
    # File/link-only submissions still need a stable source fingerprint.  In
    # that case hash the canonical metadata (and finally the URI) instead of
    # leaving an unverifiable empty hash in the evidence ledger.
    hash_input = raw if raw is not None else _stable_json(metadata or {"source_uri": source_uri}).encode("utf-8")
    artifact = Artifact(
        owner_id=owner_id,
        artifact_type=artifact_type,
        source_uri=source_uri,
        title=title,
        content_hash=hashlib.sha256(hash_input).hexdigest(),
        size_bytes=size_bytes if size_bytes is not None else len(hash_input),
        artifact_metadata=metadata or {},
        plan_id=plan_id,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
    )
    db.add(artifact)
    await db.flush()
    return artifact, True


def artifact_ref(artifact: Artifact, *, kind: str | None = None) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "kind": kind or artifact.artifact_type,
        "uri": artifact.source_uri,
        "content_hash": artifact.content_hash,
    }


async def append_observation(
    db: AsyncSession,
    *,
    owner_id: str,
    source_type: str,
    source_id: str,
    outcome: str,
    idempotency_key: str,
    run_id: str | None = None,
    session_id: str | None = None,
    plan_id: int | None = None,
    task_id: int | None = None,
    competency_id: int | None = None,
    competency_key: str | None = None,
    normalized_score: float | None = None,
    is_correct: bool | None = None,
    assistance_level: str = "unknown",
    transfer_level: str = "unknown",
    rubric_snapshot: dict[str, Any] | None = None,
    evaluator: dict[str, Any] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    supersedes_id: int | None = None,
) -> tuple[EvidenceObservation, bool]:
    """Append one observation, returning ``(observation, created)``.

    Idempotency is checked before insertion and the key is also protected by a
    unique database constraint.  Callers may safely retry after a process
    interruption; the original fact is returned instead of being duplicated.
    """

    existing = await db.scalar(
        select(EvidenceObservation).where(
            EvidenceObservation.owner_id == owner_id,
            EvidenceObservation.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False

    score = None if normalized_score is None else max(0.0, min(1.0, float(normalized_score)))
    normalized_artifact_refs = artifact_refs or []
    observation = EvidenceObservation(
        owner_id=owner_id,
        source_type=source_type,
        source_id=str(source_id),
        run_id=run_id,
        session_id=session_id,
        plan_id=plan_id,
        task_id=task_id,
        competency_id=competency_id,
        competency_key=competency_key,
        outcome=outcome,
        normalized_score=score,
        is_correct=is_correct,
        assistance_level=assistance_level,
        transfer_level=transfer_level,
        rubric_snapshot=rubric_snapshot or {},
        evaluator=evaluator or {},
        artifact_refs=normalized_artifact_refs,
        payload=payload or {},
        occurred_at=occurred_at or _utc_now(),
        recorded_at=_utc_now(),
        schema_version=EVIDENCE_SCHEMA_VERSION,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        supersedes_id=supersedes_id,
    )
    db.add(observation)
    await db.flush()
    return observation, True


def observation_dict(observation: EvidenceObservation) -> dict[str, Any]:
    return {
        "id": observation.id,
        "source_type": observation.source_type,
        "source_id": observation.source_id,
        "run_id": observation.run_id,
        "session_id": observation.session_id,
        "plan_id": observation.plan_id,
        "task_id": observation.task_id,
        "competency_key": observation.competency_key,
        "outcome": observation.outcome,
        "normalized_score": observation.normalized_score,
        "is_correct": observation.is_correct,
        "assistance_level": observation.assistance_level,
        "transfer_level": observation.transfer_level,
        "rubric_snapshot": observation.rubric_snapshot,
        "evaluator": observation.evaluator,
        "artifact_refs": observation.artifact_refs,
        "payload": observation.payload,
        "occurred_at": _iso(observation.occurred_at),
        "recorded_at": _iso(observation.recorded_at),
        "schema_version": observation.schema_version,
        "correlation_id": observation.correlation_id,
        "causation_id": observation.causation_id,
        "idempotency_key": observation.idempotency_key,
        "supersedes_id": observation.supersedes_id,
        "invalidated_at": _iso(observation.invalidated_at),
        "invalidation_reason": observation.invalidation_reason,
    }


async def list_observations(
    db: AsyncSession,
    owner_id: str,
    *,
    plan_id: int | None = None,
    task_id: int | None = None,
    limit: int = 200,
) -> list[EvidenceObservation]:
    query = select(EvidenceObservation).where(EvidenceObservation.owner_id == owner_id)
    if plan_id is not None:
        query = query.where(EvidenceObservation.plan_id == plan_id)
    if task_id is not None:
        query = query.where(EvidenceObservation.task_id == task_id)
    result = await db.execute(
        query.order_by(EvidenceObservation.occurred_at.desc(), EvidenceObservation.id.desc()).limit(limit)
    )
    return list(result.scalars())


def _projection_records(observations: list[EvidenceObservation]) -> list[EvidenceObservation]:
    superseded = {
        item.supersedes_id
        for item in observations
        if item.supersedes_id is not None and item.invalidated_at is None
    }
    return [
        item
        for item in observations
        if item.invalidated_at is None and item.id not in superseded
    ]


def _task_stage(records: list[EvidenceObservation]) -> str:
    """Return a conservative, evidence-only stage (not a mastery claim)."""

    if not records:
        return "unknown"
    outcomes = {item.outcome for item in records}
    if outcomes.intersection({"accepted", "passed", "verified"}):
        return "demonstrated"
    if outcomes.intersection({"submitted", "attempted", "needs_revision", "failed"}):
        return "practicing"
    return "exposed"


def build_evidence_state(observations: list[EvidenceObservation]) -> dict[str, Any]:
    """Build a deterministic, compact projection from immutable observations."""

    records = _projection_records(observations)
    grouped: dict[str, list[EvidenceObservation]] = defaultdict(list)
    unscoped: list[EvidenceObservation] = []
    for item in records:
        if item.task_id is None:
            unscoped.append(item)
        else:
            grouped[str(item.task_id)].append(item)

    def canonical(item: EvidenceObservation) -> dict[str, Any]:
        return {
            "id": item.id,
            "source_type": item.source_type,
            "source_id": item.source_id,
            "task_id": item.task_id,
            "outcome": item.outcome,
            "normalized_score": item.normalized_score,
            "is_correct": item.is_correct,
            "assistance_level": item.assistance_level,
            "transfer_level": item.transfer_level,
            "occurred_at": _iso(item.occurred_at),
            "supersedes_id": item.supersedes_id,
        }

    digest_payload = sorted((canonical(item) for item in records), key=lambda value: value["id"])
    digest = hashlib.sha256(_stable_json(digest_payload).encode("utf-8")).hexdigest()

    by_task: list[dict[str, Any]] = []
    for task_key, task_records in sorted(grouped.items(), key=lambda item: int(item[0])):
        scores = [item.normalized_score for item in task_records if item.normalized_score is not None]
        successful = sum(item.outcome in {"accepted", "passed", "verified"} for item in task_records)
        failed = sum(item.outcome in {"failed", "needs_revision"} for item in task_records)
        latest = max(task_records, key=lambda item: (item.occurred_at, item.id))
        by_task.append({
            "task_id": int(task_key),
            "evidence_stage": _task_stage(task_records),
            "observation_count": len(task_records),
            "success_count": successful,
            "failure_count": failed,
            "best_score": max(scores) if scores else None,
            "last_observed_at": _iso(latest.occurred_at),
            "latest_outcome": latest.outcome,
            "evidence_ids": [item.id for item in sorted(task_records, key=lambda item: item.id)],
        })

    return {
        "schema_version": 1,
        "algorithm_version": "evidence-summary-v1",
        "digest": digest,
        "observation_count": len(records),
        "task_count": len(by_task),
        "by_task": by_task,
        "unscoped_observation_ids": [item.id for item in sorted(unscoped, key=lambda item: item.id)],
        "caveats": [
            "这是基于已记录证据的保守摘要，不等同于掌握度概率。",
            "一次自述、打勾或未附证据的任务完成不会进入已证明阶段。",
            "技能图和复习算法将在后续 V2 里程碑中建立。",
        ],
    }


async def build_plan_evidence_state(
    db: AsyncSession,
    owner_id: str,
    plan_id: int,
    *,
    limit: int = 500,
) -> dict[str, Any]:
    observations = await list_observations(db, owner_id, plan_id=plan_id, limit=limit)
    return build_evidence_state(observations)


async def backfill_legacy_observations(
    db: AsyncSession,
    owner_id: str,
    *,
    plan_id: int | None = None,
) -> dict[str, int]:
    """Conservatively dual-write evidence that predates the v2 table.

    Only explicit submissions, verdicts, graded quizzes, and task events that
    already carry an evidence payload are imported.  No skill or mastery state
    is inferred from a checkbox, a plan percentage, or free-form summaries.
    The same keys used by live writes make the command safe to rerun.
    """

    created = 0
    skipped = 0

    submission_query = select(TaskSubmission).where(TaskSubmission.owner_id == owner_id)
    if plan_id is not None:
        submission_query = submission_query.where(TaskSubmission.plan_id == plan_id)
    submissions = list((await db.execute(submission_query.order_by(TaskSubmission.id))).scalars())
    for submission in submissions:
        submission_artifact, _ = await create_artifact(
            db,
            owner_id=owner_id,
            artifact_type="submission",
            source_uri=f"submission:{submission.id}",
            idempotency_key=f"submission:{submission.id}:artifact",
            title=f"历史提交：{submission.task_id}",
            content=submission.content,
            metadata={"submission_type": submission.submission_type, "artifacts": submission.artifacts},
            plan_id=submission.plan_id,
            task_id=submission.task_id,
            run_id=submission.run_id,
        )
        _, was_created = await append_observation(
            db,
            owner_id=owner_id,
            source_type="submission",
            source_id=str(submission.id),
            outcome="submitted",
            idempotency_key=f"submission:{submission.id}:created",
            run_id=submission.run_id,
            plan_id=submission.plan_id,
            task_id=submission.task_id,
            payload={
                "submission_type": submission.submission_type,
                "artifact_count": len(submission.artifacts or []),
                "has_text": bool((submission.content or "").strip()),
                "backfilled": True,
            },
            artifact_refs=[artifact_ref(submission_artifact, kind="submission")],
            occurred_at=submission.created_at or _utc_now(),
            correlation_id=submission.run_id,
        )
        created += int(was_created)
        skipped += int(not was_created)
        if submission.status == "submitted":
            continue
        _, was_created = await append_observation(
            db,
            owner_id=owner_id,
            source_type="submission",
            source_id=f"{submission.id}:check",
            outcome="accepted" if submission.status == "accepted" else "needs_revision",
            idempotency_key=f"submission:{submission.id}:checked",
            run_id=submission.run_id,
            plan_id=submission.plan_id,
            task_id=submission.task_id,
            normalized_score=(submission.score / 100) if submission.score is not None else None,
            is_correct=submission.status == "accepted",
            payload={"feedback": submission.feedback or "", "backfilled": True},
            artifact_refs=[artifact_ref(submission_artifact, kind="submission")],
            occurred_at=submission.checked_at or submission.created_at or _utc_now(),
            correlation_id=submission.run_id,
        )
        created += int(was_created)
        skipped += int(not was_created)

    quiz_query = select(Quiz).where(Quiz.owner_id == owner_id, Quiz.status != "open")
    if plan_id is not None:
        quiz_query = quiz_query.where(Quiz.plan_id == plan_id)
    quizzes = list((await db.execute(quiz_query.order_by(Quiz.id))).scalars())
    for quiz in quizzes:
        quiz_artifact, _ = await create_artifact(
            db,
            owner_id=owner_id,
            artifact_type="quiz_answer",
            source_uri=f"quiz:{quiz.id}:answer",
            idempotency_key=f"quiz:{quiz.id}:answer:artifact",
            title=f"历史测验回答：{quiz.id}",
            content=quiz.answer,
            metadata={"quiz_id": quiz.id, "evidence": quiz.evidence},
            plan_id=quiz.plan_id,
            task_id=quiz.task_id,
            run_id=quiz.run_id,
        )
        _, was_created = await append_observation(
            db,
            owner_id=owner_id,
            source_type="quiz",
            source_id=f"{quiz.id}:grade",
            outcome="passed" if quiz.status == "passed" else "needs_revision",
            idempotency_key=f"quiz:{quiz.id}:graded",
            run_id=quiz.run_id,
            plan_id=quiz.plan_id,
            task_id=quiz.task_id,
            normalized_score=(quiz.score / 100) if quiz.score is not None else None,
            is_correct=quiz.status == "passed",
            rubric_snapshot=quiz.rubric or {},
            evaluator={"type": "legacy_record", "backfilled": True},
            payload={"evidence": quiz.evidence or []},
            artifact_refs=[artifact_ref(quiz_artifact, kind="quiz_answer")],
            occurred_at=quiz.graded_at or quiz.created_at or _utc_now(),
            correlation_id=quiz.run_id,
        )
        created += int(was_created)
        skipped += int(not was_created)

    event_query = select(LearningEvent).where(
        LearningEvent.owner_id == owner_id,
        LearningEvent.event_type == "task.updated",
    )
    if plan_id is not None:
        event_query = event_query.where(LearningEvent.plan_id == plan_id)
    events = list((await db.execute(event_query.order_by(LearningEvent.id))).scalars())
    for event in events:
        payload = event.payload or {}
        evidence = payload.get("evidence")
        if not evidence or event.task_id is None:
            continue
        after = payload.get("after") or {}
        completion_artifact, _ = await create_artifact(
            db,
            owner_id=owner_id,
            artifact_type="task_evidence",
            source_uri=f"task:{event.task_id}:event:{event.id}",
            idempotency_key=f"task:{event.task_id}:event:{event.id}:artifact",
            title=f"历史任务证据：{event.task_id}",
            content=_stable_json(evidence),
            metadata={"task_id": event.task_id, "event_id": event.id},
            plan_id=event.plan_id,
            task_id=event.task_id,
            run_id=event.run_id,
        )
        _, was_created = await append_observation(
            db,
            owner_id=owner_id,
            source_type="task_completion",
            source_id=f"task:{event.task_id}:event:{event.id}",
            outcome="verified" if after.get("status") == "completed" else "observed",
            idempotency_key=f"task:{event.task_id}:event:{event.id}:evidence",
            run_id=event.run_id,
            plan_id=event.plan_id,
            task_id=event.task_id,
            payload={"evidence": evidence, "backfilled": True},
            artifact_refs=[artifact_ref(completion_artifact, kind="task_evidence")],
            occurred_at=event.occurred_at or event.created_at or _utc_now(),
            correlation_id=event.run_id,
            causation_id=f"learning_event:{event.id}",
        )
        created += int(was_created)
        skipped += int(not was_created)

    await db.commit()
    return {"created": created, "skipped": skipped}


def audit_observations(
    observations: list[EvidenceObservation],
    artifacts: Iterable[Artifact] | None = None,
) -> dict[str, Any]:
    """Run cheap, provider-independent integrity checks for the evidence ledger."""

    errors: list[str] = []
    seen_keys: set[str] = set()
    observation_ids = {item.id for item in observations}
    artifact_by_id = {item.id: item for item in artifacts} if artifacts is not None else None
    for item in observations:
        if not item.idempotency_key:
            errors.append(f"observation:{item.id} missing idempotency_key")
        elif item.idempotency_key in seen_keys:
            errors.append(f"duplicate idempotency_key:{item.idempotency_key}")
        else:
            seen_keys.add(item.idempotency_key)
        if not item.source_type or not item.source_id:
            errors.append(f"observation:{item.id} missing source identity")
        if item.source_type in {"submission", "quiz", "task_completion", "code_run", "file"} and not item.artifact_refs:
            errors.append(f"observation:{item.id} missing artifact reference")
        for ref in item.artifact_refs or []:
            if not isinstance(ref, dict) or not ref.get("artifact_id"):
                errors.append(f"observation:{item.id} has malformed artifact reference")
                continue
            if artifact_by_id is not None:
                artifact_id = ref["artifact_id"]
                artifact = artifact_by_id.get(artifact_id)
                if artifact is None:
                    errors.append(f"observation:{item.id} references missing artifact:{artifact_id}")
                else:
                    if ref.get("content_hash") and ref["content_hash"] != artifact.content_hash:
                        errors.append(f"observation:{item.id} artifact hash mismatch:{artifact_id}")
                    if ref.get("uri") and ref["uri"] != artifact.source_uri:
                        errors.append(f"observation:{item.id} artifact uri mismatch:{artifact_id}")
        if item.normalized_score is not None and not 0 <= item.normalized_score <= 1:
            errors.append(f"observation:{item.id} score outside [0,1]")
        if item.invalidated_at is not None and not item.invalidation_reason.strip():
            errors.append(f"observation:{item.id} invalidated without reason")
        if item.supersedes_id == item.id:
            errors.append(f"observation:{item.id} supersedes itself")
        elif item.supersedes_id is not None and item.supersedes_id not in observation_ids:
            errors.append(f"observation:{item.id} supersedes missing:{item.supersedes_id}")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "observation_count": len(observations),
        "unique_idempotency_keys": len(seen_keys),
        "ok": not errors,
        "errors": errors,
    }
