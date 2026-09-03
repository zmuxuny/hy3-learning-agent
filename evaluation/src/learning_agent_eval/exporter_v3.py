"""DecisionEpisode v3 exporter over captured production Runtime facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .canonical import canonical_json_bytes, sha256_digest
from .case_specs import internal_action_envelope, legacy_runtime_projection
from .e31_runtime import build_model_calls_v3
from .exporter import build_decision_episode_v2
from .integrity import (
    decision_episode_digest,
    environment_manifest_digest,
    episode_completeness_digest,
)
from .models import DecisionEpisodeV3
from .validator import resolve_evidence_path


class ExportV3Error(ValueError):
    """Captured Runtime facts cannot form a complete v3 Episode."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _decision_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected = [
        dict(record)
        for record in records
        if record.get("decision_relevant")
        and record.get("response_status") == "completed"
    ]
    if not selected:
        raise ExportV3Error("export_v3.decision_calls_missing")
    return [
        {**record, "ordinal": ordinal} for ordinal, record in enumerate(selected, 1)
    ]


def _v3_result(
    base_result: Mapping[str, Any],
    decision_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(dict(base_result))
    old_attempts = list(result["layers"]["model_attempts"])
    decision_call_ids = [str(record["call_id"]) for record in decision_records]
    if len(old_attempts) != len(decision_call_ids):
        raise ExportV3Error("export_v3.attempt_call_count_mismatch")
    turn_to_call: dict[str, str] = {}
    attempts = []
    for attempt, call_id in zip(old_attempts, decision_call_ids, strict=True):
        turn_to_call[str(attempt["turn_ref"])] = call_id
        attempts.append(
            {
                "attempt_id": attempt["attempt_id"],
                "ordinal": attempt["ordinal"],
                "call_ref": call_id,
                "attempted_action": attempt["attempted_action"],
                "invocation_refs": attempt["invocation_refs"],
            }
        )
    effects = deepcopy(result["layers"]["final_effects"])
    for effect in effects:
        for source in effect["source_refs"]:
            if source["source_type"] == "model_turn":
                source["source_type"] = "model_call"
                source["ref"] = turn_to_call[source["ref"]]
    result["layers"]["model_attempts"] = attempts
    result["layers"]["final_effects"] = effects
    return result


def _link_model_calls(
    calls: list[dict[str, Any]], trace: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Replace provider tool-call IDs with authoritative invocation references."""

    invocation_by_call = {
        item["tool_call_id"]: item["invocation_id"]
        for item in trace["tool_invocations"]
    }
    linked = deepcopy(calls)
    for call in linked:
        try:
            call["tool_call_refs"] = [
                invocation_by_call[tool_call_id]
                for tool_call_id in call["tool_call_refs"]
            ]
        except KeyError as exc:
            raise ExportV3Error("export_v3.model_call_unresolved") from exc
        if call["status"] == "completed":
            call["response_sha256"] = sha256_digest(
                {
                    "assistant_text": call["assistant_text"] or "",
                    "tool_call_refs": call["tool_call_refs"],
                }
            )
    return linked


def build_decision_episode_v3(
    *,
    case_spec: Mapping[str, Any],
    judge_reference: Mapping[str, Any],
    model_records: Sequence[Mapping[str, Any]],
    state_before: dict[str, Any],
    state_after: dict[str, Any],
    state_delta: dict[str, Any],
    resource_version: str,
    resource_digest: str,
    git_commit: str,
    invocation_mode: str,
    model_name: str,
    model_temperature: float,
    model_reasoning_effort: str,
    isolation_evidence: dict[str, Any],
    provider_attestation: Mapping[str, Any],
    dependency_lock_version: str,
    dependency_lock_sha256: str,
) -> dict[str, Any]:
    """Build v3 without using correctness criteria to decide structural validity."""

    fixture = legacy_runtime_projection(case_spec)
    envelope = internal_action_envelope(judge_reference)
    decision_records = _decision_records(model_records)
    base = build_decision_episode_v2(
        fixture=fixture,
        oracle=envelope,
        model_records=decision_records,
        state_before=state_before,
        state_after=state_after,
        state_delta=state_delta,
        resource_version=resource_version,
        resource_digest=resource_digest,
        git_commit=git_commit,
        invocation_mode=invocation_mode,
        model_name=model_name,
        model_temperature=model_temperature,
        model_reasoning_effort=model_reasoning_effort,
        isolation_evidence=isolation_evidence,
    )
    calls = _link_model_calls(
        build_model_calls_v3(model_records), base["observable_trace"]
    )
    result = _v3_result(base["result"], decision_records)
    attribution_status = provider_attestation["attribution_status"]
    formal = bool(
        invocation_mode == "real"
        and case_spec["dataset_role"] == "primary_episode"
        and attribution_status == "eligible"
    )
    if invocation_mode == "stub":
        eligibility = "ineligible_stub"
    elif case_spec["dataset_role"] != "primary_episode":
        eligibility = "ineligible_engineering"
    else:
        eligibility = "eligible" if formal else "invalid"
    result["layers"]["formal_evaluation_eligibility"] = eligibility
    environment = {
        "schema_version": "environment-manifest-v2",
        "frozen_time": base["environment"]["frozen_time"],
        "timezone": base["environment"]["timezone"],
        "prompt": base["environment"]["prompt"],
        "tools": base["environment"]["tools"],
        "policies": base["environment"]["policies"],
        "resources": base["environment"]["resources"],
        "runtime": {
            "git_commit": git_commit,
            "worktree_clean": bool(provider_attestation["worktree_clean"]),
            "database_mode": "temporary_fixture",
            "fixture_db_sha256": base["environment"]["runtime"]["fixture_db_sha256"],
            "dependency_lock_version": dependency_lock_version,
            "dependency_lock_sha256": dependency_lock_sha256,
        },
        "isolation": base["environment"]["isolation"],
        "provider_attestation": deepcopy(dict(provider_attestation)),
        "manifest_sha256": "0" * 64,
    }
    environment["manifest_sha256"] = environment_manifest_digest(environment)
    trace = {
        "capture_mode": "runtime_recording",
        "model_calls": calls,
        "decision_call_refs": [
            item["call_id"] for item in calls if item["decision_relevant"]
        ],
        "auxiliary_call_refs": [
            item["call_id"] for item in calls if not item["decision_relevant"]
        ],
        "tool_invocations": base["observable_trace"]["tool_invocations"],
        "run_events": base["observable_trace"]["run_events"],
        "operations": base["observable_trace"]["operations"],
        "guard_decisions": base["observable_trace"]["guard_decisions"],
    }
    evidence_paths = sorted(
        {
            "state_before.snapshot_sha256",
            "state_after.snapshot_sha256",
            "state_delta.delta_sha256",
            "observable_trace.model_calls",
            "observable_trace.run_events",
            "result.layers",
            "isolation_evidence",
            *(
                path
                for constraint in judge_reference["constraints"]
                for path in constraint["evidence_paths"]
            ),
        }
    )
    incomplete_paths = [
        path
        for path in evidence_paths
        if not resolve_evidence_path(
            {
                "schema_version": "decision-episode-v3",
                "state_before": state_before,
                "state_after": state_after,
                "state_delta": state_delta,
                "observable_trace": trace,
                "result": result,
                "isolation_evidence": isolation_evidence,
            },
            path,
        )[0]
    ]
    evidence_errors = [*state_delta["error_codes"]]
    if incomplete_paths:
        evidence_errors.append("completeness.reference_path_missing")
    completeness = {
        "status": "complete" if not evidence_errors else "invalid",
        "evidence_error_codes": sorted(set(evidence_errors)),
        "verified_evidence_paths": evidence_paths,
        "decision_correctness_evaluated": False,
        "completeness_sha256": "0" * 64,
    }
    completeness["completeness_sha256"] = episode_completeness_digest(completeness)
    episode = {
        "schema_version": "decision-episode-v3",
        "episode_id": fixture["episode_id"],
        "case_spec_sha256": case_spec["case_spec_sha256"],
        "judge_reference_sha256": judge_reference["reference_sha256"],
        "scenario_family_id": case_spec["scenario_family_id"],
        "track": case_spec["track"],
        "split": case_spec["split"],
        "difficulty": case_spec["difficulty"],
        "trigger": base["trigger"],
        "state_before": state_before,
        "state_after": state_after,
        "state_delta": state_delta,
        "environment": environment,
        "observable_trace": trace,
        "result": result,
        "completeness": completeness,
        "isolation_evidence": isolation_evidence,
        "provenance": {
            "source_type": "runtime_export",
            "construction_method": "runtime_recorded",
            "dataset_role": case_spec["dataset_role"],
            "runtime_executed": True,
            "formal_evaluation_result": formal,
            "evaluation_status": (
                "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
            ),
            "created_at": base["provenance"]["created_at"],
            "source_refs": [
                f"case-spec:{case_spec['case_spec_sha256']}",
                f"resource:{resource_version}",
            ],
            "episode_sha256": "0" * 64,
        },
    }
    episode["provenance"]["episode_sha256"] = decision_episode_digest(episode)
    try:
        validated = DecisionEpisodeV3.model_validate(episode).model_dump(
            mode="json", by_alias=True
        )
        canonical_json_bytes(validated)
    except ValueError as exc:
        raise ExportV3Error("export_v3.contract_invalid") from exc
    return validated
