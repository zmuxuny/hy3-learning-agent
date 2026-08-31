"""Recursive deterministic validation for Decision Episode datasets."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .canonical import CanonicalizationError, sha256_digest
from .errors import DatasetStats, ValidationIssue, ValidationReport
from .integrity import (
    context_summary_digest,
    decision_episode_digest,
    environment_manifest_digest,
    oracle_envelope_digest,
)
from .models import DATASET_DOCUMENT_MODELS
from .privacy import privacy_issues

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
_EVIDENCE_ROOTS = {
    "trigger",
    "state_before",
    "environment",
    "observable_trace",
    "result",
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
            _issue("file.unreadable", "$", "Dataset file is not valid UTF-8.", file=file)
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
            "greater_than_equal": ("schema.range", "Number is below the allowed range."),
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


def _resolve_path(document: Mapping[str, Any], path: str) -> bool:
    segments = path.split(".")
    if not segments or segments[0] not in _EVIDENCE_ROOTS:
        return False
    current: object = document
    for segment in segments:
        match = _PATH_SEGMENT.fullmatch(segment)
        if match is None or not isinstance(current, Mapping):
            return False
        name = match.group("name")
        if name not in current:
            return False
        current = current[name]
        for encoded_index in _PATH_INDEX.findall(match.group("indexes")):
            if not isinstance(current, list):
                return False
            index = int(encoded_index)
            if index >= len(current):
                return False
            current = current[index]
    return True


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
    for index, reference in enumerate(episode["state_before"]["context"]["source_refs"]):
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
        if any(trace[collection] for collection in trace if collection != "capture_mode"):
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
    if version != "decision-episode-v1":
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
        semantic_issues = _episode_semantic_issues(dict(value), file=source)
    except CanonicalizationError:
        semantic_issues = [
            _issue(
                "canonical.invalid_value",
                "$",
                "Episode contains a value outside canonical JSON.",
                file=source,
            )
        ]
    return tuple(sorted(semantic_issues, key=ValidationIssue.sort_key))


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
            _issue("schema.root_type", "$", "Dataset document must be an object.", file=file)
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
    }
    digest_field = digest_fields.get(version)
    if digest_field is not None:
        payload = {key: item for key, item in value.items() if key != digest_field}
        if value[digest_field] != sha256_digest(payload):
            return [
                _issue(
                    "digest.e1_artifact_mismatch",
                    f"$.{digest_field}",
                    "E1 artifact digest does not match canonical content.",
                    file=file,
                )
            ], None
    if version != "decision-episode-v1":
        return [], None
    episode = dict(value)
    try:
        semantic_issues = _episode_semantic_issues(episode, file=file)
    except CanonicalizationError:
        semantic_issues = [
            _issue(
                "canonical.invalid_value",
                "$",
                "Document contains a value outside canonical JSON.",
                file=file,
            )
        ]
    return semantic_issues, _EpisodeRecord(file=file, value=episode)


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
            "dataset.symlink", "$", "Dataset path must not be a symbolic link.", file="."
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
        candidates = [path for path in entries if path.is_file() and path.suffix == ".json"]
        base = root
    else:
        issue = _issue(
            "dataset.invalid_path", "$", "Dataset path is not a file or directory.", file="."
        )
        return ValidationReport((issue,), DatasetStats.from_counts({}))
    if not candidates:
        issue = _issue(
            "dataset.empty", "$", "Dataset contains no JSON documents.", file="."
        )
        return ValidationReport((issue,), DatasetStats.from_counts({}))

    issues: list[ValidationIssue] = []
    episodes: list[_EpisodeRecord] = []
    for path in candidates:
        file = path.relative_to(base).as_posix()
        value, load_issues = _load_json(path, file=file)
        issues.extend(load_issues)
        if load_issues:
            continue
        document_issues, record = _document_issues(value, file=file)
        issues.extend(document_issues)
        if record is not None:
            episodes.append(record)

    if not episodes and not issues:
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
