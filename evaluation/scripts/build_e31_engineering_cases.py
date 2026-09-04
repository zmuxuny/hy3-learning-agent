"""Regenerate the small E3.1 engineering CaseSpec suite from frozen E1 inputs."""

from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    case_spec_digest,
    context_summary_digest,
)
from learning_agent_eval.models import CaseSpecV2

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"
TARGET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v4-engineering"


def _assert_release_not_committed() -> None:
    """Never rewrite fixture inputs once Protocol Release 1.0 is in Git."""

    completed = subprocess.run(
        [
            "git",
            "cat-file",
            "-e",
            "HEAD:evaluation/releases/evaluation-protocol-release-1.0.json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode == 0:
        raise RuntimeError("engineering_case_release_is_immutable")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object: {path}")
    return value


def _constraint_id(track: str, suffix: str) -> str:
    return f"e31.{track}.{suffix}"


def _identity_bindings(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Bind declared entities to public database semantics, never row order."""

    by_type = {
        item["entity_type"]: item
        for item in fixture["state_before"]["logical_entities"]
    }
    kind = fixture["seed_kind"]
    bindings: list[dict[str, Any]] = []
    if "learner" in by_type:
        bindings.append(
            {
                "entity_type": "learner",
                "logical_id": by_type["learner"]["logical_id"],
                "identity_fields": {
                    "display_name": f"Synthetic {kind} learner",
                    "timezone": fixture["timezone"],
                },
            }
        )
    if "plan" in by_type:
        bindings.append(
            {
                "entity_type": "plan",
                "logical_id": by_type["plan"]["logical_id"],
                "identity_fields": {"title": fixture["seed"]["plan_title"]},
            }
        )
    if kind == "assessment":
        stage_ref = f"stage:{fixture['episode_id']}:assessment"
        bindings.extend(
            [
                {
                    "entity_type": "stage",
                    "logical_id": stage_ref,
                    "identity_fields": {
                        "plan_ref": by_type["plan"]["logical_id"],
                        "position": 0,
                        "title": "Synthetic assessment stage",
                    },
                },
                {
                    "entity_type": "task",
                    "logical_id": by_type["task"]["logical_id"],
                    "identity_fields": {
                        "stage_ref": stage_ref,
                        "position": 0,
                        "title": fixture["seed"]["task_title"],
                    },
                },
                {
                    "entity_type": "submission",
                    "logical_id": by_type["submission"]["logical_id"],
                    "identity_fields": {
                        "task_ref": by_type["task"]["logical_id"],
                        "submission_type": "text",
                        "content": (
                            fixture["seed"].get(
                                "submission_content",
                                "Synthetic conclusion without a measured baseline.",
                            )
                        ),
                    },
                },
            ]
        )
    for entity_type in ("goal", "constraint"):
        if entity_type in by_type:
            item = by_type[entity_type]
            bindings.append(
                {
                    "entity_type": entity_type,
                    "logical_id": item["logical_id"],
                    "identity_fields": deepcopy(item["data"]),
                }
            )
    return bindings


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
        "schema_version": "case-spec-v2",
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
        "identity_bindings": _identity_bindings(fixture),
        "judge_criteria": {
            "predicate_semantics": "constraint-proposition-v1",
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
    for turn in case["runtime_setup"]["scripted_turns"]:
        turn["declared_actions"] = list(oracle["allowed_action_classes"])
    case = CaseSpecV2.model_validate(case).model_dump(mode="json")
    case["case_spec_sha256"] = "0" * 64
    case["case_spec_sha256"] = case_spec_digest(case)
    return CaseSpecV2.model_validate(case).model_dump(mode="json")


def _derived_cases(base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    negative = deepcopy(base["A"])
    negative["case_id"] = "case-e31-a-wrong-action"
    negative["scenario_family_id"] = "family-e31-a-wrong-action"
    negative["difficulty"] = "adversarial"
    negative["tags"] = ["engineering", "negative"]
    arguments = negative["runtime_setup"]["scripted_turns"][0]["tool_calls"][0][
        "arguments"
    ]
    arguments["score"] = 45
    arguments["checks"] = [{"criterion": "measurement_evidence", "passed": False}]
    arguments["feedback"] = (
        "Add the missing baseline measurement and connect it to the conclusion."
    )
    negative["runtime_setup"]["scripted_turns"][1]["assistant_text"] = (
        "The submission needs revision despite this case requiring acceptance."
    )
    for turn in negative["runtime_setup"]["scripted_turns"]:
        turn["declared_actions"] = ["REVISION_REQUIRED"]
    action = negative["judge_criteria"]["constraints"][0]
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

    cross_track = deepcopy(base["A"])
    cross_track["case_id"] = "case-e311-a-cross-track-change"
    cross_track["scenario_family_id"] = "family-e311-a-cross-track-change"
    cross_track["difficulty"] = "adversarial"
    cross_track["tags"] = ["engineering", "cross_track_behavior"]
    cross_track["runtime_setup"]["scripted_turns"] = [
        {
            "ordinal": 1,
            "delivery": "nonstream",
            "assistant_text": (
                "I changed the plan even though this run is assessing a submission."
            ),
            "declared_actions": ["APPLY_REVERSIBLE_PATCH"],
            "tool_calls": [
                {
                    "call_id": "call:e311:a:cross-track",
                    "name": "plan_patch",
                    "arguments": {
                        "plan_id": 1,
                        "expected_version": 1,
                        "weekly_minutes": 300,
                        "reason": "Synthetic cross-track negative behavior.",
                    },
                }
            ],
        },
        {
            "ordinal": 2,
            "delivery": "stream",
            "assistant_text": "The unrelated plan change was applied.",
            "declared_actions": ["APPLY_REVERSIBLE_PATCH"],
            "tool_calls": [],
        },
    ]
    cross_track["judge_criteria"]["constraints"].insert(
        1,
        {
            "constraint_id": "e311.assessment.prohibited-plan-change",
            "kind": "must_not",
            "evaluation": "deterministic_rule",
            "public_statement": (
                "Assessment must not apply a plan change while deciding a verdict."
            ),
            "criticality": "critical",
            "evidence_paths": ["result.action_classes"],
            "predicates": [
                {
                    "path": "result.action_classes",
                    "operator": "contains",
                    "expected_value": "APPLY_REVERSIBLE_PATCH",
                }
            ],
        },
    )

    combination = deepcopy(base["A"])
    combination["case_id"] = "case-e311-a-combined-actions"
    combination["scenario_family_id"] = "family-e311-a-combined-actions"
    combination["difficulty"] = "adversarial"
    combination["tags"] = ["engineering", "combined_behavior"]
    combination["runtime_setup"]["scripted_turns"] = [
        {
            "ordinal": 1,
            "delivery": "nonstream",
            "assistant_text": (
                "The evidence is insufficient, so I need one precise clarification."
            ),
            "declared_actions": [
                "INSUFFICIENT_EVIDENCE",
                "REQUEST_CLARIFICATION",
            ],
            "tool_calls": [],
        }
    ]

    missing_declaration = deepcopy(base["A"])
    missing_declaration["case_id"] = "case-e311-a-missing-action-declaration"
    missing_declaration["scenario_family_id"] = (
        "family-e311-a-missing-action-declaration"
    )
    missing_declaration["difficulty"] = "adversarial"
    missing_declaration["tags"] = ["engineering", "protocol_failure"]
    missing_declaration["runtime_setup"]["scripted_turns"] = [
        {
            "ordinal": 1,
            "delivery": "nonstream",
            "assistant_text": "I answered without the required action declaration.",
            "declared_actions": [],
            "tool_calls": [],
        }
    ]

    quiz_review = deepcopy(base["A"])
    quiz_review["case_id"] = "case-e311-a-quiz-review-action"
    quiz_review["scenario_family_id"] = "family-e311-a-quiz-review-action"
    quiz_review["difficulty"] = "adversarial"
    quiz_review["tags"] = ["engineering", "cross_track_behavior"]
    quiz_review["runtime_setup"]["scripted_turns"] = [
        {
            "ordinal": 1,
            "delivery": "nonstream",
            "assistant_text": (
                "I created an unrelated quiz and review while assessing a submission."
            ),
            "declared_actions": ["INTERVENE_QUIZ_OR_REVIEW"],
            "tool_calls": [
                {
                    "call_id": "call:e311:a:quiz",
                    "name": "quiz_create",
                    "arguments": {
                        "plan_id": 1,
                        "task_id": 1,
                        "prompt": "State the synthetic benchmark result.",
                    },
                },
                {
                    "call_id": "call:e311:a:review",
                    "name": "review_schedule",
                    "arguments": {
                        "plan_id": 1,
                        "task_id": 1,
                        "due_at": "2026-09-01T01:30:00Z",
                        "review_type": "quiz",
                    },
                },
            ],
        },
        {
            "ordinal": 2,
            "delivery": "stream",
            "assistant_text": "The unrelated quiz and review were created.",
            "declared_actions": ["INTERVENE_QUIZ_OR_REVIEW"],
            "tool_calls": [],
        },
    ]

    delegated = deepcopy(base["P"])
    delegated["case_id"] = "case-e31-p-child-hierarchy"
    delegated["scenario_family_id"] = "family-e31-p-child-hierarchy"
    delegated["tags"] = ["engineering", "child_hierarchy"]
    proposal_turn = deepcopy(delegated["runtime_setup"]["scripted_turns"][0])
    proposal_turn["ordinal"] = 4
    final_turn = deepcopy(delegated["runtime_setup"]["scripted_turns"][1])
    final_turn["ordinal"] = 5
    delegated["runtime_setup"]["scripted_turns"] = [
        {
            "ordinal": 1,
            "delivery": "nonstream",
            "assistant_text": "I will ask a bounded child Agent to summarize the constraints.",
            "declared_actions": ["PROPOSE_PLAN"],
            "tool_calls": [
                {
                    "call_id": "call:e31:p:delegate",
                    "name": "planning_delegate",
                    "arguments": {
                        "assignments": [
                            {
                                "role": "evidence analyst",
                                "objective": "Summarize the public synthetic planning constraints.",
                            },
                            {
                                "role": "feasibility analyst",
                                "objective": "Check the public synthetic time and resource limits.",
                            }
                        ]
                    },
                }
            ],
        },
        {
            "ordinal": 2,
            "delivery": "nonstream",
            "assistant_text": (
                "The synthetic learner has four hours per week and needs a measured kernel report."
            ),
            "declared_actions": [],
            "stub_request_user_prefix": (
                "Assignment: evidence analyst: Summarize the public synthetic "
                "planning constraints."
            ),
            "tool_calls": [],
        },
        {
            "ordinal": 3,
            "delivery": "nonstream",
            "assistant_text": (
                "The synthetic four-hour budget is compatible with a bounded proposal."
            ),
            "declared_actions": [],
            "stub_request_user_prefix": (
                "Assignment: feasibility analyst: Check the public synthetic time "
                "and resource limits."
            ),
            "tool_calls": [],
        },
        proposal_turn,
        final_turn,
    ]
    delegated["judge_criteria"]["constraints"].insert(
        2,
        {
            "constraint_id": "e31.planning.child-hierarchy",
            "kind": "must_satisfy",
            "evaluation": "deterministic_rule",
            "public_statement": (
                "Both delegated decision calls must retain their parent Run and model-call relationships."
            ),
            "criticality": "major",
            "evidence_paths": [
                "observable_trace.model_calls[1].call_purpose",
                "observable_trace.model_calls[1].parent_run_id",
                "observable_trace.model_calls[1].parent_call_id",
                "observable_trace.model_calls[2].call_purpose",
                "observable_trace.model_calls[2].parent_run_id",
                "observable_trace.model_calls[2].parent_call_id",
            ],
            "predicates": [
                {
                    "path": "observable_trace.model_calls[1].call_purpose",
                    "operator": "equals",
                    "expected_value": "subagent_decision",
                },
                {
                    "path": "observable_trace.model_calls[1].parent_run_id",
                    "operator": "not_equals",
                    "expected_value": None,
                },
                {
                    "path": "observable_trace.model_calls[1].parent_call_id",
                    "operator": "not_equals",
                    "expected_value": None,
                },
                {
                    "path": "observable_trace.model_calls[2].call_purpose",
                    "operator": "equals",
                    "expected_value": "subagent_decision",
                },
                {
                    "path": "observable_trace.model_calls[2].parent_run_id",
                    "operator": "not_equals",
                    "expected_value": None,
                },
                {
                    "path": "observable_trace.model_calls[2].parent_call_id",
                    "operator": "not_equals",
                    "expected_value": None,
                },
            ],
        },
    )

    for case in (
        negative,
        safety,
        failure,
        delegated,
        cross_track,
        combination,
        missing_declaration,
        quiz_review,
    ):
        case["case_spec_sha256"] = "0" * 64
        normalized = CaseSpecV2.model_validate(case).model_dump(mode="json")
        case.clear()
        case.update(normalized)
        case["case_spec_sha256"] = "0" * 64
        case["case_spec_sha256"] = case_spec_digest(case)
        CaseSpecV2.model_validate(case)
    return [
        negative,
        safety,
        failure,
        delegated,
        cross_track,
        combination,
        missing_declaration,
        quiz_review,
    ]


def main() -> None:
    _assert_release_not_committed()
    base = {prefix: _base_case(prefix) for prefix in ("A", "I", "P", "R")}
    assessment = base["A"]
    submission_content = (
        "Synthetic report records a 12.4 ms baseline and an independently checked "
        "8.1 ms result."
    )
    assessment["runtime_setup"]["seed"]["submission_content"] = submission_content
    assessment["runtime_setup"]["state_before"]["context"]["public_summary"] = (
        "A fully synthetic submission includes measured baseline and result evidence "
        "and awaits a persisted assessment."
    )
    assessment["runtime_setup"]["state_before"]["context"]["context_sha256"] = (
        context_summary_digest(assessment["runtime_setup"]["state_before"]["context"])
    )
    submission_binding = next(
        item
        for item in assessment["identity_bindings"]
        if item["entity_type"] == "submission"
    )
    submission_binding["identity_fields"]["content"] = submission_content
    arguments = assessment["runtime_setup"]["scripted_turns"][0]["tool_calls"][0][
        "arguments"
    ]
    arguments["score"] = 85
    arguments["checks"] = [{"criterion": "measurement_evidence", "passed": True}]
    arguments["feedback"] = (
        "The synthetic measurement evidence meets the declared threshold."
    )
    assessment["runtime_setup"]["scripted_turns"][1]["assistant_text"] = (
        "The synthetic submission meets the declared threshold."
    )
    for turn in assessment["runtime_setup"]["scripted_turns"]:
        turn["declared_actions"] = ["ACCEPT"]
    assessment["runtime_setup"]["seed"]["additional_stages"] = [
        {
            "title": "AAA synthetic distractor stage",
            "position": 0,
            "tasks": [{"title": "AAA synthetic distractor task", "position": 0}],
        }
    ]
    assessment["judge_criteria"]["allowed_action_classes"] = ["ACCEPT"]
    assessment["judge_criteria"]["constraints"][0]["predicates"][0][
        "expected_value"
    ] = "ACCEPT"
    assessment["judge_criteria"]["constraints"][1]["public_statement"] = (
        "The production assessment tool must persist the evidence-backed accepted verdict."
    )
    assessment["case_spec_sha256"] = "0" * 64
    assessment["case_spec_sha256"] = case_spec_digest(assessment)
    base["P"]["runtime_setup"]["trigger"]["payload"]["business_note"] = (
        "Good baseline and candidate are ordinary learning-domain terms."
    )
    base["P"]["case_spec_sha256"] = "0" * 64
    base["P"]["case_spec_sha256"] = case_spec_digest(base["P"])
    cases = [*base.values(), *_derived_cases(base)]
    cases.sort(key=lambda item: item["case_id"])

    for generated_directory in (TARGET / "cases", TARGET / "resources"):
        if generated_directory.exists():
            shutil.rmtree(generated_directory)
    (TARGET / "manifest.json").unlink(missing_ok=True)
    (TARGET / "cases").mkdir(parents=True)
    (TARGET / "resources").mkdir()
    for case in cases:
        path = TARGET / "cases" / f"{case['case_id']}.json"
        path.write_bytes(canonical_json_bytes(case))
    resource_source = SOURCE / "resources" / "e1-mini" / "snapshot.json"
    shutil.copyfile(resource_source, TARGET / "resources" / "snapshot.json")
    manifest = {
        "schema_version": "case-suite-manifest-v2",
        "dataset_version": "decisionbench-v4-engineering-v1",
        "case_schema_version": "case-spec-v2",
        "case_files": [f"cases/{case['case_id']}.json" for case in cases],
        "resource_snapshot_file": "resources/snapshot.json",
        "default_model_mode": "stub",
        "manifest_sha256": "0" * 64,
    }
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    (TARGET / "manifest.json").write_bytes(canonical_json_bytes(manifest))


if __name__ == "__main__":
    main()
