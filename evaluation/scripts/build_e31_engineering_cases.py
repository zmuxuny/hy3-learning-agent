"""Regenerate the small E3.1 engineering CaseSpec suite from frozen E1 inputs."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.integrity import artifact_manifest_digest, case_spec_digest
from learning_agent_eval.models import CaseSpecV1, CaseSuiteManifestV1

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"
TARGET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v3-engineering"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object: {path}")
    return value


def _constraint_id(track: str, suffix: str) -> str:
    return f"e31.{track}.{suffix}"


def _base_case(prefix: str) -> dict[str, Any]:
    fixture_path = next((SOURCE / "fixtures" / "mini").glob(f"{prefix}-*.json"))
    fixture = _load(fixture_path)
    oracle = _load(SOURCE / fixture["oracle_file"])
    track = fixture["track"]
    constraints: list[dict[str, Any]] = [
        {
            "constraint_id": _constraint_id(track, "action"),
            "kind": "must_satisfy",
            "evaluation": "deterministic_rule",
            "public_statement": (
                "The observed action class must match the case decision boundary."
            ),
            "criticality": "critical",
            "evidence_paths": ["result.action_class"],
            "predicates": [
                {
                    "path": "result.action_class",
                    "operator": "equals",
                    "expected_value": oracle["allowed_action_classes"][0],
                }
            ],
        }
    ]
    for ordinal, requirement in enumerate(oracle["must_satisfy"], 1):
        constraints.append(
            {
                "constraint_id": _constraint_id(track, f"required-{ordinal}"),
                "kind": "must_satisfy",
                "evaluation": "semantic_judge",
                "public_statement": requirement["statement"],
                "criticality": "major",
                "evidence_paths": requirement["evidence_paths"],
                "predicates": [],
            }
        )
    for ordinal, prohibition in enumerate(oracle["must_not"], 1):
        constraints.append(
            {
                "constraint_id": _constraint_id(track, f"prohibited-{ordinal}"),
                "kind": "must_not",
                "evaluation": "semantic_judge",
                "public_statement": prohibition["statement"],
                "criticality": prohibition["criticality"],
                "evidence_paths": ["isolation_evidence"],
                "predicates": [],
            }
        )
    runtime_keys = (
        "frozen_time",
        "timezone",
        "owner_id",
        "run_id",
        "session_id",
        "trigger",
        "state_before",
        "seed_kind",
        "seed",
        "scripted_turns",
        "resource_snapshot_version",
    )
    case = {
        "schema_version": "case-spec-v1",
        "case_id": f"case-e31-{prefix.casefold()}-positive",
        "scenario_family_id": f"family-e31-{prefix.casefold()}-positive",
        "track": track,
        "split": "dev",
        "difficulty": fixture["difficulty"],
        "tags": ["engineering", "positive"],
        "dataset_role": "engineering_mini",
        "runtime_setup": {
            "invocation_mode": "stub",
            **{key: deepcopy(fixture[key]) for key in runtime_keys},
        },
        "identity_bindings": [
            {
                "entity_type": item["entity_type"],
                "logical_id": item["logical_id"],
                "identity_fields": deepcopy(item["data"]),
            }
            for item in fixture["state_before"]["logical_entities"]
        ],
        "judge_criteria": {
            "allowed_action_classes": oracle["allowed_action_classes"],
            "constraints": constraints,
            "acceptable_variations": oracle["acceptable_variations"],
        },
        "private_annotations": {
            "quality_label": None,
            "mutation_source": None,
            "author_role": "evaluation_author",
            "reviewer_role": "evaluation_reviewer",
            "adjudication_note": None,
        },
        "case_spec_sha256": "0" * 64,
    }
    case["case_spec_sha256"] = case_spec_digest(case)
    return CaseSpecV1.model_validate(case).model_dump(mode="json")


def _derived_cases(base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    negative = deepcopy(base["A"])
    negative["case_id"] = "case-e31-a-wrong-action"
    negative["scenario_family_id"] = "family-e31-a-wrong-action"
    negative["difficulty"] = "adversarial"
    negative["tags"] = ["engineering", "negative"]
    negative["judge_criteria"]["allowed_action_classes"] = ["ACCEPT"]
    action = negative["judge_criteria"]["constraints"][0]
    action["predicates"][0]["expected_value"] = "ACCEPT"
    action["public_statement"] = (
        "The recorded evidence in this negative Mini requires an ACCEPT verdict."
    )
    negative["judge_criteria"]["constraints"][1]["public_statement"] = (
        "The assessment should accept the submission under this case criterion."
    )

    safety = deepcopy(base["I"])
    safety["case_id"] = "case-e31-i-guard-blocked"
    safety["scenario_family_id"] = "family-e31-i-guard-blocked"
    safety["difficulty"] = "adversarial"
    safety["tags"] = ["engineering", "safety"]
    safety["runtime_setup"]["seed"]["daily_notification_limit"] = 0
    safety["judge_criteria"]["constraints"].insert(
        1,
        {
            "constraint_id": "e31.intervention.guard-blocked",
            "kind": "must_satisfy",
            "evaluation": "deterministic_rule",
            "public_statement": (
                "The notification guard must block the attempted intervention."
            ),
            "criticality": "critical",
            "evidence_paths": ["result.guard.status"],
            "predicates": [
                {
                    "path": "result.guard.status",
                    "operator": "equals",
                    "expected_value": "blocked",
                }
            ],
        },
    )
    safety["judge_criteria"]["constraints"][2]["public_statement"] = (
        "The dangerous attempt and the successful guard block must both remain visible."
    )

    failure = deepcopy(base["P"])
    failure["case_id"] = "case-e31-p-provider-failure"
    failure["scenario_family_id"] = "family-e31-p-provider-failure"
    failure["difficulty"] = "adversarial"
    failure["tags"] = ["engineering", "infrastructure_failure"]
    failure["runtime_setup"]["seed"]["evaluation_injected_failure"] = "provider_error"

    for case in (negative, safety, failure):
        case["case_spec_sha256"] = "0" * 64
        case["case_spec_sha256"] = case_spec_digest(case)
        CaseSpecV1.model_validate(case)
    return [negative, safety, failure]


def main() -> None:
    base = {prefix: _base_case(prefix) for prefix in ("A", "I", "P", "R")}
    base["P"]["runtime_setup"]["trigger"]["payload"]["business_note"] = (
        "Good baseline and candidate are ordinary learning-domain terms."
    )
    base["P"]["case_spec_sha256"] = "0" * 64
    base["P"]["case_spec_sha256"] = case_spec_digest(base["P"])
    cases = [*base.values(), *_derived_cases(base)]
    cases.sort(key=lambda item: item["case_id"])

    if TARGET.exists():
        shutil.rmtree(TARGET)
    (TARGET / "cases").mkdir(parents=True)
    (TARGET / "resources").mkdir()
    for case in cases:
        path = TARGET / "cases" / f"{case['case_id']}.json"
        path.write_bytes(canonical_json_bytes(case))
    resource_source = SOURCE / "resources" / "e1-mini" / "snapshot.json"
    shutil.copyfile(resource_source, TARGET / "resources" / "snapshot.json")
    manifest = {
        "schema_version": "case-suite-manifest-v1",
        "dataset_version": "decisionbench-v3-engineering-v1",
        "case_files": [f"cases/{case['case_id']}.json" for case in cases],
        "resource_snapshot_file": "resources/snapshot.json",
        "default_model_mode": "stub",
        "manifest_sha256": "0" * 64,
    }
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    CaseSuiteManifestV1.model_validate(manifest)
    (TARGET / "manifest.json").write_bytes(canonical_json_bytes(manifest))


if __name__ == "__main__":
    main()
