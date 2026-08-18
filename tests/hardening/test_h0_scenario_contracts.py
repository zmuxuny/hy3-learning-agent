from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.services.evidence_baseline import SCENARIOS, evaluate_baseline, records_for


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "evidence_scenarios.json"
REQUIRED_FIELDS = {
    "id",
    "legacy_index",
    "status",
    "input",
    "expected_projection",
    "expected_audit_ok",
    "failure_reason_code",
    "invariant_id",
    "mutant",
}
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+$")
INVARIANT_PATTERN = re.compile(r"^EV-[A-Z0-9-]+$")
FORBIDDEN_ORACLE_MARKERS = {"todo", "tbd", "placeholder", "computed at runtime"}
LEGACY_GENERIC_CHECKS = {"deterministic_digest", "bounded_stage"}


def _load_contracts() -> list[dict[str, Any]]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


CONTRACTS = _load_contracts()


def _fingerprint(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_h0_scenario_contract_registry_matches_the_legacy_41_names_exactly():
    expected = [(index, item["id"]) for index, item in enumerate(SCENARIOS, start=1)]
    actual = [(case["legacy_index"], case["id"]) for case in CONTRACTS]

    assert len(CONTRACTS) == 41
    assert actual == expected


def test_h0_scenario_contracts_are_complete_and_structured():
    for case in CONTRACTS:
        case_id = case["id"]
        assert set(case) == REQUIRED_FIELDS, case_id
        assert type(case["legacy_index"]) is int, case_id
        assert 1 <= case["legacy_index"] <= 41, case_id
        assert case["status"] == "pending_rewrite", case_id

        case_input = case["input"]
        assert set(case_input) == {"setup", "action"}, case_id
        assert isinstance(case_input["setup"], dict) and case_input["setup"], case_id
        assert isinstance(case_input["action"], dict) and case_input["action"], case_id
        assert isinstance(case_input["action"].get("type"), str), case_id
        assert case_input["action"]["type"], case_id

        projection = case["expected_projection"]
        assert isinstance(projection, dict) and projection, case_id
        assert type(projection.get("observation_count")) is int, case_id
        assert projection["observation_count"] >= 0, case_id
        assert isinstance(projection.get("reason_codes"), list), case_id
        assert projection["reason_codes"], case_id

        assert type(case["expected_audit_ok"]) is bool, case_id
        assert CODE_PATTERN.fullmatch(case["failure_reason_code"]), case_id
        assert INVARIANT_PATTERN.fullmatch(case["invariant_id"]), case_id

        mutant = case["mutant"]
        assert set(mutant) == {"operation", "target", "value", "expected_failure_reason_code"}, case_id
        assert isinstance(mutant["operation"], str) and mutant["operation"], case_id
        assert isinstance(mutant["target"], str) and mutant["target"], case_id
        assert mutant["target"].startswith("input."), case_id
        assert mutant["expected_failure_reason_code"] == case["failure_reason_code"], case_id


def test_h0_scenario_expected_projections_are_literal_oracles():
    for case in CONTRACTS:
        case_id = case["id"]
        oracle_strings = {item.strip().lower() for item in _strings(case["expected_projection"])}

        assert oracle_strings.isdisjoint(FORBIDDEN_ORACLE_MARKERS), case_id
        assert not any("${" in item or "{{" in item for item in oracle_strings), case_id


def test_h0_scenario_contracts_have_independent_inputs_oracles_and_mutants():
    ids = [case["id"] for case in CONTRACTS]
    indexes = [case["legacy_index"] for case in CONTRACTS]
    failure_codes = [case["failure_reason_code"] for case in CONTRACTS]
    invariant_ids = [case["invariant_id"] for case in CONTRACTS]
    input_fingerprints = [_fingerprint(case["input"]) for case in CONTRACTS]
    oracle_fingerprints = [_fingerprint(case["expected_projection"]) for case in CONTRACTS]
    mutant_fingerprints = [_fingerprint(case["mutant"]) for case in CONTRACTS]

    assert len(set(ids)) == len(CONTRACTS)
    assert len(set(indexes)) == len(CONTRACTS)
    assert len(set(failure_codes)) == len(CONTRACTS)
    assert len(set(invariant_ids)) == len(CONTRACTS)
    assert len(set(input_fingerprints)) == len(CONTRACTS)
    assert len(set(oracle_fingerprints)) == len(CONTRACTS)
    assert len(set(mutant_fingerprints)) == len(CONTRACTS)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H0-COV-001: legacy 41-case report reuses fixture inputs and generic checks; "
        "it is not 41 independent domain regression scenarios"
    ),
)
def test_h0_cov_001_legacy_suite_has_41_independent_inputs_and_specific_oracles():
    input_fingerprints = {
        _fingerprint([vars(record) for record in records_for(item["id"])])
        for item in SCENARIOS
    }
    report_by_id = {item["id"]: item for item in evaluate_baseline()["results"]}
    scenario_specific_oracles = {
        scenario_id
        for scenario_id, result in report_by_id.items()
        if set(result["checks"]) - LEGACY_GENERIC_CHECKS
    }

    assert (len(input_fingerprints), len(scenario_specific_oracles)) == (41, 41), (
        "legacy baseline currently has "
        f"{len(input_fingerprints)} distinct input fingerprints and "
        f"{len(scenario_specific_oracles)} scenario-specific oracles"
    )
