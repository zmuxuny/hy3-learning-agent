"""DecisionEpisode v4 exporter with scoreable intent/effect separation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .action_protocol import (
    ACTION_DECLARATION_PROTOCOL_SHA256,
    ACTION_DECLARATION_PROTOCOL_VERSION,
    parse_action_declaration,
)
from .canonical import canonical_json_bytes, sha256_digest
from .case_specs import legacy_runtime_projection_v2
from .e31_runtime import build_model_calls_v4
from .eligibility import isolation_evidence_protocol_eligible
from .exporter import (
    ExportError,
    _durable_status,
    _entities,
    _environment,
    _guard_decisions,
    _guard_facts,
    _guard_status,
    _trace,
)
from .integrity import (
    decision_episode_digest,
    environment_manifest_digest,
    episode_completeness_digest,
)
from .models import DecisionEpisodeV4
from .normalizers import NormalizationError, normalize_rfc3339
from .release_governance import ACTIVE_PROTOCOL_RELEASE_ID
from .validator import resolve_evidence_path

ACTION_MAPPING_VERSION_V2 = "runtime-action-mapping-v2-e6-final-2"
TOOL_ACTION_MAPPING_V2 = {
    "plan.proposal.create": "PROPOSE_PLAN",
    "notification.send": "INTERVENE_MESSAGE",
    "quiz.create": "INTERVENE_QUIZ_OR_REVIEW",
    "review.schedule": "INTERVENE_QUIZ_OR_REVIEW",
    "plan.patch": "APPLY_REVERSIBLE_PATCH",
    "task.patch": "APPLY_REVERSIBLE_PATCH",
}
ACTION_EFFECT_TYPES_V2 = {
    "PROPOSE_PLAN": "plan_proposal",
    "REQUEST_USER_INPUT": "user_input_request",
    "WAIT": "wait",
    "INTERVENE_MESSAGE": "intervention_message",
    "INTERVENE_QUIZ_OR_REVIEW": "intervention_quiz_or_review",
    "PROPOSE_PLAN_ADJUSTMENT": "plan_adjustment_proposal",
    "ACCEPT": "assessment_accept",
    "REVISION_REQUIRED": "assessment_revision",
    "INSUFFICIENT_EVIDENCE": "insufficient_evidence",
    "REQUEST_CLARIFICATION": "clarification_request",
    "NO_OP": "no_op",
    "PROPOSE_CHANGE": "change_proposal",
    "APPLY_REVERSIBLE_PATCH": "reversible_patch",
    "REQUEST_APPROVAL": "approval_request",
}
ACTION_MAPPING_SHA256_V2 = sha256_digest(
    {
        "version": ACTION_MAPPING_VERSION_V2,
        "declaration_protocol_version": ACTION_DECLARATION_PROTOCOL_VERSION,
        "declaration_protocol_sha256": ACTION_DECLARATION_PROTOCOL_SHA256,
        "tool_mapping": TOOL_ACTION_MAPPING_V2,
        "effect_types": ACTION_EFFECT_TYPES_V2,
        "combination_semantics": "last_valid_decision_plus_observed_tool_actions; historical_attempts_retained",
        "effect_entity_attribution": "tool_result_operation_and_business_state-v2",
        "unclassified_semantics": "scoreable_behavior_issue",
    }
)

INFRASTRUCTURE_ENTITY_TYPES_V2 = {
    "agent_run",
    "constraint",
    "context_snapshot",
    "goal",
    "operation",
    "outbox_receipt",
    "planning_intake",
    "proactive_decision",
    "resource",
    "run_approval",
    "run_event",
    "session",
    "tool_invocation",
}


class ExportV4Error(ValueError):
    """Captured facts cannot form a structurally complete v4 Episode."""

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
        raise ExportV4Error("export_v4.decision_calls_missing")
    return [
        {**record, "ordinal": ordinal} for ordinal, record in enumerate(selected, 1)
    ]


def _link_model_calls(
    calls: list[dict[str, Any]], trace: Mapping[str, Any], state_after: dict[str, Any]
) -> list[dict[str, Any]]:
    from .snapshots import pending_tool_call_evidence

    queued = pending_tool_call_evidence(state_after)
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
                if tool_call_id not in queued
            ]
        except KeyError as exc:
            raise ExportV4Error("export_v4.model_call_unresolved") from exc
        if call["status"] == "completed":
            call["response_sha256"] = sha256_digest(
                {
                    "assistant_text": call["assistant_text"] or "",
                    "tool_call_refs": call["tool_call_refs"],
                }
            )
    return linked


def _invocation_action(invocation: Mapping[str, Any]) -> str | None:
    tool_name = str(invocation["tool_name"])
    if tool_name == "planning.intake.update":
        args = invocation.get("canonical_args", {})
        # Recording confirmed facts is intermediate planning state, not a request
        # to the learner. Only actual open questions represent REQUEST_USER_INPUT.
        return "REQUEST_USER_INPUT" if args.get("open_questions") else None
    if tool_name == "submission.check":
        status = invocation.get("result", {}).get("status")
        if status == "accepted":
            return "ACCEPT"
        if status == "revision_required":
            return "REVISION_REQUIRED"
        return None
    return TOOL_ACTION_MAPPING_V2.get(tool_name)


def _effect_status(invocation: Mapping[str, Any]) -> str:
    observation = invocation["observation_status"]
    durable = invocation["durable_status"]
    if observation == "blocked":
        return "blocked"
    if observation == "deferred":
        return "deferred"
    if durable in {"pending_approval", "pending_delivery", "retry_pending"}:
        return "pending"
    if durable in {"failed", "cancelled", "needs_reconciliation"}:
        return "failed"
    return "applied"


def _append_unique(target: list[str], values: Sequence[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _invocation_entity_refs(
    invocation: Mapping[str, Any],
    *,
    state_after: Mapping[str, Any],
    state_delta: Mapping[str, Any],
    operations: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    refs: set[str] = set()
    operation_refs = set(invocation["operation_refs"])
    for operation_ref in operation_refs:
        operation = operations.get(operation_ref)
        if operation is not None:
            refs.update(operation["affected_entity_refs"])
    entities = {item["logical_id"]: item for item in state_after["logical_entities"]}
    for change in state_delta["changes"]:
        entity = entities.get(change["entity_ref"])
        data = entity.get("data", {}) if entity else {}
        entity_type = entity.get("entity_type") if entity else None
        canonical_message_link = entity_type == "chat_message" and any(
            row["entity_type"] == "intervention"
            and row["data"].get("canonical_message_ref") == change["entity_ref"]
            and row["data"].get("invocation_ref") == invocation["invocation_id"]
            for row in entities.values()
        )
        if (
            operation_refs.intersection(change["operation_refs"])
            or data.get("invocation_ref") == invocation["invocation_id"]
            or canonical_message_link
        ) and entity_type not in INFRASTRUCTURE_ENTITY_TYPES_V2:
            refs.add(change["entity_ref"])
    if invocation["tool_name"] == "submission.check":
        submission_ref = invocation.get("result", {}).get("submission_ref")
        task_ref = invocation.get("result", {}).get("task_ref")
        for change in state_delta["changes"]:
            entity = entities.get(change["entity_ref"])
            if not entity:
                continue
            entity_type = entity["entity_type"]
            data = entity.get("data", {})
            source_ref = data.get("source_identity", {}).get("source_ref")
            related = (
                entity_type == "artifact"
                and source_ref == submission_ref
                or entity_type == "evidence_observation"
                and data.get("source_type") == "submission"
                and data.get("task_ref") == task_ref
                or entity_type == "learning_event"
                and data.get("task_ref") == task_ref
                and data.get("event_type") in {"submission.checked", "task.updated"}
            )
            if related:
                refs.add(change["entity_ref"])
    if invocation["tool_name"] == "plan.proposal.create":
        proposal_ref = invocation.get("result", {}).get("proposal_ref")
        proposal = entities.get(proposal_ref)
        if proposal is not None and proposal["entity_type"] == "plan_proposal":
            refs.add(proposal_ref)
    if invocation["tool_name"] == "plan.patch":
        plan_ref = invocation.get("result", {}).get("plan_ref")
        for change in state_delta["changes"]:
            entity = entities.get(change["entity_ref"])
            if not entity or entity["entity_type"] != "learning_event":
                continue
            data = entity.get("data", {})
            if (
                data.get("event_type") == "plan.updated"
                and data.get("plan_ref") == plan_ref
            ):
                refs.add(change["entity_ref"])
    return sorted(refs)


def _effects_and_result(
    *,
    episode_id: str,
    calls: Sequence[Mapping[str, Any]],
    trace: Mapping[str, Any],
    state_after: Mapping[str, Any],
    state_delta: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    effects: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    actions: list[str] = []
    issues: set[str] = set()
    operations = {item["operation_id"]: item for item in trace["operations"]}
    invocations = {item["invocation_id"]: item for item in trace["tool_invocations"]}

    decision_calls = [item for item in calls if item["decision_relevant"]]
    # A later evidence-backed decision supersedes provisional read-round intent.
    # Every actual tool action and every invalid/missing declaration stays visible.
    latest = next((c for c in reversed(decision_calls)
                   if c["call_purpose"] == "decision" and c["action_declaration_status"] == "valid"), None)
    if latest is not None:
        _append_unique(actions, latest["declared_action_classes"])
    invocation_decisions = {
        ref: c["declared_action_classes"] for c in decision_calls
        if c["action_declaration_status"] == "valid" for ref in c["tool_call_refs"]
    }

    def observed_action(invocation):
        # Notification is a delivery mechanism. The explicit linked declaration
        # distinguishes an information request; delivery evidence is still checked.
        if (invocation["tool_name"] == "notification.send"
                and invocation_decisions.get(invocation["invocation_id"]) == ["REQUEST_USER_INPUT"]):
            return "REQUEST_USER_INPUT"
        return _invocation_action(invocation)
    for ordinal, call in enumerate(decision_calls, 1):
        declared = list(call["declared_action_classes"])
        declaration_status = call["action_declaration_status"]
        if call["call_purpose"] == "decision":
            if declaration_status == "missing":
                issues.add("action.declaration_missing")
            elif declaration_status == "invalid":
                issues.add("action.declaration_invalid")
        tool_actions = [
            action
            for invocation_ref in call["tool_call_refs"]
            if (action := observed_action(invocations[invocation_ref])) is not None
        ]
        if (
            call["call_purpose"] == "decision"
            and tool_actions
            and not set(tool_actions).issubset(declared)
        ):
            issues.add("action.declaration_effect_mismatch")
        attempted_action = "tool_call" if call["tool_call_refs"] else "respond"
        if not call["tool_call_refs"] and declared == ["WAIT"]:
            attempted_action = "wait"
        elif not call["tool_call_refs"] and declared == ["NO_OP"]:
            attempted_action = "no_op"
        attempts.append(
            {
                "attempt_id": f"attempt:{episode_id}:{ordinal:03d}",
                "ordinal": ordinal,
                "call_ref": call["call_id"],
                "attempted_action": attempted_action,
                "invocation_refs": list(call["tool_call_refs"]),
                "declared_action_classes": declared,
                "action_declaration_status": declaration_status,
            }
        )

    for invocation in trace["tool_invocations"]:
        action = observed_action(invocation)
        effect_status = _effect_status(invocation)
        if action is None and effect_status not in {"blocked", "deferred", "pending"}:
            continue
        if action is not None:
            _append_unique(actions, [action])
        effects.append(
            {
                "effect_id": f"effect:{episode_id}:{len(effects) + 1:03d}",
                "ordinal": len(effects) + 1,
                "effect_type": ACTION_EFFECT_TYPES_V2[action] if action is not None else "unclassified",
                "status": effect_status,
                "action_classes": [action] if action is not None else [],
                "entity_refs": _invocation_entity_refs(
                    invocation,
                    state_after=state_after,
                    state_delta=state_delta,
                    operations=operations,
                ),
                "source_refs": [
                    {
                        "source_type": "tool_invocation",
                        "ref": invocation["invocation_id"],
                    }
                ],
            }
        )

    effected_actions = {
        action for effect in effects for action in effect["action_classes"]
    }
    for call in calls:
        if call["call_purpose"] != "decision":
            continue
        for action in call["declared_action_classes"]:
            if action in effected_actions:
                continue
            status = (
                "pending"
                if action
                in {
                    "REQUEST_USER_INPUT",
                    "PROPOSE_PLAN_ADJUSTMENT",
                    "PROPOSE_CHANGE",
                    "REQUEST_APPROVAL",
                }
                else "no_change"
            )
            effects.append(
                {
                    "effect_id": f"effect:{episode_id}:{len(effects) + 1:03d}",
                    "ordinal": len(effects) + 1,
                    "effect_type": ACTION_EFFECT_TYPES_V2[action],
                    "status": status,
                    "action_classes": [action],
                    "entity_refs": [],
                    "source_refs": [
                        {"source_type": "model_call", "ref": call["call_id"]}
                    ],
                }
            )
            effected_actions.add(action)

    covered_refs = {ref for effect in effects for ref in effect["entity_refs"]}
    entity_types = {
        item["logical_id"]: item["entity_type"]
        for item in state_after["logical_entities"]
    }
    unclassified_refs = sorted(
        {
            change["entity_ref"]
            for change in state_delta["changes"]
            if change["entity_ref"] not in covered_refs
            and entity_types.get(change["entity_ref"])
            not in INFRASTRUCTURE_ENTITY_TYPES_V2
        }
    )
    if unclassified_refs:
        issues.add("action.unclassified_state_change")
        sources = [
            {"source_type": "operation", "ref": operation_ref}
            for operation_ref in sorted(
                {
                    operation_ref
                    for change in state_delta["changes"]
                    if change["entity_ref"] in unclassified_refs
                    for operation_ref in change["operation_refs"]
                }
            )
        ]
        if not sources:
            root_runs = [
                item["logical_id"]
                for item in state_after["logical_entities"]
                if item["entity_type"] == "agent_run"
                and item["data"].get("parent_run_ref") is None
            ]
            sources = [{"source_type": "runtime", "ref": ref} for ref in root_runs]
        effects.append(
            {
                "effect_id": f"effect:{episode_id}:{len(effects) + 1:03d}",
                "ordinal": len(effects) + 1,
                "effect_type": "unclassified",
                "status": "applied",
                "action_classes": [],
                "entity_refs": unclassified_refs,
                "source_refs": sources,
            }
        )

    if not actions:
        proactive_wait = any(
            item["data"].get("outcome") == "success_wait"
            for item in _entities(dict(state_after), "proactive_decision")
        )
        fallback = "WAIT" if proactive_wait else "NO_OP"
        actions.append(fallback)
        issues.add("action.declaration_missing")
        root_call = next(item for item in calls if item["call_purpose"] == "decision")
        effects.append(
            {
                "effect_id": f"effect:{episode_id}:{len(effects) + 1:03d}",
                "ordinal": len(effects) + 1,
                "effect_type": ACTION_EFFECT_TYPES_V2[fallback],
                "status": "no_change",
                "action_classes": [fallback],
                "entity_refs": [],
                "source_refs": [
                    {"source_type": "model_call", "ref": root_call["call_id"]}
                ],
            }
        )
    return attempts, effects, actions, sorted(issues)


def build_decision_episode_v4(
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
    evaluation_protocol_release_sha256: str,
    benchmark_release_id: str,
    benchmark_release_sha256: str,
    runtime_run_id: str,
    runtime_source_bundle_version: str,
    runtime_source_bundle_sha256: str,
) -> dict[str, Any]:
    """Build v4; correctness and action-protocol failures remain scoreable."""

    fixture = legacy_runtime_projection_v2(case_spec)
    decision_records = _decision_records(model_records)
    try:
        frozen_time = normalize_rfc3339(fixture["frozen_time"])
        trigger = {
            **fixture["trigger"],
            "triggered_at": normalize_rfc3339(fixture["trigger"]["triggered_at"]),
        }
    except NormalizationError as exc:
        raise ExportV4Error(exc.code) from exc
    try:
        base_trace, _ = _trace(
            episode_id=fixture["episode_id"],
            model_records=decision_records,
            after=state_after,
            include_event_observations=True,
        )
        calls = _link_model_calls(build_model_calls_v4(model_records), base_trace, state_after)
        attempts, effects, actions, classification_issues = _effects_and_result(
            episode_id=fixture["episode_id"],
            calls=calls,
            trace=base_trace,
            state_after=state_after,
            state_delta=state_delta,
        )
        guard_facts = _guard_facts(state_after, base_trace)
        guard_status, guard_reason = _guard_status(guard_facts)
        guards = _guard_decisions(
            episode_id=fixture["episode_id"],
            facts=guard_facts,
            attempts=attempts,
            effects=effects,
        )
    except ExportError as exc:
        raise ExportV4Error(exc.code) from exc
    base_trace["guard_decisions"] = guards
    root_runs = [
        item
        for item in _entities(state_after, "agent_run")
        if item["data"].get("parent_run_ref") is None
    ]
    if len(root_runs) != 1:
        raise ExportV4Error("export_v4.run_state_missing")
    run = root_runs[0]["data"]
    durable_status = _durable_status(
        after=state_after,
        trace=base_trace,
        run_status=run["status"],
        guard_status=guard_status,
        effects=effects,
    )
    provider_eligible = provider_attestation["attribution_status"] == "eligible"
    base_environment = _environment(
        fixture=fixture,
        first_record=decision_records[0],
        before=state_before,
        after=state_after,
        resource_version=resource_version,
        resource_digest=resource_digest,
        git_commit=git_commit,
        invocation_mode=invocation_mode,
        model_name=model_name,
        model_temperature=model_temperature,
        model_reasoning_effort=model_reasoning_effort,
        frozen_time=frozen_time,
    )
    environment = {
        "schema_version": "environment-manifest-v2",
        "frozen_time": base_environment["frozen_time"],
        "timezone": base_environment["timezone"],
        "prompt": base_environment["prompt"],
        "tools": base_environment["tools"],
        "policies": base_environment["policies"],
        "resources": base_environment["resources"],
        "runtime": {
            "git_commit": git_commit,
            "worktree_clean": bool(provider_attestation["worktree_clean"]),
            "database_mode": "temporary_fixture",
            "fixture_db_sha256": base_environment["runtime"]["fixture_db_sha256"],
            "dependency_lock_version": dependency_lock_version,
            "dependency_lock_sha256": dependency_lock_sha256,
        },
        "isolation": base_environment["isolation"],
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
        "tool_invocations": base_trace["tool_invocations"],
        "run_events": base_trace["run_events"],
        "operations": base_trace["operations"],
        "guard_decisions": guards,
    }
    _, _, public_output = parse_action_declaration(str(run["output"] or ""))
    isolation_eligible = isolation_evidence_protocol_eligible(isolation_evidence)
    result = {
        "action_class": actions[0],
        "action_classes": actions,
        "classification_issues": classification_issues,
        "action_mapping_version": ACTION_MAPPING_VERSION_V2,
        "action_mapping_sha256": ACTION_MAPPING_SHA256_V2,
        "user_visible_output": public_output or None,
        "guard": {
            "status": guard_status,
            "reason_code": guard_reason,
            "blocked_effect_refs": [
                effect["effect_id"]
                for effect in effects
                if effect["status"] == "blocked"
            ],
        },
        "layers": {
            "model_attempts": attempts,
            "guard_decision_refs": [item["guard_id"] for item in guards],
            "final_effects": effects,
            "run_status": run["status"],
            "durable_status": durable_status,
            "protocol_eligibility": "eligible" if isolation_eligible else "invalid",
        },
    }
    structural_paths = sorted(
        {
            "state_before.snapshot_sha256",
            "state_after.snapshot_sha256",
            "state_delta.delta_sha256",
            "observable_trace.model_calls",
            "observable_trace.run_events",
            "result.layers",
            "isolation_evidence",
        }
    )
    structural_document = {
        "schema_version": "decision-episode-v4",
        "state_before": state_before,
        "state_after": state_after,
        "state_delta": state_delta,
        "observable_trace": trace,
        "result": result,
        "isolation_evidence": isolation_evidence,
    }
    missing_structural_paths = [
        path
        for path in structural_paths
        if not resolve_evidence_path(structural_document, path)[0]
    ]
    evidence_errors = [*state_delta["error_codes"]]
    if missing_structural_paths:
        evidence_errors.append("completeness.structural_path_missing")
    protocol_eligible = bool(not evidence_errors and isolation_eligible)
    result["layers"]["protocol_eligibility"] = (
        "eligible" if protocol_eligible else "invalid"
    )
    completeness = {
        "status": "complete" if not evidence_errors else "invalid",
        "evidence_error_codes": sorted(set(evidence_errors)),
        "verified_evidence_paths": structural_paths,
        "decision_correctness_evaluated": False,
        "completeness_sha256": "0" * 64,
    }
    completeness["completeness_sha256"] = episode_completeness_digest(completeness)
    episode = {
        "schema_version": "decision-episode-v4",
        "episode_id": fixture["episode_id"],
        "case_spec_sha256": case_spec["case_spec_sha256"],
        "judge_reference_sha256": judge_reference["reference_sha256"],
        "scenario_family_id": case_spec["scenario_family_id"],
        "track": case_spec["track"],
        "split": case_spec["split"],
        "difficulty": case_spec["difficulty"],
        "trigger": trigger,
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
            "protocol_eligible": protocol_eligible,
            "provider_eligible": provider_eligible,
            "evaluation_protocol_release_id": ACTIVE_PROTOCOL_RELEASE_ID,
            "evaluation_protocol_release_sha256": evaluation_protocol_release_sha256,
            "benchmark_release_id": benchmark_release_id,
            "benchmark_release_sha256": benchmark_release_sha256,
            "runtime_run_id": runtime_run_id,
            "runtime_source_bundle_version": runtime_source_bundle_version,
            "runtime_source_bundle_sha256": runtime_source_bundle_sha256,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
            "created_at": frozen_time,
            "source_refs": [
                f"case-spec:{case_spec['case_spec_sha256']}",
                f"resource:{resource_version}",
            ],
            "episode_sha256": "0" * 64,
        },
    }
    episode["provenance"]["episode_sha256"] = decision_episode_digest(episode)
    try:
        validated = DecisionEpisodeV4.model_validate(episode).model_dump(
            mode="json", by_alias=True
        )
        canonical_json_bytes(validated)
    except ValueError as exc:
        raise ExportV4Error("export_v4.contract_invalid") from exc
    return validated
