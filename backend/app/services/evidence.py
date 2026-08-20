"""Append-only learning evidence and deterministic plan projections.

The v1 operational tables remain the source of truth for plan execution.  V2
adds this deliberately small fact layer so conversations, heartbeat runs, and
future reducers can read the same observations without inferring competence
from a mutable task status or a model-generated summary.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.uow import ensure_sqlite_write_transaction, flush as flush_uow
from app.models import (
    AgentRun,
    Artifact,
    Competency,
    EvidenceArtifactLink,
    EvidenceCompetencyLink,
    EvidenceObservation,
    EvidenceProjectionState,
    LearningEvent,
    Operation,
    OperationEvidenceLink,
    Plan,
    Quiz,
    Session,
    Stage,
    Task,
    TaskCompetencyLink,
    TaskSubmission,
)


EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_POLICY_VERSION = "evidence-eligibility-v1"
EVIDENCE_ALGORITHM_VERSION = "evidence-summary-v3-append-only"

_SUCCESS_OUTCOMES = {"accepted", "passed", "verified"}
_FAILURE_OUTCOMES = {"failed", "needs_revision", "revision_required"}
_PRACTICE_OUTCOMES = {"submitted", "attempted", *_FAILURE_OUTCOMES}
_LOW_CONFIDENCE_SOURCES = {"self_report", "chat", "conversation", "message"}
_LOW_CONFIDENCE_KINDS = {"checkbox", "text", "free_text", "self_report"}
_ASSISTED_LEVELS = {"assisted", "guided", "hint", "full"}
_STAGE_RANK = {"unknown": 0, "exposed": 1, "practicing": 2, "demonstrated": 3}


@dataclass(frozen=True)
class EligibilityDecision:
    stage: str
    reason: str
    counts_as_success: bool


class _ObservationIdentityConflict(Exception):
    """Internal sentinel: only the primary fact identity insert conflicted."""


def _iso(value: datetime | None) -> str | None:
    return canonical_utc(value)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _request_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def request_digest_matches(stored: str | None, incoming: str) -> bool:
    """Return whether an immutable idempotency key still names exact content."""

    return isinstance(stored, str) and stored == incoming


def normalize_percentage_score(value: float | int) -> float:
    """Convert an inclusive 0..100 producer score to a normalized 0..1 score."""

    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("Evidence percentage score must be between 0 and 100")
    return score / 100


def _content_value(raw: bytes | None, *, artifact_type: str) -> Any:
    if raw is None:
        return None
    if artifact_type != "file":
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    import base64

    return {
        "encoding": "base64",
        "data": base64.b64encode(raw).decode("ascii"),
    }


def _artifact_envelope_bytes(
    raw: bytes | None,
    metadata: dict[str, Any],
    *,
    artifact_type: str,
) -> bytes:
    return _stable_json(
        {
            "content": _content_value(raw, artifact_type=artifact_type),
            "metadata": metadata,
        }
    ).encode("utf-8")


def _file_uri_path(source_uri: str) -> Path:
    parsed = urlparse(source_uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("File Artifact source_uri must be a local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError("File Artifact source_uri must be absolute")
    return path


def _snapshot_artifact_content(
    *,
    artifact_type: str,
    source_uri: str,
    content: str | bytes | None,
) -> tuple[bytes | None, str]:
    if isinstance(content, str):
        return content.encode("utf-8"), "stored"
    if isinstance(content, bytes):
        return bytes(content), "stored"
    if artifact_type != "file":
        return None, "external_reference"

    # File IO is deliberately completed before the service acquires a SQLite
    # write transaction. The database stores the resulting immutable bytes;
    # the resolver never returns to this mutable source path.
    path = _file_uri_path(source_uri)
    with path.open("rb") as stream:
        before = path.stat()
        raw = stream.read()
        after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(raw) != after.st_size:
        raise OSError("File Artifact source changed while it was being snapshotted")
    return raw, "stored"


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

    normalized_metadata = metadata or {}
    if artifact_type == "file" and content is None and db.in_transaction():
        raise RuntimeError(
            "File Artifact bytes must be snapshotted before opening a database transaction"
        )
    raw, storage_state = _snapshot_artifact_content(
        artifact_type=artifact_type,
        source_uri=source_uri,
        content=content,
    )
    if raw is not None and size_bytes is not None and size_bytes != len(raw):
        raise ValueError("Artifact size_bytes does not match the immutable snapshot")
    effective_size = len(raw) if raw is not None else size_bytes
    envelope = _artifact_envelope_bytes(
        raw,
        normalized_metadata,
        artifact_type=artifact_type,
    )
    content_hash = hashlib.sha256(envelope).hexdigest()
    snapshot_sha256 = hashlib.sha256(raw).hexdigest() if raw is not None else None
    request_digest = _request_digest(
        {
            "owner_id": owner_id,
            "artifact_type": artifact_type,
            "source_uri": source_uri,
            "title": title,
            "content_hash": content_hash,
            "metadata": normalized_metadata,
            "size_bytes": effective_size,
            "storage_state": storage_state,
            "envelope_version": 1,
            "plan_id": plan_id,
            "task_id": task_id,
            "run_id": run_id,
            "session_id": session_id,
        }
    )
    await _validate_observation_scope(
        db,
        owner_id=owner_id,
        plan_id=plan_id,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    await ensure_sqlite_write_transaction(db)
    existing = await db.scalar(
        select(Artifact).where(
            Artifact.owner_id == owner_id,
            Artifact.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if not request_digest_matches(existing.request_digest, request_digest):
            raise ValueError(
                "Artifact idempotency conflict: the key was already used for a different request"
            )
        return existing, False
    artifact = Artifact(
        owner_id=owner_id,
        artifact_type=artifact_type,
        source_uri=source_uri,
        title=title,
        content_hash=content_hash,
        size_bytes=effective_size,
        artifact_metadata=normalized_metadata,
        snapshot_bytes=raw,
        snapshot_sha256=snapshot_sha256,
        storage_state=storage_state,
        envelope_version=1,
        plan_id=plan_id,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
    )
    try:
        # Keep the unique insert in its own savepoint. A concurrent exact
        # winner can be reloaded without rolling back unrelated domain facts
        # already staged by this caller's larger Unit of Work.
        async with db.begin_nested():
            db.add(artifact)
            await flush_uow(db)
        return artifact, True
    except IntegrityError:
        existing = await db.scalar(
            select(Artifact).where(
                Artifact.owner_id == owner_id,
                Artifact.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        if not request_digest_matches(existing.request_digest, request_digest):
            raise ValueError(
                "Artifact idempotency conflict: the key was already used for a different request"
            )
        return existing, False


async def resolve_artifact_content(
    db: AsyncSession,
    artifact_id: int,
    *,
    owner_id: str | None = None,
) -> bytes:
    artifact = await db.get(Artifact, artifact_id)
    if artifact is None or (owner_id is not None and artifact.owner_id != owner_id):
        raise FileNotFoundError("Artifact not found")
    if artifact.storage_state != "stored" or artifact.snapshot_bytes is None:
        raise FileNotFoundError("Artifact has no durable content snapshot")
    raw = _verified_artifact_bytes(artifact)
    return raw


def _verified_artifact_bytes(artifact: Artifact) -> bytes:
    if artifact.storage_state != "stored" or artifact.snapshot_bytes is None:
        raise FileNotFoundError("Artifact has no durable content snapshot")
    raw = bytes(artifact.snapshot_bytes)
    if artifact.size_bytes is not None and len(raw) != artifact.size_bytes:
        raise ValueError("Artifact snapshot size mismatch")
    if hashlib.sha256(raw).hexdigest() != artifact.snapshot_sha256:
        raise ValueError("Artifact snapshot hash mismatch")
    envelope_hash = hashlib.sha256(
        _artifact_envelope_bytes(
            raw,
            artifact.artifact_metadata or {},
            artifact_type=artifact.artifact_type,
        )
    ).hexdigest()
    if envelope_hash != artifact.content_hash:
        raise ValueError("Artifact canonical envelope hash mismatch")
    return raw


def artifact_ref(artifact: Artifact, *, kind: str | None = None) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "kind": kind or artifact.artifact_type,
        "uri": artifact.source_uri,
        "content_hash": artifact.content_hash,
    }


def _eligibility_decision(
    *,
    source_type: str,
    outcome: str,
    assistance_level: str,
    rubric_snapshot: dict[str, Any],
    evaluator: dict[str, Any],
    artifact_refs: list[dict[str, Any]],
    payload: dict[str, Any],
) -> EligibilityDecision:
    if source_type in _LOW_CONFIDENCE_SOURCES:
        return EligibilityDecision("exposed", "LOW_CONFIDENCE_SOURCE", False)
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    evidence_kinds = {
        str(item.get("kind", "")).strip().lower()
        for item in (evidence or [])
        if isinstance(item, dict)
    }
    if source_type in {"task_completion", "task_evidence"} and (
        not evidence_kinds or evidence_kinds.intersection(_LOW_CONFIDENCE_KINDS)
    ):
        return EligibilityDecision("exposed", "UNVERIFIED_TASK_CLAIM", False)
    if outcome in _FAILURE_OUTCOMES:
        return EligibilityDecision("practicing", "LATEST_FAILED_ATTEMPT", False)
    if outcome in {"submitted", "attempted", "needs_revision", "revision_required"}:
        return EligibilityDecision("practicing", "ATTEMPT_RECORDED", False)
    if outcome not in _SUCCESS_OUTCOMES:
        return EligibilityDecision("exposed", "NON_ASSESSMENT_OBSERVATION", False)
    if assistance_level in _ASSISTED_LEVELS:
        return EligibilityDecision("practicing", "ASSISTED_SUCCESS_CAPPED", True)
    if source_type in {"submission", "quiz", "task_completion", "task_evidence"} and not artifact_refs:
        return EligibilityDecision("practicing", "MISSING_DURABLE_ARTIFACT", False)
    if source_type in {"submission", "quiz"} and not rubric_snapshot:
        return EligibilityDecision("practicing", "MISSING_RUBRIC", False)
    if source_type in {"submission", "quiz", "task_completion", "task_evidence"} and not evaluator:
        return EligibilityDecision("practicing", "MISSING_EVALUATOR", False)
    return EligibilityDecision("demonstrated", "VERIFIED_PRIMARY_SUCCESS", True)


async def _validate_observation_scope(
    db: AsyncSession,
    *,
    owner_id: str,
    plan_id: int | None,
    task_id: int | None,
    run_id: str | None,
    session_id: str | None,
) -> None:
    if task_id is not None:
        task_scope = await db.execute(
            select(Plan.owner_id, Stage.plan_id)
            .join(Stage, Stage.plan_id == Plan.id)
            .join(Task, Task.stage_id == Stage.id)
            .where(Task.id == task_id)
        )
        row = task_scope.one_or_none()
        if row is None or row.owner_id != owner_id:
            raise ValueError("Evidence task scope is not owned by the caller")
        if plan_id is None or row.plan_id != plan_id:
            raise ValueError("Evidence task does not belong to the selected plan")
    elif plan_id is not None:
        owned = await db.scalar(
            select(Plan.id).where(Plan.id == plan_id, Plan.owner_id == owner_id)
        )
        if owned is None:
            raise ValueError("Evidence plan scope is not owned by the caller")
    if run_id is not None:
        run = await db.get(AgentRun, run_id)
        if run is None or run.owner_id != owner_id:
            raise ValueError("Evidence run scope is not owned by the caller")
        if run.plan_id is not None and run.plan_id != plan_id:
            raise ValueError("Evidence run belongs to another plan")
        if run.session_id is not None and run.session_id != session_id:
            raise ValueError("Evidence run belongs to another session")
    if session_id is not None:
        session = await db.get(Session, session_id)
        if session is None or session.owner_id != owner_id:
            raise ValueError("Evidence session scope is not owned by the caller")
        if session.plan_id is not None and session.plan_id != plan_id:
            raise ValueError("Evidence session belongs to another plan")


def _normalized_ref_input(ref: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(ref, dict) or not isinstance(ref.get("artifact_id"), int):
        raise ValueError("Evidence Artifact reference requires an integer artifact_id")
    return {
        "artifact_id": ref["artifact_id"],
        "kind": str(ref.get("kind") or "artifact"),
        "uri": ref.get("uri"),
        "content_hash": ref.get("content_hash"),
    }


async def _resolve_artifact_references(
    db: AsyncSession,
    *,
    owner_id: str,
    plan_id: int | None,
    task_id: int | None,
    artifact_refs: list[dict[str, Any]],
) -> list[tuple[Artifact, str]]:
    resolved: list[tuple[Artifact, str]] = []
    seen: set[tuple[int, str]] = set()
    for raw_ref in artifact_refs:
        ref = _normalized_ref_input(raw_ref)
        artifact = await db.get(Artifact, ref["artifact_id"])
        if artifact is None or artifact.owner_id != owner_id:
            raise ValueError("Evidence Artifact reference is not owned by the caller")
        if artifact.plan_id != plan_id or artifact.task_id != task_id:
            raise ValueError("Evidence Artifact reference has a different plan/task scope")
        if ref["content_hash"] is not None and ref["content_hash"] != artifact.content_hash:
            raise ValueError("Evidence Artifact content hash mismatch")
        if ref["uri"] is not None and ref["uri"] != artifact.source_uri:
            raise ValueError("Evidence Artifact source URI mismatch")
        identity = (artifact.id, ref["kind"])
        if identity in seen:
            raise ValueError("Evidence Artifact reference is duplicated")
        seen.add(identity)
        resolved.append((artifact, ref["kind"]))
    return resolved


async def _resolve_competency_snapshots(
    db: AsyncSession,
    *,
    owner_id: str,
    plan_id: int | None,
    task_id: int | None,
    competency_ids: list[int],
    include_task_assesses: bool = True,
) -> list[tuple[Competency, str, int | None]]:
    snapshots: dict[int, tuple[Competency, str, int | None]] = {}
    if competency_ids:
        competencies = list(
            (
                await db.execute(
                    select(Competency).where(
                        Competency.owner_id == owner_id,
                        Competency.id.in_(competency_ids),
                    )
                )
            ).scalars()
        )
        by_id = {item.id: item for item in competencies}
        if any(item not in by_id for item in competency_ids):
            raise ValueError("Evidence competency is not owned by the caller")
        for competency_id in competency_ids:
            competency = by_id[competency_id]
            if competency.scope == "plan" and competency.plan_id != plan_id:
                raise ValueError("Evidence competency belongs to another plan")
            snapshots[competency.id] = (competency, "explicit", None)
    if task_id is not None and include_task_assesses:
        assesses = list(
            (
                await db.execute(
                    select(TaskCompetencyLink, Competency)
                    .join(Competency, Competency.id == TaskCompetencyLink.competency_id)
                    .where(
                        TaskCompetencyLink.owner_id == owner_id,
                        TaskCompetencyLink.task_id == task_id,
                        TaskCompetencyLink.relation == "assesses",
                        Competency.owner_id == owner_id,
                    )
                    .order_by(TaskCompetencyLink.id)
                )
            ).all()
        )
        for link, competency in assesses:
            if competency.scope == "plan" and competency.plan_id != plan_id:
                raise ValueError("Task assesses mapping crosses Evidence plan scope")
            snapshots.setdefault(
                competency.id,
                (competency, "task_assesses", link.id),
            )
    return [snapshots[key] for key in sorted(snapshots)]


async def _persist_evidence_links(
    db: AsyncSession,
    observation: EvidenceObservation,
    *,
    artifacts: list[tuple[Artifact, str]],
    competencies: list[tuple[Competency, str, int | None]],
) -> None:
    for ordinal, (artifact, kind) in enumerate(artifacts):
        db.add(
            EvidenceArtifactLink(
                observation_id=observation.id,
                artifact_id=artifact.id,
                kind=kind,
                ordinal=ordinal,
                content_hash_snapshot=artifact.content_hash,
            )
        )
    for competency, association_kind, task_link_id in competencies:
        db.add(
            EvidenceCompetencyLink(
                observation_id=observation.id,
                competency_id=competency.id,
                association_kind=association_kind,
                task_competency_link_id_snapshot=task_link_id,
                competency_key_snapshot=competency.key,
            )
        )
    await flush_uow(db)


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
    competency_ids: list[int] | None = None,
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
    fact_kind: str = "observation",
    target_observation_id: int | None = None,
    reason_code: str = "",
    evidence_role: str = "primary",
    include_task_assesses: bool = True,
    _competency_snapshot_overrides: list[
        tuple[Competency, str, int | None]
    ] | None = None,
    refresh_projection: bool = False,
) -> tuple[EvidenceObservation, bool]:
    """Append one observation, returning ``(observation, created)``.

    Idempotency is checked before insertion and the key is also protected by a
    unique database constraint.  Callers may safely retry after a process
    interruption; the original fact is returned instead of being duplicated.
    """

    score = None if normalized_score is None else float(normalized_score)
    if score is not None and not 0 <= score <= 1:
        raise ValueError("Evidence normalized_score must be between 0 and 1")
    normalized_artifact_refs = [_normalized_ref_input(item) for item in (artifact_refs or [])]
    normalized_payload = payload or {}
    normalized_rubric = rubric_snapshot or {}
    normalized_evaluator = evaluator or {}
    decision = _eligibility_decision(
        source_type=source_type,
        outcome=outcome,
        assistance_level=assistance_level,
        rubric_snapshot=normalized_rubric,
        evaluator=normalized_evaluator,
        artifact_refs=normalized_artifact_refs,
        payload=normalized_payload,
    )
    requested_competencies = list(dict.fromkeys([
        *([competency_id] if competency_id is not None else []),
        *(competency_ids or []),
    ]))
    snapshot_override_digest = [
        {
            "competency_id": competency.id,
            "competency_key": competency.key,
            "association_kind": association_kind,
            "task_competency_link_id_snapshot": task_link_id,
        }
        for competency, association_kind, task_link_id in (
            _competency_snapshot_overrides or []
        )
    ]
    normalized_occurred_at = canonical_utc(occurred_at) if occurred_at is not None else "auto"
    request_digest = _request_digest(
        {
            "owner_id": owner_id,
            "source_type": source_type,
            "source_id": str(source_id),
            "outcome": outcome,
            "run_id": run_id,
            "session_id": session_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "competency_ids": requested_competencies,
            "normalized_score": score,
            "is_correct": is_correct,
            "assistance_level": assistance_level,
            "transfer_level": transfer_level,
            "rubric_snapshot": normalized_rubric,
            "evaluator": normalized_evaluator,
            "artifact_refs": normalized_artifact_refs,
            "payload": normalized_payload,
            "occurred_at": normalized_occurred_at,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "fact_kind": fact_kind,
            "target_observation_id": target_observation_id,
            "reason_code": reason_code,
            "evidence_role": evidence_role,
            "include_task_assesses": include_task_assesses,
            "competency_snapshot_overrides": snapshot_override_digest,
            "eligibility_policy_version": EVIDENCE_POLICY_VERSION,
        }
    )
    await ensure_sqlite_write_transaction(db)
    existing = await db.scalar(
        select(EvidenceObservation).where(
            EvidenceObservation.owner_id == owner_id,
            EvidenceObservation.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if not request_digest_matches(existing.request_digest, request_digest):
            raise ValueError(
                "Evidence idempotency conflict: the key was already used for a different request"
            )
        return existing, False

    await _validate_observation_scope(
        db,
        owner_id=owner_id,
        plan_id=plan_id,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    target: EvidenceObservation | None = None
    if fact_kind == "observation":
        if target_observation_id is not None:
            raise ValueError("Primary Evidence observations cannot target another fact")
    else:
        if target_observation_id is None or not reason_code.strip():
            raise ValueError("Evidence amendment requires a target and reason_code")
        target = await db.get(EvidenceObservation, target_observation_id)
        if target is None or target.owner_id != owner_id:
            raise ValueError("Evidence amendment target is not owned by the caller")
        if (target.plan_id, target.task_id) != (plan_id, task_id):
            raise ValueError("Evidence amendment target has a different plan/task scope")
        child_exists = await db.scalar(
            select(EvidenceObservation.id).where(
                EvidenceObservation.target_observation_id == target.id
            ).limit(1)
        )
        if child_exists is not None:
            raise ValueError("Evidence amendment target already has a successor")
    if fact_kind in {"invalidation", "reinstatement"}:
        evidence_role = "control"
        decision = EligibilityDecision("unknown", "CONTROL_FACT", False)
    elif evidence_role == "supporting":
        decision = EligibilityDecision(
            min(decision.stage, "practicing", key=lambda stage: _STAGE_RANK[stage]),
            "SUPPORTING_EVIDENCE_ONLY",
            False,
        )

    resolved_artifacts = await _resolve_artifact_references(
        db,
        owner_id=owner_id,
        plan_id=plan_id,
        task_id=task_id,
        artifact_refs=normalized_artifact_refs,
    )
    if decision.counts_as_success:
        for artifact, _kind in resolved_artifacts:
            try:
                _verified_artifact_bytes(artifact)
            except FileNotFoundError:
                decision = EligibilityDecision(
                    "practicing", "ARTIFACT_NOT_DURABLY_STORED", False
                )
                break
    if _competency_snapshot_overrides is None:
        competency_snapshots = await _resolve_competency_snapshots(
            db,
            owner_id=owner_id,
            plan_id=plan_id,
            task_id=task_id,
            competency_ids=requested_competencies,
            include_task_assesses=include_task_assesses,
        )
    else:
        competency_snapshots = _competency_snapshot_overrides
        for competency, association_kind, task_link_id in competency_snapshots:
            if competency.owner_id != owner_id:
                raise ValueError("Evidence competency snapshot crosses owner scope")
            if association_kind not in {"explicit", "task_assesses", "legacy"}:
                raise ValueError("Evidence competency snapshot has an invalid association")
            if association_kind == "task_assesses" and task_link_id is None:
                raise ValueError("Task-assesses snapshot is missing its immutable link id")

    observation = EvidenceObservation(
        owner_id=owner_id,
        source_type=source_type,
        source_id=str(source_id),
        run_id=run_id,
        session_id=session_id,
        plan_id=plan_id,
        task_id=task_id,
        fact_kind=fact_kind,
        target_observation_id=target_observation_id,
        reason_code=reason_code,
        evidence_role=evidence_role,
        eligibility_stage=decision.stage,
        eligibility_reason=decision.reason,
        eligibility_policy_version=EVIDENCE_POLICY_VERSION,
        counts_as_success=decision.counts_as_success,
        outcome=outcome,
        normalized_score=score,
        is_correct=is_correct,
        assistance_level=assistance_level,
        transfer_level=transfer_level,
        rubric_snapshot=normalized_rubric,
        evaluator=normalized_evaluator,
        payload=normalized_payload,
        occurred_at=occurred_at or utc_now(),
        recorded_at=utc_now(),
        schema_version=EVIDENCE_SCHEMA_VERSION,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
    )
    try:
        async with db.begin_nested():
            db.add(observation)
            try:
                await flush_uow(db)
            except IntegrityError as exc:
                raise _ObservationIdentityConflict() from exc
            await _persist_evidence_links(
                db,
                observation,
                artifacts=resolved_artifacts,
                competencies=competency_snapshots,
            )
            if refresh_projection and plan_id is not None:
                await refresh_plan_evidence_projection(db, owner_id, plan_id)
        return observation, True
    except _ObservationIdentityConflict as conflict:
        existing = await db.scalar(
            select(EvidenceObservation).where(
                EvidenceObservation.owner_id == owner_id,
                EvidenceObservation.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            if conflict.__cause__ is not None:
                raise conflict.__cause__
            raise RuntimeError("Evidence identity insert conflicted without a durable winner")
        if not request_digest_matches(existing.request_digest, request_digest):
            raise ValueError(
                "Evidence idempotency conflict: the key was already used for a different request"
            )
        return existing, False


async def link_operation_observations(
    db: AsyncSession,
    operation: Operation,
    observations: Iterable[EvidenceObservation],
    *,
    generation: int = 0,
    role: str = "produced",
) -> list[OperationEvidenceLink]:
    """Link an Operation to immutable Evidence facts in the same caller UoW."""

    if generation < 0 or role not in {
        "produced",
        "amendment",
        "invalidation",
        "reinstatement",
    }:
        raise ValueError("Invalid Operation Evidence link")
    if not operation.id:
        await flush_uow(db)
    linked: list[OperationEvidenceLink] = []
    for observation in observations:
        if observation.owner_id != operation.owner_id:
            raise ValueError("Operation Evidence link crosses owner scope")
        existing = await db.scalar(
            select(OperationEvidenceLink).where(
                OperationEvidenceLink.operation_id == operation.id,
                OperationEvidenceLink.generation == generation,
                OperationEvidenceLink.observation_id == observation.id,
                OperationEvidenceLink.role == role,
            )
        )
        if existing is not None:
            linked.append(existing)
            continue
        link = OperationEvidenceLink(
            operation_id=operation.id,
            observation_id=observation.id,
            generation=generation,
            role=role,
        )
        db.add(link)
        linked.append(link)
    if linked:
        await flush_uow(db)
    return linked


async def operation_evidence_generation(
    db: AsyncSession,
    operation_id: str,
) -> int | None:
    generation = await db.scalar(
        select(func.max(OperationEvidenceLink.generation)).where(
            OperationEvidenceLink.operation_id == operation_id,
            OperationEvidenceLink.role == "produced",
        )
    )
    return int(generation) if generation is not None else None


async def _operation_generation_observations(
    db: AsyncSession,
    operation: Operation,
    generation: int,
) -> list[EvidenceObservation]:
    observations = list(
        (
            await db.execute(
                select(EvidenceObservation)
                .join(
                    OperationEvidenceLink,
                    OperationEvidenceLink.observation_id == EvidenceObservation.id,
                )
                .where(
                    OperationEvidenceLink.operation_id == operation.id,
                    OperationEvidenceLink.generation == generation,
                    OperationEvidenceLink.role == "produced",
                    EvidenceObservation.owner_id == operation.owner_id,
                )
                .order_by(EvidenceObservation.id)
            )
        ).scalars()
    )
    return await _attach_evidence_links(db, observations)


async def append_operation_invalidations(
    db: AsyncSession,
    operation: Operation,
) -> list[EvidenceObservation]:
    generation = await operation_evidence_generation(db, operation.id)
    if generation is None:
        return []
    produced = await _operation_generation_observations(db, operation, generation)
    invalidations: list[EvidenceObservation] = []
    for target in produced:
        invalidation, _ = await append_observation(
            db,
            owner_id=operation.owner_id,
            source_type="operation",
            source_id=f"{operation.id}:undo:{generation}:{target.id}",
            outcome="invalidated",
            idempotency_key=f"operation:{operation.id}:undo:{generation}:{target.id}",
            run_id=operation.run_id,
            session_id=target.session_id,
            plan_id=target.plan_id,
            task_id=target.task_id,
            payload={"operation_id": operation.id, "generation": generation},
            occurred_at=utc_now(),
            causation_id=f"operation:{operation.id}",
            fact_kind="invalidation",
            target_observation_id=target.id,
            reason_code="OPERATION_UNDONE",
            evidence_role="control",
            include_task_assesses=False,
        )
        invalidations.append(invalidation)
    await link_operation_observations(
        db,
        operation,
        invalidations,
        generation=generation,
        role="invalidation",
    )
    return invalidations


async def append_operation_redo_generation(
    db: AsyncSession,
    operation: Operation,
) -> tuple[int, list[EvidenceObservation]]:
    previous_generation = await operation_evidence_generation(db, operation.id)
    if previous_generation is None:
        raise ValueError("Operation has no replayable Evidence generation")
    originals = await _operation_generation_observations(
        db,
        operation,
        previous_generation,
    )
    if not originals:
        raise ValueError("Operation Evidence generation is empty")
    generation = previous_generation + 1
    produced: list[EvidenceObservation] = []
    for original in originals:
        competency_rows = list(
            (
                await db.execute(
                    select(EvidenceCompetencyLink, Competency)
                    .join(Competency, Competency.id == EvidenceCompetencyLink.competency_id)
                    .where(EvidenceCompetencyLink.observation_id == original.id)
                    .order_by(EvidenceCompetencyLink.id)
                )
            ).all()
        )
        overrides = [
            (
                competency,
                link.association_kind,
                link.task_competency_link_id_snapshot,
            )
            for link, competency in competency_rows
        ]
        clone, _ = await append_observation(
            db,
            owner_id=operation.owner_id,
            source_type=original.source_type,
            source_id=f"{original.source_id}:redo:{generation}",
            outcome=original.outcome,
            idempotency_key=(
                f"operation:{operation.id}:redo:{generation}:source:{original.id}"
            ),
            run_id=operation.run_id,
            session_id=original.session_id,
            plan_id=original.plan_id,
            task_id=original.task_id,
            normalized_score=original.normalized_score,
            is_correct=original.is_correct,
            assistance_level=original.assistance_level,
            transfer_level=original.transfer_level,
            rubric_snapshot=original.rubric_snapshot,
            evaluator=original.evaluator,
            artifact_refs=_artifact_refs_for(original),
            payload=original.payload,
            occurred_at=utc_now(),
            correlation_id=original.correlation_id,
            causation_id=f"operation:{operation.id}:redo:{generation}",
            include_task_assesses=False,
            _competency_snapshot_overrides=overrides,
        )
        produced.append(clone)
    await link_operation_observations(
        db,
        operation,
        produced,
        generation=generation,
        role="produced",
    )
    return generation, produced


def _artifact_refs_for(observation: EvidenceObservation) -> list[dict[str, Any]]:
    return list(getattr(observation, "_evidence_artifact_refs", []) or [])


def _competency_refs_for(observation: EvidenceObservation) -> list[dict[str, Any]]:
    return list(getattr(observation, "_evidence_competency_refs", []))


def _public_data(raw: Any, known: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("data")
    data = dict(nested) if isinstance(nested, dict) else {}
    for key in sorted(raw):
        if key not in known and key != "data":
            data[key] = raw[key]
    return {key: data[key] for key in sorted(data)}


def _public_rubric(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    checks: list[dict[str, Any]] = []
    for raw_check in value.get("checks", []) if isinstance(value.get("checks"), list) else []:
        if not isinstance(raw_check, dict):
            continue
        known = {"key", "name", "kind", "passed", "score", "message", "data"}
        check: dict[str, Any] = {
            "data": _public_data(raw_check, known),
        }
        key = raw_check.get("key", raw_check.get("name"))
        if key is not None:
            check["key"] = str(key)
        for field in ("kind", "passed", "score", "message"):
            if raw_check.get(field) is not None:
                check[field] = raw_check[field]
        checks.append(check)
    result: dict[str, Any] = {
        "rubric_version": str(value.get("rubric_version") or "unspecified"),
        "checks": checks,
        "data": _public_data(value, {"rubric_version", "pass_threshold", "checks", "data"}),
    }
    if value.get("pass_threshold") is not None:
        result["pass_threshold"] = value["pass_threshold"]
    return result


def _public_evaluator(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    result: dict[str, Any] = {
        "type": str(value.get("type") or "unknown"),
        "evaluator_version": str(value.get("evaluator_version") or "unspecified"),
        "data": _public_data(
            value,
            {"type", "run_id", "model", "evaluator_version", "data"},
        ),
    }
    for field in ("run_id", "model"):
        if value.get(field) is not None:
            result[field] = value[field]
    return result


def _public_payload(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    evidence: list[dict[str, Any]] = []
    raw_evidence = value.get("evidence")
    if isinstance(raw_evidence, list):
        for raw_item in raw_evidence:
            if not isinstance(raw_item, dict):
                continue
            kind = raw_item.get("kind") or raw_item.get("criterion") or raw_item.get("name") or "claim"
            item: dict[str, Any] = {
                "kind": str(kind),
                "data": _public_data(
                    raw_item,
                    {"kind", "criterion", "name", "verified", "passed", "data"},
                ),
            }
            verified = raw_item.get("verified", raw_item.get("passed"))
            if verified is not None:
                item["verified"] = bool(verified)
            evidence.append(item)
    known = {
        "feedback",
        "answer_length",
        "artifact_count",
        "has_text",
        "submission_type",
        "backfilled",
        "evidence",
        "data",
    }
    result: dict[str, Any] = {
        "evidence": evidence,
        "data": _public_data(value, known),
    }
    for field in (
        "feedback",
        "answer_length",
        "artifact_count",
        "has_text",
        "submission_type",
        "backfilled",
    ):
        if value.get(field) is not None:
            result[field] = value[field]
    return result


def observation_dict(observation: EvidenceObservation) -> dict[str, Any]:
    return {
        "id": observation.id,
        "source_type": observation.source_type,
        "source_id": observation.source_id,
        "run_id": observation.run_id,
        "session_id": observation.session_id,
        "plan_id": observation.plan_id,
        "task_id": observation.task_id,
        "fact_kind": observation.fact_kind,
        "target_observation_id": observation.target_observation_id,
        "reason_code": observation.reason_code,
        "evidence_role": observation.evidence_role,
        "eligibility_stage": observation.eligibility_stage,
        "eligibility_reason": observation.eligibility_reason,
        "eligibility_policy_version": observation.eligibility_policy_version,
        "counts_as_success": observation.counts_as_success,
        "competency_refs": _competency_refs_for(observation),
        "outcome": observation.outcome,
        "normalized_score": observation.normalized_score,
        "is_correct": observation.is_correct,
        "assistance_level": observation.assistance_level,
        "transfer_level": observation.transfer_level,
        "rubric_snapshot": _public_rubric(observation.rubric_snapshot),
        "evaluator": _public_evaluator(observation.evaluator),
        "artifact_refs": _artifact_refs_for(observation),
        "payload": _public_payload(observation.payload),
        "occurred_at": _iso(observation.occurred_at),
        "recorded_at": _iso(observation.recorded_at),
        "schema_version": observation.schema_version,
        "correlation_id": observation.correlation_id,
        "causation_id": observation.causation_id,
        "idempotency_key": observation.idempotency_key,
    }


async def _attach_evidence_links(
    db: AsyncSession,
    observations: list[EvidenceObservation],
) -> list[EvidenceObservation]:
    if not observations:
        return observations
    ids = [item.id for item in observations]
    artifact_rows = list(
        (
            await db.execute(
                select(EvidenceArtifactLink, Artifact)
                .join(Artifact, Artifact.id == EvidenceArtifactLink.artifact_id)
                .where(EvidenceArtifactLink.observation_id.in_(ids))
                .order_by(EvidenceArtifactLink.observation_id, EvidenceArtifactLink.ordinal)
            )
        ).all()
    )
    competency_rows = list(
        (
            await db.execute(
                select(EvidenceCompetencyLink)
                .where(EvidenceCompetencyLink.observation_id.in_(ids))
                .order_by(EvidenceCompetencyLink.observation_id, EvidenceCompetencyLink.id)
            )
        ).scalars()
    )
    artifacts_by_observation: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for link, artifact in artifact_rows:
        artifacts_by_observation[link.observation_id].append(
            {
                "artifact_id": artifact.id,
                "kind": link.kind,
                "uri": artifact.source_uri,
                "content_hash": link.content_hash_snapshot,
                "ordinal": link.ordinal,
            }
        )
    competencies_by_observation: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for link in competency_rows:
        competencies_by_observation[link.observation_id].append(
            {
                "competency_id": link.competency_id,
                "competency_key": link.competency_key_snapshot,
                "association_kind": link.association_kind,
                "task_competency_link_id_snapshot": link.task_competency_link_id_snapshot,
            }
        )
    for observation in observations:
        observation._evidence_artifact_refs = artifacts_by_observation[observation.id]
        observation._evidence_competency_refs = competencies_by_observation[observation.id]
    return observations


async def list_observations(
    db: AsyncSession,
    owner_id: str,
    *,
    plan_id: int | None = None,
    task_id: int | None = None,
    competency_id: int | None = None,
    limit: int | None = 200,
) -> list[EvidenceObservation]:
    query = select(EvidenceObservation).where(EvidenceObservation.owner_id == owner_id)
    if plan_id is not None:
        query = query.where(EvidenceObservation.plan_id == plan_id)
    if task_id is not None:
        query = query.where(EvidenceObservation.task_id == task_id)
    if competency_id is not None:
        query = query.where(
            select(EvidenceCompetencyLink.id)
            .where(
                EvidenceCompetencyLink.observation_id == EvidenceObservation.id,
                EvidenceCompetencyLink.competency_id == competency_id,
            )
            .exists()
        )
    query = query.order_by(EvidenceObservation.occurred_at.desc(), EvidenceObservation.id.desc())
    if limit is not None:
        query = query.limit(limit)
    observations = list((await db.execute(query)).scalars())
    return await _attach_evidence_links(db, observations)


def _supersession_error_codes(
    observations: list[EvidenceObservation],
) -> list[str]:
    by_id = {item.id: item for item in observations}
    child_counts: dict[int, int] = defaultdict(int)
    codes: set[str] = set()
    for item in observations:
        target_id = getattr(item, "target_observation_id", None)
        if target_id is None or target_id not in by_id:
            continue
        child_counts[target_id] += 1
        target = by_id[target_id]
        for field, suffix in (
            ("owner_id", "OWNER"),
            ("plan_id", "PLAN"),
            ("task_id", "TASK"),
        ):
            if getattr(item, field, None) != getattr(target, field, None):
                codes.add(f"EVIDENCE_SUPERSESSION_CROSS_{suffix}")
    if any(count > 1 for count in child_counts.values()):
        codes.add("EVIDENCE_SUPERSESSION_BRANCH")

    for start_id in by_id:
        seen: set[int] = set()
        current_id: int | None = start_id
        while current_id in by_id:
            if current_id in seen:
                codes.add("EVIDENCE_SUPERSESSION_CYCLE")
                break
            seen.add(current_id)
            current_id = getattr(by_id[current_id], "target_observation_id", None)
    return sorted(codes)


def _projection_records(observations: list[EvidenceObservation]) -> list[EvidenceObservation]:
    by_id = {item.id: item for item in observations}
    child_by_target = {
        item.target_observation_id: item
        for item in observations
        if getattr(item, "target_observation_id", None) is not None
        and item.target_observation_id in by_id
    }
    active_cache: dict[int, bool] = {}

    def is_active(item_id: int, visiting: set[int] | None = None) -> bool:
        cached = active_cache.get(item_id)
        if cached is not None:
            return cached
        current_visiting = set() if visiting is None else visiting
        if item_id in current_visiting:
            # The database trigger rejects cycles; fail closed if a damaged
            # fixture bypassed it rather than recursing forever.
            active_cache[item_id] = False
            return False
        child = child_by_target.get(item_id)
        if child is None:
            active_cache[item_id] = True
        else:
            current_visiting.add(item_id)
            active_cache[item_id] = not is_active(child.id, current_visiting)
            current_visiting.remove(item_id)
        return active_cache[item_id]

    return [
        item
        for item in observations
        if getattr(item, "fact_kind", "observation") in {"observation", "amendment"}
        and is_active(item.id)
    ]


def _task_stage(records: list[EvidenceObservation]) -> str:
    """Return a conservative, evidence-only stage (not a mastery claim)."""

    if not records:
        return "unknown"
    latest = max(records, key=lambda item: (coerce_legacy_utc(item.occurred_at), item.id))
    if latest.outcome in _FAILURE_OUTCOMES:
        return "practicing"
    return max(
        (_decision_for_item(item).stage for item in records),
        key=lambda stage: _STAGE_RANK[stage],
    )


def _decision_for_item(item: EvidenceObservation) -> EligibilityDecision:
    persisted_stage = getattr(item, "eligibility_stage", None)
    if persisted_stage in _STAGE_RANK:
        return EligibilityDecision(
            persisted_stage,
            getattr(item, "eligibility_reason", "PERSISTED_POLICY"),
            bool(getattr(item, "counts_as_success", False)),
        )
    return _eligibility_decision(
        source_type=item.source_type,
        outcome=item.outcome,
        assistance_level=getattr(item, "assistance_level", "unknown"),
        rubric_snapshot=getattr(item, "rubric_snapshot", {}) or {},
        evaluator=getattr(item, "evaluator", {}) or {},
        artifact_refs=_artifact_refs_for(item),
        payload=getattr(item, "payload", {}) or {},
    )


def _canonical_observation(item: EvidenceObservation) -> dict[str, Any]:
    decision = _decision_for_item(item)
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "plan_id": getattr(item, "plan_id", None),
        "task_id": item.task_id,
        "fact_kind": getattr(item, "fact_kind", "observation"),
        "target_observation_id": getattr(item, "target_observation_id", None),
        "reason_code": getattr(item, "reason_code", ""),
        "evidence_role": getattr(item, "evidence_role", "primary"),
        "eligibility_stage": decision.stage,
        "eligibility_reason": decision.reason,
        "eligibility_policy_version": getattr(
            item, "eligibility_policy_version", EVIDENCE_POLICY_VERSION
        ),
        "counts_as_success": decision.counts_as_success,
        "outcome": item.outcome,
        "normalized_score": item.normalized_score,
        "is_correct": item.is_correct,
        "assistance_level": item.assistance_level,
        "transfer_level": item.transfer_level,
        "rubric_snapshot": getattr(item, "rubric_snapshot", {}) or {},
        "evaluator": getattr(item, "evaluator", {}) or {},
        "artifact_refs": _artifact_refs_for(item),
        "competency_refs": _competency_refs_for(item),
        "payload": getattr(item, "payload", {}) or {},
        "occurred_at": _iso(item.occurred_at),
    }


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

    digest_payload = sorted(
        (_canonical_observation(item) for item in records),
        key=lambda value: value["id"],
    )
    digest = hashlib.sha256(_stable_json(digest_payload).encode("utf-8")).hexdigest()

    by_task: list[dict[str, Any]] = []
    for task_key, task_records in sorted(grouped.items(), key=lambda item: int(item[0])):
        scores = [item.normalized_score for item in task_records if item.normalized_score is not None]
        successful = sum(_decision_for_item(item).counts_as_success for item in task_records)
        failed = sum(item.outcome in _FAILURE_OUTCOMES for item in task_records)
        latest = max(task_records, key=lambda item: (coerce_legacy_utc(item.occurred_at), item.id))
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

    state = {
        "schema_version": 2,
        "algorithm_version": EVIDENCE_ALGORITHM_VERSION,
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
    conflicts = _supersession_error_codes(observations)
    if conflicts:
        state["supersession_conflicts"] = conflicts
    return state


async def build_plan_evidence_state(
    db: AsyncSession,
    owner_id: str,
    plan_id: int,
) -> dict[str, Any]:
    observations = await list_observations(db, owner_id, plan_id=plan_id, limit=None)
    return build_evidence_state(observations)


def _build_incremental_candidate(
    previous_projection: dict[str, Any],
    observations: list[EvidenceObservation],
    *,
    watermark: int,
) -> dict[str, Any]:
    """Build the next candidate from prior active ids plus post-watermark facts.

    A later control fact can reactivate an older, previously inactive ancestor,
    which cannot be recovered from the compact projection alone. The refresh
    protocol therefore compares this candidate with the unbounded rebuild and
    records a deterministic recovery marker when that information boundary is
    crossed.
    """

    previous_ids = {
        evidence_id
        for task_state in previous_projection.get("by_task", [])
        for evidence_id in task_state.get("evidence_ids", [])
    }
    previous_ids.update(previous_projection.get("unscoped_observation_ids", []))
    incremental_input = [
        item for item in observations if item.id in previous_ids or item.id > watermark
    ]
    return build_evidence_state(incremental_input)


async def build_incremental_plan_evidence_state(
    db: AsyncSession,
    owner_id: str,
    plan_id: int,
) -> dict[str, Any]:
    """Advance and return the durable, full-oracle-checked projection."""

    return await refresh_plan_evidence_projection(db, owner_id, plan_id)


def _ledger_digest(observations: list[EvidenceObservation]) -> str:
    payload = sorted(
        (_canonical_observation(item) for item in observations),
        key=lambda value: value["id"],
    )
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


async def refresh_plan_evidence_projection(
    db: AsyncSession,
    owner_id: str,
    plan_id: int,
) -> dict[str, Any]:
    """Advance the durable projection and verify it against a full rebuild.

    The incremental candidate starts from the last active Evidence ids plus all
    facts beyond the durable watermark. Every advance is compared with a full,
    unbounded rebuild before the new watermark is stored.
    """

    all_observations = await list_observations(
        db,
        owner_id,
        plan_id=plan_id,
        limit=None,
    )
    all_observations.sort(key=lambda item: item.id)
    full_projection = build_evidence_state(all_observations)
    ledger_digest = _ledger_digest(all_observations)
    watermark = max((item.id for item in all_observations), default=0)
    state = await db.scalar(
        select(EvidenceProjectionState).where(
            EvidenceProjectionState.owner_id == owner_id,
            EvidenceProjectionState.plan_id == plan_id,
        )
    )
    if state is not None and state.algorithm_version == EVIDENCE_ALGORITHM_VERSION:
        incremental_projection = _build_incremental_candidate(
            state.projection or {},
            all_observations,
            watermark=state.watermark,
        )
        if incremental_projection["digest"] != full_projection["digest"]:
            full_projection = {
                **full_projection,
                "projection_recovery": {
                    "reason": "INCREMENTAL_DIVERGENCE",
                    "previous_watermark": state.watermark,
                    "candidate_digest": incremental_projection["digest"],
                },
            }
    await ensure_sqlite_write_transaction(db)
    if state is None:
        state = EvidenceProjectionState(
            owner_id=owner_id,
            plan_id=plan_id,
            watermark=watermark,
            ledger_digest=ledger_digest,
            projection_digest=full_projection["digest"],
            projection=full_projection,
            algorithm_version=EVIDENCE_ALGORITHM_VERSION,
        )
        db.add(state)
    else:
        state.watermark = watermark
        state.ledger_digest = ledger_digest
        state.projection_digest = full_projection["digest"]
        state.projection = full_projection
        state.algorithm_version = EVIDENCE_ALGORITHM_VERSION
        state.updated_at = utc_now()
    await flush_uow(db)
    return full_projection


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
    affected_plans: set[int] = set()

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
            occurred_at=submission.created_at or utc_now(),
            correlation_id=submission.run_id,
        )
        created += int(was_created)
        skipped += int(not was_created)
        affected_plans.add(submission.plan_id)
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
            normalized_score=(
                normalize_percentage_score(submission.score)
                if submission.score is not None
                else None
            ),
            is_correct=submission.status == "accepted",
            rubric_snapshot={"rubric_version": "legacy-unspecified"},
            evaluator={"type": "legacy_record", "evaluator_version": "legacy-v1"},
            payload={"feedback": submission.feedback or "", "backfilled": True},
            artifact_refs=[artifact_ref(submission_artifact, kind="submission")],
            occurred_at=submission.checked_at or submission.created_at or utc_now(),
            correlation_id=submission.run_id,
        )
        created += int(was_created)
        skipped += int(not was_created)
        affected_plans.add(submission.plan_id)

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
            normalized_score=(
                normalize_percentage_score(quiz.score)
                if quiz.score is not None
                else None
            ),
            is_correct=quiz.status == "passed",
            rubric_snapshot=quiz.rubric or {},
            evaluator={"type": "legacy_record", "backfilled": True},
            payload={"evidence": quiz.evidence or []},
            artifact_refs=[artifact_ref(quiz_artifact, kind="quiz_answer")],
            occurred_at=quiz.graded_at or quiz.created_at or utc_now(),
            correlation_id=quiz.run_id,
        )
        created += int(was_created)
        skipped += int(not was_created)
        affected_plans.add(quiz.plan_id)

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
        if any(
            isinstance(item, dict) and item.get("submission_id") is not None
            for item in evidence
        ):
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
            evaluator={"type": "legacy_record", "evaluator_version": "legacy-v1"},
            evidence_role="supporting",
            payload={"evidence": evidence, "backfilled": True},
            artifact_refs=[artifact_ref(completion_artifact, kind="task_evidence")],
            occurred_at=event.occurred_at or event.created_at or utc_now(),
            correlation_id=event.run_id,
            causation_id=f"learning_event:{event.id}",
        )
        created += int(was_created)
        skipped += int(not was_created)
        if event.plan_id is not None:
            affected_plans.add(event.plan_id)

    await flush_uow(db)
    for affected_plan_id in sorted(affected_plans):
        await refresh_plan_evidence_projection(db, owner_id, affected_plan_id)
    return {"created": created, "skipped": skipped}


def audit_observations(
    observations: list[EvidenceObservation],
    artifacts: Iterable[Artifact] | None = None,
) -> dict[str, Any]:
    """Run cheap, provider-independent integrity checks for the evidence ledger."""

    errors: list[str] = []
    artifact_error_codes: set[str] = set()
    seen_keys: set[str] = set()
    observation_by_id = {item.id: item for item in observations}
    observation_ids = set(observation_by_id)
    artifact_by_id = {item.id: item for item in artifacts} if artifacts is not None else None
    child_by_target: dict[int, int] = {}
    for item in observations:
        if not item.idempotency_key:
            errors.append(f"observation:{item.id} missing idempotency_key")
        elif item.idempotency_key in seen_keys:
            errors.append(f"duplicate idempotency_key:{item.idempotency_key}")
        else:
            seen_keys.add(item.idempotency_key)
        if not item.source_type or not item.source_id:
            errors.append(f"observation:{item.id} missing source identity")
        refs = _artifact_refs_for(item)
        if item.source_type in {"submission", "quiz", "task_completion", "code_run", "file"} and not refs:
            errors.append(f"observation:{item.id} missing artifact reference")
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("artifact_id"):
                errors.append(f"observation:{item.id} has malformed artifact reference")
                continue
            if artifact_by_id is not None:
                artifact_id = ref["artifact_id"]
                artifact = artifact_by_id.get(artifact_id)
                if artifact is None:
                    errors.append(f"observation:{item.id} references missing artifact:{artifact_id}")
                    artifact_error_codes.add("EVIDENCE_ARTIFACT_MISSING")
                else:
                    for field, suffix in (
                        ("owner_id", "OWNER"),
                        ("plan_id", "PLAN"),
                        ("task_id", "TASK"),
                    ):
                        if getattr(artifact, field, None) != getattr(item, field, None):
                            errors.append(
                                f"observation:{item.id} artifact crosses {field}:{artifact_id}"
                            )
                            artifact_error_codes.add(
                                f"EVIDENCE_ARTIFACT_CROSS_{suffix}"
                            )
                    if ref.get("content_hash") and ref["content_hash"] != artifact.content_hash:
                        errors.append(f"observation:{item.id} artifact hash mismatch:{artifact_id}")
                        artifact_error_codes.add("EVIDENCE_ARTIFACT_REF_HASH_MISMATCH")
                    if ref.get("uri") and ref["uri"] != artifact.source_uri:
                        errors.append(f"observation:{item.id} artifact uri mismatch:{artifact_id}")
                        artifact_error_codes.add("EVIDENCE_ARTIFACT_REF_URI_MISMATCH")
                    requires_durable = item.source_type in {
                        "submission",
                        "quiz",
                        "task_completion",
                        "task_evidence",
                        "code_run",
                        "file",
                    }
                    if artifact.storage_state != "stored" or artifact.snapshot_bytes is None:
                        if requires_durable:
                            errors.append(
                                f"observation:{item.id} artifact unavailable:{artifact_id}"
                            )
                            artifact_error_codes.add("EVIDENCE_ARTIFACT_NOT_DURABLE")
                    else:
                        raw = bytes(artifact.snapshot_bytes)
                        if (
                            getattr(artifact, "size_bytes", None) is not None
                            and len(raw) != artifact.size_bytes
                        ):
                            errors.append(
                                f"observation:{item.id} artifact size mismatch:{artifact_id}"
                            )
                            artifact_error_codes.add("EVIDENCE_ARTIFACT_SIZE_MISMATCH")
                        if hashlib.sha256(raw).hexdigest() != artifact.snapshot_sha256:
                            errors.append(
                                f"observation:{item.id} artifact snapshot hash mismatch:{artifact_id}"
                            )
                            artifact_error_codes.add(
                                "EVIDENCE_ARTIFACT_SNAPSHOT_HASH_MISMATCH"
                            )
                        envelope_hash = hashlib.sha256(
                            _artifact_envelope_bytes(
                                raw,
                                artifact.artifact_metadata or {},
                                artifact_type=artifact.artifact_type,
                            )
                        ).hexdigest()
                        if envelope_hash != artifact.content_hash:
                            errors.append(
                                f"observation:{item.id} artifact envelope hash mismatch:{artifact_id}"
                            )
                            artifact_error_codes.add(
                                "EVIDENCE_ARTIFACT_ENVELOPE_HASH_MISMATCH"
                            )
        if item.normalized_score is not None and not 0 <= item.normalized_score <= 1:
            errors.append(f"observation:{item.id} score outside [0,1]")
        fact_kind = getattr(item, "fact_kind", "observation")
        target_id = getattr(item, "target_observation_id", None)
        reason_code = getattr(item, "reason_code", "")
        role = getattr(item, "evidence_role", "primary")
        if fact_kind == "observation":
            if target_id is not None:
                errors.append(f"observation:{item.id} primary fact has a target")
        else:
            if target_id is None or not reason_code.strip():
                errors.append(f"observation:{item.id} amendment missing target/reason")
            elif target_id == item.id:
                errors.append(f"observation:{item.id} targets itself")
            elif target_id not in observation_ids:
                errors.append(f"observation:{item.id} targets missing:{target_id}")
            else:
                target = observation_by_id[target_id]
                if (
                    getattr(target, "owner_id", None),
                    getattr(target, "plan_id", None),
                    getattr(target, "task_id", None),
                ) != (
                    getattr(item, "owner_id", None),
                    getattr(item, "plan_id", None),
                    getattr(item, "task_id", None),
                ):
                    errors.append(f"observation:{item.id} target crosses scope")
                previous_child = child_by_target.setdefault(target_id, item.id)
                if previous_child != item.id:
                    errors.append(f"observation:{target_id} has multiple successors")
        if fact_kind in {"invalidation", "reinstatement"} and role != "control":
            errors.append(f"observation:{item.id} control fact has non-control role")

    for start_id in observation_ids:
        seen: set[int] = set()
        current_id: int | None = start_id
        while current_id is not None:
            if current_id in seen:
                errors.append(f"observation:{start_id} lifecycle cycle")
                break
            seen.add(current_id)
            target = observation_by_id[current_id]
            current_id = getattr(target, "target_observation_id", None)
            if current_id not in observation_by_id:
                break
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "observation_count": len(observations),
        "unique_idempotency_keys": len(seen_keys),
        "ok": not errors,
        "errors": errors,
        "error_codes": sorted(
            set(_supersession_error_codes(observations)) | artifact_error_codes
        ),
    }
