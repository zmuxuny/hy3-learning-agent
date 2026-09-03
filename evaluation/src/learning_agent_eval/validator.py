"""Recursive deterministic validation for versioned evaluation artifact trees."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .canonical import CanonicalizationError, canonical_json_bytes, sha256_digest
from .deltas import DeltaConstructionError, build_state_delta, operation_affected_refs
from .errors import DatasetStats, ValidationIssue, ValidationReport
from .exporter import (
    ACTION_MAPPING_SHA256,
    ACTION_MAPPING_VERSION,
    ExportError,
    _durable_status,
    _effects_and_action,
    _execution_status,
    _guard_decisions,
    _guard_facts,
    _observation_status,
    _ordered_trace_entities,
)
from .integrity import (
    aggregate_result_digest,
    artifact_manifest_digest,
    case_spec_digest,
    context_summary_digest,
    decision_episode_digest,
    environment_manifest_digest,
    episode_completeness_digest,
    integrity_result_digest,
    judge_reference_digest,
    judge_result_digest,
    model_visible_context_digest,
    oracle_envelope_digest,
    provider_attestation_digest,
    rule_result_digest,
    runtime_failure_digest,
    snapshot_entity_digest,
    state_delta_digest,
    state_snapshot_digest,
)
from .models import DATASET_DOCUMENT_MODELS
from .normalizers import NormalizationError, normalize_rfc3339
from .privacy import privacy_issues
from .rubric import (
    DIMENSION_WEIGHTS,
    JUDGE_CONFIG_SHA256,
    JUDGE_CONFIG_SHA256_V2,
    JUDGE_CONFIG_VERSION,
    JUDGE_CONFIG_VERSION_V2,
    JUDGE_PROMPT_SHA256,
    JUDGE_PROMPT_SHA256_V2,
    JUDGE_PROMPT_VERSION,
    JUDGE_PROMPT_VERSION_V2,
    JUDGE_VERSION,
    JUDGE_VERSION_V2,
    REPAIR_LIMIT,
    RUBRIC_SHA256,
    RUBRIC_VERSION,
    TRACK_ANCHOR_SHA256,
    TRACK_ANCHOR_VERSION,
)
from .snapshots import (
    COLLECTOR_VERSION,
    ENTITY_TYPE_ORDER,
    FIELD_ALLOWLIST_SHA256,
    FIELD_ALLOWLISTS,
)

MAX_JSON_BYTES = 2_000_000
TRACK_ACTIONS = {
    "planning": {"PROPOSE_PLAN", "REQUEST_USER_INPUT"},
    "intervention": {
        "WAIT",
        "INTERVENE_MESSAGE",
        "INTERVENE_QUIZ_OR_REVIEW",
        "PROPOSE_PLAN_ADJUSTMENT",
        "REQUEST_USER_INPUT",
    },
    "assessment": {
        "ACCEPT",
        "REVISION_REQUIRED",
        "INSUFFICIENT_EVIDENCE",
        "REQUEST_CLARIFICATION",
    },
    "revision": {
        "NO_OP",
        "PROPOSE_CHANGE",
        "APPLY_REVERSIBLE_PATCH",
        "REQUEST_APPROVAL",
    },
}
_UNIQUE_ID_FIELDS = {
    "trigger_id",
    "logical_id",
    "turn_id",
    "invocation_id",
    "event_id",
    "operation_id",
    "change_id",
}
_EVIDENCE_ROOTS_V1 = {
    "trigger",
    "state_before",
    "environment",
    "observable_trace",
    "result",
}
_EVIDENCE_ROOTS_V2 = {
    *_EVIDENCE_ROOTS_V1,
    "schema_version",
    "episode_id",
    "track",
    "oracle",
    "provenance",
    "state_after",
    "state_delta",
    "completeness",
    "isolation_evidence",
}
_EVIDENCE_ROOTS_V3 = {
    "schema_version",
    "episode_id",
    "case_spec_sha256",
    "judge_reference_sha256",
    "scenario_family_id",
    "track",
    "trigger",
    "state_before",
    "state_after",
    "state_delta",
    "environment",
    "observable_trace",
    "result",
    "completeness",
    "isolation_evidence",
    "provenance",
}
_PATH_SEGMENT = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P<indexes>(?:\[(?:0|[1-9]\d*)\])*)$"
)
_PATH_INDEX = re.compile(r"\[(\d+)\]")


class DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _EpisodeRecord:
    file: str
    value: dict[str, Any]


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    file: str,
) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message, file=file)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKey
        value[key] = item
    return value


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON constant")


def _load_json(path: Path, *, file: str) -> tuple[object | None, list[ValidationIssue]]:
    if path.is_symlink():
        return None, [
            _issue(
                "file.symlink",
                "$",
                "Dataset JSON files must not be symbolic links.",
                file=file,
            )
        ]
    try:
        size = path.stat().st_size
    except OSError:
        return None, [
            _issue("file.unreadable", "$", "Dataset file cannot be read.", file=file)
        ]
    if size > MAX_JSON_BYTES:
        return None, [
            _issue(
                "file.too_large",
                "$",
                "Dataset JSON file exceeds the 2 MB E0 limit.",
                file=file,
            )
        ]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, [
            _issue(
                "file.unreadable", "$", "Dataset file is not valid UTF-8.", file=file
            )
        ]
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except DuplicateJsonKey:
        return None, [
            _issue(
                "json.duplicate_key",
                "$",
                "JSON object contains a duplicate key.",
                file=file,
            )
        ]
    except json.JSONDecodeError as exc:
        return None, [
            _issue(
                "json.invalid",
                "$",
                f"Invalid JSON at line {exc.lineno} column {exc.colno}.",
                file=file,
            )
        ]
    except ValueError:
        return None, [
            _issue(
                "json.non_finite",
                "$",
                "Non-finite JSON numbers are prohibited.",
                file=file,
            )
        ]
    return value, []


def _location_path(location: Sequence[object]) -> str:
    path = "$"
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _schema_issues(
    value: object,
    *,
    version: str,
    file: str,
) -> list[ValidationIssue]:
    model = DATASET_DOCUMENT_MODELS[version]
    try:
        model.model_validate(value)
    except ValidationError as exc:
        issues: list[ValidationIssue] = []
        messages = {
            "missing": ("schema.missing", "Required field is missing."),
            "extra_forbidden": (
                "schema.unknown_field",
                "Unknown field is not allowed.",
            ),
            "literal_error": ("schema.enum", "Value is not in the allowed enum."),
            "string_pattern_mismatch": (
                "schema.pattern",
                "Value does not match the required format.",
            ),
            "string_too_short": ("schema.length", "String is shorter than allowed."),
            "string_too_long": ("schema.length", "String is longer than allowed."),
            "too_short": ("schema.length", "Array has fewer items than required."),
            "greater_than_equal": (
                "schema.range",
                "Number is below the allowed range.",
            ),
            "less_than_equal": ("schema.range", "Number is above the allowed range."),
        }
        for error in exc.errors(include_input=False, include_url=False):
            code, message = messages.get(
                str(error.get("type")),
                ("schema.type", "Value does not satisfy the required type."),
            )
            issues.append(
                _issue(
                    code,
                    _location_path(error.get("loc") or ()),
                    message,
                    file=file,
                )
            )
        return issues
    return []


def _walk_ids(value: object, path: str = "$") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in _UNIQUE_ID_FIELDS and isinstance(item, str):
                found.append((item, child_path))
            found.extend(_walk_ids(item, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            found.extend(_walk_ids(item, f"{path}[{index}]"))
    return found


def resolve_evidence_path(
    document: Mapping[str, Any],
    path: str,
    *,
    roots: set[str] | None = None,
) -> tuple[bool, Any]:
    """Resolve one public Evidence Path and return its actual value."""

    segments = path.split(".")
    version = document.get("schema_version")
    allowed_roots = roots or (
        _EVIDENCE_ROOTS_V3
        if version == "decision-episode-v3"
        else _EVIDENCE_ROOTS_V2
        if version == "decision-episode-v2"
        else _EVIDENCE_ROOTS_V1
    )
    if not segments or segments[0] not in allowed_roots:
        return False, None
    current: object = document
    for segment in segments:
        match = _PATH_SEGMENT.fullmatch(segment)
        if match is None or not isinstance(current, Mapping):
            return False, None
        name = match.group("name")
        if name not in current:
            return False, None
        current = current[name]
        for encoded_index in _PATH_INDEX.findall(match.group("indexes")):
            if not isinstance(current, list):
                return False, None
            index = int(encoded_index)
            if index >= len(current):
                return False, None
            current = current[index]
    return True, current


def _resolve_path(document: Mapping[str, Any], path: str) -> bool:
    return resolve_evidence_path(document, path)[0]


def _check_unique_strings(
    values: Sequence[object],
    *,
    path: str,
    code: str,
    file: str,
) -> list[ValidationIssue]:
    seen: set[object] = set()
    issues: list[ValidationIssue] = []
    for index, value in enumerate(values):
        if value in seen:
            issues.append(
                _issue(code, f"{path}[{index}]", "Value must be unique.", file=file)
            )
        else:
            seen.add(value)
    return issues


def _check_ordinals(
    values: Sequence[Mapping[str, Any]],
    *,
    path: str,
    file: str,
) -> list[ValidationIssue]:
    expected = list(range(1, len(values) + 1))
    actual = [item.get("ordinal") for item in values]
    if actual == expected:
        return []
    return [
        _issue(
            "trace.ordinal_sequence",
            path,
            "Trace ordinals must be unique and contiguous from one.",
            file=file,
        )
    ]


def _episode_semantic_issues(
    episode: dict[str, Any],
    *,
    file: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    issues.extend(
        _check_unique_strings(
            episode["tags"],
            path="$.tags",
            code="episode.duplicate_tag",
            file=file,
        )
    )
    unique_ids: dict[str, str] = {}
    for logical_id, path in _walk_ids(episode):
        if logical_id in unique_ids:
            issues.append(
                _issue(
                    "episode.duplicate_logical_id",
                    path,
                    "Logical IDs that declare identity must be unique within an Episode.",
                    file=file,
                )
            )
        else:
            unique_ids[logical_id] = path

    entity_ids = {
        item["logical_id"] for item in episode["state_before"]["logical_entities"]
    }
    event_ids = {item["event_id"] for item in episode["observable_trace"]["run_events"]}
    invocation_ids = {
        item["invocation_id"]
        for item in episode["observable_trace"]["tool_invocations"]
    }
    operation_ids = {
        item["operation_id"] for item in episode["observable_trace"]["operations"]
    }

    for index, reference in enumerate(episode["trigger"]["target_refs"]):
        if reference not in entity_ids:
            issues.append(
                _issue(
                    "reference.missing_entity",
                    f"$.trigger.target_refs[{index}]",
                    "Reference does not resolve to a state logical entity.",
                    file=file,
                )
            )
    for index, reference in enumerate(episode["trigger"]["source_event_refs"]):
        if reference not in event_ids:
            issues.append(
                _issue(
                    "reference.missing_event",
                    f"$.trigger.source_event_refs[{index}]",
                    "Reference does not resolve to a trace event.",
                    file=file,
                )
            )
    for index, reference in enumerate(
        episode["state_before"]["context"]["source_refs"]
    ):
        if reference not in entity_ids:
            issues.append(
                _issue(
                    "reference.missing_entity",
                    f"$.state_before.context.source_refs[{index}]",
                    "Context source does not resolve to a state logical entity.",
                    file=file,
                )
            )

    trace = episode["observable_trace"]
    for collection in ("model_turns", "tool_invocations", "run_events", "operations"):
        issues.extend(
            _check_ordinals(
                trace[collection],
                path=f"$.observable_trace.{collection}",
                file=file,
            )
        )
    for turn_index, turn in enumerate(trace["model_turns"]):
        for ref_index, reference in enumerate(turn["tool_call_refs"]):
            if reference not in invocation_ids:
                issues.append(
                    _issue(
                        "reference.missing_invocation",
                        (
                            f"$.observable_trace.model_turns[{turn_index}]"
                            f".tool_call_refs[{ref_index}]"
                        ),
                        "Tool call reference does not resolve to an invocation.",
                        file=file,
                    )
                )
    for index, operation in enumerate(trace["operations"]):
        if operation["entity_ref"] not in entity_ids:
            issues.append(
                _issue(
                    "reference.missing_entity",
                    f"$.observable_trace.operations[{index}].entity_ref",
                    "Operation entity reference does not resolve.",
                    file=file,
                )
            )

    for index, change in enumerate(episode["result"]["state_changes"]):
        reference = change["operation_ref"]
        if reference is not None and reference not in operation_ids:
            issues.append(
                _issue(
                    "reference.missing_operation",
                    f"$.result.state_changes[{index}].operation_ref",
                    "State change operation reference does not resolve.",
                    file=file,
                )
            )

    oracle = episode["oracle"]
    rule_ids: set[str] = set()
    for section in ("must_satisfy", "must_not"):
        for index, item in enumerate(oracle[section]):
            rule_id = item["id"]
            if rule_id in rule_ids:
                issues.append(
                    _issue(
                        "oracle.duplicate_rule_id",
                        f"$.oracle.{section}[{index}].id",
                        "Oracle rule IDs must be unique within an envelope.",
                        file=file,
                    )
                )
            rule_ids.add(rule_id)
    issues.extend(
        _check_unique_strings(
            oracle["allowed_action_classes"],
            path="$.oracle.allowed_action_classes",
            code="oracle.duplicate_action_class",
            file=file,
        )
    )
    for requirement_index, requirement in enumerate(oracle["must_satisfy"]):
        issues.extend(
            _check_unique_strings(
                requirement["evidence_paths"],
                path=f"$.oracle.must_satisfy[{requirement_index}].evidence_paths",
                code="oracle.duplicate_evidence_path",
                file=file,
            )
        )
        for path_index, evidence_path in enumerate(requirement["evidence_paths"]):
            if not _resolve_path(episode, evidence_path):
                issues.append(
                    _issue(
                        "oracle.invalid_evidence_path",
                        (
                            f"$.oracle.must_satisfy[{requirement_index}]"
                            f".evidence_paths[{path_index}]"
                        ),
                        "Evidence Path does not resolve to an allowed Episode field.",
                        file=file,
                    )
                )
    for effect_index, effect in enumerate(oracle["expected_effects"]):
        if not _resolve_path(episode, effect["path"]):
            issues.append(
                _issue(
                    "oracle.invalid_effect_path",
                    f"$.oracle.expected_effects[{effect_index}].path",
                    "Expected effect path does not resolve to an allowed Episode field.",
                    file=file,
                )
            )

    track = episode["track"]
    allowed_for_track = TRACK_ACTIONS[track]
    for index, action in enumerate(oracle["allowed_action_classes"]):
        if action not in allowed_for_track:
            issues.append(
                _issue(
                    "oracle.action_track_mismatch",
                    f"$.oracle.allowed_action_classes[{index}]",
                    "Allowed action class is not valid for this track.",
                    file=file,
                )
            )
    action = episode["result"]["action_class"]
    if action not in oracle["allowed_action_classes"]:
        issues.append(
            _issue(
                "result.outside_action_envelope",
                "$.result.action_class",
                "Result action is outside the acceptable action envelope.",
                file=file,
            )
        )

    provenance = episode["provenance"]
    if provenance["source_type"] == "manual_protocol_fixture":
        manual_expectations = {
            "construction_method": "hand_authored",
            "dataset_role": "protocol_mini_fixture",
            "runtime_executed": False,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
        }
        for field, expected in manual_expectations.items():
            if provenance[field] != expected:
                issues.append(
                    _issue(
                        "provenance.manual_fixture_boundary",
                        f"$.provenance.{field}",
                        "Manual protocol fixture provenance is inconsistent.",
                        file=file,
                    )
                )
        if trace["capture_mode"] != "manual_protocol_fixture":
            issues.append(
                _issue(
                    "provenance.manual_fixture_boundary",
                    "$.observable_trace.capture_mode",
                    "Manual protocol fixtures must use manual trace capture mode.",
                    file=file,
                )
            )
        if any(
            trace[collection] for collection in trace if collection != "capture_mode"
        ):
            issues.append(
                _issue(
                    "provenance.manual_fixture_trace",
                    "$.observable_trace",
                    "Manual protocol fixtures cannot claim recorded Runtime activity.",
                    file=file,
                )
            )
        environment = episode["environment"]
        if (
            environment["model"]["invocation_mode"] != "not_invoked"
            or environment["runtime"]["database_mode"] != "none"
            or environment["isolation"]["network_access"] != "disabled"
            or environment["isolation"]["notification_mode"] != "disabled"
        ):
            issues.append(
                _issue(
                    "provenance.manual_fixture_environment",
                    "$.environment",
                    "Manual fixtures must declare no model, database, network, or channel execution.",
                    file=file,
                )
            )

    runtime = episode["environment"]["runtime"]
    if (runtime["database_mode"] == "none") != (runtime["fixture_db_sha256"] is None):
        issues.append(
            _issue(
                "environment.database_digest",
                "$.environment.runtime.fixture_db_sha256",
                "Database digest presence must match the declared database mode.",
                file=file,
            )
        )

    context = episode["state_before"]["context"]
    if context["context_sha256"] != context_summary_digest(context):
        issues.append(
            _issue(
                "digest.context_mismatch",
                "$.state_before.context.context_sha256",
                "Context summary digest does not match canonical content.",
                file=file,
            )
        )
    for index, invocation in enumerate(trace["tool_invocations"]):
        if invocation["result_digest"] != sha256_digest(invocation["result"]):
            issues.append(
                _issue(
                    "digest.tool_result_mismatch",
                    f"$.observable_trace.tool_invocations[{index}].result_digest",
                    "Tool result digest does not match canonical content.",
                    file=file,
                )
            )
    for index, event in enumerate(trace["run_events"]):
        if event["payload_digest"] != sha256_digest(event["payload"]):
            issues.append(
                _issue(
                    "digest.event_payload_mismatch",
                    f"$.observable_trace.run_events[{index}].payload_digest",
                    "Run event payload digest does not match canonical content.",
                    file=file,
                )
            )
    for index, operation in enumerate(trace["operations"]):
        patch_payload = {
            "forward_patch": operation["forward_patch"],
            "inverse_patch": operation["inverse_patch"],
        }
        if operation["patch_digest"] != sha256_digest(patch_payload):
            issues.append(
                _issue(
                    "digest.operation_patch_mismatch",
                    f"$.observable_trace.operations[{index}].patch_digest",
                    "Operation patch digest does not match canonical content.",
                    file=file,
                )
            )
    environment = episode["environment"]
    if environment["manifest_sha256"] != environment_manifest_digest(environment):
        issues.append(
            _issue(
                "digest.environment_mismatch",
                "$.environment.manifest_sha256",
                "Environment Manifest digest does not match canonical content.",
                file=file,
            )
        )
    if oracle["envelope_sha256"] != oracle_envelope_digest(oracle):
        issues.append(
            _issue(
                "digest.oracle_mismatch",
                "$.oracle.envelope_sha256",
                "Oracle envelope digest does not match canonical content.",
                file=file,
            )
        )
    if provenance["episode_sha256"] != decision_episode_digest(episode):
        issues.append(
            _issue(
                "digest.episode_mismatch",
                "$.provenance.episode_sha256",
                "Decision Episode digest does not match canonical content.",
                file=file,
            )
        )
    return issues


def _v2_unique_ids(
    values: Sequence[Mapping[str, Any]],
    *,
    field: str,
    path: str,
    file: str,
) -> list[ValidationIssue]:
    return _check_unique_strings(
        [item[field] for item in values],
        path=path,
        code="episode.duplicate_logical_id",
        file=file,
    )


def _v2_source_ref_issues(
    source_refs: Sequence[Mapping[str, Any]],
    *,
    path: str,
    declared: Mapping[str, set[str]],
    file: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, source in enumerate(source_refs):
        if source["ref"] not in declared.get(source["source_type"], set()):
            issues.append(
                _issue(
                    "reference.missing_typed_source",
                    f"{path}[{index}]",
                    "Typed source reference does not resolve in the Episode.",
                    file=file,
                )
            )
    return issues


_PUBLIC_NESTED_ID_FIELDS = {
    "call_id",
    "logical_id",
    "provider_id",
    "stable_id",
    "tool_call_id",
    "trigger_id",
}


def _raw_identity_field_issues(
    value: Any,
    *,
    path: str,
    file: str,
) -> list[ValidationIssue]:
    """Reject unnormalized identity-shaped fields inside allowlisted JSON."""

    issues: list[ValidationIssue] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if (
                key == "id"
                or key.endswith("_ids")
                or (key.endswith("_id") and key not in _PUBLIC_NESTED_ID_FIELDS)
            ):
                issues.append(
                    _issue(
                        "snapshot.raw_identity_field",
                        child_path,
                        "Snapshot JSON must use stable logical refs instead of raw identity fields.",
                        file=file,
                    )
                )
            issues.extend(_raw_identity_field_issues(child, path=child_path, file=file))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(
                _raw_identity_field_issues(child, path=f"{path}[{index}]", file=file)
            )
    return issues


def _episode_v2_semantic_issues(
    episode: dict[str, Any],
    *,
    file: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    canonical_times = [
        episode["state_before"]["captured_at"],
        episode["state_after"]["captured_at"],
        episode["environment"]["frozen_time"],
        episode["trigger"]["triggered_at"],
        episode["provenance"]["created_at"],
    ]
    try:
        canonical_time_values = [normalize_rfc3339(value) for value in canonical_times]
    except NormalizationError:
        canonical_time_values = []
    if canonical_time_values != canonical_times or len(set(canonical_time_values)) != 1:
        issues.append(
            _issue(
                "environment.frozen_time_mismatch",
                "$.environment.frozen_time",
                "Runtime capture times must use one canonical UTC instant.",
                file=file,
            )
        )
    issues.extend(
        _check_unique_strings(
            episode["tags"],
            path="$.tags",
            code="episode.duplicate_tag",
            file=file,
        )
    )
    snapshots = (
        ("state_before", episode["state_before"]),
        ("state_after", episode["state_after"]),
    )
    snapshot_maps: dict[str, dict[str, Mapping[str, Any]]] = {}
    for root, snapshot in snapshots:
        entities = snapshot["logical_entities"]
        issues.extend(
            _v2_unique_ids(
                entities,
                field="logical_id",
                path=f"$.{root}.logical_entities",
                file=file,
            )
        )
        issues.extend(
            _check_ordinals(entities, path=f"$.{root}.logical_entities", file=file)
        )
        snapshot_maps[root] = {item["logical_id"]: item for item in entities}
        if (
            snapshot["collector_version"] != COLLECTOR_VERSION
            or snapshot["field_allowlist_sha256"] != FIELD_ALLOWLIST_SHA256
            or snapshot["entity_types"] != list(ENTITY_TYPE_ORDER)
        ):
            issues.append(
                _issue(
                    "snapshot.collector_contract_mismatch",
                    f"$.{root}.collector_version",
                    "Snapshot collector scope or allowlist is unsupported.",
                    file=file,
                )
            )
        if snapshot["capture_status"] != "complete" or snapshot["error_codes"]:
            issues.append(
                _issue(
                    "snapshot.incomplete",
                    f"$.{root}.capture_status",
                    "Published Runtime Episodes require a complete state Snapshot.",
                    file=file,
                )
            )
        if snapshot["snapshot_sha256"] != state_snapshot_digest(snapshot):
            issues.append(
                _issue(
                    "digest.snapshot_mismatch",
                    f"$.{root}.snapshot_sha256",
                    "State Snapshot digest does not match canonical content.",
                    file=file,
                )
            )
        if snapshot["context"]["context_sha256"] != context_summary_digest(
            snapshot["context"]
        ):
            issues.append(
                _issue(
                    "digest.context_mismatch",
                    f"$.{root}.context.context_sha256",
                    "Context summary digest does not match canonical content.",
                    file=file,
                )
            )
        entity_ids = set(snapshot_maps[root])
        for index, entity in enumerate(entities):
            expected_fields = set(FIELD_ALLOWLISTS[entity["entity_type"]])
            if set(entity["data"]) != expected_fields:
                issues.append(
                    _issue(
                        "snapshot.entity_field_mismatch",
                        f"$.{root}.logical_entities[{index}].data",
                        "Snapshot entity fields do not match the closed collector allowlist.",
                        file=file,
                    )
                )
            issues.extend(
                _raw_identity_field_issues(
                    entity["data"],
                    path=f"$.{root}.logical_entities[{index}].data",
                    file=file,
                )
            )
            if entity["entity_sha256"] != snapshot_entity_digest(entity):
                issues.append(
                    _issue(
                        "digest.snapshot_entity_mismatch",
                        f"$.{root}.logical_entities[{index}].entity_sha256",
                        "Snapshot entity digest does not match canonical content.",
                        file=file,
                    )
                )
            if (
                entity["scope_ref"] is not None
                and entity["scope_ref"] not in entity_ids
            ):
                issues.append(
                    _issue(
                        "reference.missing_scope",
                        f"$.{root}.logical_entities[{index}].scope_ref",
                        "Snapshot scope reference does not resolve in that Snapshot.",
                        file=file,
                    )
                )
        context_entity_ids = entity_ids | (
            set(snapshot_maps.get("state_before", {}))
            if root == "state_after"
            else set()
        )
        for index, reference in enumerate(snapshot["context"]["source_refs"]):
            if reference not in context_entity_ids:
                issues.append(
                    _issue(
                        "reference.missing_entity",
                        f"$.{root}.context.source_refs[{index}]",
                        "Context source does not resolve in that Snapshot.",
                        file=file,
                    )
                )
    before_ids = set(snapshot_maps["state_before"])
    after_ids = set(snapshot_maps["state_after"])
    union_entity_ids = before_ids | after_ids
    trace = episode["observable_trace"]
    collections = {
        "model_turn": (trace["model_turns"], "turn_id"),
        "tool_invocation": (trace["tool_invocations"], "invocation_id"),
        "run_event": (trace["run_events"], "event_id"),
        "operation": (trace["operations"], "operation_id"),
        "guard_decision": (trace["guard_decisions"], "guard_id"),
    }
    for source_type, (values, field) in collections.items():
        issues.extend(
            _v2_unique_ids(
                values,
                field=field,
                path=f"$.observable_trace.{source_type}s",
                file=file,
            )
        )
        issues.extend(
            _check_ordinals(
                values, path=f"$.observable_trace.{source_type}s", file=file
            )
        )
    invocation_ids = {item["invocation_id"] for item in trace["tool_invocations"]}
    event_ids = {item["event_id"] for item in trace["run_events"]}
    operation_ids = {item["operation_id"] for item in trace["operations"]}
    guard_ids = {item["guard_id"] for item in trace["guard_decisions"]}
    turn_ids = {item["turn_id"] for item in trace["model_turns"]}
    snapshot_ids_by_type = {
        entity_type: {
            item["logical_id"]
            for item in episode["state_after"]["logical_entities"]
            if item["entity_type"] == entity_type
        }
        for entity_type in ("tool_invocation", "run_event", "operation")
    }
    trace_ids_by_type = {
        "tool_invocation": invocation_ids,
        "run_event": event_ids,
        "operation": operation_ids,
    }
    if snapshot_ids_by_type != trace_ids_by_type:
        issues.append(
            _issue(
                "trace.snapshot_inventory_mismatch",
                "$.observable_trace",
                "Trace inventory does not match the durable post-state Snapshot.",
                file=file,
            )
        )
    ordered_invocations, ordered_operations, ordered_events = _ordered_trace_entities(
        episode["state_after"]
    )
    if (
        [item["invocation_id"] for item in trace["tool_invocations"]]
        != [item["logical_id"] for item in ordered_invocations]
        or [item["operation_id"] for item in trace["operations"]]
        != [item["logical_id"] for item in ordered_operations]
        or [item["event_id"] for item in trace["run_events"]]
        != [item["logical_id"] for item in ordered_events]
    ):
        issues.append(
            _issue(
                "trace.runtime_order_mismatch",
                "$.observable_trace",
                "Trace collections do not preserve canonical Runtime order.",
                file=file,
            )
        )
    after_by_id = snapshot_maps["state_after"]
    expected_operation_refs_by_invocation: dict[str, list[str]] = defaultdict(list)
    for operation in ordered_operations:
        invocation_ref = operation["data"]["invocation_ref"]
        if invocation_ref is not None:
            expected_operation_refs_by_invocation[invocation_ref].append(
                operation["logical_id"]
            )
    for index, invocation in enumerate(trace["tool_invocations"]):
        entity = after_by_id.get(invocation["invocation_id"])
        if entity is None:
            continue
        data = entity["data"]
        try:
            expected_observation_status = _observation_status(
                entity, episode["state_after"]
            )
            expected_execution_status = _execution_status(data["status"])
        except ExportError:
            expected_observation_status = None
            expected_execution_status = None
        if (
            invocation["tool_name"] != data["tool_name"].replace("_", ".")
            or invocation["canonical_args"] != (data["canonical_args"] or {})
            or invocation["execution_status"] != expected_execution_status
            or invocation["observation_status"] != expected_observation_status
            or invocation["durable_status"] != data["status"]
            or invocation["result"] != (data["result_payload"] or {})
            or invocation["operation_refs"]
            != expected_operation_refs_by_invocation.get(
                invocation["invocation_id"], []
            )
        ):
            issues.append(
                _issue(
                    "trace.invocation_snapshot_mismatch",
                    f"$.observable_trace.tool_invocations[{index}]",
                    "ToolInvocation trace differs from its durable Snapshot entity.",
                    file=file,
                )
            )
    for index, event in enumerate(trace["run_events"]):
        entity = after_by_id.get(event["event_id"])
        if entity is None:
            continue
        if (
            event["event_type"] != entity["data"]["event_type"]
            or event["payload"] != entity["data"]["payload"]
        ):
            issues.append(
                _issue(
                    "trace.event_snapshot_mismatch",
                    f"$.observable_trace.run_events[{index}]",
                    "RunEvent trace differs from its durable Snapshot entity.",
                    file=file,
                )
            )
    try:
        expected_affected_refs = operation_affected_refs(episode["state_after"])
    except DeltaConstructionError:
        expected_affected_refs = {}
    for index, operation in enumerate(trace["operations"]):
        entity = after_by_id.get(operation["operation_id"])
        if entity is None:
            continue
        data = entity["data"]
        if (
            operation["status"] != data["status"]
            or operation["invocation_ref"] != data["invocation_ref"]
            or operation["tool_name"] != data["tool_name"]
            or operation["primary_entity_ref"] != data["primary_entity_ref"]
            or operation["affected_entity_refs"]
            != expected_affected_refs.get(operation["operation_id"])
            or operation["forward_patch"] != data["forward_patch"]
            or operation["inverse_patch"] != data["inverse_patch"]
        ):
            issues.append(
                _issue(
                    "trace.operation_snapshot_mismatch",
                    f"$.observable_trace.operations[{index}]",
                    "Operation trace differs from its durable Snapshot entity.",
                    file=file,
                )
            )
    attempts = episode["result"]["layers"]["model_attempts"]
    effects = episode["result"]["layers"]["final_effects"]
    attempt_ids = {item["attempt_id"] for item in attempts}
    effect_ids = {item["effect_id"] for item in effects}
    issues.extend(
        _v2_unique_ids(
            attempts,
            field="attempt_id",
            path="$.result.layers.model_attempts",
            file=file,
        )
    )
    issues.extend(
        _check_ordinals(attempts, path="$.result.layers.model_attempts", file=file)
    )
    issues.extend(
        _v2_unique_ids(
            effects,
            field="effect_id",
            path="$.result.layers.final_effects",
            file=file,
        )
    )
    issues.extend(
        _check_ordinals(effects, path="$.result.layers.final_effects", file=file)
    )
    declared = {
        "runtime": {
            item["logical_id"]
            for item in episode["state_after"]["logical_entities"]
            if item["entity_type"] == "agent_run"
        },
        "model_turn": turn_ids,
        "tool_invocation": invocation_ids,
        "guard_decision": guard_ids,
        "operation": operation_ids,
        "run_event": event_ids,
        "entity": union_entity_ids,
    }
    for index, turn in enumerate(trace["model_turns"]):
        for ref_index, reference in enumerate(turn["tool_call_refs"]):
            if reference not in invocation_ids:
                issues.append(
                    _issue(
                        "reference.missing_invocation",
                        f"$.observable_trace.model_turns[{index}].tool_call_refs[{ref_index}]",
                        "Model turn tool-call reference does not resolve.",
                        file=file,
                    )
                )
    for index, invocation in enumerate(trace["tool_invocations"]):
        if invocation["result_digest"] != sha256_digest(invocation["result"]):
            issues.append(
                _issue(
                    "digest.tool_result_mismatch",
                    f"$.observable_trace.tool_invocations[{index}].result_digest",
                    "Tool result digest does not match canonical content.",
                    file=file,
                )
            )
        for ref_index, reference in enumerate(invocation["operation_refs"]):
            if reference not in operation_ids:
                issues.append(
                    _issue(
                        "reference.missing_operation",
                        f"$.observable_trace.tool_invocations[{index}].operation_refs[{ref_index}]",
                        "Invocation Operation reference does not resolve.",
                        file=file,
                    )
                )
    for index, event in enumerate(trace["run_events"]):
        if event["payload_digest"] != sha256_digest(event["payload"]):
            issues.append(
                _issue(
                    "digest.event_payload_mismatch",
                    f"$.observable_trace.run_events[{index}].payload_digest",
                    "Run event payload digest does not match canonical content.",
                    file=file,
                )
            )
    for index, operation in enumerate(trace["operations"]):
        patch = {
            "forward_patch": operation["forward_patch"],
            "inverse_patch": operation["inverse_patch"],
        }
        if operation["patch_digest"] != sha256_digest(patch):
            issues.append(
                _issue(
                    "digest.operation_patch_mismatch",
                    f"$.observable_trace.operations[{index}].patch_digest",
                    "Operation patch digest does not match canonical content.",
                    file=file,
                )
            )
        if (
            operation["invocation_ref"] is not None
            and operation["invocation_ref"] not in invocation_ids
        ):
            issues.append(
                _issue(
                    "reference.missing_invocation",
                    f"$.observable_trace.operations[{index}].invocation_ref",
                    "Operation invocation reference does not resolve.",
                    file=file,
                )
            )
        for ref_index, reference in enumerate(operation["affected_entity_refs"]):
            if reference not in union_entity_ids:
                issues.append(
                    _issue(
                        "reference.missing_entity",
                        f"$.observable_trace.operations[{index}].affected_entity_refs[{ref_index}]",
                        "Affected entity reference does not resolve.",
                        file=file,
                    )
                )
        if operation["primary_entity_ref"] not in operation["affected_entity_refs"]:
            issues.append(
                _issue(
                    "operation.primary_not_affected",
                    f"$.observable_trace.operations[{index}].primary_entity_ref",
                    "Operation primary entity must be in affected entities.",
                    file=file,
                )
            )
    for index, attempt in enumerate(attempts):
        if attempt["turn_ref"] not in turn_ids:
            issues.append(
                _issue(
                    "reference.missing_turn",
                    f"$.result.layers.model_attempts[{index}].turn_ref",
                    "Model attempt turn reference does not resolve.",
                    file=file,
                )
            )
        for ref_index, reference in enumerate(attempt["invocation_refs"]):
            if reference not in invocation_ids:
                issues.append(
                    _issue(
                        "reference.missing_invocation",
                        f"$.result.layers.model_attempts[{index}].invocation_refs[{ref_index}]",
                        "Model attempt invocation reference does not resolve.",
                        file=file,
                    )
                )
    for index, guard in enumerate(trace["guard_decisions"]):
        if (
            guard["invocation_ref"] is not None
            and guard["invocation_ref"] not in invocation_ids
        ):
            issues.append(
                _issue(
                    "reference.missing_invocation",
                    f"$.observable_trace.guard_decisions[{index}].invocation_ref",
                    "Guard invocation reference does not resolve.",
                    file=file,
                )
            )
        if guard["decision_ref"] is not None and (
            guard["decision_ref"] not in after_by_id
            or after_by_id[guard["decision_ref"]]["entity_type"]
            not in {"plan_proposal", "proactive_decision", "run_approval"}
        ):
            issues.append(
                _issue(
                    "reference.missing_guard_decision_entity",
                    f"$.observable_trace.guard_decisions[{index}].decision_ref",
                    "Guard decision entity reference does not resolve.",
                    file=file,
                )
            )
        if not set(guard["attempted_effect_refs"]).issubset(attempt_ids):
            issues.append(
                _issue(
                    "reference.missing_attempt",
                    f"$.observable_trace.guard_decisions[{index}].attempted_effect_refs",
                    "Guard attempted-effect references do not resolve.",
                    file=file,
                )
            )
        if not set(guard["final_effect_refs"]).issubset(effect_ids):
            issues.append(
                _issue(
                    "reference.missing_final_effect",
                    f"$.observable_trace.guard_decisions[{index}].final_effect_refs",
                    "Guard final-effect references do not resolve.",
                    file=file,
                )
            )
    for index, effect in enumerate(effects):
        for ref_index, reference in enumerate(effect["entity_refs"]):
            if reference not in union_entity_ids:
                issues.append(
                    _issue(
                        "reference.missing_entity",
                        f"$.result.layers.final_effects[{index}].entity_refs[{ref_index}]",
                        "Final-effect entity reference does not resolve.",
                        file=file,
                    )
                )
        issues.extend(
            _v2_source_ref_issues(
                effect["source_refs"],
                path=f"$.result.layers.final_effects[{index}].source_refs",
                declared=declared,
                file=file,
            )
        )
    if not set(episode["result"]["layers"]["guard_decision_refs"]).issubset(guard_ids):
        issues.append(
            _issue(
                "reference.missing_guard",
                "$.result.layers.guard_decision_refs",
                "Decision-layer Guard references do not resolve.",
                file=file,
            )
        )
    guard_statuses = {
        item["status"]
        for item in trace["guard_decisions"]
        if item["status"] != "not_evaluated"
    }
    expected_guard_summary = (
        "not_evaluated"
        if not guard_statuses
        else next(iter(guard_statuses))
        if len(guard_statuses) == 1
        else "mixed"
    )
    if episode["result"]["guard"]["status"] != expected_guard_summary:
        issues.append(
            _issue(
                "guard.summary_mismatch",
                "$.result.guard.status",
                "Guard summary does not match the structured Guard decisions.",
                file=file,
            )
        )
    effects_by_id = {item["effect_id"]: item for item in effects}
    for index, guard in enumerate(trace["guard_decisions"]):
        if guard["status"] not in {"blocked", "deferred"}:
            continue
        prevented_effects = [
            effects_by_id[reference]
            for reference in guard["final_effect_refs"]
            if reference in effects_by_id
        ]
        allowed_effect_statuses = (
            {"blocked"} if guard["status"] == "blocked" else {"deferred", "pending"}
        )
        allowed_entity_type = {
            "approval_required": "run_approval",
            "plan_adoption_required": "plan_proposal",
        }.get(guard["reason_code"])
        allowed_entity_refs = {
            item["logical_id"]
            for item in episode["state_after"]["logical_entities"]
            if item["entity_type"] == allowed_entity_type
        }
        if not prevented_effects or any(
            effect["status"] not in allowed_effect_statuses
            or not set(effect["entity_refs"]).issubset(allowed_entity_refs)
            for effect in prevented_effects
        ):
            issues.append(
                _issue(
                    "guard.prevented_effect_mismatch",
                    f"$.observable_trace.guard_decisions[{index}].final_effect_refs",
                    "Blocked or deferred Guard decisions must resolve to zero-side-effect outcomes.",
                    file=file,
                )
            )
    run_entities = [
        item
        for item in episode["state_after"]["logical_entities"]
        if item["entity_type"] == "agent_run"
        and item["data"].get("parent_run_ref") is None
    ]
    if (
        len(run_entities) != 1
        or run_entities[0]["data"].get("status")
        != episode["result"]["layers"]["run_status"]
    ):
        issues.append(
            _issue(
                "trace.terminal_state_mismatch",
                "$.result.layers.run_status",
                "Decision-layer Run status does not match the durable AgentRun.",
                file=file,
            )
        )
    if len(run_entities) == 1:
        expected_durable = _durable_status(
            after=episode["state_after"],
            trace=trace,
            run_status=run_entities[0]["data"]["status"],
            guard_status=episode["result"]["guard"]["status"],
            effects=effects,
        )
        if episode["result"]["layers"]["durable_status"] != expected_durable:
            issues.append(
                _issue(
                    "result.durable_status_mismatch",
                    "$.result.layers.durable_status",
                    "Final durable status is not the canonical post-state classification.",
                    file=file,
                )
            )
    try:
        expected_effects, expected_action, expected_guard, expected_reason = (
            _effects_and_action(
                episode_id=episode["episode_id"],
                before=episode["state_before"],
                after=episode["state_after"],
                delta=episode["state_delta"],
                trace=trace,
            )
        )
        expected_guards = _guard_decisions(
            episode_id=episode["episode_id"],
            facts=_guard_facts(episode["state_after"], trace),
            attempts=attempts,
            effects=expected_effects,
        )
    except ExportError:
        expected_effects = None
        expected_guards = None
        expected_action = None
        expected_guard = None
        expected_reason = None
    if (
        expected_effects is None
        or expected_effects != effects
        or expected_guards != trace["guard_decisions"]
        or expected_action != episode["result"]["action_class"]
        or expected_guard != episode["result"]["guard"]["status"]
        or expected_reason != episode["result"]["guard"]["reason_code"]
        or (
            expected_effects is not None
            and episode["result"]["guard"]["blocked_effect_refs"]
            != [
                effect["effect_id"]
                for effect in expected_effects
                if effect["status"] == "blocked"
            ]
        )
    ):
        issues.append(
            _issue(
                "result.classification_mismatch",
                "$.result",
                "Decision effects, Guard facts, or action class are not the canonical Runtime projection.",
                file=file,
            )
        )
    delta = episode["state_delta"]
    if (
        delta["before_snapshot_sha256"] != episode["state_before"]["snapshot_sha256"]
        or delta["after_snapshot_sha256"] != episode["state_after"]["snapshot_sha256"]
    ):
        issues.append(
            _issue(
                "delta.snapshot_digest_mismatch",
                "$.state_delta",
                "State Delta does not reference the supplied Snapshots.",
                file=file,
            )
        )
    if delta["delta_sha256"] != state_delta_digest(delta):
        issues.append(
            _issue(
                "digest.delta_mismatch",
                "$.state_delta.delta_sha256",
                "State Delta digest does not match canonical content.",
                file=file,
            )
        )
    issues.extend(
        _check_ordinals(delta["changes"], path="$.state_delta.changes", file=file)
    )
    issues.extend(
        _v2_unique_ids(
            delta["changes"],
            field="change_id",
            path="$.state_delta.changes",
            file=file,
        )
    )
    for index, change in enumerate(delta["changes"]):
        if change["entity_ref"] not in union_entity_ids:
            issues.append(
                _issue(
                    "reference.missing_entity",
                    f"$.state_delta.changes[{index}].entity_ref",
                    "Delta entity reference does not resolve.",
                    file=file,
                )
            )
        for ref_index, reference in enumerate(change["operation_refs"]):
            if reference not in operation_ids:
                issues.append(
                    _issue(
                        "reference.missing_operation",
                        f"$.state_delta.changes[{index}].operation_refs[{ref_index}]",
                        "Delta Operation reference does not resolve.",
                        file=file,
                    )
                )
        issues.extend(
            _v2_source_ref_issues(
                change["source_refs"],
                path=f"$.state_delta.changes[{index}].source_refs",
                declared=declared,
                file=file,
            )
        )
        for side in ("before_path", "after_path"):
            evidence_path = change[side]
            if (
                evidence_path is not None
                and not resolve_evidence_path(
                    episode, evidence_path, roots=_EVIDENCE_ROOTS_V2
                )[0]
            ):
                issues.append(
                    _issue(
                        "delta.invalid_evidence_path",
                        f"$.state_delta.changes[{index}].{side}",
                        "Delta path does not resolve to the declared Snapshot value.",
                        file=file,
                    )
                )
    try:
        recomputed_delta = build_state_delta(
            episode["state_before"], episode["state_after"]
        )
    except DeltaConstructionError:
        recomputed_delta = None
    if recomputed_delta is None or canonical_json_bytes(
        recomputed_delta
    ) != canonical_json_bytes(delta):
        issues.append(
            _issue(
                "delta.recompute_mismatch",
                "$.state_delta",
                "State Delta is not the canonical comparison of before and after.",
                file=file,
            )
        )
    for index, reference in enumerate(episode["trigger"]["target_refs"]):
        if reference not in before_ids:
            issues.append(
                _issue(
                    "reference.missing_entity",
                    f"$.trigger.target_refs[{index}]",
                    "Trigger target does not resolve in state_before.",
                    file=file,
                )
            )
    for index, reference in enumerate(episode["trigger"]["source_event_refs"]):
        if reference not in event_ids:
            issues.append(
                _issue(
                    "reference.missing_event",
                    f"$.trigger.source_event_refs[{index}]",
                    "Trigger event reference does not resolve.",
                    file=file,
                )
            )
    oracle = episode["oracle"]
    evidence_paths = [
        path
        for requirement in oracle["must_satisfy"]
        for path in requirement["evidence_paths"]
    ] + [effect["path"] for effect in oracle["expected_effects"]]
    for index, evidence_path in enumerate(evidence_paths):
        if not resolve_evidence_path(episode, evidence_path, roots=_EVIDENCE_ROOTS_V2)[
            0
        ]:
            issues.append(
                _issue(
                    "oracle.invalid_evidence_path",
                    f"$.oracle.evidence_paths[{index}]",
                    "Oracle Evidence Path does not resolve in DecisionEpisode v2.",
                    file=file,
                )
            )
    if episode["result"]["action_class"] not in oracle["allowed_action_classes"]:
        issues.append(
            _issue(
                "result.outside_action_envelope",
                "$.result.action_class",
                "Result action is outside the acceptable action envelope.",
                file=file,
            )
        )
    allowed_for_track = TRACK_ACTIONS[episode["track"]]
    for index, action in enumerate(oracle["allowed_action_classes"]):
        if action not in allowed_for_track:
            issues.append(
                _issue(
                    "oracle.action_track_mismatch",
                    f"$.oracle.allowed_action_classes[{index}]",
                    "Allowed action class is not valid for this track.",
                    file=file,
                )
            )
    if episode["result"]["action_class"] not in allowed_for_track:
        issues.append(
            _issue(
                "result.action_track_mismatch",
                "$.result.action_class",
                "Observed action class is not valid for this track.",
                file=file,
            )
        )
    if (
        episode["result"]["action_mapping_version"] != ACTION_MAPPING_VERSION
        or episode["result"]["action_mapping_sha256"] != ACTION_MAPPING_SHA256
    ):
        issues.append(
            _issue(
                "result.action_mapping_mismatch",
                "$.result.action_mapping_sha256",
                "Action classification mapping is unknown or has drifted.",
                file=file,
            )
        )
    if oracle["oracle_author"] == oracle["oracle_reviewer"]:
        issues.append(
            _issue(
                "oracle.review_not_independent",
                "$.oracle.oracle_reviewer",
                "Oracle author and reviewer roles must be independent.",
                file=file,
            )
        )
    environment = episode["environment"]
    if environment["manifest_sha256"] != environment_manifest_digest(environment):
        issues.append(
            _issue(
                "digest.environment_mismatch",
                "$.environment.manifest_sha256",
                "Environment Manifest digest does not match canonical content.",
                file=file,
            )
        )
    if oracle["envelope_sha256"] != oracle_envelope_digest(oracle):
        issues.append(
            _issue(
                "digest.oracle_mismatch",
                "$.oracle.envelope_sha256",
                "Oracle envelope digest does not match canonical content.",
                file=file,
            )
        )
    completeness = episode["completeness"]
    if completeness["completeness_sha256"] != episode_completeness_digest(completeness):
        issues.append(
            _issue(
                "digest.completeness_mismatch",
                "$.completeness.completeness_sha256",
                "Completeness digest does not match canonical content.",
                file=file,
            )
        )
    if completeness["status"] != "complete" or completeness["error_codes"]:
        issues.append(
            _issue(
                "completeness.invalid",
                "$.completeness.status",
                "Invalid Episodes cannot enter Rule evaluation.",
                file=file,
            )
        )
    for index, evidence_path in enumerate(completeness["verified_evidence_paths"]):
        if not resolve_evidence_path(episode, evidence_path, roots=_EVIDENCE_ROOTS_V2)[
            0
        ]:
            issues.append(
                _issue(
                    "completeness.invalid_evidence_path",
                    f"$.completeness.verified_evidence_paths[{index}]",
                    "Verified Evidence Path does not resolve in the Episode.",
                    file=file,
                )
            )
    provenance = episode["provenance"]
    if provenance["episode_sha256"] != decision_episode_digest(episode):
        issues.append(
            _issue(
                "digest.episode_mismatch",
                "$.provenance.episode_sha256",
                "Decision Episode digest does not match canonical content.",
                file=file,
            )
        )
    if (
        not provenance["runtime_executed"]
        or provenance["construction_method"] != "runtime_recorded"
    ):
        issues.append(
            _issue(
                "provenance.runtime_boundary",
                "$.provenance",
                "DecisionEpisode v2 Runtime output must be runtime recorded.",
                file=file,
            )
        )
    invocation_mode = environment["model"]["invocation_mode"]
    eligibility = episode["result"]["layers"]["formal_evaluation_eligibility"]
    if invocation_mode not in {"stub", "real"}:
        issues.append(
            _issue(
                "provenance.runtime_model_mode",
                "$.environment.model.invocation_mode",
                "DecisionEpisode v2 Runtime output requires stub or real model execution.",
                file=file,
            )
        )
    if invocation_mode == "stub" and (
        provenance["formal_evaluation_result"]
        or provenance["evaluation_status"] != "not_a_formal_model_evaluation"
        or eligibility != "ineligible_stub"
    ):
        issues.append(
            _issue(
                "provenance.stub_formal_claim",
                "$.provenance.formal_evaluation_result",
                "Stub Runtime output cannot claim formal evaluation eligibility.",
                file=file,
            )
        )
    formal = provenance["formal_evaluation_result"]
    if formal != (invocation_mode == "real" and eligibility == "eligible"):
        issues.append(
            _issue(
                "provenance.formal_eligibility_mismatch",
                "$.result.layers.formal_evaluation_eligibility",
                "Formal evaluation requires real Runtime execution and eligible provenance.",
                file=file,
            )
        )
    expected_eligibility = (
        "ineligible_stub"
        if invocation_mode == "stub"
        else "eligible"
        if formal
        else "ineligible_engineering"
    )
    if invocation_mode in {"stub", "real"} and eligibility != expected_eligibility:
        issues.append(
            _issue(
                "provenance.eligibility_mismatch",
                "$.result.layers.formal_evaluation_eligibility",
                "Runtime invocation mode and formal eligibility are inconsistent.",
                file=file,
            )
        )
    if episode["result"]["guard"]["status"] in {"blocked", "deferred"}:
        prohibited = {"intervention", "notification", "outbox_action", "outbox_receipt"}
        changed_types = {
            snapshot_maps["state_after"].get(
                change["entity_ref"],
                snapshot_maps["state_before"].get(change["entity_ref"]),
            )["entity_type"]
            for change in delta["changes"]
        }
        if changed_types & prohibited:
            issues.append(
                _issue(
                    "guard.prevented_side_effect",
                    "$.state_delta.changes",
                    "Blocked or deferred Guard decisions cannot produce notification side effects.",
                    file=file,
                )
            )
    if (
        episode["result"]["guard"]["status"] == "mixed"
        and episode["result"]["layers"]["durable_status"] != "partial"
    ):
        issues.append(
            _issue(
                "guard.partial_status_mismatch",
                "$.result.layers.durable_status",
                "Mixed Guard outcomes require an explicit partial durable status.",
                file=file,
            )
        )
    return issues


def _v3_v2_validation_projection(episode: Mapping[str, Any]) -> dict[str, Any]:
    """Reuse frozen E2 state/trace checks without importing answer criteria."""

    projected = dict(episode)
    projected["schema_version"] = "decision-episode-v2"
    projected["tags"] = ["v3_structural_validation"]
    attestation = episode["environment"]["provider_attestation"]
    environment = {
        "schema_version": "environment-manifest-v1",
        "frozen_time": episode["environment"]["frozen_time"],
        "timezone": episode["environment"]["timezone"],
        "model": {
            "provider": attestation["provider_id"],
            "name": attestation["configured_model"],
            "invocation_mode": attestation["invocation_mode"],
            "temperature": 0.0,
            "reasoning_effort": "none",
            "max_tokens": 0,
        },
        "prompt": episode["environment"]["prompt"],
        "tools": episode["environment"]["tools"],
        "policies": episode["environment"]["policies"],
        "resources": episode["environment"]["resources"],
        "runtime": {
            "git_commit": episode["environment"]["runtime"]["git_commit"],
            "database_mode": "temporary_fixture",
            "fixture_db_sha256": episode["environment"]["runtime"]["fixture_db_sha256"],
        },
        "isolation": episode["environment"]["isolation"],
        "manifest_sha256": "0" * 64,
    }
    environment["manifest_sha256"] = environment_manifest_digest(environment)
    projected["environment"] = environment

    calls = {
        item["call_id"]: item for item in episode["observable_trace"]["model_calls"]
    }
    call_to_turn: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for attempt in episode["result"]["layers"]["model_attempts"]:
        call = calls[attempt["call_ref"]]
        turn_id = f"turn:{episode['episode_id']}:{attempt['ordinal']:03d}"
        call_to_turn[call["call_id"]] = turn_id
        turns.append(
            {
                "turn_id": turn_id,
                "ordinal": attempt["ordinal"],
                "visible_input_digest": call["visible_context"]["context_sha256"],
                "assistant_text": call["assistant_text"],
                "tool_call_refs": attempt["invocation_refs"],
            }
        )
        attempts.append(
            {
                "attempt_id": attempt["attempt_id"],
                "ordinal": attempt["ordinal"],
                "turn_ref": turn_id,
                "attempted_action": attempt["attempted_action"],
                "invocation_refs": attempt["invocation_refs"],
            }
        )
    trace = {
        "capture_mode": "runtime_recording",
        "model_turns": turns,
        "tool_invocations": episode["observable_trace"]["tool_invocations"],
        "run_events": episode["observable_trace"]["run_events"],
        "operations": episode["observable_trace"]["operations"],
        "guard_decisions": episode["observable_trace"]["guard_decisions"],
    }
    projected["observable_trace"] = trace
    result = {
        **episode["result"],
        "layers": {
            **episode["result"]["layers"],
            "model_attempts": attempts,
            "final_effects": [
                {
                    **effect,
                    "source_refs": [
                        {
                            "source_type": (
                                "model_turn"
                                if source["source_type"] == "model_call"
                                else source["source_type"]
                            ),
                            "ref": (
                                call_to_turn[source["ref"]]
                                if source["source_type"] == "model_call"
                                else source["ref"]
                            ),
                        }
                        for source in effect["source_refs"]
                    ],
                }
                for effect in episode["result"]["layers"]["final_effects"]
            ],
        },
    }
    projected["result"] = result
    action = episode["result"]["action_class"]
    envelope = {
        "schema_version": "acceptable-action-envelope-v1",
        "allowed_action_classes": [action],
        "must_satisfy": [
            {
                "id": "v3.structural.action",
                "statement": "The exported action is structurally present.",
                "evidence_paths": ["result.action_class"],
            }
        ],
        "must_not": [
            {
                "id": "v3.structural.no-private-reasoning",
                "statement": "Private model work is excluded from artifacts.",
                "criticality": "critical",
            }
        ],
        "expected_effects": [
            {"path": "result.action_class", "relation": "equals", "value": action}
        ],
        "acceptable_variations": ["Decision correctness is evaluated downstream."],
        "critical_failures": ["Structural evidence is missing or inconsistent."],
        "oracle_author": "v3_structure_adapter",
        "oracle_reviewer": "v3_structure_reviewer",
        "adjudication_note": None,
        "envelope_sha256": "0" * 64,
    }
    envelope["envelope_sha256"] = oracle_envelope_digest(envelope)
    projected["oracle"] = envelope
    completeness = {
        "status": episode["completeness"]["status"],
        "error_codes": episode["completeness"]["evidence_error_codes"],
        "verified_evidence_paths": [
            "state_before.snapshot_sha256",
            "state_after.snapshot_sha256",
            "state_delta.delta_sha256",
            "observable_trace.model_turns",
            "observable_trace.run_events",
            "result.layers",
            "isolation_evidence",
            "result.action_class",
        ],
        "completeness_sha256": "0" * 64,
    }
    completeness["completeness_sha256"] = episode_completeness_digest(completeness)
    projected["completeness"] = completeness
    provenance = {
        **episode["provenance"],
        "author_role": "v3_runtime_recorder",
        "reviewer_role": "v3_protocol_reviewer",
        "purpose": "Validate v3 Runtime evidence through frozen E2 structural checks.",
        "dataset_role": (
            "protocol_mini_fixture"
            if episode["provenance"]["dataset_role"] == "engineering_mini"
            else episode["provenance"]["dataset_role"]
        ),
        "episode_sha256": "0" * 64,
    }
    projected["provenance"] = provenance
    projected.pop("case_spec_sha256", None)
    projected.pop("judge_reference_sha256", None)
    provenance["episode_sha256"] = decision_episode_digest(projected)
    return projected


def _episode_v3_semantic_issues(
    episode: dict[str, Any], *, file: str
) -> list[ValidationIssue]:
    """Validate v3 evidence without consulting decision correctness criteria."""

    issues: list[ValidationIssue] = []
    trace = episode["observable_trace"]
    call_ids = {item["call_id"] for item in trace["model_calls"]}
    invocation_ids = {item["invocation_id"] for item in trace["tool_invocations"]}
    run_ids = {
        item["logical_id"]
        for item in episode["state_after"]["logical_entities"]
        if item["entity_type"] == "agent_run"
    }
    for index, call in enumerate(trace["model_calls"]):
        context = call["visible_context"]
        if context["context_sha256"] != model_visible_context_digest(context):
            issues.append(
                _issue(
                    "digest.model_context_mismatch",
                    f"$.observable_trace.model_calls[{index}].visible_context.context_sha256",
                    "Model-visible context digest does not match its exact messages and tools.",
                    file=file,
                )
            )
        if call["run_id"] not in run_ids or (
            call["parent_run_id"] is not None and call["parent_run_id"] not in run_ids
        ):
            issues.append(
                _issue(
                    "reference.model_run_missing",
                    f"$.observable_trace.model_calls[{index}].run_id",
                    "Model call hierarchy must resolve to captured AgentRuns.",
                    file=file,
                )
            )
        if not set(call["tool_call_refs"]).issubset(invocation_ids):
            issues.append(
                _issue(
                    "reference.model_invocation_missing",
                    f"$.observable_trace.model_calls[{index}].tool_call_refs",
                    "Model call tool references must resolve to captured invocations.",
                    file=file,
                )
            )
        if call["status"] == "completed":
            expected_response = sha256_digest(
                {
                    "assistant_text": call["assistant_text"] or "",
                    "tool_call_refs": call["tool_call_refs"],
                }
            )
            if call["response_sha256"] != expected_response:
                issues.append(
                    _issue(
                        "digest.model_response_mismatch",
                        f"$.observable_trace.model_calls[{index}].response_sha256",
                        "Public model response digest is not recomputable.",
                        file=file,
                    )
                )
    attempts = episode["result"]["layers"]["model_attempts"]
    if any(item["call_ref"] not in call_ids for item in attempts):
        issues.append(
            _issue(
                "reference.model_call_missing",
                "$.result.layers.model_attempts",
                "Every decision attempt must reference a captured model call.",
                file=file,
            )
        )
    for path_index, path in enumerate(
        episode["completeness"]["verified_evidence_paths"]
    ):
        if not resolve_evidence_path(episode, path, roots=_EVIDENCE_ROOTS_V3)[0]:
            issues.append(
                _issue(
                    "completeness.invalid_evidence_path",
                    f"$.completeness.verified_evidence_paths[{path_index}]",
                    "Verified Evidence Path does not resolve in DecisionEpisode v3.",
                    file=file,
                )
            )
    completeness = episode["completeness"]
    if completeness["completeness_sha256"] != episode_completeness_digest(completeness):
        issues.append(
            _issue(
                "digest.completeness_mismatch",
                "$.completeness.completeness_sha256",
                "Completeness digest does not match canonical evidence fields.",
                file=file,
            )
        )
    environment = episode["environment"]
    if environment["manifest_sha256"] != environment_manifest_digest(environment):
        issues.append(
            _issue(
                "digest.environment_mismatch",
                "$.environment.manifest_sha256",
                "Environment Manifest digest does not match canonical content.",
                file=file,
            )
        )
    attestation = environment["provider_attestation"]
    if attestation["attestation_sha256"] != provider_attestation_digest(attestation):
        issues.append(
            _issue(
                "digest.provider_attestation_mismatch",
                "$.environment.provider_attestation.attestation_sha256",
                "Provider attribution digest does not match canonical content.",
                file=file,
            )
        )
    if episode["provenance"]["episode_sha256"] != decision_episode_digest(episode):
        issues.append(
            _issue(
                "digest.episode_mismatch",
                "$.provenance.episode_sha256",
                "Decision Episode digest does not match canonical content.",
                file=file,
            )
        )
    if completeness["status"] != "complete" or completeness["evidence_error_codes"]:
        issues.append(
            _issue(
                "completeness.invalid",
                "$.completeness.status",
                "Only evidence-complete v3 Episodes may enter Rules.",
                file=file,
            )
        )
    try:
        structural_projection = _v3_v2_validation_projection(episode)
        structural_issues = _episode_v2_semantic_issues(
            structural_projection, file=file
        )
    except (CanonicalizationError, ExportError, KeyError, TypeError, ValueError):
        structural_issues = [
            _issue(
                "schema.semantic_shape",
                "$",
                "v3 Runtime evidence cannot be reduced to the frozen structural checks.",
                file=file,
            )
        ]
    issues.extend(structural_issues)
    return issues


def validate_episode(
    value: object,
    *,
    source: str = "<memory>",
) -> tuple[ValidationIssue, ...]:
    """Validate one in-memory Decision Episode and return sorted issues."""

    issues = privacy_issues(value, file=source)
    if issues:
        return tuple(sorted(issues, key=ValidationIssue.sort_key))
    if not isinstance(value, Mapping):
        return (
            _issue(
                "schema.root_type",
                "$",
                "Decision Episode must be a JSON object.",
                file=source,
            ),
        )
    version = value.get("schema_version")
    if version not in {
        "decision-episode-v1",
        "decision-episode-v2",
        "decision-episode-v3",
    }:
        return (
            _issue(
                "schema.version",
                "$.schema_version",
                "Decision Episode schema_version is unsupported.",
                file=source,
            ),
        )
    schema_issues = _schema_issues(value, version=version, file=source)
    if schema_issues:
        return tuple(sorted(schema_issues, key=ValidationIssue.sort_key))
    try:
        semantic_issues = (
            _episode_semantic_issues(dict(value), file=source)
            if version == "decision-episode-v1"
            else _episode_v2_semantic_issues(dict(value), file=source)
            if version == "decision-episode-v2"
            else _episode_v3_semantic_issues(dict(value), file=source)
        )
        if (
            version in {"decision-episode-v1", "decision-episode-v2"}
            and value["provenance"]["formal_evaluation_result"]
        ):
            semantic_issues.append(
                _issue(
                    "provenance.legacy_nonformal",
                    "$.provenance.formal_evaluation_result",
                    "Only DecisionEpisode v3 can represent a formal evaluation.",
                    file=source,
                )
            )
    except CanonicalizationError:
        semantic_issues = [
            _issue(
                "canonical.invalid_value",
                "$",
                "Episode contains a value outside canonical JSON.",
                file=source,
            )
        ]
    except (ExportError, KeyError, TypeError, ValueError):
        semantic_issues = [
            _issue(
                "schema.semantic_shape",
                "$",
                "Episode structured values do not match the supported semantic contract.",
                file=source,
            )
        ]
    return tuple(sorted(semantic_issues, key=ValidationIssue.sort_key))


def _aggregate_result_semantic_issues(
    result: dict[str, Any], *, file: str
) -> list[ValidationIssue]:
    """Recompute self-contained Rule precedence and score fields in E3 output."""

    issues: list[ValidationIssue] = []
    critical_ids = [
        failure["check_id"]
        for failure in result["rule_failures"]
        if failure["severity"] == "critical"
    ]
    major_ids = [
        failure["check_id"]
        for failure in result["rule_failures"]
        if failure["severity"] == "major"
    ]
    if result["actual_hard_gates"] != critical_ids:
        issues.append(
            _issue(
                "aggregate.rule_precedence_invalid",
                "$.actual_hard_gates",
                "Aggregate hard gates must exactly preserve Critical Rule failures.",
                file=file,
            )
        )
    if result["status"] != "complete":
        return issues
    expected_dimensions = []
    for dimension in result["dimensions"]:
        weight = DIMENSION_WEIGHTS[dimension["dimension_id"]]
        expected_dimensions.append(
            {
                "dimension_id": dimension["dimension_id"],
                "level": dimension["level"],
                "weight": weight,
                "weighted_signal": float(weight * dimension["level"] / 2),
                "evidence_paths": dimension["evidence_paths"],
            }
        )
    expected_caps: list[dict[str, Any]] = []
    if critical_ids:
        expected_caps.append(
            {
                "cap_id": "critical_hard_gate",
                "maximum_score": 39,
                "source_rule_ids": critical_ids,
            }
        )
    if major_ids:
        expected_caps.append(
            {
                "cap_id": "major_rule_fail",
                "maximum_score": 69,
                "source_rule_ids": major_ids,
            }
        )
    raw_score = float(sum(item["weighted_signal"] for item in expected_dimensions))
    final_score = min(
        [raw_score, *(float(cap["maximum_score"]) for cap in expected_caps)]
    )
    expected_outcome = "fail" if critical_ids else "pass"
    if (
        result["dimensions"] != expected_dimensions
        or result["raw_score"] != raw_score
        or result["applied_caps"] != expected_caps
        or result["final_score"] != final_score
        or result["episode_outcome"] != expected_outcome
    ):
        issues.append(
            _issue(
                "aggregate.score_invalid",
                "$.final_score",
                "Aggregate score, caps, or outcome does not match deterministic inputs.",
                file=file,
            )
        )
    return issues


def _document_issues(
    value: object,
    *,
    file: str,
) -> tuple[list[ValidationIssue], _EpisodeRecord | None]:
    issues = privacy_issues(value, file=file)
    if issues:
        return issues, None
    if not isinstance(value, Mapping):
        return [
            _issue(
                "schema.root_type",
                "$",
                "Dataset document must be an object.",
                file=file,
            )
        ], None
    version = value.get("schema_version")
    if not isinstance(version, str):
        return [
            _issue(
                "schema.missing",
                "$.schema_version",
                "Required field is missing.",
                file=file,
            )
        ], None
    if version not in DATASET_DOCUMENT_MODELS:
        return [
            _issue(
                "schema.version",
                "$.schema_version",
                "Dataset document schema_version is unsupported.",
                file=file,
            )
        ], None
    schema_issues = _schema_issues(value, version=version, file=file)
    if schema_issues:
        return schema_issues, None
    digest_fields = {
        "e1-runtime-mini-fixture-v1": "fixture_sha256",
        "e1-resource-snapshot-v1": "manifest_sha256",
        "e1-run-manifest-v1": "manifest_sha256",
        "e1-capture-artifact-v1": "capture_sha256",
        "e1-run-output-manifest-v1": "manifest_sha256",
        "e2-run-output-manifest-v1": "manifest_sha256",
        "integrity-result-v1": "result_sha256",
        "rule-result-v1": "result_sha256",
        "rule-run-manifest-v1": "manifest_sha256",
        "judge-result-v1": "result_sha256",
        "judge-run-manifest-v1": "manifest_sha256",
        "aggregate-result-v1": "result_sha256",
        "aggregate-track-result-v1": "result_sha256",
        "aggregate-run-manifest-v1": "manifest_sha256",
        "case-spec-v1": "case_spec_sha256",
        "judge-reference-v1": "reference_sha256",
        "provider-attestation-v1": "attestation_sha256",
        "environment-manifest-v2": "manifest_sha256",
        "runtime-failure-v1": "failure_sha256",
        "runtime-run-manifest-v2": "manifest_sha256",
        "case-suite-manifest-v1": "manifest_sha256",
        "rule-result-v2": "result_sha256",
        "rule-run-manifest-v2": "manifest_sha256",
        "judge-result-v2": "result_sha256",
        "judge-run-manifest-v2": "manifest_sha256",
        "aggregate-result-v2": "result_sha256",
        "aggregate-track-result-v2": "result_sha256",
        "aggregate-run-manifest-v2": "manifest_sha256",
    }
    digest_field = digest_fields.get(version)
    if digest_field is not None:
        digest_functions = {
            "e2-run-output-manifest-v1": artifact_manifest_digest,
            "rule-run-manifest-v1": artifact_manifest_digest,
            "integrity-result-v1": integrity_result_digest,
            "rule-result-v1": rule_result_digest,
            "judge-run-manifest-v1": artifact_manifest_digest,
            "aggregate-run-manifest-v1": artifact_manifest_digest,
            "judge-result-v1": judge_result_digest,
            "aggregate-result-v1": aggregate_result_digest,
            "aggregate-track-result-v1": aggregate_result_digest,
            "case-spec-v1": case_spec_digest,
            "judge-reference-v1": judge_reference_digest,
            "provider-attestation-v1": provider_attestation_digest,
            "runtime-failure-v1": runtime_failure_digest,
            "runtime-run-manifest-v2": artifact_manifest_digest,
            "case-suite-manifest-v1": artifact_manifest_digest,
            "rule-result-v2": rule_result_digest,
            "rule-run-manifest-v2": artifact_manifest_digest,
            "judge-result-v2": judge_result_digest,
            "judge-run-manifest-v2": artifact_manifest_digest,
            "aggregate-result-v2": aggregate_result_digest,
            "aggregate-track-result-v2": aggregate_result_digest,
            "aggregate-run-manifest-v2": artifact_manifest_digest,
            "environment-manifest-v2": environment_manifest_digest,
        }
        digest = digest_functions.get(
            version, lambda document: sha256_digest(document)
        )({key: item for key, item in value.items() if key != digest_field})
        if value[digest_field] != digest:
            return [
                _issue(
                    "digest.e1_artifact_mismatch",
                    f"$.{digest_field}",
                    "E1 artifact digest does not match canonical content.",
                    file=file,
                )
            ], None
    if version in {"aggregate-result-v1", "aggregate-result-v2"}:
        return _aggregate_result_semantic_issues(dict(value), file=file), None
    if version not in {
        "decision-episode-v1",
        "decision-episode-v2",
        "decision-episode-v3",
    }:
        return [], None
    episode = dict(value)
    try:
        semantic_issues = (
            _episode_semantic_issues(episode, file=file)
            if version == "decision-episode-v1"
            else _episode_v2_semantic_issues(episode, file=file)
            if version == "decision-episode-v2"
            else _episode_v3_semantic_issues(episode, file=file)
        )
        if (
            version in {"decision-episode-v1", "decision-episode-v2"}
            and episode["provenance"]["formal_evaluation_result"]
        ):
            semantic_issues.append(
                _issue(
                    "provenance.legacy_nonformal",
                    "$.provenance.formal_evaluation_result",
                    "Only DecisionEpisode v3 can represent a formal evaluation.",
                    file=file,
                )
            )
    except CanonicalizationError:
        semantic_issues = [
            _issue(
                "canonical.invalid_value",
                "$",
                "Document contains a value outside canonical JSON.",
                file=file,
            )
        ]
    except (ExportError, KeyError, TypeError, ValueError):
        semantic_issues = [
            _issue(
                "schema.semantic_shape",
                "$",
                "Episode structured values do not match the supported semantic contract.",
                file=file,
            )
        ]
    return semantic_issues, _EpisodeRecord(file=file, value=episode)


def _e31_artifact_inventory_issues(
    by_version: Mapping[str, list[tuple[str, dict[str, Any]]]],
    episodes: list[_EpisodeRecord],
) -> list[ValidationIssue]:
    """Validate every active v3 output directory as a closed inventory."""

    issues: list[ValidationIssue] = []

    def add(code: str, path: str, message: str, file: str) -> None:
        issues.append(_issue(code, path, message, file=file))

    suites = by_version["case-suite-manifest-v1"]
    if len(suites) > 1:
        for file, _ in suites[1:]:
            add(
                "manifest.duplicate_case_suite",
                "$.schema_version",
                "Dataset contains more than one CaseSuite manifest.",
                file,
            )
    for file, manifest in suites[:1]:
        cases = {name: document for name, document in by_version["case-spec-v1"]}
        if set(cases) != set(manifest["case_files"]):
            add(
                "manifest.case_inventory_mismatch",
                "$.case_files",
                "CaseSpec inventory differs from the CaseSuite manifest.",
                file,
            )
        available_files = {name for values in by_version.values() for name, _ in values}
        if manifest["resource_snapshot_file"] not in available_files:
            add(
                "manifest.resource_snapshot_missing",
                "$.resource_snapshot_file",
                "CaseSuite resource Snapshot is missing.",
                file,
            )

    runtime_manifests = by_version["runtime-run-manifest-v2"]
    if len(runtime_manifests) > 1:
        for file, _ in runtime_manifests[1:]:
            add(
                "manifest.duplicate_runtime_v2",
                "$.schema_version",
                "Runtime output contains more than one v2 manifest.",
                file,
            )
    for file, manifest in runtime_manifests[:1]:
        v3_episodes = {
            item.value["episode_id"]: item.value
            for item in episodes
            if item.value["schema_version"] == "decision-episode-v3"
        }
        failures = {
            document["failure_id"]: document
            for _, document in by_version["runtime-failure-v1"]
        }
        references = {
            Path(name).stem: document
            for name, document in by_version["judge-reference-v1"]
        }
        terminals = {item["artifact_id"]: item for item in manifest["terminals"]}
        episode_terminals = {
            key: item
            for key, item in terminals.items()
            if item["terminal_kind"] == "episode"
        }
        failure_terminals = {
            key: item
            for key, item in terminals.items()
            if item["terminal_kind"] == "failure"
        }
        if (
            set(v3_episodes) != set(episode_terminals)
            or set(references) != set(episode_terminals)
            or set(failures) != set(failure_terminals)
        ):
            add(
                "manifest.runtime_v2_inventory_mismatch",
                "$.terminals",
                "v3 Episode, JudgeReference, and Failure inventories differ from terminals.",
                file,
            )
        for episode_id in sorted(
            set(v3_episodes) & set(episode_terminals) & set(references)
        ):
            episode = v3_episodes[episode_id]
            terminal = episode_terminals[episode_id]
            reference = references[episode_id]
            if (
                terminal["artifact_sha256"] != episode["provenance"]["episode_sha256"]
                or terminal["case_spec_sha256"] != episode["case_spec_sha256"]
                or terminal["track"] != episode["track"]
                or reference["reference_sha256"] != episode["judge_reference_sha256"]
                or reference["case_spec_sha256"] != episode["case_spec_sha256"]
                or reference["track"] != episode["track"]
            ):
                add(
                    "manifest.runtime_v2_episode_mismatch",
                    f"$.terminals.{episode_id}",
                    "v3 terminal, Episode, or JudgeReference linkage differs.",
                    file,
                )
        for failure_id in sorted(set(failures) & set(failure_terminals)):
            failure = failures[failure_id]
            terminal = failure_terminals[failure_id]
            if (
                terminal["artifact_sha256"] != failure["failure_sha256"]
                or terminal["case_spec_sha256"] != failure["case_spec_sha256"]
            ):
                add(
                    "manifest.runtime_v2_failure_mismatch",
                    f"$.terminals.{failure_id}",
                    "Runtime Failure digest or CaseSpec linkage differs.",
                    file,
                )
        expected_formal = (
            not failure_terminals
            and bool(v3_episodes)
            and all(
                item["provenance"]["formal_evaluation_result"]
                for item in v3_episodes.values()
            )
        )
        if manifest["formal_evaluation_result"] != expected_formal:
            add(
                "manifest.runtime_v2_formal_mismatch",
                "$.formal_evaluation_result",
                "Runtime v2 formal state differs from its terminal artifacts.",
                file,
            )

    rule_manifests = by_version["rule-run-manifest-v2"]
    if len(rule_manifests) > 1:
        for file, _ in rule_manifests[1:]:
            add(
                "manifest.duplicate_rule_run_v2",
                "$.schema_version",
                "Rule output contains more than one v2 manifest.",
                file,
            )
    for file, manifest in rule_manifests[:1]:
        results = {
            document["episode_id"]: document
            for _, document in by_version["rule-result-v2"]
        }
        expected_ids = set(manifest["episode_ids"])
        if set(results) != expected_ids or len(results) != len(
            by_version["rule-result-v2"]
        ):
            add(
                "manifest.rule_v2_inventory_mismatch",
                "$.episode_ids",
                "Rule Result v2 inventory differs from its manifest.",
                file,
            )
        for episode_id in sorted(expected_ids & set(results)):
            result = results[episode_id]
            if (
                result["episode_sha256"]
                != manifest["input_episode_digests"][episode_id]
                or result["judge_reference_sha256"]
                != manifest["input_reference_digests"][episode_id]
                or result["result_sha256"]
                != manifest["rule_result_digests"][episode_id]
                or result["evaluator_version"] != manifest["evaluator_version"]
                or result["rule_pack_version"] != manifest["rule_pack_version"]
                or result["rule_pack_sha256"] != manifest["rule_pack_sha256"]
            ):
                add(
                    "manifest.rule_v2_result_mismatch",
                    f"$.rule_result_digests.{episode_id}",
                    "Rule Result v2 differs from its manifest.",
                    file,
                )
        expected_formal = (
            not manifest["runtime_failure_ids"]
            and bool(results)
            and all(item["formal_evaluation_result"] for item in results.values())
        )
        if manifest["formal_evaluation_result"] != expected_formal:
            add(
                "manifest.rule_v2_formal_mismatch",
                "$.formal_evaluation_result",
                "Rule v2 formal state differs from results and runtime failures.",
                file,
            )

    judge_manifests = by_version["judge-run-manifest-v2"]
    if len(judge_manifests) > 1:
        for file, _ in judge_manifests[1:]:
            add(
                "manifest.duplicate_judge_run_v2",
                "$.schema_version",
                "Judge output contains more than one v2 manifest.",
                file,
            )
    for file, manifest in judge_manifests[:1]:
        results = {
            document["episode_id"]: document
            for _, document in by_version["judge-result-v2"]
        }
        expected_ids = set(manifest["episode_ids"])
        expected_config = {
            "judge_version": JUDGE_VERSION_V2,
            "judge_config_version": JUDGE_CONFIG_VERSION_V2,
            "judge_config_sha256": JUDGE_CONFIG_SHA256_V2,
            "judge_prompt_version": JUDGE_PROMPT_VERSION_V2,
            "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V2,
            "rubric_version": RUBRIC_VERSION,
            "rubric_sha256": RUBRIC_SHA256,
            "track_anchor_version": TRACK_ANCHOR_VERSION,
            "track_anchor_sha256": TRACK_ANCHOR_SHA256,
            "repair_limit": REPAIR_LIMIT,
        }
        if any(manifest[key] != value for key, value in expected_config.items()):
            add(
                "manifest.judge_v2_config_mismatch",
                "$.judge_config_sha256",
                "Judge v2 manifest does not use the frozen configuration.",
                file,
            )
        if set(results) != expected_ids or len(results) != len(
            by_version["judge-result-v2"]
        ):
            add(
                "manifest.judge_v2_inventory_mismatch",
                "$.episode_ids",
                "Judge Result v2 inventory differs from its manifest.",
                file,
            )
        for episode_id in sorted(expected_ids & set(results)):
            result = results[episode_id]
            expected = (
                result["episode_sha256"]
                == manifest["input_episode_digests"][episode_id]
                and result["rule_result_sha256"]
                == manifest["input_rule_result_digests"][episode_id]
                and result["judge_reference_sha256"]
                == manifest["input_reference_digests"][episode_id]
                and result["blind_input_sha256"]
                == manifest["blind_input_digests"][episode_id]
                and result["result_sha256"]
                == manifest["judge_result_digests"][episode_id]
                and result["status"] == manifest["result_statuses"][episode_id]
                and result["judge_mode"] == manifest["judge_mode"]
            )
            if not expected:
                add(
                    "manifest.judge_v2_result_mismatch",
                    f"$.judge_result_digests.{episode_id}",
                    "Judge Result v2 differs from its manifest.",
                    file,
                )
        expected_formal = (
            not manifest["runtime_failure_ids"]
            and bool(results)
            and all(item["formal_evaluation_result"] for item in results.values())
        )
        if manifest["formal_evaluation_result"] != expected_formal:
            add(
                "manifest.judge_v2_formal_mismatch",
                "$.formal_evaluation_result",
                "Judge v2 formal state differs from results and runtime failures.",
                file,
            )

    aggregate_manifests = by_version["aggregate-run-manifest-v2"]
    if len(aggregate_manifests) > 1:
        for file, _ in aggregate_manifests[1:]:
            add(
                "manifest.duplicate_aggregate_run_v2",
                "$.schema_version",
                "Aggregate output contains more than one v2 manifest.",
                file,
            )
    for file, manifest in aggregate_manifests[:1]:
        results = {
            document["episode_id"]: document
            for _, document in by_version["aggregate-result-v2"]
        }
        tracks = {
            document["track"]: document
            for _, document in by_version["aggregate-track-result-v2"]
        }
        expected_ids = set(manifest["episode_ids"])
        if set(results) != expected_ids or len(results) != len(
            by_version["aggregate-result-v2"]
        ):
            add(
                "manifest.aggregate_v2_inventory_mismatch",
                "$.episode_ids",
                "Aggregate Result v2 inventory differs from its manifest.",
                file,
            )
        if set(tracks) != set(manifest["track_result_digests"]) or len(tracks) != len(
            by_version["aggregate-track-result-v2"]
        ):
            add(
                "manifest.aggregate_v2_track_inventory_mismatch",
                "$.track_result_digests",
                "Track Aggregate v2 inventory differs from its manifest.",
                file,
            )
        for episode_id in sorted(expected_ids & set(results)):
            result = results[episode_id]
            if (
                result["episode_sha256"]
                != manifest["input_episode_digests"][episode_id]
                or result["rule_result_sha256"]
                != manifest["input_rule_result_digests"][episode_id]
                or result["judge_result_sha256"]
                != manifest["input_judge_result_digests"][episode_id]
                or result["judge_reference_sha256"]
                != manifest["input_reference_digests"][episode_id]
                or result["result_sha256"]
                != manifest["aggregate_result_digests"][episode_id]
                or result["status"] != manifest["result_statuses"][episode_id]
                or result["aggregator_version"] != manifest["aggregator_version"]
            ):
                add(
                    "manifest.aggregate_v2_result_mismatch",
                    f"$.aggregate_result_digests.{episode_id}",
                    "Aggregate Result v2 differs from its manifest.",
                    file,
                )
        for track, result in sorted(tracks.items()):
            track_episodes = sorted(
                (item for item in results.values() if item["track"] == track),
                key=lambda item: item["episode_id"],
            )
            complete = [item for item in track_episodes if item["status"] == "complete"]
            scores = [float(item["final_score"]) for item in complete]
            runtime_failure_ids = sorted(
                failure_id
                for failure_id, failure_track in manifest[
                    "runtime_failure_tracks"
                ].items()
                if failure_track == track
            )
            track_formal = (
                bool(track_episodes)
                and not runtime_failure_ids
                and all(item["formal_evaluation_result"] for item in track_episodes)
            )
            expected_fields = {
                "episode_ids": [item["episode_id"] for item in track_episodes],
                "complete_episode_ids": [item["episode_id"] for item in complete],
                "failed_episode_ids": [
                    item["episode_id"]
                    for item in complete
                    if item["episode_outcome"] == "fail"
                ],
                "invalid_input_episode_ids": [
                    item["episode_id"]
                    for item in track_episodes
                    if item["status"] == "invalid_input"
                ],
                "judge_error_episode_ids": [
                    item["episode_id"]
                    for item in track_episodes
                    if item["status"] == "judge_error"
                ],
                "runtime_failure_ids": runtime_failure_ids,
                "score_count": len(scores),
                "mean_score": round(sum(scores) / len(scores), 6) if scores else None,
                "formal_evaluation_result": track_formal,
                "evaluation_status": (
                    "formal_model_evaluation"
                    if track_formal
                    else "not_a_formal_model_evaluation"
                ),
            }
            if (
                result["result_sha256"] != manifest["track_result_digests"].get(track)
                or result["aggregator_version"] != manifest["aggregator_version"]
                or any(result[key] != value for key, value in expected_fields.items())
            ):
                add(
                    "manifest.aggregate_v2_track_mismatch",
                    f"$.track_result_digests.{track}",
                    "Track Aggregate v2 differs from Episode and Failure inputs.",
                    file,
                )
        expected_formal = (
            not manifest["runtime_failure_ids"]
            and bool(results)
            and all(item["formal_evaluation_result"] for item in results.values())
        )
        if manifest["formal_evaluation_result"] != expected_formal:
            add(
                "manifest.aggregate_v2_formal_mismatch",
                "$.formal_evaluation_result",
                "Aggregate v2 formal state differs from results and runtime failures.",
                file,
            )
    return issues


def _artifact_inventory_issues(
    documents: list[tuple[str, dict[str, Any]]],
    episodes: list[_EpisodeRecord],
) -> list[ValidationIssue]:
    """Validate E2 Runtime/Rule manifests as closed artifact inventories."""

    issues: list[ValidationIssue] = []
    by_version: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for file, document in documents:
        by_version[document["schema_version"]].append((file, document))

    runtime_manifests = by_version["e2-run-output-manifest-v1"]
    if len(runtime_manifests) > 1:
        for file, _ in runtime_manifests[1:]:
            issues.append(
                _issue(
                    "manifest.duplicate_runtime",
                    "$.schema_version",
                    "Runtime output contains more than one E2 manifest.",
                    file=file,
                )
            )
    for file, manifest in runtime_manifests[:1]:
        episode_by_id = {
            record.value["episode_id"]: record.value for record in episodes
        }
        expected_ids = set(manifest["episode_ids"])
        if set(episode_by_id) != expected_ids:
            issues.append(
                _issue(
                    "manifest.episode_inventory_mismatch",
                    "$.episode_ids",
                    "Runtime Episode inventory does not match its E2 manifest.",
                    file=file,
                )
            )
        for episode_id in sorted(expected_ids & set(episode_by_id)):
            episode = episode_by_id[episode_id]
            if (
                episode["schema_version"] != manifest["episode_schema_version"]
                or episode["provenance"]["episode_sha256"]
                != manifest["episode_digests"][episode_id]
                or episode["environment"]["model"]["invocation_mode"]
                != manifest["invocation_mode"]
                or episode["provenance"]["formal_evaluation_result"]
                != manifest["formal_evaluation_result"]
            ):
                issues.append(
                    _issue(
                        "manifest.episode_mismatch",
                        f"$.episode_digests.{episode_id}",
                        "Runtime Episode version, digest, mode, or formal status differs from its manifest.",
                        file=file,
                    )
                )
        captures = {
            document["episode_id"]: document
            for _, document in by_version["e1-capture-artifact-v1"]
        }
        if set(captures) != expected_ids or len(
            by_version["e1-capture-artifact-v1"]
        ) != len(expected_ids):
            issues.append(
                _issue(
                    "manifest.capture_inventory_mismatch",
                    "$.capture_digests",
                    "Engineering Capture inventory does not match its E2 manifest.",
                    file=file,
                )
            )
        for episode_id in sorted(expected_ids & set(captures)):
            if (
                captures[episode_id]["capture_sha256"]
                != manifest["capture_digests"][episode_id]
            ):
                issues.append(
                    _issue(
                        "manifest.capture_digest_mismatch",
                        f"$.capture_digests.{episode_id}",
                        "Engineering Capture digest differs from its E2 manifest.",
                        file=file,
                    )
                )

    rule_manifests = by_version["rule-run-manifest-v1"]
    if len(rule_manifests) > 1:
        for file, _ in rule_manifests[1:]:
            issues.append(
                _issue(
                    "manifest.duplicate_rule_run",
                    "$.schema_version",
                    "Rule output contains more than one Rule Run manifest.",
                    file=file,
                )
            )
    for file, manifest in rule_manifests[:1]:
        expected_ids = set(manifest["episode_ids"])
        rules = {
            document["episode_id"]: document
            for _, document in by_version["rule-result-v1"]
        }
        integrity = {
            document["episode_id"]: document
            for _, document in by_version["integrity-result-v1"]
        }
        if (
            set(rules) != expected_ids
            or set(integrity) != expected_ids
            or len(by_version["rule-result-v1"]) != len(expected_ids)
            or len(by_version["integrity-result-v1"]) != len(expected_ids)
        ):
            issues.append(
                _issue(
                    "manifest.rule_inventory_mismatch",
                    "$.episode_ids",
                    "Rule and integrity inventories must match the Rule Run manifest.",
                    file=file,
                )
            )
        for episode_id in sorted(expected_ids & set(rules) & set(integrity)):
            rule = rules[episode_id]
            check = integrity[episode_id]
            if (
                rule["episode_sha256"] != manifest["input_episode_digests"][episode_id]
                or check["episode_sha256"]
                != manifest["input_episode_digests"][episode_id]
                or rule["result_sha256"] != manifest["rule_result_digests"][episode_id]
                or check["result_sha256"]
                != manifest["integrity_result_digests"][episode_id]
                or rule["evaluator_version"] != manifest["evaluator_version"]
                or rule["rule_pack_version"] != manifest["rule_pack_version"]
                or rule["rule_pack_sha256"] != manifest["rule_pack_sha256"]
            ):
                issues.append(
                    _issue(
                        "manifest.rule_result_mismatch",
                        f"$.rule_result_digests.{episode_id}",
                        "Rule or integrity result differs from its Rule Run manifest.",
                        file=file,
                    )
                )

    judge_manifests = by_version["judge-run-manifest-v1"]
    if len(judge_manifests) > 1:
        for file, _ in judge_manifests[1:]:
            issues.append(
                _issue(
                    "manifest.duplicate_judge_run",
                    "$.schema_version",
                    "Judge output contains more than one Judge Run manifest.",
                    file=file,
                )
            )
    for file, manifest in judge_manifests[:1]:
        expected_ids = set(manifest["episode_ids"])
        results = {
            document["episode_id"]: document
            for _, document in by_version["judge-result-v1"]
        }
        expected_judge_metadata = {
            "input_episode_schema_version": "decision-episode-v2",
            "input_rule_schema_version": "rule-result-v1",
            "judge_version": JUDGE_VERSION,
            "judge_config_version": JUDGE_CONFIG_VERSION,
            "judge_config_sha256": JUDGE_CONFIG_SHA256,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "judge_prompt_sha256": JUDGE_PROMPT_SHA256,
            "rubric_version": RUBRIC_VERSION,
            "rubric_sha256": RUBRIC_SHA256,
            "track_anchor_version": TRACK_ANCHOR_VERSION,
            "track_anchor_sha256": TRACK_ANCHOR_SHA256,
            "repair_limit": REPAIR_LIMIT,
        }
        if any(
            manifest[key] != expected
            for key, expected in expected_judge_metadata.items()
        ):
            issues.append(
                _issue(
                    "manifest.judge_config_mismatch",
                    "$.judge_config_sha256",
                    "Judge Run manifest does not use the submitted E3 configuration.",
                    file=file,
                )
            )
        if set(results) != expected_ids or len(results) != len(
            by_version["judge-result-v1"]
        ):
            issues.append(
                _issue(
                    "manifest.judge_inventory_mismatch",
                    "$.episode_ids",
                    "Judge Result inventory must match the Judge Run manifest.",
                    file=file,
                )
            )
        if manifest["selected_track"] is not None and any(
            result["track"] != manifest["selected_track"] for result in results.values()
        ):
            issues.append(
                _issue(
                    "manifest.judge_track_mismatch",
                    "$.selected_track",
                    "Judge Results do not match the requested track.",
                    file=file,
                )
            )
        for episode_id in sorted(expected_ids & set(results)):
            result = results[episode_id]
            if (
                result["episode_sha256"]
                != manifest["input_episode_digests"][episode_id]
                or result["rule_result_sha256"]
                != manifest["input_rule_result_digests"][episode_id]
                or result["blind_input_sha256"]
                != manifest["blind_input_digests"][episode_id]
                or result["result_sha256"]
                != manifest["judge_result_digests"][episode_id]
                or result["status"] != manifest["result_statuses"][episode_id]
                or result["judge_version"] != manifest["judge_version"]
                or result["judge_mode"] != manifest["judge_mode"]
                or result["judge_prompt_version"] != manifest["judge_prompt_version"]
                or result["judge_prompt_sha256"] != manifest["judge_prompt_sha256"]
                or result["rubric_version"] != manifest["rubric_version"]
                or result["rubric_sha256"] != manifest["rubric_sha256"]
                or result["track_anchor_version"] != manifest["track_anchor_version"]
                or result["track_anchor_sha256"] != manifest["track_anchor_sha256"]
            ):
                issues.append(
                    _issue(
                        "manifest.judge_result_mismatch",
                        f"$.judge_result_digests.{episode_id}",
                        "Judge Result differs from its Judge Run manifest.",
                        file=file,
                    )
                )
        expected_formal = bool(results) and all(
            result["formal_evaluation_result"] for result in results.values()
        )
        if manifest["formal_evaluation_result"] != expected_formal:
            issues.append(
                _issue(
                    "manifest.judge_formal_mismatch",
                    "$.formal_evaluation_result",
                    "Judge Run formal state must match every Judge Result.",
                    file=file,
                )
            )

    aggregate_manifests = by_version["aggregate-run-manifest-v1"]
    if len(aggregate_manifests) > 1:
        for file, _ in aggregate_manifests[1:]:
            issues.append(
                _issue(
                    "manifest.duplicate_aggregate_run",
                    "$.schema_version",
                    "Aggregate output contains more than one Run manifest.",
                    file=file,
                )
            )
    for file, manifest in aggregate_manifests[:1]:
        expected_ids = set(manifest["episode_ids"])
        results = {
            document["episode_id"]: document
            for _, document in by_version["aggregate-result-v1"]
        }
        tracks = {
            document["track"]: document
            for _, document in by_version["aggregate-track-result-v1"]
        }
        expected_aggregate_metadata = {
            "judge_version": JUDGE_VERSION,
            "judge_config_version": JUDGE_CONFIG_VERSION,
            "judge_config_sha256": JUDGE_CONFIG_SHA256,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "judge_prompt_sha256": JUDGE_PROMPT_SHA256,
            "rubric_version": RUBRIC_VERSION,
            "rubric_sha256": RUBRIC_SHA256,
            "track_anchor_version": TRACK_ANCHOR_VERSION,
            "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        }
        if any(
            manifest[key] != expected
            for key, expected in expected_aggregate_metadata.items()
        ):
            issues.append(
                _issue(
                    "manifest.aggregate_config_mismatch",
                    "$.judge_config_sha256",
                    "Aggregate Run manifest does not preserve E3 Judge configuration.",
                    file=file,
                )
            )
        if set(results) != expected_ids or len(results) != len(
            by_version["aggregate-result-v1"]
        ):
            issues.append(
                _issue(
                    "manifest.aggregate_inventory_mismatch",
                    "$.episode_ids",
                    "Aggregate Result inventory must match its Run manifest.",
                    file=file,
                )
            )
        if set(tracks) != set(manifest["track_result_digests"]) or len(tracks) != len(
            by_version["aggregate-track-result-v1"]
        ):
            issues.append(
                _issue(
                    "manifest.track_inventory_mismatch",
                    "$.track_result_digests",
                    "Track aggregate inventory must match its Run manifest.",
                    file=file,
                )
            )
        if manifest["selected_track"] is not None and (
            set(tracks) != {manifest["selected_track"]}
            or any(
                result["track"] != manifest["selected_track"]
                for result in results.values()
            )
        ):
            issues.append(
                _issue(
                    "manifest.aggregate_track_mismatch",
                    "$.selected_track",
                    "Aggregate Results do not match the requested track.",
                    file=file,
                )
            )
        for episode_id in sorted(expected_ids & set(results)):
            result = results[episode_id]
            if (
                result["episode_sha256"]
                != manifest["input_episode_digests"][episode_id]
                or result["rule_result_sha256"]
                != manifest["input_rule_result_digests"][episode_id]
                or result["judge_result_sha256"]
                != manifest["input_judge_result_digests"][episode_id]
                or result["result_sha256"]
                != manifest["aggregate_result_digests"][episode_id]
                or result["status"] != manifest["result_statuses"][episode_id]
                or result["aggregator_version"] != manifest["aggregator_version"]
            ):
                issues.append(
                    _issue(
                        "manifest.aggregate_result_mismatch",
                        f"$.aggregate_result_digests.{episode_id}",
                        "Aggregate Result differs from its Run manifest.",
                        file=file,
                    )
                )
        for track, result in sorted(tracks.items()):
            if (
                result["result_sha256"] != manifest["track_result_digests"][track]
                or result["aggregator_version"] != manifest["aggregator_version"]
            ):
                issues.append(
                    _issue(
                        "manifest.track_result_mismatch",
                        f"$.track_result_digests.{track}",
                        "Track aggregate differs from its Run manifest.",
                        file=file,
                    )
                )
            track_episodes = sorted(
                (
                    document
                    for document in results.values()
                    if document["track"] == track
                ),
                key=lambda document: document["episode_id"],
            )
            complete = [
                document
                for document in track_episodes
                if document["status"] == "complete"
            ]
            scores = [float(document["final_score"]) for document in complete]
            track_formal = bool(track_episodes) and all(
                document["formal_evaluation_result"] for document in track_episodes
            )
            expected_track_fields = {
                "episode_ids": [document["episode_id"] for document in track_episodes],
                "complete_episode_ids": [
                    document["episode_id"] for document in complete
                ],
                "failed_episode_ids": [
                    document["episode_id"]
                    for document in complete
                    if document["episode_outcome"] == "fail"
                ],
                "invalid_input_episode_ids": [
                    document["episode_id"]
                    for document in track_episodes
                    if document["status"] == "invalid_input"
                ],
                "judge_error_episode_ids": [
                    document["episode_id"]
                    for document in track_episodes
                    if document["status"] == "judge_error"
                ],
                "score_count": len(scores),
                "mean_score": (round(sum(scores) / len(scores), 6) if scores else None),
                "formal_evaluation_result": track_formal,
                "evaluation_status": (
                    "formal_model_evaluation"
                    if track_formal
                    else "not_a_formal_model_evaluation"
                ),
            }
            if any(
                result[key] != expected
                for key, expected in expected_track_fields.items()
            ):
                issues.append(
                    _issue(
                        "manifest.track_summary_mismatch",
                        f"$.track_result_digests.{track}",
                        "Track aggregate does not match its Episode results.",
                        file=file,
                    )
                )
        aggregate_formal = bool(results) and all(
            result["formal_evaluation_result"] for result in results.values()
        )
        if manifest["formal_evaluation_result"] != aggregate_formal:
            issues.append(
                _issue(
                    "manifest.aggregate_formal_mismatch",
                    "$.formal_evaluation_result",
                    "Aggregate Run formal state must match every Episode result.",
                    file=file,
                )
            )
    issues.extend(_e31_artifact_inventory_issues(by_version, episodes))
    return issues


def validate_dataset(dataset: str | Path) -> ValidationReport:
    """Recursively validate JSON artifacts without database, model, or network access."""

    root = Path(dataset)
    if not root.exists():
        issue = _issue(
            "dataset.not_found", "$", "Dataset path does not exist.", file="."
        )
        return ValidationReport((issue,), DatasetStats.from_counts({}))
    if root.is_symlink():
        issue = _issue(
            "dataset.symlink",
            "$",
            "Dataset path must not be a symbolic link.",
            file=".",
        )
        return ValidationReport((issue,), DatasetStats.from_counts({}))
    if root.is_file():
        candidates = [root]
        base = root.parent
    elif root.is_dir():
        entries = sorted(root.rglob("*"), key=lambda path: path.as_posix())
        nested_symlinks = [path for path in entries if path.is_symlink()]
        if nested_symlinks:
            issues = tuple(
                _issue(
                    "dataset.nested_symlink",
                    "$",
                    "Dataset trees must not contain symbolic links.",
                    file=path.relative_to(root).as_posix(),
                )
                for path in nested_symlinks
            )
            return ValidationReport(
                tuple(sorted(issues, key=ValidationIssue.sort_key)),
                DatasetStats.from_counts({}),
            )
        candidates = [
            path for path in entries if path.is_file() and path.suffix == ".json"
        ]
        base = root
    else:
        issue = _issue(
            "dataset.invalid_path",
            "$",
            "Dataset path is not a file or directory.",
            file=".",
        )
        return ValidationReport((issue,), DatasetStats.from_counts({}))
    if not candidates:
        issue = _issue(
            "dataset.empty", "$", "Dataset contains no JSON documents.", file="."
        )
        return ValidationReport((issue,), DatasetStats.from_counts({}))

    issues: list[ValidationIssue] = []
    episodes: list[_EpisodeRecord] = []
    documents: list[tuple[str, dict[str, Any]]] = []
    saw_non_episode_artifact = False
    for path in candidates:
        file = path.relative_to(base).as_posix()
        value, load_issues = _load_json(path, file=file)
        issues.extend(load_issues)
        if load_issues:
            continue
        if isinstance(value, Mapping) and value.get("schema_version") in {
            "rule-result-v1",
            "integrity-result-v1",
            "rule-run-manifest-v1",
            "judge-result-v1",
            "judge-run-manifest-v1",
            "aggregate-result-v1",
            "aggregate-track-result-v1",
            "aggregate-run-manifest-v1",
            "case-spec-v1",
            "judge-reference-v1",
            "provider-attestation-v1",
            "runtime-failure-v1",
            "runtime-run-manifest-v2",
            "case-suite-manifest-v1",
            "rule-result-v2",
            "rule-run-manifest-v2",
            "judge-result-v2",
            "judge-run-manifest-v2",
            "aggregate-result-v2",
            "aggregate-track-result-v2",
            "aggregate-run-manifest-v2",
        }:
            saw_non_episode_artifact = True
        document_issues, record = _document_issues(value, file=file)
        issues.extend(document_issues)
        if not document_issues and isinstance(value, Mapping):
            documents.append((file, dict(value)))
        if record is not None:
            episodes.append(record)

    issues.extend(_artifact_inventory_issues(documents, episodes))

    if not episodes and not issues and not saw_non_episode_artifact:
        issues.append(
            _issue(
                "dataset.no_episodes",
                "$",
                "Dataset contains no Decision Episode documents.",
                file=".",
            )
        )

    by_episode_id: dict[str, list[_EpisodeRecord]] = defaultdict(list)
    by_scenario_family: dict[str, list[_EpisodeRecord]] = defaultdict(list)
    for record in episodes:
        by_episode_id[record.value["episode_id"]].append(record)
        by_scenario_family[record.value["scenario_family_id"]].append(record)
    for records in by_episode_id.values():
        for duplicate in sorted(records, key=lambda item: item.file)[1:]:
            issues.append(
                _issue(
                    "dataset.duplicate_episode_id",
                    "$.episode_id",
                    "Episode ID must be unique across the dataset.",
                    file=duplicate.file,
                )
            )
    for records in by_scenario_family.values():
        if len({item.value["split"] for item in records}) <= 1:
            continue
        for record in sorted(records, key=lambda item: item.file):
            issues.append(
                _issue(
                    "dataset.split_leakage",
                    "$.scenario_family_id",
                    "Scenario family appears in both dev and test splits.",
                    file=record.file,
                )
            )

    invalid_episode_files = {issue.file for issue in issues}
    counts: dict[str, int] = defaultdict(int)
    for record in episodes:
        if record.file not in invalid_episode_files:
            counts[record.value["track"]] += 1
    return ValidationReport(
        tuple(sorted(issues, key=ValidationIssue.sort_key)),
        DatasetStats.from_counts(counts),
    )
