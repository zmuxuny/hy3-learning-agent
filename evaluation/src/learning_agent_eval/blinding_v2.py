"""Path-exact v3 Judge projection that preserves ordinary business language."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .canonical import sha256_digest
from .privacy import is_private_reasoning_field, privacy_issues
from .rubric import rubric_projection
from .validator import resolve_evidence_path

BLIND_INPUT_VERSION_V2 = "blind-judge-input-v2"
BLIND_INPUT_VERSION_V3 = "blind-judge-input-v3"
_OPAQUE_TRANSLATION = str.maketrans("0123456789abcdef", "ghijklmnopqrstuv")
_IDENTITY_KEYS = {
    "artifact_id",
    "call_id",
    "decision_ref",
    "effect_id",
    "entity_ref",
    "event_id",
    "failure_id",
    "invocation_id",
    "invocation_ref",
    "logical_id",
    "operation_id",
    "parent_call_id",
    "parent_run_id",
    "primary_entity_ref",
    "run_id",
    "scope_ref",
    "source_ref",
    "tool_call_id",
    "trigger_id",
}
_DROP_EXACT_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credentials",
    "endpoint",
    "endpoint_origin",
    "password",
    "private_key",
    "provider_request_id",
    "recipient",
    "refresh_token",
    "reply_token",
    "routing",
    "secret",
    "smtp_from",
    "smtp_to",
}
_HIDDEN_RULE_IDS = {"common.provider_attribution"}
_HIDDEN_EVIDENCE_PREFIXES = (
    "environment.provider_attestation",
    "provenance.",
)


class BlindProjectionV2Error(ValueError):
    """Authoritative inputs cannot produce the required safe projection."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BlindJudgeInputV2:
    judge_id: str
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class BlindJudgeInputV3:
    judge_id: str
    document: dict[str, Any]
    sha256: str


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")


def _identity_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _IDENTITY_KEYS or normalized.endswith(("_ref", "_refs"))


def _opaque(value: str, *, salt: str) -> str:
    digest = hashlib.sha256(
        f"{BLIND_INPUT_VERSION_V2}\0{salt}\0{value}".encode()
    ).hexdigest()
    return f"opaque-{digest[:20].translate(_OPAQUE_TRANSLATION)}"


def _sanitize(value: object, *, salt: str, parent_key: str = "") -> Any:
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = _normalized_key(key)
            if is_private_reasoning_field(key) or normalized in _DROP_EXACT_KEYS:
                continue
            projected[key] = _sanitize(child, salt=salt, parent_key=key)
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(item, salt=salt, parent_key=parent_key) for item in value]
    if isinstance(value, str) and _identity_key(parent_key):
        return _opaque(value, salt=salt)
    return value


def _state_projection(state: Mapping[str, Any], *, salt: str) -> dict[str, Any]:
    projected = deepcopy(dict(state))
    for entity in projected.get("logical_entities", []):
        if entity.get("entity_type") == "agent_run":
            data = entity.get("data")
            if isinstance(data, dict):
                data.pop("model", None)
    return _sanitize(projected, salt=salt)


def _trace_projection(trace: Mapping[str, Any], *, salt: str) -> dict[str, Any]:
    projected = deepcopy(dict(trace))
    for call in projected.get("model_calls", []):
        call.pop("request_model", None)
        call.pop("request_config", None)
        call.pop("token_usage", None)
    return _sanitize(projected, salt=salt)


def _delta_projection(episode: Mapping[str, Any], *, salt: str) -> dict[str, Any]:
    projected = deepcopy(dict(episode["state_delta"]))
    run_refs = {
        entity["logical_id"]
        for root in ("state_before", "state_after")
        for entity in episode[root]["logical_entities"]
        if entity["entity_type"] == "agent_run"
    }
    for change in projected.get("changes", []):
        if change.get("entity_ref") not in run_refs:
            continue
        for side in ("before", "after"):
            value = change.get(side, {}).get("value")
            if isinstance(value, dict):
                value.pop("model", None)
                if isinstance(value.get("data"), dict):
                    value["data"].pop("model", None)
            elif change.get("field_path") == "data.model":
                change[side]["value"] = "<withheld-model-identity>"
    return _sanitize(projected, salt=salt)


def _episode_projection(episode: Mapping[str, Any], *, salt: str) -> dict[str, Any]:
    environment = episode["environment"]
    projection = {
        "schema_version": episode["schema_version"],
        "track": episode["track"],
        "trigger": _sanitize(episode["trigger"], salt=salt),
        "state_after": _state_projection(episode["state_after"], salt=salt),
        "state_delta": _delta_projection(episode, salt=salt),
        "environment": _sanitize(
            {
                "schema_version": environment["schema_version"],
                "frozen_time": environment["frozen_time"],
                "timezone": environment["timezone"],
                "prompt": environment["prompt"],
                "tools": environment["tools"],
                "policies": environment["policies"],
                "resources": environment["resources"],
                "isolation": environment["isolation"],
            },
            salt=salt,
        ),
        "observable_trace": _trace_projection(episode["observable_trace"], salt=salt),
        "result": _sanitize(episode["result"], salt=salt),
        "completeness": _sanitize(episode["completeness"], salt=salt),
        "isolation_evidence": _sanitize(episode["isolation_evidence"], salt=salt),
    }
    return projection


def _rule_projection(rule_result: Mapping[str, Any], *, salt: str) -> dict[str, Any]:
    checks = []
    for item in rule_result["checks"]:
        check_id = str(item["check_id"])
        if check_id in _HIDDEN_RULE_IDS:
            continue
        evidence_paths = [
            path
            for path in item["evidence_paths"]
            if not path.startswith(_HIDDEN_EVIDENCE_PREFIXES)
        ]
        if not evidence_paths:
            continue
        if check_id.startswith("case."):
            check_id = _opaque(check_id, salt=salt)
        checks.append(
            {
                "check_id": check_id,
                "rule_pack": item["rule_pack"],
                "status": item["status"],
                "severity": item["severity"],
                "evidence_paths": deepcopy(evidence_paths),
                "reason_code": item["reason_code"],
            }
        )
    return {
        "schema_version": rule_result["schema_version"],
        "status": rule_result["status"],
        "hard_gates": [
            _opaque(str(item), salt=salt) if str(item).startswith("case.") else item
            for item in rule_result["hard_gates"]
            if str(item) not in _HIDDEN_RULE_IDS
        ],
        "checks": checks,
        "dimension_signals": deepcopy(rule_result["dimension_signals"]),
    }


def _reference_projection(reference: Mapping[str, Any], *, salt: str) -> dict[str, Any]:
    return {
        "schema_version": reference["schema_version"],
        "track": reference["track"],
        "allowed_action_classes": deepcopy(reference["allowed_action_classes"]),
        "constraints": [
            {
                "constraint_ref": _opaque(item["constraint_id"], salt=salt),
                "kind": item["kind"],
                "evaluation": item["evaluation"],
                "public_statement": item["public_statement"],
                "criticality": item["criticality"],
                "evidence_paths": deepcopy(item["evidence_paths"]),
                "predicates": deepcopy(item["predicates"]),
            }
            for item in reference["constraints"]
        ],
        "acceptable_variations": deepcopy(reference["acceptable_variations"]),
    }


def _reference_projection_v3(
    reference: Mapping[str, Any], *, salt: str
) -> dict[str, Any]:
    projected = _reference_projection(reference, salt=salt)
    projected["predicate_semantics"] = reference["predicate_semantics"]
    return projected


def build_blind_judge_input_v2(
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> BlindJudgeInputV2:
    """Build the deterministic v3 projection without modifying business terms."""

    episode_digest = str(episode["provenance"]["episode_sha256"])
    rule_digest = str(rule_result["result_sha256"])
    reference_digest = str(reference["reference_sha256"])
    salt = sha256_digest(
        {
            "episode": episode_digest,
            "rules": rule_digest,
            "reference": reference_digest,
        }
    )
    judge_id = f"judge-{salt[:20].translate(_OPAQUE_TRANSLATION)}"
    episode_projection = _episode_projection(episode, salt=salt)
    for constraint in reference["constraints"]:
        for path in constraint["evidence_paths"]:
            if (
                not resolve_evidence_path(episode, path)[0]
                or not resolve_evidence_path(episode_projection, path)[0]
            ):
                raise BlindProjectionV2Error("blind.reference_evidence_unavailable")
    document = {
        "schema_version": BLIND_INPUT_VERSION_V2,
        "judge_id": judge_id,
        "track": episode["track"],
        "episode": episode_projection,
        "authoritative_rule_facts": _rule_projection(rule_result, salt=salt),
        "judge_reference": _reference_projection(reference, salt=salt),
        "rubric": rubric_projection(episode["track"]),
    }
    if privacy_issues(document, file="blind-judge-input-v2"):
        raise BlindProjectionV2Error("blind.privacy_invalid")
    if (
        set(document["episode"])
        != {
            "schema_version",
            "track",
            "trigger",
            "state_after",
            "state_delta",
            "environment",
            "observable_trace",
            "result",
            "completeness",
            "isolation_evidence",
        }
        or "provider_attestation" in document["episode"]["environment"]
        or any(
            "request_model" in call
            for call in document["episode"]["observable_trace"]["model_calls"]
        )
    ):
        raise BlindProjectionV2Error("blind.forbidden_metadata")
    return BlindJudgeInputV2(
        judge_id=judge_id,
        document=document,
        sha256=sha256_digest(document),
    )


def path_visible_to_judge_v2(
    blind_input: BlindJudgeInputV2, evidence_path: str
) -> bool:
    """Return whether an original v3 Evidence Path survives the projection."""

    return resolve_evidence_path(blind_input.document["episode"], evidence_path)[0]


def build_blind_judge_input_v3(
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> BlindJudgeInputV3:
    """Build the active v4 projection with explicit predicate semantics."""

    if (
        episode.get("schema_version") != "decision-episode-v4"
        or rule_result.get("schema_version") != "rule-result-v3"
        or reference.get("schema_version") != "judge-reference-v2"
        or reference.get("predicate_semantics") != "constraint-proposition-v1"
    ):
        raise BlindProjectionV2Error("blind.contract_version_invalid")
    salt = sha256_digest(
        {
            "version": BLIND_INPUT_VERSION_V3,
            "episode": episode["provenance"]["episode_sha256"],
            "rules": rule_result["result_sha256"],
            "reference": reference["reference_sha256"],
        }
    )
    judge_id = f"judge-{salt[:20].translate(_OPAQUE_TRANSLATION)}"
    episode_projection = _episode_projection(episode, salt=salt)
    for constraint in reference["constraints"]:
        for path in constraint["evidence_paths"]:
            original_found = resolve_evidence_path(episode, path)[0]
            projected_found = resolve_evidence_path(episode_projection, path)[0]
            if original_found != projected_found:
                raise BlindProjectionV2Error("blind.reference_evidence_mismatch")
    document = {
        "schema_version": BLIND_INPUT_VERSION_V3,
        "judge_id": judge_id,
        "track": episode["track"],
        "episode": episode_projection,
        "authoritative_rule_facts": _rule_projection(rule_result, salt=salt),
        "judge_reference": _reference_projection_v3(reference, salt=salt),
        "rubric": rubric_projection(episode["track"]),
    }
    if privacy_issues(document, file="blind-judge-input-v3"):
        raise BlindProjectionV2Error("blind.privacy_invalid")
    if (
        "provider_attestation" in document["episode"]["environment"]
        or any(
            "request_model" in call
            for call in document["episode"]["observable_trace"]["model_calls"]
        )
    ):
        raise BlindProjectionV2Error("blind.forbidden_metadata")
    return BlindJudgeInputV3(
        judge_id=judge_id,
        document=document,
        sha256=sha256_digest(document),
    )


def path_visible_to_judge_v3(
    blind_input: BlindJudgeInputV3, evidence_path: str
) -> bool:
    """Return whether an original v4 Evidence Path survives the projection."""

    return resolve_evidence_path(blind_input.document["episode"], evidence_path)[0]
