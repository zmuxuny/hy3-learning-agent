"""Deterministic before/after State Delta construction and operation alignment."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_json_bytes, sha256_digest

_NO_OPERATION_ENTITY_TYPES = {
    # Canonical messages, like notification deliveries, have no reversible Operation.
    # Their run and entity sources remain explicit in every delta entry.
    "chat_message",
    "session",
    "agent_run",
    "context_snapshot",
    "planning_intake",
    "plan_proposal",
    "artifact",
    "evidence_observation",
    "learning_event",
    "tool_invocation",
    "run_approval",
    "run_event",
    "operation",
    "proactive_decision",
    "intervention",
    "notification",
    "outbox_action",
    "outbox_receipt",
    "resource",
    "goal",
    "constraint",
}
_SYSTEM_MANAGED_FIELDS = {"updated_at", "created_at", "version"}
_FIELD_TOKEN = re.compile(r"([^.[\]]+)|\[(\d+)\]")


class DeltaConstructionError(RuntimeError):
    """A complete snapshot pair cannot be aligned into a truthful Delta."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _LeafChange:
    kind: str
    field_path: str
    before_path: str | None
    after_path: str | None
    before_presence: str
    before: Any
    after_presence: str
    after: Any


def _entity_map(snapshot: Mapping[str, Any]) -> dict[str, tuple[int, Mapping[str, Any]]]:
    result: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, entity in enumerate(snapshot["logical_entities"]):
        logical_id = entity["logical_id"]
        if logical_id in result:
            raise DeltaConstructionError("delta.duplicate_entity")
        result[logical_id] = index, entity
    return result


def _stable_list_index(values: Sequence[Any]) -> dict[str, tuple[int, Any]] | None:
    result: dict[str, tuple[int, Any]] = {}
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            return None
        identity: object | None = None
        for key in ("logical_id", "stable_id", "key"):
            if key in value and isinstance(value[key], str):
                identity = value[key]
                break
        if identity is None or identity in result:
            return None
        result[str(identity)] = (index, value)
    return result


def _path(root: str, entity_index: int, suffix: str) -> str:
    base = f"{root}.logical_entities[{entity_index}]"
    return base if suffix == "$entity" else f"{base}.{suffix}"


def _diff_value(
    before: Any,
    after: Any,
    *,
    field_path: str,
    before_root: str,
    after_root: str,
) -> list[_LeafChange]:
    if canonical_json_bytes(before) == canonical_json_bytes(after):
        return []
    changes: list[_LeafChange] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            child_path = f"{field_path}.{key}"
            if key not in before:
                changes.append(
                    _LeafChange(
                        "added", child_path, None, f"{after_root}.{key}",
                        "missing", None, "present", after[key],
                    )
                )
            elif key not in after:
                changes.append(
                    _LeafChange(
                        "removed", child_path, f"{before_root}.{key}", None,
                        "present", before[key], "missing", None,
                    )
                )
            else:
                changes.extend(
                    _diff_value(
                        before[key],
                        after[key],
                        field_path=child_path,
                        before_root=f"{before_root}.{key}",
                        after_root=f"{after_root}.{key}",
                    )
                )
        return changes
    if isinstance(before, list) and isinstance(after, list):
        before_items = _stable_list_index(before)
        after_items = _stable_list_index(after)
        if before_items is not None and after_items is not None:
            for identity in sorted(set(before_items) | set(after_items)):
                if identity not in before_items:
                    after_index, value = after_items[identity]
                    changes.append(
                        _LeafChange(
                            "added", f"{field_path}[{after_index}]", None,
                            f"{after_root}[{after_index}]", "missing", None,
                            "present", value,
                        )
                    )
                elif identity not in after_items:
                    before_index, value = before_items[identity]
                    changes.append(
                        _LeafChange(
                            "removed", f"{field_path}[{before_index}]",
                            f"{before_root}[{before_index}]", None, "present", value,
                            "missing", None,
                        )
                    )
                else:
                    before_index, before_value = before_items[identity]
                    after_index, after_value = after_items[identity]
                    changes.extend(
                        _diff_value(
                            before_value,
                            after_value,
                            field_path=f"{field_path}[{after_index}]",
                            before_root=f"{before_root}[{before_index}]",
                            after_root=f"{after_root}[{after_index}]",
                        )
                    )
            return changes
    return [
        _LeafChange(
            "changed", field_path, before_root, after_root,
            "present", before, "present", after,
        )
    ]


def _field_segments(path: str) -> list[str | int]:
    if path == "$entity":
        return []
    value = path.removeprefix("data.")
    segments: list[str | int] = []
    for match in _FIELD_TOKEN.finditer(value):
        key, index = match.groups()
        segments.append(key if key is not None else int(index))
    return segments


def _nested_value(value: Any, segments: Sequence[str | int]) -> tuple[bool, Any]:
    current = value
    for segment in segments:
        if isinstance(segment, str) and isinstance(current, Mapping) and segment in current or isinstance(segment, int) and isinstance(current, list) and segment < len(current):
            current = current[segment]
        else:
            return False, None
    return True, current


def _patch_value(patch: Mapping[str, Any], entity_type: str, path: str, entity_ref: str | None = None) -> tuple[bool, Any]:
    segments = _field_segments(path)
    if not segments or not isinstance(segments[0], str):
        return False, None
    first = segments[0]
    candidates: list[Any] = [patch]
    candidates.extend(
        item["changes"] for item in patch.get("entities", [])
        if isinstance(item, Mapping) and item.get(f"{entity_type}_ref") == entity_ref
        and isinstance(item.get("changes"), Mapping)
    )
    if isinstance(patch.get(entity_type), Mapping):
        candidates.append(patch[entity_type])
    if isinstance(patch.get("changes"), Mapping):
        candidates.append(patch["changes"])
    award = patch.get("award")
    if isinstance(award, Mapping):
        award_key = {
            "learner": "profile",
            "activity_day": "day",
        }.get(entity_type)
        if award_key is not None and isinstance(award.get(award_key), Mapping):
            candidates.append(award[award_key])
    for candidate in candidates:
        if isinstance(candidate, Mapping) and first in candidate:
            return _nested_value(candidate[first], segments[1:])
    return False, None


def _entity_transition_marked(
    operation: Mapping[str, Any],
    *,
    entity_type: str,
    entity_ref: str,
    added: bool,
) -> bool:
    forward = operation["data"]["forward_patch"]
    inverse = operation["data"]["inverse_patch"]
    if added:
        if (
            forward.get("created_ref") == entity_ref
            and inverse.get("delete_ref") == entity_ref
        ):
            return True
        forward_award = forward.get("award")
        inverse_award = inverse.get("award")
        if not isinstance(forward_award, Mapping) or not isinstance(
            inverse_award, Mapping
        ):
            return False
        if entity_type == "achievement":
            achievements = forward_award.get("achievements")
            deleted = inverse_award.get("delete_achievement_refs")
            return (
                isinstance(achievements, list)
                and any(
                    isinstance(item, Mapping)
                    and item.get("achievement_ref") == entity_ref
                    for item in achievements
                )
                and isinstance(deleted, list)
                and entity_ref in deleted
            )
        if entity_type == "activity_day":
            day = forward_award.get("day")
            return (
                isinstance(day, Mapping)
                and day.get("activity_day_ref") == entity_ref
                and inverse_award.get("day_ref") == entity_ref
                and "day" in inverse_award
                and inverse_award["day"] is None
            )
        return False
    return (
        forward.get("delete_ref") == entity_ref
        and inverse.get("created_ref") == entity_ref
    )


def _operation_context(
    after_entities: Mapping[str, tuple[int, Mapping[str, Any]]]
) -> list[Mapping[str, Any]]:
    event_rows = [
        entity
        for _, entity in after_entities.values()
        if entity["entity_type"] == "run_event"
    ]
    call_sequence = {
        entity["data"]["payload"]["tool_call_id"]: entity["data"]["sequence"]
        for entity in event_rows
        if entity["data"]["event_type"] == "tool.started"
        and isinstance(entity["data"]["payload"].get("tool_call_id"), str)
    }
    invocation_sequence = {
        entity["logical_id"]: call_sequence.get(
            entity["data"]["tool_call_id"], 2**31
        )
        for _, entity in after_entities.values()
        if entity["entity_type"] == "tool_invocation"
    }
    operations = [
        entity
        for _, entity in after_entities.values()
        if entity["entity_type"] == "operation"
    ]
    return sorted(
        operations,
        key=lambda item: (
            invocation_sequence.get(item["data"]["invocation_ref"], 2**31),
            item["data"].get("created_at") or "",
            item["logical_id"],
        ),
    )


def _affected_refs(
    operation: Mapping[str, Any],
    after_entities: Mapping[str, tuple[int, Mapping[str, Any]]],
) -> list[str]:
    data = operation["data"]
    affected = [data["primary_entity_ref"]]
    patch = data["forward_patch"]
    for item in patch.get("entities", []):
        if isinstance(item, Mapping):
            affected.extend(ref for key, ref in item.items()
                            if key.endswith("_ref") and isinstance(ref, str)
                            and ref in after_entities and ref not in affected)
    explicit = patch.get("affected")
    if isinstance(explicit, Mapping):
        for key in ("plan_ref", "stage_ref", "task_ref"):
            reference = explicit.get(key)
            if (
                isinstance(reference, str)
                and reference in after_entities
                and reference not in affected
            ):
                affected.append(reference)
    present_types = {item[1]["entity_type"] for item in after_entities.values()}
    for entity_type in sorted(set(patch) & present_types):
        candidates = [
            logical_id
            for logical_id, (_, entity) in after_entities.items()
            if entity["entity_type"] == entity_type
        ]
        if len(candidates) == 1 and candidates[0] not in affected:
            affected.append(candidates[0])
    primary = after_entities.get(data["primary_entity_ref"])
    if primary is not None and isinstance(patch.get("task"), Mapping):
        task_ref = primary[1]["data"].get("task_ref")
        if isinstance(task_ref, str) and task_ref not in affected:
            affected.append(task_ref)
    award = patch.get("award")
    if isinstance(award, Mapping):
        if isinstance(award.get("profile"), Mapping):
            learner_ref = primary[1].get("scope_ref") if primary is not None else None
            if isinstance(learner_ref, str) and learner_ref not in affected:
                affected.append(learner_ref)
        day = award.get("day")
        if isinstance(day, Mapping):
            day_ref = day.get("activity_day_ref")
            if isinstance(day_ref, str) and day_ref not in affected:
                affected.append(day_ref)
        achievements = award.get("achievements")
        if isinstance(achievements, list):
            for item in achievements:
                reference = item.get("achievement_ref") if isinstance(item, Mapping) else None
                if isinstance(reference, str) and reference not in affected:
                    affected.append(reference)
    return affected


def operation_affected_refs(
    after_snapshot: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Return stable affected-entity refs for each captured Operation."""

    entities = _entity_map(after_snapshot)
    return {
        operation["logical_id"]: _affected_refs(operation, entities)
        for operation in _operation_context(entities)
    }


def _alignment(
    *,
    entity_ref: str,
    entity_type: str,
    field_path: str,
    before: Any,
    after: Any,
    operations: Sequence[Mapping[str, Any]],
    affected: Mapping[str, list[str]],
) -> tuple[list[str], str]:
    candidates = [
        operation
        for operation in operations
        if entity_ref in affected[operation["logical_id"]]
    ]
    if not candidates:
        if entity_type in _NO_OPERATION_ENTITY_TYPES:
            return [], "not_applicable"
        return [], "unattributed"
    if field_path == "$entity":
        exact = [
            operation["logical_id"]
            for operation in candidates
            if _entity_transition_marked(
                operation,
                entity_type=entity_type,
                entity_ref=entity_ref,
                added=before is None and after is not None,
            )
        ]
        if exact:
            return exact, "matched"
        return [operation["logical_id"] for operation in candidates], "mismatch"
    chained: list[str] = []
    current = before
    for operation in candidates:
        forward_found, forward_value = _patch_value(
            operation["data"]["forward_patch"], entity_type, field_path, entity_ref
        )
        inverse_found, inverse_value = _patch_value(
            operation["data"]["inverse_patch"], entity_type, field_path, entity_ref
        )
        if not forward_found:
            continue
        if inverse_found and canonical_json_bytes(inverse_value) != canonical_json_bytes(
            current
        ):
            chained = []
            break
        current = forward_value
        chained.append(operation["logical_id"])
    if chained and canonical_json_bytes(current) == canonical_json_bytes(after):
        return chained, "matched"
    exact: list[str] = []
    for operation in candidates:
        forward_found, forward_value = _patch_value(
            operation["data"]["forward_patch"], entity_type, field_path, entity_ref
        )
        inverse_found, inverse_value = _patch_value(
            operation["data"]["inverse_patch"], entity_type, field_path, entity_ref
        )
        field = field_path.removeprefix("data.").split(".", 1)[0].split("[", 1)[0]
        if (
            forward_found
            and canonical_json_bytes(forward_value) == canonical_json_bytes(after)
            and (
                not inverse_found
                or canonical_json_bytes(inverse_value) == canonical_json_bytes(before)
            )
        ) or field in _SYSTEM_MANAGED_FIELDS:
            exact.append(operation["logical_id"])
    if exact:
        return exact, "matched"
    return [operation["logical_id"] for operation in candidates], "mismatch"


def build_state_delta(
    before_snapshot: Mapping[str, Any],
    after_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two complete snapshots; tool names never determine the changes."""

    if before_snapshot["capture_status"] != "complete" or after_snapshot[
        "capture_status"
    ] != "complete":
        raise DeltaConstructionError("delta.snapshot_incomplete")
    before_entities = _entity_map(before_snapshot)
    after_entities = _entity_map(after_snapshot)
    operations = _operation_context(after_entities)
    affected = {
        operation["logical_id"]: _affected_refs(operation, after_entities)
        for operation in operations
    }
    leaves: list[tuple[str, str, _LeafChange]] = []
    unchanged: list[str] = []
    compared = sorted(set(before_entities) & set(after_entities))
    for logical_id in sorted(set(before_entities) | set(after_entities)):
        if logical_id not in before_entities:
            after_index, after_entity = after_entities[logical_id]
            leaves.append(
                (
                    logical_id,
                    after_entity["entity_type"],
                    _LeafChange(
                        "added", "$entity", None,
                        _path("state_after", after_index, "$entity"), "missing", None,
                        "present", after_entity["data"],
                    ),
                )
            )
            continue
        if logical_id not in after_entities:
            before_index, before_entity = before_entities[logical_id]
            leaves.append(
                (
                    logical_id,
                    before_entity["entity_type"],
                    _LeafChange(
                        "removed", "$entity",
                        _path("state_before", before_index, "$entity"), None,
                        "present", before_entity["data"], "missing", None,
                    ),
                )
            )
            continue
        before_index, before_entity = before_entities[logical_id]
        after_index, after_entity = after_entities[logical_id]
        identity_shape = (before_entity["entity_type"], before_entity["source"], before_entity["scope_ref"])
        if identity_shape != (
            after_entity["entity_type"], after_entity["source"], after_entity["scope_ref"]
        ):
            raise DeltaConstructionError("delta.identity_shape_changed")
        entity_leaves = _diff_value(
            before_entity["data"],
            after_entity["data"],
            field_path="data",
            before_root=_path("state_before", before_index, "data"),
            after_root=_path("state_after", after_index, "data"),
        )
        if not entity_leaves:
            unchanged.append(logical_id)
        leaves.extend(
            (logical_id, before_entity["entity_type"], leaf) for leaf in entity_leaves
        )
    leaves.sort(
        key=lambda item: (
            item[0], item[2].field_path, item[2].kind,
            canonical_json_bytes(item[2].before), canonical_json_bytes(item[2].after),
        )
    )
    changes: list[dict[str, Any]] = []
    errors: set[str] = set()
    for ordinal, (entity_ref, entity_type, leaf) in enumerate(leaves, 1):
        operation_refs, alignment = _alignment(
            entity_ref=entity_ref,
            entity_type=entity_type,
            field_path=leaf.field_path,
            before=leaf.before,
            after=leaf.after,
            operations=operations,
            affected=affected,
        )
        if alignment in {"unattributed", "mismatch", "ambiguous"}:
            errors.add(f"delta.operation_{alignment}.{entity_type}")
        source_refs = [
            {"source_type": "operation", "ref": reference}
            for reference in operation_refs
        ]
        if not source_refs:
            entity = (
                after_entities.get(entity_ref) or before_entities.get(entity_ref)
            )[1]
            data = entity["data"]
            typed_candidates = (
                ("runtime", entity_ref)
                if entity_type == "agent_run"
                else ("tool_invocation", entity_ref)
                if entity_type == "tool_invocation"
                else ("run_event", entity_ref)
                if entity_type == "run_event"
                else ("operation", entity_ref)
                if entity_type == "operation"
                else None
            )
            source_refs = []
            if typed_candidates is not None:
                source_refs.append(
                    {
                        "source_type": typed_candidates[0],
                        "ref": typed_candidates[1],
                    }
                )
            for source_type, key in (
                ("tool_invocation", "invocation_ref"),
                ("runtime", "run_ref"),
                ("runtime", "source_run_ref"),
            ):
                reference = data.get(key) if isinstance(data, Mapping) else None
                candidate = {"source_type": source_type, "ref": reference}
                if isinstance(reference, str) and candidate not in source_refs:
                    source_refs.append(candidate)
            entity_source = {"source_type": "entity", "ref": entity_ref}
            if entity_source not in source_refs:
                source_refs.append(entity_source)
        changes.append(
            {
                "change_id": f"change:{ordinal:04d}",
                "ordinal": ordinal,
                "kind": leaf.kind,
                "entity_ref": entity_ref,
                "field_path": leaf.field_path,
                "before_path": leaf.before_path,
                "after_path": leaf.after_path,
                "before": {"presence": leaf.before_presence, "value": leaf.before},
                "after": {"presence": leaf.after_presence, "value": leaf.after},
                "source_refs": source_refs,
                "operation_refs": operation_refs,
                "operation_alignment": alignment,
            }
        )
    delta = {
        "capture_status": "complete" if not errors else "incomplete",
        "before_snapshot_sha256": before_snapshot["snapshot_sha256"],
        "after_snapshot_sha256": after_snapshot["snapshot_sha256"],
        "changes": changes,
        "compared_entity_refs": compared,
        "unchanged_entity_refs": unchanged,
        "error_codes": sorted(errors),
        "delta_sha256": "0" * 64,
    }
    delta["delta_sha256"] = sha256_digest(
        {key: value for key, value in delta.items() if key != "delta_sha256"}
    )
    return delta
