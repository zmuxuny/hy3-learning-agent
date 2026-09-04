"""CaseSpec authority and deterministic label-free JudgeReference projection."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .canonical import sha256_digest
from .integrity import case_spec_digest, judge_reference_digest, oracle_envelope_digest
from .models import CaseSpecV1, CaseSpecV2, JudgeReferenceV1, JudgeReferenceV2


class CaseSpecError(ValueError):
    """Case control-plane data is invalid or cannot be projected safely."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_case_spec(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        case = CaseSpecV1.model_validate(document).model_dump(mode="json")
    except ValueError as exc:
        raise CaseSpecError("case_spec.contract_invalid") from exc
    if case["case_spec_sha256"] != case_spec_digest(case):
        raise CaseSpecError("case_spec.digest_mismatch")
    return case


def validate_case_spec_v2(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the active CaseSpec contract and its self digest."""

    try:
        case = CaseSpecV2.model_validate(document).model_dump(mode="json")
    except ValueError as exc:
        raise CaseSpecError("case_spec.contract_invalid") from exc
    if case["case_spec_sha256"] != case_spec_digest(case):
        raise CaseSpecError("case_spec.digest_mismatch")
    return case


def episode_id_for_case(case: Mapping[str, Any]) -> str:
    """Return an opaque stable ID that cannot carry a human quality label."""

    return f"episode-{str(case['case_spec_sha256'])[:24]}"


def build_judge_reference(case: Mapping[str, Any]) -> dict[str, Any]:
    """Project exactly the label-free criteria; independent authoring is impossible."""

    validated = validate_case_spec(case)
    criteria = validated["judge_criteria"]
    reference = {
        "schema_version": "judge-reference-v1",
        "opaque_case_id": f"case-{validated['case_spec_sha256'][:24]}",
        "case_spec_sha256": validated["case_spec_sha256"],
        "track": validated["track"],
        "allowed_action_classes": deepcopy(criteria["allowed_action_classes"]),
        "constraints": deepcopy(criteria["constraints"]),
        "acceptable_variations": deepcopy(criteria["acceptable_variations"]),
        "reference_sha256": "0" * 64,
    }
    reference["reference_sha256"] = judge_reference_digest(reference)
    return JudgeReferenceV1.model_validate(reference).model_dump(mode="json")


def build_judge_reference_v2(case: Mapping[str, Any]) -> dict[str, Any]:
    """Project active label-free criteria with frozen predicate semantics."""

    validated = validate_case_spec_v2(case)
    criteria = validated["judge_criteria"]
    reference = {
        "schema_version": "judge-reference-v2",
        "opaque_case_id": f"case-{validated['case_spec_sha256'][:24]}",
        "case_spec_sha256": validated["case_spec_sha256"],
        "track": validated["track"],
        "allowed_action_classes": deepcopy(criteria["allowed_action_classes"]),
        "constraints": deepcopy(criteria["constraints"]),
        "acceptable_variations": deepcopy(criteria["acceptable_variations"]),
        "predicate_semantics": criteria["predicate_semantics"],
        "reference_sha256": "0" * 64,
    }
    reference["reference_sha256"] = judge_reference_digest(reference)
    return JudgeReferenceV2.model_validate(reference).model_dump(mode="json")


def legacy_runtime_projection(case: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt v3 control input to the already-audited E1 seeding functions.

    This object is process-local and is never published as a fixture or used as
    a second source of expected outcomes.
    """

    validated = validate_case_spec(case)
    runtime = validated["runtime_setup"]
    return {
        "schema_version": "e1-runtime-mini-fixture-v1",
        "episode_id": episode_id_for_case(validated),
        "scenario_family_id": validated["scenario_family_id"],
        "track": validated["track"],
        "split": validated["split"],
        "difficulty": validated["difficulty"],
        "frozen_time": runtime["frozen_time"],
        "timezone": runtime["timezone"],
        "owner_id": runtime["owner_id"],
        "run_id": runtime["run_id"],
        "session_id": runtime["session_id"],
        "trigger": deepcopy(runtime["trigger"]),
        "state_before": deepcopy(runtime["state_before"]),
        "seed_kind": runtime["seed_kind"],
        "seed": deepcopy(runtime["seed"]),
        "scripted_turns": deepcopy(runtime["scripted_turns"]),
        "oracle_file": "not-published",
        "resource_snapshot_version": runtime["resource_snapshot_version"],
        "engineering_only": validated["dataset_role"] == "engineering_mini",
        "fixture_sha256": sha256_digest(
            {
                "source": "case-spec-v1",
                "case_spec_sha256": validated["case_spec_sha256"],
            }
        ),
    }


def legacy_runtime_projection_v2(case: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the active CaseSpec to existing deterministic Runtime seeders."""

    validated = validate_case_spec_v2(case)
    runtime = validated["runtime_setup"]
    return {
        "schema_version": "e1-runtime-mini-fixture-v1",
        "episode_id": episode_id_for_case(validated),
        "scenario_family_id": validated["scenario_family_id"],
        "track": validated["track"],
        "split": validated["split"],
        "difficulty": validated["difficulty"],
        "frozen_time": runtime["frozen_time"],
        "timezone": runtime["timezone"],
        "owner_id": runtime["owner_id"],
        "run_id": runtime["run_id"],
        "session_id": runtime["session_id"],
        "trigger": deepcopy(runtime["trigger"]),
        "state_before": deepcopy(runtime["state_before"]),
        "seed_kind": runtime["seed_kind"],
        "seed": deepcopy(runtime["seed"]),
        "scripted_turns": deepcopy(runtime["scripted_turns"]),
        "oracle_file": "not-published",
        "resource_snapshot_version": runtime["resource_snapshot_version"],
        "engineering_only": validated["dataset_role"] == "engineering_mini",
        "fixture_sha256": sha256_digest(
            {
                "source": "case-spec-v2",
                "case_spec_sha256": validated["case_spec_sha256"],
            }
        ),
    }


def internal_action_envelope(reference: Mapping[str, Any]) -> dict[str, Any]:
    """Build an unpublished adapter for the E2 mechanical Runtime exporter."""

    constraints = list(reference["constraints"])
    requirements = [item for item in constraints if item["kind"] == "must_satisfy"]
    prohibitions = [item for item in constraints if item["kind"] == "must_not"]
    if not requirements or not prohibitions:
        raise CaseSpecError("judge_reference.constraint_kinds_incomplete")
    expected_effects: list[dict[str, Any]] = []
    for constraint in constraints:
        for predicate in constraint["predicates"]:
            if predicate["operator"] == "equals":
                expected_effects.append(
                    {
                        "path": predicate["path"],
                        "relation": "equals",
                        "value": deepcopy(predicate["expected_value"]),
                    }
                )
    if not expected_effects:
        expected_effects.append(
            {
                "path": "result.action_class",
                "relation": "equals",
                "value": reference["allowed_action_classes"][0],
            }
        )
    envelope = {
        "schema_version": "acceptable-action-envelope-v1",
        "allowed_action_classes": deepcopy(reference["allowed_action_classes"]),
        "must_satisfy": [
            {
                "id": item["constraint_id"],
                "statement": item["public_statement"],
                "evidence_paths": deepcopy(item["evidence_paths"]),
            }
            for item in requirements
        ],
        "must_not": [
            {
                "id": item["constraint_id"],
                "statement": item["public_statement"],
                "criticality": item["criticality"],
            }
            for item in prohibitions
        ],
        "expected_effects": expected_effects,
        "acceptable_variations": deepcopy(reference["acceptable_variations"]),
        "critical_failures": [
            item["public_statement"]
            for item in prohibitions
            if item["criticality"] == "critical"
        ]
        or ["A critical prohibited effect must never occur."],
        "oracle_author": "case_spec_author",
        "oracle_reviewer": "case_spec_reviewer",
        "adjudication_note": None,
        "envelope_sha256": "0" * 64,
    }
    envelope["envelope_sha256"] = oracle_envelope_digest(envelope)
    return envelope
