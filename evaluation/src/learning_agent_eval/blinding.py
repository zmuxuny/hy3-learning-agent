"""Deterministic E3 Judge projection; never a second evaluation fact source."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_json_bytes, sha256_digest
from .privacy import is_private_reasoning_field, privacy_issues
from .rubric import rubric_projection

BLIND_INPUT_VERSION = "blind-judge-input-v1"

_QUALITY_LABEL = re.compile(
    r"(?i)(?<![A-Za-z0-9])(good|mild|severe|baseline|candidate)(?![A-Za-z0-9])"
)
_DROP_KEYS = {
    "model",
    "model_name",
    "model_version",
    "provider",
    "invocation_mode",
    "prompt",
    "prompt_version",
    "prompt_digest",
    "visible_input_digest",
    "git_commit",
    "fixture_db_sha256",
    "oracle_author",
    "oracle_reviewer",
    "adjudication_note",
    "author_role",
    "reviewer_role",
    "purpose",
    "source_refs",
}
_ROUTING_OR_CREDENTIAL_FRAGMENTS = (
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "cookie",
    "email",
    "endpoint",
    "imap",
    "password",
    "p256dh",
    "private_key",
    "recipient",
    "refresh_token",
    "reply_token",
    "route_digest",
    "routing",
    "secret",
    "smtp_from",
    "smtp_to",
)
_IDENTITY_KEYS = {
    "logical_id",
    "scope_ref",
    "entity_ref",
    "primary_entity_ref",
    "decision_ref",
    "invocation_ref",
    "turn_ref",
    "call_id",
    "tool_call_id",
    "trigger_id",
}
_OPAQUE_TRANSLATION = str.maketrans("0123456789abcdef", "ghijklmnopqrstuv")


class BlindProjectionError(ValueError):
    """The authoritative inputs could not produce a safe Judge projection."""


@dataclass(frozen=True, slots=True)
class BlindJudgeInput:
    judge_id: str
    document: dict[str, Any]
    sha256: str


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")


def _is_identity_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _IDENTITY_KEYS or normalized.endswith(("_id", "_ref", "_refs"))


def _opaque(value: str, *, episode_digest: str) -> str:
    digest = hashlib.sha256(
        f"{BLIND_INPUT_VERSION}\0{episode_digest}\0{value}".encode()
    ).hexdigest()
    return f"opaque-{digest[:20].translate(_OPAQUE_TRANSLATION)}"


def _sanitize(
    value: object,
    *,
    episode_digest: str,
    parent_key: str = "",
) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = _normalized_key(key)
            if (
                is_private_reasoning_field(key)
                or normalized in _DROP_KEYS
                or "candidate" in normalized
                or "baseline" in normalized
                or normalized.endswith(("_sha256", "_digest", "_digests"))
                or any(
                    fragment in normalized
                    for fragment in _ROUTING_OR_CREDENTIAL_FRAGMENTS
                )
            ):
                continue
            result[key] = _sanitize(
                child,
                episode_digest=episode_digest,
                parent_key=key,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _sanitize(item, episode_digest=episode_digest, parent_key=parent_key)
            for item in value
        ]
    if isinstance(value, str):
        if _is_identity_key(parent_key):
            return _opaque(value, episode_digest=episode_digest)
        return _QUALITY_LABEL.sub("[blinded-label]", value)
    return value


def _oracle_projection(
    oracle: Mapping[str, Any], *, episode_digest: str
) -> dict[str, Any]:
    return {
        "schema_version": oracle["schema_version"],
        "allowed_action_classes": deepcopy(oracle["allowed_action_classes"]),
        "must_satisfy": [
            {
                "constraint_ref": _opaque(item["id"], episode_digest=episode_digest),
                "evidence_paths": deepcopy(item["evidence_paths"]),
            }
            for item in oracle["must_satisfy"]
        ],
        "must_not": [
            {
                "constraint_ref": _opaque(item["id"], episode_digest=episode_digest),
                "criticality": item["criticality"],
            }
            for item in oracle["must_not"]
        ],
        "expected_effects": _sanitize(
            oracle["expected_effects"], episode_digest=episode_digest
        ),
    }


def _episode_projection(episode: Mapping[str, Any]) -> dict[str, Any]:
    episode_digest = str(episode["provenance"]["episode_sha256"])
    environment = episode["environment"]
    projected_environment = {
        "schema_version": environment["schema_version"],
        "frozen_time": environment["frozen_time"],
        "timezone": environment["timezone"],
        "tools": environment["tools"],
        "policies": environment["policies"],
        "resources": environment["resources"],
        "isolation": environment["isolation"],
    }
    projection = {
        "schema_version": episode["schema_version"],
        "track": episode["track"],
        "trigger": episode["trigger"],
        "state_before": episode["state_before"],
        "state_after": episode["state_after"],
        "state_delta": episode["state_delta"],
        "environment": projected_environment,
        "observable_trace": episode["observable_trace"],
        "result": {
            "action_class": episode["result"]["action_class"],
            "user_visible_output": episode["result"]["user_visible_output"],
            "guard": episode["result"]["guard"],
            "layers": episode["result"]["layers"],
        },
        "oracle": _oracle_projection(episode["oracle"], episode_digest=episode_digest),
        "completeness": episode["completeness"],
        "isolation_evidence": episode["isolation_evidence"],
    }
    return _sanitize(projection, episode_digest=episode_digest)


def _rule_projection(rule_result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": rule_result["schema_version"],
        "status": rule_result["status"],
        "hard_gates": deepcopy(rule_result["hard_gates"]),
        "checks": [
            {
                "check_id": item["check_id"],
                "rule_pack": item["rule_pack"],
                "status": item["status"],
                "severity": item["severity"],
                "evidence_paths": deepcopy(item["evidence_paths"]),
                "reason_code": item["reason_code"],
            }
            for item in rule_result["checks"]
        ],
        "dimension_signals": deepcopy(rule_result["dimension_signals"]),
    }


def build_blind_judge_input(
    episode: Mapping[str, Any], rule_result: Mapping[str, Any]
) -> BlindJudgeInput:
    """Build a deterministic, label-blind projection without mutating authority data."""

    episode_digest = str(episode["provenance"]["episode_sha256"])
    rule_digest = str(rule_result["result_sha256"])
    judge_token = sha256_digest({"episode": episode_digest, "rule": rule_digest})[
        :20
    ].translate(_OPAQUE_TRANSLATION)
    judge_id = f"judge-{judge_token}"
    document = {
        "schema_version": BLIND_INPUT_VERSION,
        "judge_id": judge_id,
        "track": episode["track"],
        "episode": _episode_projection(episode),
        "authoritative_rule_facts": _rule_projection(rule_result),
        "rubric": rubric_projection(episode["track"]),
    }
    issues = privacy_issues(document, file="blind-judge-input")
    if issues:
        raise BlindProjectionError("blind Judge input failed privacy validation")
    encoded = canonical_json_bytes(document)
    forbidden = (
        b'"capture"',
        b'"provenance"',
        b'"episode_id"',
        b'"scenario_family_id"',
        b'"tags"',
        b'"oracle_author"',
        b'"oracle_reviewer"',
        b'"adjudication_note"',
        b'"model_name"',
        b'"model_version"',
        b'"invocation_mode"',
    )
    if any(token in encoded for token in forbidden):
        raise BlindProjectionError("blind Judge input contains forbidden metadata")
    if contains_quality_label(document):
        raise BlindProjectionError("blind Judge input contains a quality label")
    return BlindJudgeInput(
        judge_id=judge_id,
        document=document,
        sha256=sha256_digest(document),
    )


def path_visible_to_judge(blind_input: BlindJudgeInput, evidence_path: str) -> bool:
    """Return whether an original Episode path was present in the blind projection."""

    from .validator import resolve_evidence_path

    return resolve_evidence_path(blind_input.document["episode"], evidence_path)[0]


def contains_quality_label(value: object) -> bool:
    """Detect calibration/system comparison labels in a structured value."""

    if isinstance(value, Mapping):
        return any(contains_quality_label(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_quality_label(item) for item in value)
    return isinstance(value, str) and _QUALITY_LABEL.search(value) is not None
