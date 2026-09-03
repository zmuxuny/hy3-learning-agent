from __future__ import annotations

from copy import deepcopy

import pytest
from learning_agent_eval.integrity import (
    case_spec_digest,
    judge_reference_digest,
    model_visible_context_digest,
    provider_attestation_digest,
    runtime_failure_digest,
)
from learning_agent_eval.models import (
    CaseSpecV1,
    EpisodeCompletenessV3,
    JudgeReferenceV1,
    ModelVisibleContextV1,
    ProviderAttestationV1,
    RuntimeRunManifestV2,
)
from learning_agent_eval.schemas import schema_documents
from pydantic import ValidationError

SHA = "0" * 64
GIT = "0" * 40
NOW = "2026-01-02T03:04:05Z"


def _case_spec() -> dict[str, object]:
    return {
        "schema_version": "case-spec-v1",
        "case_id": "case-engineering-planning",
        "scenario_family_id": "family-planning",
        "track": "planning",
        "split": "dev",
        "difficulty": "standard",
        "tags": ["engineering"],
        "dataset_role": "engineering_mini",
        "runtime_setup": {
            "invocation_mode": "stub",
            "frozen_time": NOW,
            "timezone": "UTC",
            "owner_id": "owner-1",
            "run_id": "run-1",
            "session_id": None,
            "trigger": {
                "trigger_id": "trigger-1",
                "trigger_type": "user_goal",
                "objective": "Prepare a plan.",
                "triggered_at": NOW,
                "target_refs": ["goal-1"],
                "source_event_refs": [],
                "payload": {},
            },
            "state_before": {
                "snapshot_type": "synthetic_fixture",
                "logical_entities": [
                    {
                        "logical_id": "goal-1",
                        "entity_type": "goal",
                        "data": {"title": "Good baseline candidate study plan"},
                    }
                ],
                "facts": {},
                "context": {
                    "public_summary": "Normal business baseline and candidate context.",
                    "source_refs": ["goal-1"],
                    "context_sha256": SHA,
                },
            },
            "seed_kind": "planning",
            "seed": {},
            "scripted_turns": [
                {
                    "ordinal": 1,
                    "delivery": "nonstream",
                    "assistant_text": "I will propose a plan.",
                    "tool_calls": [],
                }
            ],
            "resource_snapshot_version": "snapshot-v1",
        },
        "identity_bindings": [
            {
                "entity_type": "goal",
                "logical_id": "goal-1",
                "identity_fields": {"title": "Good baseline candidate study plan"},
            }
        ],
        "judge_criteria": {
            "allowed_action_classes": ["PROPOSE_PLAN", "REQUEST_USER_INPUT"],
            "constraints": [
                {
                    "constraint_id": "planning.goal-grounded",
                    "kind": "must_satisfy",
                    "evaluation": "semantic_judge",
                    "public_statement": "The plan must address the stated goal.",
                    "criticality": "major",
                    "evidence_paths": [
                        "trigger.objective",
                        "result.user_visible_output",
                    ],
                    "predicates": [],
                }
            ],
            "acceptable_variations": ["Equivalent ordering is acceptable."],
        },
        "private_annotations": {
            "quality_label": None,
            "mutation_source": None,
            "author_role": "evaluation_author",
            "reviewer_role": "evaluation_reviewer",
            "adjudication_note": None,
        },
        "case_spec_sha256": SHA,
    }


def _stub_attestation() -> dict[str, object]:
    return {
        "schema_version": "provider-attestation-v1",
        "scope": "agent_runtime",
        "invocation_mode": "stub",
        "provider_id": "none",
        "endpoint_policy_version": "hy3-endpoints-v1",
        "endpoint_policy_sha256": SHA,
        "endpoint_id": None,
        "endpoint_origin": None,
        "configured_model": "scripted-model-v1",
        "calls": [],
        "configuration_sha256": SHA,
        "git_commit": GIT,
        "worktree_clean": True,
        "dependency_lock_version": "runtime-lock-v1",
        "dependency_lock_sha256": SHA,
        "attribution_status": "ineligible_stub",
        "reason_codes": ["provider.stub"],
        "attestation_sha256": SHA,
    }


def test_e31_schema_set_is_strict_and_versioned() -> None:
    expected = {
        "case-spec-v1.schema.json",
        "judge-reference-v1.schema.json",
        "provider-attestation-v1.schema.json",
        "environment-manifest-v2.schema.json",
        "decision-episode-v3.schema.json",
        "runtime-failure-v1.schema.json",
        "runtime-run-manifest-v2.schema.json",
        "rule-result-v2.schema.json",
        "rule-run-manifest-v2.schema.json",
        "judge-result-v2.schema.json",
        "judge-run-manifest-v2.schema.json",
        "aggregate-result-v2.schema.json",
        "aggregate-track-result-v2.schema.json",
        "aggregate-run-manifest-v2.schema.json",
    }
    schemas = schema_documents()
    assert expected <= set(schemas)
    for name in expected:
        schema = schemas[name]
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False


def test_case_spec_keeps_private_labels_outside_judge_reference_contract() -> None:
    case = CaseSpecV1.model_validate(_case_spec())
    assert case.runtime_setup.state_before.logical_entities[0].data["title"] == (
        "Good baseline candidate study plan"
    )

    reference = JudgeReferenceV1.model_validate(
        {
            "schema_version": "judge-reference-v1",
            "opaque_case_id": "blind-123",
            "case_spec_sha256": SHA,
            "track": case.track,
            "allowed_action_classes": case.judge_criteria.allowed_action_classes,
            "constraints": [
                item.model_dump(mode="json") for item in case.judge_criteria.constraints
            ],
            "acceptable_variations": case.judge_criteria.acceptable_variations,
            "reference_sha256": SHA,
        }
    )
    dumped = reference.model_dump(mode="json")
    serialized = str(dumped)
    assert "quality_label" not in serialized
    assert "mutation_source" not in serialized
    assert "author_role" not in serialized
    assert "case-engineering-planning" not in serialized


def test_calibration_label_rules_fail_closed() -> None:
    primary = _case_spec()
    primary["dataset_role"] = "primary_episode"
    primary_annotations = primary["private_annotations"]
    assert isinstance(primary_annotations, dict)
    primary_annotations["quality_label"] = "good"
    with pytest.raises(ValidationError):
        CaseSpecV1.model_validate(primary)

    calibration = _case_spec()
    calibration["dataset_role"] = "calibration_output"
    calibration_runtime = calibration["runtime_setup"]
    assert isinstance(calibration_runtime, dict)
    calibration_runtime["invocation_mode"] = "real"
    calibration_runtime["scripted_turns"] = []
    with pytest.raises(ValidationError):
        CaseSpecV1.model_validate(calibration)


def test_v3_completeness_never_evaluates_decision_correctness() -> None:
    complete = EpisodeCompletenessV3.model_validate(
        {
            "status": "complete",
            "evidence_error_codes": [],
            "verified_evidence_paths": ["result.action_class"],
            "decision_correctness_evaluated": False,
            "completeness_sha256": SHA,
        }
    )
    assert complete.status == "complete"
    with pytest.raises(ValidationError):
        EpisodeCompletenessV3.model_validate(
            {
                **complete.model_dump(mode="json"),
                "decision_correctness_evaluated": True,
            }
        )


def test_visible_context_preserves_business_language_but_rejects_private_fields() -> (
    None
):
    context = {
        "context_version": "model-visible-context-v1",
        "messages": [
            {
                "ordinal": 1,
                "role": "user",
                "payload": {
                    "content": "Good candidate baseline reasoning 推理过程 are lesson terms."
                },
            }
        ],
        "tool_schemas": [],
        "context_sha256": SHA,
    }
    parsed = ModelVisibleContextV1.model_validate(context)
    assert "Good candidate baseline reasoning 推理过程" in str(
        parsed.messages[0].payload
    )

    private = deepcopy(context)
    private_messages = private["messages"]
    assert isinstance(private_messages, list)
    private_messages[0]["payload"]["reasoning_content"] = "secret"
    with pytest.raises(ValidationError):
        ModelVisibleContextV1.model_validate(private)


def test_stub_provider_cannot_claim_endpoint_or_formal_eligibility() -> None:
    ProviderAttestationV1.model_validate(_stub_attestation())
    forged = _stub_attestation()
    forged["attribution_status"] = "eligible"
    with pytest.raises(ValidationError):
        ProviderAttestationV1.model_validate(forged)


def test_runtime_manifest_requires_one_terminal_per_selected_case() -> None:
    manifest = {
        "schema_version": "runtime-run-manifest-v2",
        "dataset_version": "decisionbench-engineering-v3",
        "case_schema_version": "case-spec-v1",
        "episode_schema_version": "decision-episode-v3",
        "failure_schema_version": "runtime-failure-v1",
        "invocation_mode": "stub",
        "selected_case_ids": ["case-a", "case-b"],
        "terminals": [
            {
                "case_id": "case-a",
                "case_spec_sha256": SHA,
                "terminal_kind": "episode",
                "artifact_id": "episode-a",
                "artifact_sha256": SHA,
            },
            {
                "case_id": "case-b",
                "case_spec_sha256": SHA,
                "terminal_kind": "failure",
                "artifact_id": "failure-b",
                "artifact_sha256": SHA,
            },
        ],
        "formal_evaluation_result": False,
        "evaluation_status": "not_a_formal_model_evaluation",
        "git_commit": GIT,
        "dependency_lock_version": "runtime-lock-v1",
        "dependency_lock_sha256": SHA,
        "manifest_sha256": SHA,
    }
    RuntimeRunManifestV2.model_validate(manifest)
    missing = deepcopy(manifest)
    missing["terminals"] = missing["terminals"][:1]
    with pytest.raises(ValidationError):
        RuntimeRunManifestV2.model_validate(missing)


def test_all_new_contracts_reject_unknown_fields() -> None:
    case = _case_spec()
    case["unexpected"] = True
    with pytest.raises(ValidationError):
        CaseSpecV1.model_validate(case)


def test_e31_self_digest_functions_exclude_only_the_declared_self_field() -> None:
    functions = (
        (case_spec_digest, "case_spec_sha256"),
        (judge_reference_digest, "reference_sha256"),
        (provider_attestation_digest, "attestation_sha256"),
        (model_visible_context_digest, "context_sha256"),
        (runtime_failure_digest, "failure_sha256"),
    )
    for digest_function, self_field in functions:
        base = {"payload": "Good baseline candidate", self_field: "a" * 64}
        changed_self = {**base, self_field: "b" * 64}
        changed_payload = {**base, "payload": "different"}
        assert digest_function(base) == digest_function(changed_self)
        assert digest_function(base) != digest_function(changed_payload)
