"""Deterministic v3 trajectory, attribution, and terminal artifact builders."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .action_protocol import (
    ACTION_DECLARATION_PROTOCOL_SHA256,
    ACTION_DECLARATION_PROTOCOL_VERSION,
    parse_action_declaration,
)
from .canonical import sha256_digest
from .eligibility import isolation_evidence_protocol_eligible
from .integrity import (
    artifact_manifest_digest,
    model_visible_context_digest,
    provider_attestation_digest,
    runtime_failure_digest,
)
from .models import (
    ModelCallV3,
    ModelCallV4,
    ProviderAttestationV1,
    RuntimeFailureV1,
    RuntimeFailureV2,
    RuntimeRunManifestV2,
    RuntimeRunManifestV3,
)
from .runtime_metadata import (
    DEPENDENCY_LOCK_VERSION,
    ENDPOINT_POLICY_SHA256,
    ENDPOINT_POLICY_VERSION,
    HY3_ENDPOINT_ID,
    HY3_ENDPOINT_ORIGIN,
    HY3_MODEL,
    provider_attribution_reason_codes,
)
from .runtime_metadata import (
    dependency_lock_sha256 as current_dependency_lock_sha256,
)


class E31RuntimeArtifactError(ValueError):
    """A recorded call cannot be represented without guessing."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonicalize_concurrent_model_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return a stable causal linearization of concurrent model-call branches.

    Calls retain their order inside each Run. Child Runs follow the parent call
    that created them, while concurrent siblings are ordered by their already
    normalized stable Run identity. This records the causal partial order
    without pretending that scheduler-dependent wall-clock order is semantic.
    """

    copied = [dict(record) for record in records]
    call_ids = [str(record.get("call_id") or "") for record in copied]
    if any(not call_id for call_id in call_ids) or len(call_ids) != len(set(call_ids)):
        raise E31RuntimeArtifactError("trajectory.call_identity_invalid")

    by_run: dict[str, list[dict[str, Any]]] = {}
    for record in sorted(copied, key=lambda item: int(item.get("ordinal") or 0)):
        run_id = str(record.get("run_id") or "")
        if not run_id:
            raise E31RuntimeArtifactError("trajectory.call_scope_missing")
        by_run.setdefault(run_id, []).append(record)

    run_parent: dict[str, tuple[str | None, str | None]] = {}
    children_by_call: dict[str, list[str]] = {}
    roots: list[str] = []
    for run_id, run_records in by_run.items():
        relationships = {
            (
                str(record["parent_run_id"])
                if record.get("parent_run_id") is not None
                else None,
                str(record["parent_call_id"])
                if record.get("parent_call_id") is not None
                else None,
            )
            for record in run_records
        }
        if len(relationships) != 1:
            raise E31RuntimeArtifactError("trajectory.run_parent_inconsistent")
        parent_run_id, parent_call_id = relationships.pop()
        run_parent[run_id] = (parent_run_id, parent_call_id)
        if parent_run_id is None:
            if parent_call_id is not None:
                raise E31RuntimeArtifactError("trajectory.root_parent_invalid")
            roots.append(run_id)
        else:
            if parent_call_id is None:
                raise E31RuntimeArtifactError("trajectory.parent_call_missing")
            children_by_call.setdefault(parent_call_id, []).append(run_id)

    ordered: list[dict[str, Any]] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(run_id: str) -> None:
        if run_id in visiting:
            raise E31RuntimeArtifactError("trajectory.run_cycle")
        if run_id in visited:
            raise E31RuntimeArtifactError("trajectory.run_reused")
        visiting.add(run_id)
        for record in by_run[run_id]:
            ordered.append(record)
            for child_run_id in sorted(
                children_by_call.get(str(record["call_id"]), [])
            ):
                parent_run_id, _ = run_parent[child_run_id]
                if parent_run_id != run_id:
                    raise E31RuntimeArtifactError("trajectory.parent_run_mismatch")
                visit(child_run_id)
        visiting.remove(run_id)
        visited.add(run_id)

    for root_run_id in sorted(roots):
        visit(root_run_id)
    if visited != set(by_run):
        raise E31RuntimeArtifactError("trajectory.call_unreachable")

    remapped_ids = {
        str(record["call_id"]): f"model-call:{ordinal:03d}"
        for ordinal, record in enumerate(ordered, 1)
    }
    result: list[dict[str, Any]] = []
    for ordinal, record in enumerate(ordered, 1):
        parent_call_id = record.get("parent_call_id")
        result.append(
            {
                **record,
                "call_id": remapped_ids[str(record["call_id"])],
                "ordinal": ordinal,
                "parent_call_id": (
                    remapped_ids[str(parent_call_id)]
                    if parent_call_id is not None
                    else None
                ),
            }
        )
    return result


def _message_projection(message: object, ordinal: int) -> dict[str, object]:
    if not isinstance(message, Mapping):
        raise E31RuntimeArtifactError("trajectory.message_not_object")
    role = message.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise E31RuntimeArtifactError("trajectory.message_role_invalid")
    payload = {str(key): value for key, value in message.items() if key != "role"}
    return {"ordinal": ordinal, "role": role, "payload": payload}


def build_model_calls_v3(
    records: Sequence[Mapping[str, Any]],
    *,
    normalize_run_id: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Build a strict hierarchical trace from the public recorder projection."""

    normalize = normalize_run_id or (lambda value: value)
    calls: list[dict[str, Any]] = []
    for ordinal, record in enumerate(records, 1):
        if record.get("ordinal") != ordinal:
            raise E31RuntimeArtifactError("trajectory.call_order_invalid")
        raw_run_id = str(record.get("run_id") or "")
        if not raw_run_id or raw_run_id == "unscoped-run":
            raise E31RuntimeArtifactError("trajectory.call_scope_missing")
        raw_parent = record.get("parent_run_id")
        messages = [
            _message_projection(message, index)
            for index, message in enumerate(record.get("visible_messages") or [], 1)
        ]
        tools = record.get("visible_tool_schemas") or []
        if not isinstance(tools, list):
            raise E31RuntimeArtifactError("trajectory.tool_schemas_invalid")
        context = {
            "context_version": "model-visible-context-v1",
            "messages": messages,
            "tool_schemas": tools,
            "context_sha256": "0" * 64,
        }
        context["context_sha256"] = model_visible_context_digest(context)
        function_calls = record.get("function_calls") or []
        if not isinstance(function_calls, list):
            raise E31RuntimeArtifactError("trajectory.function_calls_invalid")
        status = str(record.get("response_status") or "")
        if status not in {"completed", "provider_error", "framework_error", "cancelled"}:
            raise E31RuntimeArtifactError("trajectory.response_status_invalid")
        call = {
            "call_id": str(record.get("call_id") or f"model-call:{ordinal:03d}"),
            "ordinal": ordinal,
            "run_id": normalize(raw_run_id),
            "parent_run_id": normalize(str(raw_parent)) if raw_parent is not None else None,
            "parent_call_id": record.get("parent_call_id"),
            "depth": int(record.get("depth") or 0),
            "call_purpose": str(record.get("call_purpose") or ""),
            "decision_relevant": bool(record.get("decision_relevant")),
            "visible_context": context,
            "request_model": str(record.get("request_model") or "unspecified-model"),
            "assistant_text": (
                str(record.get("assistant_text") or "") if status == "completed" else None
            ),
            "tool_call_refs": [
                str(item.get("call_id"))
                for item in function_calls
                if isinstance(item, Mapping) and item.get("call_id")
            ],
            "status": status,
            "response_sha256": record.get("response_digest"),
        }
        calls.append(ModelCallV3.model_validate(call).model_dump(mode="json"))
    return calls


def build_model_calls_v4(
    records: Sequence[Mapping[str, Any]],
    *,
    normalize_run_id: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Build the active trace with exact, non-heuristic action declarations."""

    calls = build_model_calls_v3(records, normalize_run_id=normalize_run_id)
    result: list[dict[str, Any]] = []
    for call, record in zip(calls, records, strict=True):
        applicable = call["call_purpose"] == "decision" and call["status"] == "completed"
        if applicable:
            declaration_status, action_classes, _ = parse_action_declaration(
                str(call["assistant_text"] or "")
            )
        else:
            declaration_status, action_classes = "not_applicable", ()
        enriched = {
            **call,
            "request_config": dict(record.get("request_config") or {}),
            "token_usage": record.get("token_usage"),
            "response_validation_errors": [
                {"tool_call_id": item["call_id"], "code": item["argument_error"]}
                for item in record.get("function_calls", []) if item.get("argument_error")
            ] + ([{"code": record["record_error"]}] if record.get("record_error") else []),
            "action_protocol_version": ACTION_DECLARATION_PROTOCOL_VERSION,
            "action_protocol_sha256": ACTION_DECLARATION_PROTOCOL_SHA256,
            "action_declaration_status": declaration_status,
            "declared_action_classes": list(action_classes),
        }
        result.append(ModelCallV4.model_validate(enriched).model_dump(mode="json"))
    return result


def build_stub_provider_attestation(
    records: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    configured_model: str,
    frozen_time: str,
    git_commit: str,
    dependency_lock_version: str,
    dependency_lock_sha256: str,
    endpoint_policy_version: str,
    endpoint_policy_sha256: str,
    worktree_clean: bool = True,
) -> dict[str, Any]:
    """Describe a fixed stub honestly; it can never receive formal status."""

    calls = []
    for record in records:
        status = str(record.get("response_status") or "framework_error")
        if status not in {"completed", "provider_error", "framework_error"}:
            status = "framework_error"
        calls.append(
            {
                "call_id": str(record.get("call_id") or "missing-call"),
                "request_model": str(record.get("request_model") or configured_model),
                "response_model": (
                    str(record.get("response_model") or "unreported")
                    if status == "completed"
                    else None
                ),
                "provider_request_id": (
                    str(record["provider_request_id"])
                    if record.get("provider_request_id") is not None
                    else None
                ),
                "requested_at": frozen_time,
                "responded_at": frozen_time if status == "completed" else None,
                "status": status,
            }
        )
    attestation = {
        "schema_version": "provider-attestation-v1",
        "scope": scope,
        "invocation_mode": "stub",
        "provider_id": "none",
        "endpoint_policy_version": endpoint_policy_version,
        "endpoint_policy_sha256": endpoint_policy_sha256,
        "endpoint_id": None,
        "endpoint_origin": None,
        "configured_model": configured_model,
        "calls": calls,
        "configuration_sha256": sha256_digest(
            {
                "mode": "stub",
                "model": configured_model,
                "scope": scope,
            }
        ),
        "git_commit": git_commit,
        "worktree_clean": worktree_clean,
        "dependency_lock_version": dependency_lock_version,
        "dependency_lock_sha256": dependency_lock_sha256,
        "dependency_lock_verified": False,
        "attribution_status": "ineligible_stub",
        "reason_codes": ["provider.stub"],
        "attestation_sha256": "0" * 64,
    }
    attestation["attestation_sha256"] = provider_attestation_digest(attestation)
    return ProviderAttestationV1.model_validate(attestation).model_dump(mode="json")


def build_real_provider_attestation(
    records: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    configuration_sha256: str,
    git_commit: str,
    worktree_clean: bool,
    dependency_lock_verified: bool,
) -> dict[str, Any]:
    """Build a recomputable audit attribution for the fixed Hy3 endpoint."""

    calls = []
    for record in records:
        status = str(record.get("response_status") or "framework_error")
        if status not in {"completed", "provider_error", "framework_error"}:
            status = "framework_error"
        calls.append(
            {
                "call_id": str(record.get("call_id") or "missing-call"),
                "request_model": str(record.get("request_model") or "unspecified-model"),
                "response_model": (
                    str(record["response_model"])
                    if record.get("response_model") is not None
                    else None
                ),
                "provider_request_id": (
                    str(record["provider_request_id"])
                    if record.get("provider_request_id") is not None
                    else None
                ),
                "requested_at": str(record.get("requested_at") or ""),
                "responded_at": (
                    str(record["responded_at"])
                    if record.get("responded_at") is not None
                    else None
                ),
                "status": status,
            }
        )
    lock_digest = current_dependency_lock_sha256()
    attestation = {
        "schema_version": "provider-attestation-v1",
        "scope": scope,
        "invocation_mode": "real",
        "provider_id": "tencent-tokenhub",
        "endpoint_policy_version": ENDPOINT_POLICY_VERSION,
        "endpoint_policy_sha256": ENDPOINT_POLICY_SHA256,
        "endpoint_id": HY3_ENDPOINT_ID,
        "endpoint_origin": HY3_ENDPOINT_ORIGIN,
        "configured_model": HY3_MODEL,
        "calls": calls,
        "configuration_sha256": configuration_sha256,
        "git_commit": git_commit,
        "worktree_clean": worktree_clean,
        "dependency_lock_version": DEPENDENCY_LOCK_VERSION,
        "dependency_lock_sha256": lock_digest,
        "dependency_lock_verified": dependency_lock_verified,
        "attribution_status": "invalid",
        "reason_codes": [],
        "attestation_sha256": "0" * 64,
    }
    reasons = list(provider_attribution_reason_codes(attestation))
    attestation["reason_codes"] = reasons
    attestation["attribution_status"] = "invalid" if reasons else "eligible"
    attestation["attestation_sha256"] = provider_attestation_digest(attestation)
    return ProviderAttestationV1.model_validate(attestation).model_dump(mode="json")


def build_runtime_failure(
    *,
    case_id: str,
    case_spec_sha256: str,
    stage: str,
    failure_class: str,
    reason_code: str,
    public_summary: str,
    model_calls: Sequence[Mapping[str, Any]],
    provider_attestation: Mapping[str, Any],
    isolation_evidence: Mapping[str, Any] | None,
    started_at: str,
    failed_at: str,
) -> dict[str, Any]:
    """Create a safe Failure without retaining exceptions or provider payloads."""

    failure = {
        "schema_version": "runtime-failure-v1",
        "failure_id": f"failure:{case_id}",
        "case_id": case_id,
        "case_spec_sha256": case_spec_sha256,
        "stage": stage,
        "failure_class": failure_class,
        "reason_code": reason_code,
        "public_summary": public_summary,
        "model_calls": list(model_calls),
        "provider_attestation": dict(provider_attestation),
        "isolation_evidence": (
            dict(isolation_evidence) if isolation_evidence is not None else None
        ),
        "started_at": started_at,
        "failed_at": failed_at,
        "formal_evaluation_result": False,
        "evaluation_status": "not_a_formal_model_evaluation",
        "failure_sha256": "0" * 64,
    }
    failure["failure_sha256"] = runtime_failure_digest(failure)
    return RuntimeFailureV1.model_validate(failure).model_dump(mode="json", by_alias=True)


def build_runtime_failure_v2(
    *,
    case_id: str,
    case_spec_sha256: str,
    stage: str,
    failure_class: str,
    reason_code: str,
    public_summary: str,
    model_calls: Sequence[Mapping[str, Any]],
    provider_attestation: Mapping[str, Any],
    isolation_evidence: Mapping[str, Any] | None,
    started_at: str,
    failed_at: str,
    evaluation_protocol_release_sha256: str,
    benchmark_release_id: str,
    benchmark_release_sha256: str,
    runtime_run_id: str,
    runtime_source_bundle_version: str,
    runtime_source_bundle_sha256: str,
) -> dict[str, Any]:
    """Create an active Failure carrying the v4 model-call contract."""

    failure = {
        "schema_version": "runtime-failure-v2",
        "failure_id": f"failure:{case_id}",
        "case_id": case_id,
        "case_spec_sha256": case_spec_sha256,
        "stage": stage,
        "failure_class": failure_class,
        "reason_code": reason_code,
        "public_summary": public_summary,
        "model_calls": list(model_calls),
        "provider_attestation": dict(provider_attestation),
        "isolation_evidence": (
            dict(isolation_evidence) if isolation_evidence is not None else None
        ),
        "started_at": started_at,
        "failed_at": failed_at,
        "protocol_eligible": bool(
            failure_class != "isolation_violation"
            and isolation_evidence_protocol_eligible(isolation_evidence)
        ),
        "provider_eligible": provider_attestation["attribution_status"] == "eligible",
        "evaluation_protocol_release_id": "evaluation-protocol-release-1.0",
        "evaluation_protocol_release_sha256": evaluation_protocol_release_sha256,
        "benchmark_release_id": benchmark_release_id,
        "benchmark_release_sha256": benchmark_release_sha256,
        "runtime_run_id": runtime_run_id,
        "runtime_source_bundle_version": runtime_source_bundle_version,
        "runtime_source_bundle_sha256": runtime_source_bundle_sha256,
        "formal_evaluation_result": False,
        "evaluation_status": "not_a_formal_model_evaluation",
        "failure_sha256": "0" * 64,
    }
    failure["failure_sha256"] = runtime_failure_digest(failure)
    return RuntimeFailureV2.model_validate(failure).model_dump(mode="json", by_alias=True)


def build_runtime_manifest_v2(
    *,
    dataset_version: str,
    invocation_mode: str,
    terminals: Sequence[Mapping[str, Any]],
    git_commit: str,
    dependency_lock_version: str,
    dependency_lock_sha256: str,
) -> dict[str, Any]:
    """Build the exhaustive one-terminal-per-case batch manifest."""

    ordered = sorted((dict(item) for item in terminals), key=lambda item: item["case_id"])
    formal = bool(
        invocation_mode == "real"
        and ordered
        and all(
            item["terminal_kind"] == "episode"
            and item["formal_evaluation_result"]
            for item in ordered
        )
    )
    manifest = {
        "schema_version": "runtime-run-manifest-v2",
        "dataset_version": dataset_version,
        "case_schema_version": "case-spec-v1",
        "episode_schema_version": "decision-episode-v3",
        "failure_schema_version": "runtime-failure-v1",
        "invocation_mode": invocation_mode,
        "selected_case_ids": [item["case_id"] for item in ordered],
        "terminals": ordered,
        "formal_evaluation_result": formal,
        "evaluation_status": (
            "formal_model_evaluation"
            if formal
            else "not_a_formal_model_evaluation"
        ),
        "git_commit": git_commit,
        "dependency_lock_version": dependency_lock_version,
        "dependency_lock_sha256": dependency_lock_sha256,
        "manifest_sha256": "0" * 64,
    }
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    return RuntimeRunManifestV2.model_validate(manifest).model_dump(mode="json")


def build_runtime_manifest_v3(
    *,
    runtime_run_id: str,
    dataset_version: str,
    evaluation_protocol_release_sha256: str,
    benchmark_release_id: str,
    benchmark_release_sha256: str,
    case_suite_sha256: str,
    benchmark_expected_total_cases: int,
    benchmark_expected_track_counts: Mapping[str, int],
    invocation_mode: str,
    terminals: Sequence[Mapping[str, Any]],
    requested_episode_ids: Sequence[str],
    selected_track: str | None,
    git_commit: str,
    worktree_clean: bool,
    dependency_lock_version: str,
    dependency_lock_sha256: str,
    runtime_source_bundle_version: str,
    runtime_source_bundle_sha256: str,
    protocol_release_verified: bool,
    benchmark_release_trusted: bool,
    suite_complete: bool,
    trust_reason_codes: Sequence[str],
) -> dict[str, Any]:
    """Build the active exhaustive manifest with pre-run selection provenance."""

    ordered = sorted((dict(item) for item in terminals), key=lambda item: item["case_id"])
    selection_mode = (
        "adhoc_filter"
        if requested_episode_ids or selected_track is not None
        else "unfiltered_suite"
    )
    provider_eligible = bool(
        invocation_mode == "real"
        and ordered
        and all(item["provider_eligible"] for item in ordered)
    )
    protocol_eligible = bool(
        worktree_clean
        and protocol_release_verified
        and ordered
        and all(item["protocol_eligible"] for item in ordered)
    )
    trusted = bool(
        protocol_eligible
        and provider_eligible
        and benchmark_release_trusted
        and suite_complete
        and selection_mode == "unfiltered_suite"
    )
    reasons = set(trust_reason_codes)
    if not worktree_clean:
        reasons.add("runtime.worktree_dirty")
    if not protocol_release_verified:
        reasons.add("runtime.protocol_release_unverified")
    if not provider_eligible:
        reasons.add("runtime.provider_ineligible")
    manifest = {
        "schema_version": "runtime-run-manifest-v3",
        "runtime_run_id": runtime_run_id,
        "dataset_version": dataset_version,
        "evaluation_protocol_release_id": "evaluation-protocol-release-1.0",
        "evaluation_protocol_release_sha256": evaluation_protocol_release_sha256,
        "benchmark_release_id": benchmark_release_id,
        "benchmark_release_sha256": benchmark_release_sha256,
        "case_suite_sha256": case_suite_sha256,
        "benchmark_expected_total_cases": benchmark_expected_total_cases,
        "benchmark_expected_track_counts": dict(benchmark_expected_track_counts),
        "case_schema_version": "case-spec-v2",
        "episode_schema_version": "decision-episode-v4",
        "failure_schema_version": "runtime-failure-v2",
        "invocation_mode": invocation_mode,
        "selection_mode": selection_mode,
        "requested_episode_ids": sorted(set(requested_episode_ids)),
        "selected_track": selected_track,
        "selected_case_ids": [item["case_id"] for item in ordered],
        "terminals": ordered,
        "runtime_source_bundle_version": runtime_source_bundle_version,
        "runtime_source_bundle_sha256": runtime_source_bundle_sha256,
        "protocol_release_verified": protocol_release_verified,
        "protocol_eligible": protocol_eligible,
        "provider_eligible": provider_eligible,
        "benchmark_release_trusted": benchmark_release_trusted,
        "suite_complete": suite_complete,
        "trusted_benchmark_run": trusted,
        "trust_reason_codes": sorted(reasons),
        "formal_evaluation_result": False,
        "evaluation_status": "not_a_formal_model_evaluation",
        "git_commit": git_commit,
        "worktree_clean": worktree_clean,
        "dependency_lock_version": dependency_lock_version,
        "dependency_lock_sha256": dependency_lock_sha256,
        "manifest_sha256": "0" * 64,
    }
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    return RuntimeRunManifestV3.model_validate(manifest).model_dump(mode="json")
