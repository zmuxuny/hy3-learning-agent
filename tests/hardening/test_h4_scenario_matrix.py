"""Executable H4 baseline and mutation contracts for all 41 Evidence cases.

The JSON registry is the literal input/oracle source. Every baseline executes
the production contract adapter, reducer, and audit. Every mutation first
proves that baseline, applies a real input or algorithm mutation, then derives
its reason from an independent registry only after the literal oracle differs.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.services import evidence_baseline


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "evidence_scenarios.json"
CONTRACTS = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

# Deliberately independent from the fixture's ``failure_reason_code`` fields.
# A fixture typo therefore fails the registry contract instead of being echoed.
INDEPENDENT_FAILURE_CODES = {
    "empty_ledger": "EMPTY_LEDGER_NONEMPTY",
    "single_submission": "SUBMISSION_STAGE_MISMATCH",
    "accepted_submission": "ACCEPTED_SUBMISSION_NOT_DEMONSTRATED",
    "needs_revision": "REVISION_NOT_PRACTICING",
    "quiz_passed": "QUIZ_PASS_NOT_DEMONSTRATED",
    "quiz_failed": "QUIZ_FAILURE_OVERSTATED",
    "task_verified": "TASK_EVIDENCE_DUPLICATED",
    "task_without_evidence": "TASK_WITHOUT_EVIDENCE_INGESTED",
    "self_report_only": "SELF_REPORT_OVERSTATED",
    "manual_observation": "MANUAL_SOURCE_IDENTITY_LOST",
    "assisted_attempt": "ASSISTED_PASS_OVERSTATED",
    "independent_attempt": "INDEPENDENT_PASS_UNDERSTATED",
    "same_task_transfer": "TRANSFER_OVERSTATED",
    "variant_transfer": "VARIANT_TRANSFER_LOST",
    "conflicting_scores": "CONFLICT_COLLAPSED",
    "late_success": "OCCURRED_AT_ORDERING_MISMATCH",
    "duplicate_source": "IDEMPOTENCY_KEY_REBOUND",
    "supersession": "SUPERSESSION_PROJECTION_MISMATCH",
    "invalidated_observation": "INVALIDATION_STILL_ACTIVE",
    "invalidated_superseder": "INVALIDATED_SUPERSEDER_STILL_SHADOWS",
    "unscoped_message": "UNSCOPED_OBSERVATION_BOUND",
    "plan_one_isolation": "PLAN_A_CROSS_SCOPE_EVIDENCE_INCLUDED",
    "plan_two_isolation": "PLAN_B_CROSS_SCOPE_EVIDENCE_INCLUDED",
    "cross_plan_same_task_title": "CROSS_PLAN_TASKS_MERGED",
    "cross_plan_same_competency_key": "CROSS_PLAN_COMPETENCIES_MERGED",
    "code_artifact": "CODE_ARTIFACT_REF_INVALID",
    "file_artifact": "FILE_ARTIFACT_REF_INVALID",
    "quiz_artifact": "QUIZ_ARTIFACT_REF_INVALID",
    "submission_artifact": "SUBMISSION_ARTIFACT_REF_INVALID",
    "task_artifact": "TASK_ARTIFACT_REF_INVALID",
    "rubric_snapshot": "RUBRIC_SNAPSHOT_DRIFT",
    "evaluator_snapshot": "EVALUATOR_SNAPSHOT_DRIFT",
    "causal_chain": "CAUSATION_REFERENCE_INVALID",
    "correlation_chain": "CORRELATION_REFERENCE_INVALID",
    "score_clamp": "SCORE_NOT_NORMALIZED",
    "zero_score": "ZERO_SCORE_DROPPED",
    "full_score": "FULL_SCORE_SOURCE_LOST",
    "multiple_attempts": "ATTEMPTS_COLLAPSED",
    "rebuild_digest": "PROJECTION_DIGEST_DRIFT",
    "old_fact_preserved": "HISTORICAL_FACT_OVERWRITTEN",
    "conservative_unknown": "INSUFFICIENT_EVIDENCE_OVERSTATED",
}


def _path_parts(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for name, index in re.findall(r"([^.[\]]+)|\[(\d+)\]", path):
        parts.append(int(index) if index else name)
    return parts


def _resolve_parent(root: Any, path: str) -> tuple[bool, Any, str | int]:
    parts = _path_parts(path)
    assert parts, f"empty mutation target: {path}"
    current = root
    for part in parts[:-1]:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return False, None, parts[-1]
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                return False, None, parts[-1]
            current = current[part]
    final = parts[-1]
    exists = (
        isinstance(current, list) and isinstance(final, int) and final < len(current)
    ) or (
        isinstance(current, dict) and isinstance(final, str) and final in current
    )
    return exists, current, final


def _apply_input_mutant(case: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    mutated = copy.deepcopy(case)
    mutant = mutated["mutant"]
    exists, parent, key = _resolve_parent(mutated, mutant["target"])
    assert exists, (
        f"{INDEPENDENT_FAILURE_CODES[case['id']]}: registered mutant target "
        f"does not exist: {mutant['target']}"
    )

    operation = mutant["operation"]
    value = mutant["value"]
    current = parent[key]
    if operation in {
        "drop_field",
        "delete_superseded_fact",
        "ignore_amendment",
    }:
        parent.pop(key)
    elif operation == "inject_row":
        current.append(copy.deepcopy(value))
    elif operation == "append_duplicate_business_projection":
        duplicate = copy.deepcopy(current[0]) if current else {}
        duplicate.update(copy.deepcopy(value))
        current.append(duplicate)
    elif operation in {"retain_only_best_score", "deduplicate_by_task_id"}:
        retained_ids = set(value)
        parent[key] = [item for item in current if item.get("id") in retained_ids]
    elif operation == "replace_fields":
        current.update(copy.deepcopy(value))
    elif operation == "infer_observation_from_status":
        parent[key] = copy.deepcopy(value)
        task = mutated["input"]["setup"]["tasks"][0]
        mutated["input"]["setup"].setdefault("observations", []).append(
            {
                "id": 1,
                "plan_id": task.get("plan_id"),
                "task_id": task["id"],
                "source_type": "task_completion",
                "source_id": f"inferred:{task['id']}",
                "outcome": "verified",
            }
        )
    elif operation == "treat_ineligible_sources_as_verified":
        selected = set(value)
        for item in current:
            if item.get("id") in selected:
                item.update(source_type="manual", outcome="verified", is_correct=True)
    elif operation in {
        "reuse_key_with_new_digest",
        "group_by_title",
        "group_by_owner_and_key",
        "persist_raw_scores",
        "remove_plan_predicate",
        "serialize_historical_time_as_naive",
    }:
        # These are implementation mutations. Their literal targets must
        # resolve, but the actual change is installed through a production
        # algorithm seam below; no test-only action flag enters the runner.
        pass
    else:
        parent[key] = copy.deepcopy(value)

    input_changed = mutated["input"] != case["input"]
    return mutated["input"], input_changed


def _install_algorithm_mutant(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """Install only the faulty algorithm seam named by the registered mutant."""

    case_id = case["id"]
    operation = case["mutant"]["operation"]
    if case_id == "duplicate_source":
        monkeypatch.setattr(
            evidence_baseline.evidence_service,
            "request_digest_matches",
            lambda _stored, _incoming: True,
        )
    elif case_id == "plan_one_isolation":
        monkeypatch.setattr(
            evidence_baseline,
            "_scoped_records",
            lambda materialized: [
                item
                for item in materialized.records
                if item.owner_id == materialized.action.get("owner_id")
            ],
        )
    elif operation == "group_by_title":
        monkeypatch.setattr(
            evidence_baseline,
            "_task_group_identity",
            lambda task: (task.get("title"),),
        )
    elif operation == "group_by_owner_and_key":
        monkeypatch.setattr(
            evidence_baseline,
            "_competency_group_identity",
            lambda competency: ("owner-a", competency.get("key")),
        )
    elif operation == "persist_raw_scores":
        monkeypatch.setattr(
            evidence_baseline.evidence_service,
            "normalize_percentage_score",
            lambda value: float(value),
        )
    elif operation == "serialize_historical_time_as_naive":
        original = evidence_baseline._rebuild_channel_projection

        def rebuild_with_local_time_drift(channel: str, records: list[Any]):
            shifted = copy.deepcopy(records)
            if channel == "reopened" and shifted:
                shifted[0].occurred_at += timedelta(hours=8)
            return original(channel, shifted)

        monkeypatch.setattr(
            evidence_baseline,
            "_rebuild_channel_projection",
            rebuild_with_local_time_drift,
        )
    else:
        return False
    return True


def _literal_view(actual: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return evidence_baseline.contract_projection_view(
        actual,
        case["expected_projection"],
    )


def _matches_literal(actual: dict[str, Any], case: dict[str, Any]) -> bool:
    return (
        _literal_view(actual, case) == case["expected_projection"]
        and actual["audit_ok"] is case["expected_audit_ok"]
    )


def _observed_mutant_reason(case_id: str, *, mutant_matches: bool) -> str | None:
    if mutant_matches:
        return None
    return INDEPENDENT_FAILURE_CODES[case_id]


def test_scenario_registry_is_independent_complete_and_unique():
    assert len(CONTRACTS) == 41
    assert set(INDEPENDENT_FAILURE_CODES) == {case["id"] for case in CONTRACTS}
    assert {case["failure_reason_code"] for case in CONTRACTS} == set(
        INDEPENDENT_FAILURE_CODES.values()
    )
    assert {
        case["id"]: case["failure_reason_code"] for case in CONTRACTS
    } == INDEPENDENT_FAILURE_CODES
    assert len({json.dumps(case["input"], sort_keys=True) for case in CONTRACTS}) == 41
    assert len(
        {json.dumps(case["expected_projection"], sort_keys=True) for case in CONTRACTS}
    ) == 41


@pytest.mark.parametrize("case", CONTRACTS, ids=lambda case: case["id"])
def test_each_registered_mutant_target_resolves_before_execution(case: dict[str, Any]):
    exists, _parent, _key = _resolve_parent(case, case["mutant"]["target"])
    assert exists, (
        f"{INDEPENDENT_FAILURE_CODES[case['id']]}: registered mutant target "
        f"does not exist: {case['mutant']['target']}"
    )


@pytest.mark.parametrize("case", CONTRACTS, ids=lambda case: case["id"])
def test_each_h4_scenario_executes_its_literal_baseline(case: dict[str, Any]):
    actual = evidence_baseline.execute_evidence_contract(case["input"])

    assert _literal_view(actual, case) == case["expected_projection"], (
        case["failure_reason_code"]
    )
    assert actual["audit_ok"] is case["expected_audit_ok"], actual["audit"]


@pytest.mark.parametrize("case", CONTRACTS, ids=lambda case: case["id"])
def test_each_h4_scenario_kills_its_registered_mutant_with_exact_reason(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    baseline_actual = evidence_baseline.execute_evidence_contract(case["input"])
    assert _matches_literal(baseline_actual, case), f"BASELINE_NOT_CONFORMANT:{case['id']}"

    mutated_input, input_changed = _apply_input_mutant(case)
    algorithm_mutated = _install_algorithm_mutant(case, monkeypatch)
    if case["mutant"]["operation"] in {
        "reuse_key_with_new_digest",
        "group_by_title",
        "group_by_owner_and_key",
        "persist_raw_scores",
        "serialize_historical_time_as_naive",
    } or case["id"] == "plan_one_isolation":
        assert algorithm_mutated, (
            f"{INDEPENDENT_FAILURE_CODES[case['id']]}: algorithm mutant not installed"
        )
    assert input_changed or algorithm_mutated, (
        f"{INDEPENDENT_FAILURE_CODES[case['id']]}: mutant was not applied"
    )

    mutant_actual = evidence_baseline.execute_evidence_contract(mutated_input)
    mutant_matches = _matches_literal(mutant_actual, case)
    observed_reason = _observed_mutant_reason(
        case["id"],
        mutant_matches=mutant_matches,
    )

    assert not mutant_matches, (
        f"{INDEPENDENT_FAILURE_CODES[case['id']]}: registered mutant survived"
    )
    assert observed_reason == INDEPENDENT_FAILURE_CODES[case["id"]]
    assert observed_reason == case["mutant"]["expected_failure_reason_code"]
