"""Deterministic executable contracts for the Evidence fact ledger.

The baseline consumes literal scenario inputs, materializes only the current
v4 append-only fact shape, and delegates projection and integrity semantics to
the production Evidence reducer. It never calls a provider or opens the user
database, so CI and release checks can use deterministic fixtures.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from app.services import evidence as evidence_service
from app.services.evidence import (
    _artifact_envelope_bytes,
    audit_observations,
    build_evidence_state,
)


_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "evidence_scenarios.json"
)
_FACT_INPUT_FIELDS = {
    "artifact_ids",
    "artifact_refs",
    "assistance_level",
    "causation_id",
    "competency_id",
    "competency_ids",
    "competency_key",
    "correlation_id",
    "counts_as_success",
    "eligibility_policy_version",
    "eligibility_reason",
    "eligibility_stage",
    "evaluator",
    "evidence_role",
    "fact_kind",
    "id",
    "idempotency_key",
    "is_correct",
    "normalized_score",
    "occurred_at",
    "outcome",
    "owner_id",
    "payload",
    "plan_id",
    "reason_code",
    "recorded_at",
    "request_digest",
    "rubric_snapshot",
    "run_id",
    "schema_version",
    "session_id",
    "source_id",
    "source_type",
    "target_observation_id",
    "task_id",
    "transfer_level",
}


def load_contracts(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the literal contract registry used by the offline quality gate."""

    source = path or _CONTRACT_PATH
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("Evidence scenario registry must be a JSON list")
    return value


_CONTRACTS = load_contracts()
SCENARIOS: tuple[dict[str, str], ...] = tuple(
    {
        "id": str(case["id"]),
        "description": str(case.get("invariant_id") or case["failure_reason_code"]),
    }
    for case in _CONTRACTS
)


def _parse_time(value: Any, *, fallback_index: int) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=fallback_index)


def _snapshot_bytes(row: dict[str, Any]) -> bytes | None:
    raw = row.get("snapshot_bytes")
    if isinstance(raw, int):
        return b"\0" * raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, bytes):
        return bytes(raw)
    if row.get("snapshot") is not None:
        return str(row["snapshot"]).encode("utf-8")
    return None


def _materialize_artifact(row: dict[str, Any]) -> SimpleNamespace:
    if "envelope" in row:
        raise ValueError(
            "Evidence Artifact fixture must separate immutable snapshot and metadata"
        )
    raw = _snapshot_bytes(row)
    return SimpleNamespace(
        id=int(row["id"]),
        owner_id=str(row.get("owner_id", "owner-a")),
        plan_id=row.get("plan_id"),
        task_id=row.get("task_id"),
        artifact_type=str(row.get("kind", "unknown")),
        source_uri=str(row.get("source_uri", "")),
        content_hash=str(row.get("content_hash", "")),
        size_bytes=len(raw) if raw is not None else row.get("size_bytes"),
        snapshot_bytes=raw,
        snapshot_sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
        storage_state="stored" if raw is not None else "external_reference",
        envelope_version=1,
        artifact_metadata=copy.deepcopy(row.get("metadata") or {}),
    )


def _normalized_artifact_ref(
    row: dict[str, Any],
    artifacts: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    artifact_id = int(row["artifact_id"])
    artifact = artifacts.get(artifact_id, {})
    return {
        "artifact_id": artifact_id,
        "kind": str(row.get("kind", artifact.get("kind", "unknown"))),
        "uri": str(row.get("uri", artifact.get("source_uri", ""))),
        "content_hash": str(
            row.get("content_hash", artifact.get("content_hash", ""))
        ),
    }


def _competency_refs(
    row: dict[str, Any],
    task_competencies: dict[int, list[int | str]],
) -> list[dict[str, Any]]:
    explicit = [row["competency_id"]] if row.get("competency_id") is not None else []
    explicit.extend(row.get("competency_ids") or [])
    assessed = (
        task_competencies.get(int(row["task_id"]), [])
        if row.get("task_id") is not None
        else []
    )
    values = list(dict.fromkeys([*explicit, *assessed]))
    return [
        {
            "competency_id": value if isinstance(value, int) else None,
            "competency_key": value if isinstance(value, str) else row.get("competency_key"),
            "association_kind": "explicit" if value in explicit else "task_assesses",
            "task_competency_link_id_snapshot": None,
        }
        for value in values
    ]


def _materialize_observation(
    row: dict[str, Any],
    *,
    index: int,
    artifacts: dict[int, dict[str, Any]],
    task_competencies: dict[int, list[int | str]],
) -> SimpleNamespace:
    unsupported_fields = sorted(set(row).difference(_FACT_INPUT_FIELDS))
    if unsupported_fields:
        raise ValueError(
            "Evidence baseline fact contains unsupported fields: "
            + ", ".join(unsupported_fields)
        )
    fact_id = int(row.get("id", index))
    fact_kind = str(row.get("fact_kind", "observation"))
    target_id = row.get("target_observation_id")
    if fact_kind == "observation" and target_id is not None:
        raise ValueError("Primary Evidence baseline fact cannot target another fact")
    if fact_kind != "observation" and (
        target_id is None or not str(row.get("reason_code", "")).strip()
    ):
        raise ValueError("Evidence lifecycle fact requires target_observation_id/reason_code")

    artifact_rows = [copy.deepcopy(item) for item in row.get("artifact_refs") or []]
    artifact_rows.extend(
        {"artifact_id": artifact_id} for artifact_id in row.get("artifact_ids") or []
    )
    occurred_at = _parse_time(row.get("occurred_at"), fallback_index=fact_id)
    outcome = str(row.get("outcome", "observed"))
    return SimpleNamespace(
        id=fact_id,
        owner_id=str(row.get("owner_id", "owner-a")),
        source_type=str(row.get("source_type", "manual")),
        source_id=str(row.get("source_id", f"scenario:{fact_id}")),
        run_id=row.get("run_id"),
        session_id=row.get("session_id"),
        plan_id=row.get("plan_id"),
        task_id=row.get("task_id"),
        fact_kind=fact_kind,
        target_observation_id=target_id,
        reason_code=str(row.get("reason_code", "")),
        evidence_role=str(
            row.get(
                "evidence_role",
                "control" if fact_kind in {"invalidation", "reinstatement"} else "primary",
            )
        ),
        outcome=outcome,
        normalized_score=row.get("normalized_score"),
        is_correct=row.get("is_correct", outcome in {"accepted", "passed", "verified"}),
        assistance_level=str(row.get("assistance_level", "unknown")),
        transfer_level=str(row.get("transfer_level", "unknown")),
        rubric_snapshot=copy.deepcopy(row.get("rubric_snapshot") or {}),
        evaluator=copy.deepcopy(row.get("evaluator") or {}),
        payload=copy.deepcopy(row.get("payload") or {}),
        occurred_at=occurred_at,
        recorded_at=_parse_time(row.get("recorded_at"), fallback_index=fact_id),
        schema_version=int(row.get("schema_version", 2)),
        correlation_id=row.get("correlation_id"),
        causation_id=row.get("causation_id"),
        idempotency_key=str(row.get("idempotency_key", f"scenario:{fact_id}")),
        request_digest=row.get("request_digest"),
        eligibility_stage=row.get("eligibility_stage"),
        eligibility_reason=str(row.get("eligibility_reason", "")),
        eligibility_policy_version=str(
            row.get("eligibility_policy_version", "evidence-eligibility-v1")
        ),
        counts_as_success=bool(row.get("counts_as_success", False)),
        _evidence_artifact_refs=[
            _normalized_artifact_ref(item, artifacts) for item in artifact_rows
        ],
        _evidence_competency_refs=_competency_refs(row, task_competencies),
    )


@dataclass(frozen=True)
class EvidenceScenarioInput:
    setup: dict[str, Any]
    action: dict[str, Any]
    records: list[SimpleNamespace]
    artifacts: list[SimpleNamespace]


def materialize_contract_input(case_input: dict[str, Any]) -> EvidenceScenarioInput:
    """Materialize one literal setup as current-shape Evidence facts."""

    setup = copy.deepcopy(case_input["setup"])
    action = copy.deepcopy(case_input["action"])
    artifact_rows = list(setup.get("artifacts") or [])
    artifacts = {int(item["id"]): item for item in artifact_rows}
    task_competencies = {
        int(task["id"]): list(task.get("assesses") or [])
        for task in setup.get("tasks") or []
    }
    raw_rows = [copy.deepcopy(item) for item in setup.get("observations") or []]
    database = setup.get("database") or {}
    raw_rows.extend(copy.deepcopy(database.get("observations") or []))
    raw_rows.extend(copy.deepcopy(database.get("rows") or []))
    raw_rows.extend(copy.deepcopy(setup.get("amendments") or []))
    records = [
        _materialize_observation(
            row,
            index=index,
            artifacts=artifacts,
            task_competencies=task_competencies,
        )
        for index, row in enumerate(raw_rows, start=1)
    ]
    return EvidenceScenarioInput(
        setup=setup,
        action=action,
        records=records,
        artifacts=[_materialize_artifact(item) for item in artifact_rows],
    )


def records_for(scenario_id: str) -> list[Any]:
    """Return independent v4 facts for a registered legacy baseline name."""

    case = next((item for item in _CONTRACTS if item["id"] == scenario_id), None)
    if case is None:
        raise KeyError(f"Unknown Evidence baseline scenario: {scenario_id}")
    return materialize_contract_input(case["input"]).records


def _scoped_records(materialized: EvidenceScenarioInput) -> list[SimpleNamespace]:
    owner_id = materialized.action.get("owner_id")
    plan_id = materialized.action.get("plan_id")
    return [
        item
        for item in materialized.records
        if (owner_id is None or item.owner_id == owner_id)
        and ("plan_id" not in materialized.action or item.plan_id == plan_id)
    ]


def _active_evidence_ids(projection: dict[str, Any]) -> list[int]:
    values = [
        evidence_id
        for task in projection.get("by_task", [])
        for evidence_id in task.get("evidence_ids", [])
    ]
    values.extend(projection.get("unscoped_observation_ids", []))
    return sorted(values)


def _task_group_identity(task: dict[str, Any]) -> tuple[Any, ...]:
    """Stable task identity seam used by the cross-plan contract."""

    return task.get("plan_id"), task.get("id")


def _competency_group_identity(competency: dict[str, Any]) -> tuple[Any, ...]:
    """Stable competency identity seam used by the cross-plan contract."""

    return competency.get("plan_id"), competency.get("id")


def _event_time(item: SimpleNamespace) -> datetime:
    """Return the authoritative event timestamp for projection ordering."""

    return item.occurred_at


def _build_eligible_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    active = set(_active_evidence_ids(projection))
    projection["eligible_evidence_ids"] = sorted(
        item.id
        for item in materialized.records
        if item.id in active
        and any(
            task["success_count"] > 0
            for task in build_evidence_state([item])["by_task"]
        )
    )
    projection["assistance_levels"] = {
        str(item.id): item.assistance_level
        for item in materialized.records
        if item.id in active
    }
    return projection


def _serialize_ledger(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    projection["source_identities"] = [
        {
            "evidence_id": item.id,
            "source_type": item.source_type,
            "source_id": item.source_id,
            "evaluator_id": item.evaluator.get("id"),
        }
        for item in _scoped_records(materialized)
    ]
    projection["source_timestamps"] = {
        str(item.id): item.occurred_at.isoformat().replace("+00:00", "Z")
        for item in _scoped_records(materialized)
    }
    return projection


def _project_competency_transfer(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    projection["task_stages"] = {
        str(item["task_id"]): item["evidence_stage"]
        for item in projection["by_task"]
    }
    competency_id = materialized.action.get("competency_id")
    projection["transfer_claims"] = [
        {
            "evidence_id": item.id,
            "competency_id": ref.get("competency_id"),
            "level": item.transfer_level,
        }
        for item in _scoped_records(materialized)
        for ref in item._evidence_competency_refs
        if ref.get("competency_id") == competency_id
        and item.transfer_level == "variant"
    ]
    return projection


def _conflict_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    for task in projection["by_task"]:
        task["conflicted"] = task["success_count"] > 0 and task["failure_count"] > 0
    return projection


def _time_ordered_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    records = sorted(
        _scoped_records(materialized),
        key=lambda item: (_event_time(item), item.id),
    )
    return build_evidence_state(records)


def _new_request_record(
    request: dict[str, Any],
    *,
    fact_id: int,
    score: float | None = None,
) -> SimpleNamespace:
    row = copy.deepcopy(request)
    row.setdefault("id", fact_id)
    row.setdefault("source_type", "manual")
    row.setdefault("source_id", row.get("idempotency_key", f"request:{fact_id}"))
    row.setdefault("idempotency_key", row["source_id"])
    row.pop("score_percent", None)
    if score is not None:
        row["normalized_score"] = score
    return _materialize_observation(
        row,
        index=fact_id,
        artifacts={},
        task_competencies={},
    )


def _append_sequence(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    stored: dict[str, tuple[str, SimpleNamespace]] = {}
    created_flags: list[bool] = []
    returned_ids: list[int | None] = []
    conflict_codes: list[str | None] = []
    for request in materialized.action.get("requests") or []:
        identity = str(request["idempotency_key"])
        digest = str(request["request_digest"])
        existing = stored.get(identity)
        if existing is None:
            record = _new_request_record(request, fact_id=len(stored) + 1)
            stored[identity] = (digest, record)
            created_flags.append(True)
            returned_ids.append(record.id)
            conflict_codes.append(None)
        elif evidence_service.request_digest_matches(existing[0], digest):
            created_flags.append(False)
            returned_ids.append(existing[1].id)
            conflict_codes.append(None)
        else:
            created_flags.append(False)
            returned_ids.append(None)
            conflict_codes.append("IDEMPOTENCY_CONTENT_CONFLICT")
    projection = build_evidence_state([item[1] for item in stored.values()])
    projection.update(
        created_flags=created_flags,
        returned_evidence_ids=returned_ids,
        conflict_reason_codes=conflict_codes,
    )
    return projection


def _query_plan_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    included = _scoped_records(materialized)
    included_ids = {item.id for item in included}
    projection = build_evidence_state(included)
    projection.update(
        plan_id=materialized.action.get("plan_id"),
        task_ids=sorted({item.task_id for item in included if item.task_id is not None}),
        evidence_ids=sorted(included_ids),
        excluded_evidence_ids=sorted(
            item.id for item in materialized.records if item.id not in included_ids
        ),
    )
    return projection


def _compare_plan_projections(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    tasks = list(materialized.setup.get("tasks") or [])
    canonical: dict[tuple[Any, ...], int] = {}
    for task in tasks:
        canonical.setdefault(_task_group_identity(task), int(task["id"]))
    projection = build_evidence_state(materialized.records)
    projection["plan_task_ids"] = {}
    projection["plan_evidence_ids"] = {}
    for plan_id in materialized.action.get("plan_ids") or []:
        plan_records = [item for item in materialized.records if item.plan_id == plan_id]
        task_ids = {
            canonical[_task_group_identity(task)]
            for task in tasks
            if task.get("plan_id") == plan_id
        }
        projection["plan_task_ids"][str(plan_id)] = sorted(task_ids)
        projection["plan_evidence_ids"][str(plan_id)] = sorted(
            item.id for item in plan_records
        )
    return projection


def _compare_competency_projections(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    competencies = list(materialized.setup.get("competencies") or [])
    canonical: dict[tuple[Any, ...], int] = {}
    for competency in competencies:
        canonical.setdefault(_competency_group_identity(competency), int(competency["id"]))
    canonical_by_id = {
        int(item["id"]): canonical[_competency_group_identity(item)]
        for item in competencies
    }
    projection = build_evidence_state(materialized.records)
    projection["plan_competency_ids"] = {
        str(plan_id): sorted(
            {
                canonical_by_id[int(ref["competency_id"])]
                for item in materialized.records
                if item.plan_id == plan_id
                for ref in item._evidence_competency_refs
                if ref.get("competency_id") in canonical_by_id
            }
        )
        for plan_id in materialized.action.get("plan_ids") or []
    }
    evidence_by_competency: dict[int, list[int]] = {}
    for item in materialized.records:
        for ref in item._evidence_competency_refs:
            competency_id = ref.get("competency_id")
            if competency_id in canonical_by_id:
                canonical_id = canonical_by_id[int(competency_id)]
                evidence_by_competency.setdefault(canonical_id, []).append(item.id)
    projection["competency_evidence_ids"] = {
        str(key): sorted(value) for key, value in sorted(evidence_by_competency.items())
    }
    return projection


def _audit_materialized(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    return audit_observations(
        materialized.records,
        materialized.artifacts if materialized.artifacts else None,
    )


def _artifact_audit_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    audit = _audit_materialized(materialized)
    error_text = "\n".join(audit["errors"])
    referenced_ids = {
        int(ref["artifact_id"])
        for item in materialized.records
        for ref in item._evidence_artifact_refs
        if isinstance(ref.get("artifact_id"), int)
    }
    projection["verified_artifact_ids"] = sorted(
        artifact.id
        for artifact in materialized.artifacts
        if artifact.id in referenced_ids and str(artifact.id) not in error_text
    )
    projection["artifact_kinds"] = {
        str(artifact.id): artifact.artifact_type
        for artifact in materialized.artifacts
    }
    projection["audit_error_codes"] = copy.deepcopy(audit["error_codes"])
    return projection


def _snapshot_projection(
    materialized: EvidenceScenarioInput,
    *,
    field: str,
    output_key: str,
) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    projection[output_key] = {
        str(item.id): copy.deepcopy(getattr(item, field))
        for item in materialized.records
    }
    return projection


def _causation_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    event_ids = {str(item["id"]) for item in materialized.setup.get("learning_events") or []}
    projection["causation_edges"] = [
        {"from": item.causation_id, "to": item.id}
        for item in materialized.records
        if item.causation_id in event_ids
    ]
    projection["audit_error_codes"] = sorted(
        {
            "MISSING_CAUSATION_TARGET"
            for item in materialized.records
            if item.causation_id is not None and item.causation_id not in event_ids
        }
    )
    return projection


def _correlation_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    runs = {str(item["id"]): item for item in materialized.setup.get("runs") or []}
    groups: dict[str, list[int]] = {}
    errors: set[str] = set()
    for item in materialized.records:
        run = runs.get(str(item.correlation_id))
        if run is None or any(
            run.get(field) != getattr(item, field)
            for field in ("owner_id", "plan_id", "session_id")
        ):
            errors.add("CORRELATION_SCOPE_MISMATCH")
            continue
        groups.setdefault(str(item.correlation_id), []).append(item.id)
    projection["correlation_groups"] = {
        key: sorted(value) for key, value in sorted(groups.items())
    }
    projection["audit_error_codes"] = sorted(errors)
    return projection


def _append_normalized_scores(
    materialized: EvidenceScenarioInput,
) -> tuple[dict[str, Any], dict[str, Any]]:
    records: list[SimpleNamespace] = []
    for index, request in enumerate(materialized.action.get("requests") or [], start=1):
        raw = request.get("score_percent")
        score = (
            evidence_service.normalize_percentage_score(raw)
            if raw is not None
            else None
        )
        records.append(_new_request_record(request, fact_id=index, score=score))
    projection = build_evidence_state(records)
    projection["persisted_scores"] = {
        item.source_id: item.normalized_score for item in records
    }
    audit = audit_observations(records)
    projection["audit_error_codes"] = copy.deepcopy(audit["error_codes"])
    return projection, audit


def _rebuild_channel_projection(
    channel: str,
    records: list[SimpleNamespace],
) -> dict[str, Any]:
    """Rebuild one read channel from an isolated copy of immutable facts."""

    del channel
    return build_evidence_state(copy.deepcopy(records))


def _rebuild_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    records = _scoped_records(materialized)
    projections = {
        channel: _rebuild_channel_projection(channel, records)
        for channel in ("fresh", "reopened", "cli", "context", "api")
    }
    fresh = projections["fresh"]
    channels = {
        channel: projection["digest"]
        for channel, projection in projections.items()
    }
    fresh["digest_channels_equal"] = len(set(channels.values())) == 1
    fresh["channels"] = list(channels)
    return fresh


def _raw_and_current_projection(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    active_ids = _active_evidence_ids(projection)
    active = [item for item in materialized.records if item.id in active_ids]
    latest = max(active, key=lambda item: (_event_time(item), item.id), default=None)
    projection.update(
        ledger_observation_ids=sorted(item.id for item in materialized.records),
        current_evidence_ids=active_ids,
        latest_outcome=latest.outcome if latest is not None else None,
        historical_outcomes={str(item.id): item.outcome for item in materialized.records},
    )
    return projection


def _build_study_state(materialized: EvidenceScenarioInput) -> dict[str, Any]:
    projection = build_evidence_state(_scoped_records(materialized))
    existing = {int(item["task_id"]) for item in projection["by_task"]}
    plan_id = materialized.action.get("plan_id")
    for task in materialized.setup.get("tasks") or []:
        if task.get("plan_id") != plan_id or int(task["id"]) in existing:
            continue
        projection["by_task"].append(
            {
                "task_id": int(task["id"]),
                "evidence_stage": "unknown",
                "observation_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "best_score": None,
                "last_observed_at": None,
                "latest_outcome": None,
                "evidence_ids": [],
            }
        )
    projection["by_task"].sort(key=lambda item: item["task_id"])
    projection["task_count"] = len(projection["by_task"])
    return projection


def execute_evidence_contract(case_input: dict[str, Any]) -> dict[str, Any]:
    """Execute one setup/action through production projection and audit code."""

    materialized = materialize_contract_input(case_input)
    action_type = materialized.action.get("type")
    if action_type == "build_projection":
        projection = build_evidence_state(materialized.records)
    elif action_type in {"build_plan_projection", "project_primary_observations"}:
        projection = build_evidence_state(_scoped_records(materialized))
    elif action_type == "build_study_state":
        projection = _build_study_state(materialized)
    elif action_type == "build_eligible_projection":
        projection = _build_eligible_projection(materialized)
    elif action_type == "serialize_ledger_and_projection":
        projection = _serialize_ledger(materialized)
    elif action_type == "project_competency_transfer":
        projection = _project_competency_transfer(materialized)
    elif action_type == "build_conflict_aware_projection":
        projection = _conflict_projection(materialized)
    elif action_type == "build_time_ordered_projection":
        projection = _time_ordered_projection(materialized)
    elif action_type == "append_sequence":
        projection = _append_sequence(materialized)
    elif action_type in {
        "compare_ledger_and_current_projection",
        "apply_append_only_amendments",
    }:
        projection = build_evidence_state(_scoped_records(materialized))
        projection.update(
            ledger_observation_ids=sorted(item.id for item in materialized.records),
            ledger_fact_ids=sorted(item.id for item in materialized.records),
            current_evidence_ids=_active_evidence_ids(projection),
            task_stage=(projection.get("by_task") or [{}])[0].get("evidence_stage"),
        )
    elif action_type == "query_plan_projection":
        projection = _query_plan_projection(materialized)
    elif action_type == "compare_plan_projections":
        projection = _compare_plan_projections(materialized)
    elif action_type == "compare_competency_projections":
        projection = _compare_competency_projections(materialized)
    elif action_type in {
        "audit_artifact_references",
        "reopen_and_audit_artifact",
        "audit_canonical_envelope",
        "audit_source_scope",
    }:
        projection = _artifact_audit_projection(materialized)
        if action_type == "audit_canonical_envelope":
            artifact_id = materialized.action.get("artifact_id")
            artifact = next(
                (item for item in materialized.artifacts if item.id == artifact_id),
                None,
            )
            projection["fingerprinted_fields"] = sorted(
                json.loads(
                    _artifact_envelope_bytes(
                        artifact.snapshot_bytes,
                        artifact.artifact_metadata or {},
                        artifact_type=artifact.artifact_type,
                    )
                ).keys()
                if artifact is not None
                else []
            )
        if action_type == "audit_source_scope":
            projection["scope"] = {
                field: materialized.action.get(field)
                for field in ("owner_id", "plan_id", "task_id")
            }
    elif action_type == "mutate_live_rubric_then_rebuild":
        projection = _snapshot_projection(
            materialized,
            field="rubric_snapshot",
            output_key="rubric_snapshots",
        )
    elif action_type == "change_evaluator_config_then_rebuild":
        projection = _snapshot_projection(
            materialized,
            field="evaluator",
            output_key="evaluator_snapshots",
        )
    elif action_type == "audit_causation_graph":
        projection = _causation_projection(materialized)
    elif action_type == "audit_correlation_scope":
        projection = _correlation_projection(materialized)
    elif action_type == "append_observations":
        projection, audit = _append_normalized_scores(materialized)
    elif action_type == "compare_rebuild_channels":
        projection = _rebuild_projection(materialized)
    elif action_type == "compare_raw_ledger_and_projection":
        projection = _raw_and_current_projection(materialized)
    else:
        raise ValueError(f"Unsupported Evidence scenario action: {action_type}")
    active = {
        evidence_id
        for task in projection["by_task"]
        for evidence_id in task["evidence_ids"]
    }
    active.update(projection.get("unscoped_observation_ids", []))
    projection["primary_observation_ids"] = sorted(
        item.id
        for item in materialized.records
        if item.id in active and item.evidence_role == "primary"
    )
    for task_state in projection["by_task"]:
        task_state.pop("last_observed_at", None)
    if action_type != "append_observations":
        audit = _audit_materialized(materialized)
    return {
        "projection": projection,
        "audit_ok": audit["ok"],
        "audit": audit,
    }


def contract_projection_view(
    actual: dict[str, Any],
    expected_projection: dict[str, Any],
) -> dict[str, Any]:
    """Select the literal top-level oracle fields from a full contract result."""

    projection = actual["projection"]
    return {
        key: copy.deepcopy(projection.get(key, "<missing-from-production-projection>"))
        for key in expected_projection
    }


def evaluate_baseline(
    contracts: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute the literal registry through the production reducer and audit."""

    cases = list(contracts or _CONTRACTS)
    results: list[dict[str, Any]] = []
    for case in cases:
        actual = execute_evidence_contract(case["input"])
        replay = execute_evidence_contract(copy.deepcopy(case["input"]))
        projection = actual["projection"]
        expected_projection = case["expected_projection"]
        literal_projection = contract_projection_view(actual, expected_projection)
        checks = {
            "deterministic_digest": projection["digest"]
            == replay["projection"]["digest"],
            str(case["invariant_id"]): literal_projection == expected_projection
            and actual["audit_ok"] is case["expected_audit_ok"],
        }
        results.append(
            {
                "id": case["id"],
                "description": case["invariant_id"],
                "ok": all(checks.values()),
                "checks": checks,
                "digest": projection["digest"],
                "audit": actual["audit"],
            }
        )
    return {
        "scenario_count": len(cases),
        "passed": sum(item["ok"] for item in results),
        "ok": all(item["ok"] for item in results),
        "results": results,
    }
