"""Allowlisted production-state snapshots for DecisionEpisode v2."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal

from .canonical import sha256_digest
from .integrity import context_summary_digest
from .normalizers import (
    IdentityCandidate,
    NormalizationError,
    StableIdentityRegistry,
    normalize_json,
    normalize_rfc3339,
)

COLLECTOR_VERSION = "e2-snapshot-collector-v1"

# This is both documentation and the closed public surface of the collector.
# ORM ``__dict__`` and unlisted columns are never inspected or serialized.
FIELD_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "learner": (
        "display_name",
        "timezone",
        "agent_style",
        "preferences",
        "quiet_hours",
        "daily_notification_limit",
        "xp",
        "level",
        "streak_days",
        "follow_up_behavior",
        "proactive_paused",
        "declared_context",
        "created_at",
        "updated_at",
    ),
    "session": (
        "plan_ref",
        "title",
        "summary",
        "archived_at",
        "created_at",
        "updated_at",
    ),
    "agent_run": (
        "session_ref",
        "plan_ref",
        "parent_run_ref",
        "trigger",
        "objective",
        "status",
        "phase",
        "state_version",
        "attempt",
        "retry_count",
        "available_at",
        "status_reason",
        "model",
        "cancel_requested",
        "pending_approval",
        "budget_usage",
        "output",
        "execution_mode",
        "proactive_candidate_state",
        "proactive_candidate_key",
        "proactive_candidate_kind",
        "proactive_candidate_payload",
        "proactive_candidate_digest",
        "proactive_detected_at",
        "created_plan_ref",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "context_snapshot": (
        "plan_ref",
        "session_ref",
        "run_ref",
        "estimated_tokens",
        "context_generation",
        "snapshot_version",
        "assembler_version",
        "context_digest",
        "source_digest",
        "validity_state",
        "invalidated_at",
        "invalidation_reason",
        "created_at",
    ),
    "planning_intake": (
        "session_ref",
        "run_ref",
        "goal",
        "confirmed_facts",
        "open_questions",
        "readiness",
        "readiness_confidence",
        "rationale",
        "created_at",
        "updated_at",
    ),
    "plan": (
        "title",
        "description",
        "goal",
        "current_level",
        "deadline",
        "weekly_minutes",
        "preferences",
        "expected_outcome",
        "available_resources",
        "avoid_methods",
        "status",
        "archived_from_status",
        "version",
        "progress",
        "created_at",
        "updated_at",
    ),
    "stage": ("plan_ref", "title", "description", "objectives", "position", "status"),
    "task": (
        "stage_ref",
        "title",
        "description",
        "kind",
        "status",
        "is_core",
        "evidence_required",
        "estimated_minutes",
        "position",
        "due_at",
        "completed_at",
        "review_due_at",
        "resource_url",
        "task_metadata",
    ),
    "plan_proposal": (
        "session_ref",
        "run_ref",
        "title",
        "rationale",
        "plan_payload",
        "specialist_reports",
        "status",
        "plan_ref",
        "decided_at",
        "created_at",
        "updated_at",
    ),
    "submission": (
        "plan_ref",
        "task_ref",
        "run_ref",
        "submission_type",
        "content",
        "artifacts",
        "status",
        "score",
        "feedback",
        "checked_at",
        "created_at",
    ),
    "review": ("plan_ref", "task_ref", "due_at", "review_type", "status", "created_at"),
    "quiz": (
        "plan_ref",
        "task_ref",
        "run_ref",
        "prompt",
        "rubric",
        "answer",
        "score",
        "feedback",
        "evidence",
        "status",
        "created_at",
        "graded_at",
    ),
    "achievement": (
        "key",
        "title",
        "description",
        "badge_kind",
        "badge_image_url",
        "unlocked_at",
    ),
    "activity_day": (
        "date",
        "xp",
        "completed_tasks",
        "passed_quizzes",
    ),
    "artifact": (
        "artifact_type",
        "source_identity",
        "title",
        "content_hash",
        "size_bytes",
        "artifact_metadata",
        "snapshot_sha256",
        "storage_state",
        "envelope_version",
        "plan_ref",
        "task_ref",
        "run_ref",
        "session_ref",
        "request_digest",
        "created_at",
    ),
    "evidence_observation": (
        "source_type",
        "source_identity",
        "run_ref",
        "session_ref",
        "plan_ref",
        "task_ref",
        "fact_kind",
        "target_observation_ref",
        "reason_code",
        "evidence_role",
        "eligibility_stage",
        "eligibility_reason",
        "eligibility_policy_version",
        "counts_as_success",
        "outcome",
        "normalized_score",
        "is_correct",
        "assistance_level",
        "transfer_level",
        "rubric_snapshot",
        "evaluator",
        "payload",
        "occurred_at",
        "recorded_at",
        "schema_version",
        "request_digest",
    ),
    "learning_event": (
        "plan_ref",
        "task_ref",
        "run_ref",
        "event_type",
        "payload",
        "schema_version",
        "occurred_at",
        "invalidated_at",
        "invalidation_reason",
        "created_at",
    ),
    "tool_invocation": (
        "run_ref",
        "tool_call_id",
        "tool_name",
        "args_hash",
        "request_digest",
        "canonical_args",
        "effect_kind",
        "status",
        "result_payload",
        "attempt",
        "version",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "run_approval": (
        "run_ref",
        "invocation_ref",
        "tool_call_id",
        "tool_name",
        "remaining_tool_call_count",
        "reason",
        "decision",
        "decided_at",
        "consumed_at",
        "created_at",
    ),
    "run_event": ("run_ref", "sequence", "event_type", "payload", "created_at"),
    "operation": (
        "run_ref",
        "invocation_ref",
        "tool_name",
        "primary_entity_type",
        "primary_entity_ref",
        "forward_patch",
        "inverse_patch",
        "status",
        "created_at",
        "undone_at",
    ),
    "proactive_decision": (
        "plan_ref",
        "run_ref",
        "invocation_ref",
        "candidate_key",
        "candidate_kind",
        "candidate_payload",
        "candidate_digest",
        "policy_version",
        "policy_digest",
        "status",
        "outcome",
        "reason_code",
        "next_eligible_at",
        "decision_payload",
        "decision_digest",
        "decided_at",
        "created_at",
    ),
    "intervention": (
        "decision_ref",
        "run_ref",
        "invocation_ref",
        "plan_ref",
        "session_ref",
        "title",
        "body",
        "content_digest",
        "reason_code",
        "state",
        "outcome",
        "read_at",
        "archived_at",
        "resolved_at",
        "created_at",
    ),
    "notification": (
        "run_ref",
        "invocation_ref",
        "session_ref",
        "plan_ref",
        "intervention_ref",
        "delivery_generation",
        "legacy_unlinked",
        "channel",
        "title",
        "body",
        "status",
        "sent_at",
        "read_at",
        "archived_at",
        "created_at",
    ),
    "outbox_action": (
        "run_ref",
        "invocation_ref",
        "notification_ref",
        "operation_ref",
        "action_key",
        "request_digest",
        "effect_kind",
        "destination",
        "status",
        "attempt",
        "version",
        "available_at",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "outbox_receipt": (
        "outbox_action_ref",
        "action_key",
        "status",
        "provider_id",
        "response",
        "accepted_at",
        "created_at",
    ),
    "goal": ("declared_context",),
    "constraint": ("declared_context",),
    "resource": ("snapshot_version", "snapshot_digest"),
}
FIELD_ALLOWLIST_SHA256 = sha256_digest(FIELD_ALLOWLISTS)
ENTITY_TYPE_ORDER = tuple(FIELD_ALLOWLISTS)

_REFERENCE_TYPES = {
    "owner_id": "learner",
    "session_id": "session",
    "plan_id": "plan",
    "stage_id": "stage",
    "task_id": "task",
    "submission_id": "submission",
    "run_id": "agent_run",
    "child_run_id": "agent_run",
    "source_run_id": "agent_run",
    "parent_run_id": "agent_run",
    "invocation_id": "tool_invocation",
    "source_invocation_id": "tool_invocation",
    "notification_id": "notification",
    "operation_id": "operation",
    "intervention_id": "intervention",
    "proactive_decision_id": "proactive_decision",
    "proposal_id": "plan_proposal",
    "outbox_action_id": "outbox_action",
    "target_observation_id": "evidence_observation",
    "created_plan_id": "plan",
    "day_id": "activity_day",
    "artifact_id": "artifact",
    "achievement_id": "achievement",
    "evidence_observation_id": "evidence_observation",
    "learning_event_id": "learning_event",
    "quiz_id": "quiz",
    "review_id": "review",
    "snapshot_id": "context_snapshot",
    "canonical_message_id": "notification",
    "reply_to_intervention_id": "intervention",
    "approval_id": "run_approval",
}
_CONTAINER_ID_TYPES = {
    "achievements": "achievement",
    "artifacts": "artifact",
    "notifications": "notification",
    "operations": "operation",
    "plans": "plan",
    "quizzes": "quiz",
    "reviews": "review",
    "stages": "stage",
    "submissions": "submission",
    "tasks": "task",
    "current_task": "task",
    "recommended_next": "task",
    "overdue_tasks": "task",
    "blocked_tasks": "task",
    "scheduled_reviews": "review",
    "recent_submissions": "submission",
}
_TOOL_RESULT_ID_TYPES = {
    "plan_get": "plan", "submission_get": "submission", "quiz_get": "quiz",
    "plan.get": "plan", "submission.get": "submission", "quiz.get": "quiz",
    "planning_intake_get": "planning_intake",
    "planning.intake.get": "planning_intake",
}
_REFERENCE_LIST_TYPES = {
    "saved_resource_ids": "resource",
    "unscoped_observation_ids": "evidence_observation",
    "source_run_ids": "agent_run",
}
_PUBLIC_ID_FIELDS = {
    "call_id",
    "parent_call_id",
    "provider_id",
    "provider_request_id",
    "tool_call_id",
    "trigger_id",
}
_TASK_EVENT_SOURCE = re.compile(r"^task:([^:]+):event:([^:]+)$")
_OPERATION_UNDO_SOURCE = re.compile(r"^([^:]+):undo:(\d+):([^:]+)$")


class SnapshotCollectionError(RuntimeError):
    """The production state cannot be represented by the closed collector."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SnapshotCapture:
    document: dict[str, Any]


async def _rows(db: Any, model: Any, predicate: Any | None = None) -> list[Any]:
    select = import_module("sqlalchemy").select
    statement = select(model)
    if predicate is not None:
        statement = statement.where(predicate)
    return list((await db.execute(statement)).scalars())


async def _load_rows(
    session_factory: Any,
    *,
    owner_id: str,
    run_id: str,
) -> dict[str, list[Any]]:
    models = import_module("app.models")
    async with session_factory() as db:
        owners = await _rows(db, models.Owner, models.Owner.id == owner_id)
        profiles = await _rows(
            db, models.UserProfile, models.UserProfile.owner_id == owner_id
        )
        sessions = await _rows(db, models.Session, models.Session.owner_id == owner_id)
        plans = await _rows(db, models.Plan, models.Plan.owner_id == owner_id)
        plan_ids = [row.id for row in plans]
        stages = (
            await _rows(db, models.Stage, models.Stage.plan_id.in_(plan_ids))
            if plan_ids
            else []
        )
        stage_ids = [row.id for row in stages]
        tasks = (
            await _rows(db, models.Task, models.Task.stage_id.in_(stage_ids))
            if stage_ids
            else []
        )
        owner_runs = await _rows(
            db,
            models.AgentRun,
            models.AgentRun.owner_id == owner_id,
        )
        scoped_run_ids = {run_id}
        while True:
            descendants = {
                row.id for row in owner_runs if row.parent_run_id in scoped_run_ids
            }
            expanded = scoped_run_ids | descendants
            if expanded == scoped_run_ids:
                break
            scoped_run_ids = expanded
        agent_runs = [row for row in owner_runs if row.id in scoped_run_ids]
        ordered_run_ids = sorted(scoped_run_ids)
        outbox = await _rows(
            db, models.OutboxAction, models.OutboxAction.owner_id == owner_id
        )
        outbox_ids = [row.id for row in outbox]
        return {
            "learner": owners,
            "profiles": profiles,
            "session": sessions,
            "agent_run": agent_runs,
            "context_snapshot": await _rows(
                db, models.ContextSnapshot, models.ContextSnapshot.owner_id == owner_id
            ),
            "planning_intake": await _rows(
                db, models.PlanningIntake, models.PlanningIntake.owner_id == owner_id
            ),
            "plan": plans,
            "stage": stages,
            "task": tasks,
            "plan_proposal": await _rows(
                db, models.PlanProposal, models.PlanProposal.owner_id == owner_id
            ),
            "submission": await _rows(
                db, models.TaskSubmission, models.TaskSubmission.owner_id == owner_id
            ),
            "review": await _rows(
                db, models.ReviewSchedule, models.ReviewSchedule.owner_id == owner_id
            ),
            "quiz": await _rows(db, models.Quiz, models.Quiz.owner_id == owner_id),
            "achievement": await _rows(
                db, models.Achievement, models.Achievement.owner_id == owner_id
            ),
            "activity_day": await _rows(
                db, models.ActivityDay, models.ActivityDay.owner_id == owner_id
            ),
            "artifact": await _rows(
                db, models.Artifact, models.Artifact.owner_id == owner_id
            ),
            "evidence_observation": await _rows(
                db,
                models.EvidenceObservation,
                models.EvidenceObservation.owner_id == owner_id,
            ),
            "learning_event": await _rows(
                db, models.LearningEvent, models.LearningEvent.owner_id == owner_id
            ),
            "tool_invocation": await _rows(
                db,
                models.ToolInvocation,
                models.ToolInvocation.run_id.in_(ordered_run_ids),
            ),
            "run_approval": await _rows(
                db,
                models.RunApproval,
                models.RunApproval.run_id.in_(ordered_run_ids),
            ),
            "run_event": await _rows(
                db,
                models.RunEvent,
                models.RunEvent.run_id.in_(ordered_run_ids),
            ),
            "operation": await _rows(
                db,
                models.Operation,
                models.Operation.run_id.in_(ordered_run_ids),
            ),
            "proactive_decision": await _rows(
                db,
                models.ProactiveDecision,
                models.ProactiveDecision.owner_id == owner_id,
            ),
            "intervention": await _rows(
                db, models.Intervention, models.Intervention.owner_id == owner_id
            ),
            "notification": await _rows(
                db, models.Notification, models.Notification.owner_id == owner_id
            ),
            "outbox_action": outbox,
            "outbox_receipt": (
                await _rows(
                    db,
                    models.OutboxReceipt,
                    models.OutboxReceipt.outbox_action_id.in_(outbox_ids),
                )
                if outbox_ids
                else []
            ),
        }


def _raw_id(entity_type: str, row: Any) -> object:
    if entity_type == "planning_intake":
        return row.session_id
    return row.id


def _semantic_key(
    entity_type: str, row: Any, registry: StableIdentityRegistry
) -> object:
    fields: dict[str, tuple[str, ...]] = {
        "learner": ("display_name", "timezone", "created_at"),
        "session": ("title", "created_at"),
        "agent_run": ("trigger", "objective", "created_at"),
        "context_snapshot": (
            "context_generation",
            "snapshot_version",
            "context_digest",
        ),
        "planning_intake": ("goal", "created_at"),
        "plan": ("title", "created_at"),
        "stage": ("position", "title"),
        "task": ("position", "title"),
        "plan_proposal": ("title", "created_at"),
        "submission": ("submission_type", "created_at"),
        "review": ("due_at", "review_type", "created_at"),
        "quiz": ("prompt", "created_at"),
        "achievement": ("key", "unlocked_at"),
        "activity_day": ("date",),
        "artifact": ("artifact_type", "content_hash", "created_at"),
        "evidence_observation": (
            "source_type",
            "fact_kind",
            "outcome",
            "occurred_at",
            "recorded_at",
        ),
        "learning_event": ("event_type", "occurred_at", "created_at"),
        "tool_invocation": ("tool_call_id", "tool_name", "created_at"),
        "run_approval": ("tool_call_id", "tool_name", "created_at"),
        "run_event": ("sequence",),
        "proactive_decision": ("candidate_key", "created_at"),
        "intervention": ("content_digest", "created_at"),
        "notification": ("channel", "delivery_generation", "created_at"),
        "outbox_action": ("destination", "request_digest", "created_at"),
        "outbox_receipt": ("action_key", "accepted_at"),
    }
    if entity_type == "session":
        return {
            "title": row.title,
            "created_at": row.created_at,
        }
    if entity_type == "agent_run":
        return {
            "parent_run_ref": registry.resolve("agent_run", row.parent_run_id),
            "trigger": row.trigger,
            "objective": row.objective,
            "created_at": row.created_at,
        }
    if entity_type == "stage":
        return {
            "plan_ref": registry.resolve("plan", row.plan_id),
            "position": row.position,
            "title": row.title,
        }
    if entity_type == "task":
        return {
            "stage_ref": registry.resolve("stage", row.stage_id),
            "position": row.position,
            "title": row.title,
        }
    if entity_type == "submission":
        return {
            "plan_ref": registry.resolve("plan", row.plan_id),
            "task_ref": registry.resolve("task", row.task_id),
            "submission_type": row.submission_type,
            "content": row.content,
            "created_at": row.created_at,
        }
    if entity_type == "learning_event":
        return {
            "plan_ref": registry.resolve("plan", row.plan_id),
            "task_ref": registry.resolve("task", row.task_id),
            "run_ref": registry.resolve("agent_run", row.run_id),
            "event_type": row.event_type,
            "payload": normalize_json(row.payload),
            "occurred_at": row.occurred_at,
        }
    if entity_type == "run_event":
        return {
            "run_ref": registry.resolve("agent_run", row.run_id),
            "sequence": row.sequence,
        }
    if entity_type == "operation":
        target_type = {
            "submission": "submission",
            "plan": "plan",
            "stage": "stage",
            "task": "task",
            "review_schedule": "review",
            "quiz": "quiz",
            "plan_proposal": "plan_proposal",
        }.get(row.entity_type)
        if target_type is None:
            raise SnapshotCollectionError("snapshot.unsupported_operation_entity")
        target_raw: object = (
            int(row.entity_id) if row.entity_id.isdigit() else row.entity_id
        )
        return {
            "tool_name": row.tool_name,
            "primary_entity_ref": registry.resolve(target_type, target_raw),
            "invocation_ref": registry.resolve("tool_invocation", row.invocation_id),
            "forward_patch": _operation_patch(
                row.forward_patch,
                registry,
                primary_type=target_type,
                primary_raw=target_raw,
            ),
            "inverse_patch": _operation_patch(
                row.inverse_patch,
                registry,
                primary_type=target_type,
                primary_raw=target_raw,
            ),
            "created_at": row.created_at,
        }
    try:
        return {name: getattr(row, name) for name in fields[entity_type]}
    except (AttributeError, KeyError) as exc:
        raise SnapshotCollectionError("snapshot.unsupported_entity_shape") from exc


def _register_identities(
    registry: StableIdentityRegistry,
    rows: dict[str, list[Any]],
) -> None:
    for entity_type in ENTITY_TYPE_ORDER:
        if entity_type in {"goal", "constraint", "resource"}:
            continue
        if entity_type == "agent_run":
            remaining = list(rows.get(entity_type, []))
            while remaining:
                ready = [
                    row
                    for row in remaining
                    if row.parent_run_id is None
                    or registry.is_registered("agent_run", row.parent_run_id)
                ]
                if not ready:
                    raise SnapshotCollectionError("snapshot.run_hierarchy_invalid")
                registry.register_many(
                    entity_type,
                    [
                        IdentityCandidate(
                            raw_id=_raw_id(entity_type, row),
                            semantic_key=_semantic_key(entity_type, row, registry),
                        )
                        for row in ready
                    ],
                )
                ready_ids = {row.id for row in ready}
                remaining = [row for row in remaining if row.id not in ready_ids]
            continue
        entity_rows = rows.get(entity_type, [])
        if entity_type in {"learning_event", "evidence_observation"}:
            # Frozen clocks can contain several equal append-only events. The
            # committed insertion order distinguishes occurrences; raw DB IDs
            # determine order only and never become public logical identities.
            occurrences: dict[str, int] = {}
            candidates = []
            for row in sorted(entity_rows, key=lambda item: item.id):
                semantic = _semantic_key(entity_type, row, registry)
                digest = sha256_digest(normalize_json(semantic))
                occurrences[digest] = occurrences.get(digest, 0) + 1
                candidates.append(IdentityCandidate(raw_id=_raw_id(entity_type, row),
                                  semantic_key={**semantic, "occurrence": occurrences[digest]}))
            registry.register_many(entity_type, candidates)
            continue
        candidates = [
            IdentityCandidate(
                raw_id=_raw_id(entity_type, row),
                semantic_key=_semantic_key(entity_type, row, registry),
            )
            for row in rows.get(entity_type, [])
        ]
        registry.register_many(entity_type, candidates)


def _ref(
    registry: StableIdentityRegistry,
    entity_type: str,
    value: object | None,
) -> str | None:
    return registry.resolve(entity_type, value)


def _normalize_references(value: object, registry: StableIdentityRegistry) -> Any:
    normalized = normalize_json(value)

    def visit(item: Any, *, container_key: str | None = None, tool_name: str | None = None, argument_context: bool = False) -> Any:
        if isinstance(item, dict):
            declared_tool = item.get("tool_name", item.get("name"))
            if isinstance(declared_tool, str) and declared_tool in _TOOL_RESULT_ID_TYPES:
                tool_name = declared_tool
            result: dict[str, Any] = {}
            for key, child in item.items():
                entity_type = _REFERENCE_TYPES.get(key)
                public_key = key
                if key == "logical_id" and container_key == "entities":
                    # Public Case context is echoed by profile_get. Its entity
                    # keys already name declared logical facts, not ORM rows.
                    declared_type = item.get("entity_type")
                    if (not isinstance(declared_type, str)
                            or not isinstance(child, str)
                            or child not in registry.declared(declared_type)):
                        raise SnapshotCollectionError("snapshot.undeclared_logical_reference")
                    result[key] = child
                    continue
                if key == "id":
                    if container_key == "open_questions":
                        result["question_key"] = child  # A public form key, not an ORM ID.
                        continue
                    entity_type = _CONTAINER_ID_TYPES.get(container_key or "")
                    if entity_type is None and container_key == "data":
                        entity_type = _TOOL_RESULT_ID_TYPES.get(tool_name or "")
                    if entity_type is None and not argument_context:
                        raise SnapshotCollectionError("snapshot.unsupported_nested_id")
                    if entity_type is not None:
                        try:
                            child = registry.resolve(entity_type, child) if child is not None else None
                            public_key = f"{entity_type}_ref"
                        except NormalizationError:
                            if not argument_context:
                                raise
                elif entity_type is not None:
                    public_key = f"{key.removesuffix('_id')}_ref"
                    if (key == "session_id" and child == "" and container_key == "data"
                            and tool_name in {"planning_intake_get", "planning.intake.get"}
                            and item.get("exists") is False):
                        # The product's absent-intake response uses an empty
                        # sentinel; there is no row identity to bind here.
                        child = None
                    if child is not None:
                        try:
                            child = registry.resolve(entity_type, child)
                        except NormalizationError:
                            if not argument_context:
                                raise
                            public_key = key  # Preserve the invalid attempted target as input.
                elif key in _REFERENCE_LIST_TYPES:
                    if not isinstance(child, list):
                        raise SnapshotCollectionError("snapshot.invalid_reference_list")
                    child = [registry.resolve(_REFERENCE_LIST_TYPES[key], ref) for ref in child]
                    public_key = f"{key.removesuffix('_ids')}_refs"
                elif key == "memory_ids":
                    if child:
                        raise SnapshotCollectionError(
                            "snapshot.unsupported_memory_reference"
                        )
                    public_key = "memory_refs"
                elif key.endswith("_id") and key not in _PUBLIC_ID_FIELDS and not argument_context:
                    raise SnapshotCollectionError(
                        "snapshot.unsupported_nested_reference"
                    )
                elif key.endswith("_ids") and not argument_context:
                    raise SnapshotCollectionError(
                        "snapshot.unsupported_nested_reference_list"
                    )
                if isinstance(child, str) and (
                    public_key.endswith("_at")
                    or public_key in {"deadline", "due_at", "review_due_at"}
                ):
                    child = normalize_rfc3339(child)
                if public_key in result:
                    raise SnapshotCollectionError("snapshot.duplicate_public_field")
                result[public_key] = visit(child, container_key=key, tool_name=tool_name,
                                          argument_context=argument_context or key in {"arguments", "canonical_args"})
            return result
        if isinstance(item, list):
            return [visit(child, container_key=container_key, tool_name=tool_name, argument_context=argument_context) for child in item]
        return item

    return visit(normalized)


def _task_event_identity(
    value: str, registry: StableIdentityRegistry
) -> dict[str, Any]:
    match = _TASK_EVENT_SOURCE.fullmatch(value)
    if match is None:
        raise SnapshotCollectionError("snapshot.unsupported_task_event_identity")
    task_id, event_id = match.groups()
    return {
        "identity_type": "task_event",
        "task_ref": registry.resolve("task", task_id),
        "learning_event_ref": registry.resolve("learning_event", event_id),
    }


def _source_identity(row: Any, registry: StableIdentityRegistry) -> dict[str, Any]:
    source_type = str(row.source_type)
    source_id = str(row.source_id)
    direct_types = {
        "agent_run": "agent_run",
        "quiz": "quiz",
        "submission": "submission",
        "user": "learner",
    }
    if source_type in direct_types:
        entity_type = direct_types[source_type]
        return {
            "identity_type": "logical_ref",
            "source_ref": registry.resolve_compound(entity_type, source_id),
        }
    if source_type in {"task_completion", "task_evidence"}:
        return _task_event_identity(source_id, registry)
    if source_type == "operation":
        undo = _OPERATION_UNDO_SOURCE.fullmatch(source_id)
        if undo is not None:
            operation_id, generation, observation_id = undo.groups()
            return {
                "identity_type": "operation_undo",
                "operation_ref": registry.resolve("operation", operation_id),
                "generation": int(generation),
                "target_observation_ref": registry.resolve(
                    "evidence_observation", observation_id
                ),
            }
        return {
            "identity_type": "logical_ref",
            "source_ref": registry.resolve_compound("operation", source_id),
        }
    if source_type in {"manual", "manual_assessment", "self_report"}:
        return {
            "identity_type": "public_key",
            "public_key": normalize_json(source_id),
        }
    raise SnapshotCollectionError("snapshot.unsupported_evidence_source")


def _artifact_source_identity(
    source_uri: object, registry: StableIdentityRegistry
) -> dict[str, Any]:
    if not isinstance(source_uri, str):
        raise SnapshotCollectionError("snapshot.unsupported_artifact_source")
    if source_uri.startswith("submission:"):
        return {
            "identity_type": "logical_ref",
            "source_ref": registry.resolve_compound(
                "submission", source_uri.removeprefix("submission:")
            ),
        }
    if source_uri.startswith("quiz:"):
        return {
            "identity_type": "logical_ref",
            "source_ref": registry.resolve_compound(
                "quiz", source_uri.removeprefix("quiz:")
            ),
        }
    if source_uri.startswith("task:"):
        return _task_event_identity(source_uri, registry)
    raise SnapshotCollectionError("snapshot.unsupported_artifact_source")


def normalize_reference_fields(value: object, registry: StableIdentityRegistry) -> Any:
    """Normalize explicit ``*_id`` fields in public Runtime records to refs."""

    return _normalize_references(value, registry)


def _operation_patch(
    patch: object,
    registry: StableIdentityRegistry,
    *,
    primary_type: str,
    primary_raw: object,
) -> Any:
    """Normalize identities embedded in allowlisted production patches."""

    normalized = normalize_json(patch)
    if not isinstance(normalized, dict):
        raise SnapshotCollectionError("snapshot.unsupported_operation_patch")
    primary_ref = registry.resolve(primary_type, primary_raw)
    for key in ("created", "delete"):
        if key in normalized and str(normalized[key]) == str(primary_raw):
            normalized[f"{key}_ref"] = primary_ref
            del normalized[key]
    award = normalized.get("award")
    if isinstance(award, dict):
        day = award.get("day")
        if isinstance(day, dict) and "id" in day:
            day["activity_day_ref"] = registry.resolve("activity_day", day.pop("id"))
        achievements = award.get("achievements")
        if isinstance(achievements, list):
            for item in achievements:
                if isinstance(item, dict) and "id" in item:
                    item["achievement_ref"] = registry.resolve(
                        "achievement", item.pop("id")
                    )
        deleted = award.pop("delete_achievements", None)
        if isinstance(deleted, list):
            award["delete_achievement_refs"] = [
                registry.resolve("achievement", item) for item in deleted
            ]
    return _normalize_references(normalized, registry)


def _pending_approval_projection(
    value: object, registry: StableIdentityRegistry
) -> dict[str, Any] | None:
    """Expose the durable approval identity without embedded raw tool arguments."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise SnapshotCollectionError("snapshot.unsupported_pending_approval")
    tool_call = value.get("tool_call")
    remaining = value.get("remaining_tool_calls")
    if not isinstance(tool_call, dict) or not isinstance(remaining, list):
        raise SnapshotCollectionError("snapshot.unsupported_pending_approval")
    tool_call_id = tool_call.get("id")
    tool_name = tool_call.get("name")
    step = value.get("step")
    if (
        not isinstance(tool_call_id, str)
        or not isinstance(tool_name, str)
        or not isinstance(step, int)
        or isinstance(step, bool)
    ):
        raise SnapshotCollectionError("snapshot.unsupported_pending_approval")
    approval_ref = _ref(registry, "run_approval", value.get("approval_id"))
    if approval_ref is None:
        raise SnapshotCollectionError("snapshot.pending_approval_fact_missing")
    projection = {
        "approval_ref": approval_ref,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "remaining_tool_call_count": len(remaining),
        "reason": value.get("reason"),
        "step": step,
    }
    if remaining:
        from .recorder import public_tool_arguments

        queued = []
        for call in remaining:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(call.get("name"), str):
                raise SnapshotCollectionError("snapshot.invalid_pending_tool_call")
            arguments, error = public_tool_arguments(call.get("arguments") or "{}")
            queued.append({"tool_call_id": call["id"], "tool_name": call["name"],
                           "arguments_sha256": sha256_digest(arguments), "argument_error": error})
        projection["unexecuted_tool_calls"] = queued
    return projection


def pending_tool_call_evidence(after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Identify queued intent backed by the exact durable approval boundary."""
    entities = {item["logical_id"]: item for item in after["logical_entities"]}
    attempted = {item["data"]["tool_call_id"] for item in entities.values()
                 if item["entity_type"] == "tool_invocation"}
    queued = {}
    for run in entities.values():
        if run["entity_type"] != "agent_run":
            continue
        pending = run["data"].get("pending_approval")
        if not isinstance(pending, dict):
            continue
        calls = pending.get("unexecuted_tool_calls", [])
        if not calls:
            continue
        approval = entities.get(pending["approval_ref"], {}).get("data", {})
        if (run["data"]["status"] != "waiting_approval" or approval.get("decision") != "pending"
                or approval.get("run_ref") != run["logical_id"]
                or approval.get("tool_call_id") != pending["tool_call_id"]
                or len(calls) != pending["remaining_tool_call_count"]
                or len(calls) != approval.get("remaining_tool_call_count")):
            raise SnapshotCollectionError("snapshot.pending_tool_evidence_mismatch")
        for call in calls:
            key = call["tool_call_id"]
            if key in queued or key in attempted:
                raise SnapshotCollectionError("snapshot.duplicate_pending_tool_call")
            queued[key] = {**call, "run_ref": run["logical_id"]}
    return queued


def _data(
    entity_type: str, row: Any, registry: StableIdentityRegistry
) -> dict[str, Any]:
    def values(*names: str) -> dict[str, Any]:
        return {name: getattr(row, name) for name in names}

    if entity_type == "learner":
        raw = values("display_name", "timezone", "created_at")
    elif entity_type == "session":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            **values("title", "summary", "archived_at", "created_at", "updated_at"),
        }
    elif entity_type == "agent_run":
        raw = {
            "session_ref": _ref(registry, "session", row.session_id),
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "parent_run_ref": _ref(registry, "agent_run", row.parent_run_id),
            **values(
                "trigger",
                "objective",
                "status",
                "phase",
                "state_version",
                "attempt",
                "retry_count",
                "available_at",
                "status_reason",
                "model",
                "cancel_requested",
                "budget_usage",
                "output",
                "execution_mode",
                "proactive_candidate_state",
                "proactive_candidate_key",
                "proactive_candidate_kind",
                "proactive_candidate_payload",
                "proactive_candidate_digest",
                "proactive_detected_at",
                "started_at",
                "completed_at",
                "created_at",
                "updated_at",
            ),
            "pending_approval": _pending_approval_projection(
                row.pending_approval, registry
            ),
            "created_plan_ref": _ref(registry, "plan", row.created_plan_id),
        }
    elif entity_type == "context_snapshot":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "session_ref": _ref(registry, "session", row.session_id),
            "run_ref": _ref(registry, "agent_run", row.run_id),
            **values(
                "estimated_tokens",
                "context_generation",
                "snapshot_version",
                "assembler_version",
                "context_digest",
                "source_digest",
                "validity_state",
                "invalidated_at",
                "invalidation_reason",
                "created_at",
            ),
        }
    elif entity_type == "planning_intake":
        raw = {
            "session_ref": _ref(registry, "session", row.session_id),
            "run_ref": _ref(registry, "agent_run", row.source_run_id),
            **values(
                "goal",
                "confirmed_facts",
                "open_questions",
                "readiness",
                "readiness_confidence",
                "rationale",
                "created_at",
                "updated_at",
            ),
        }
    elif entity_type == "plan":
        raw = values(*FIELD_ALLOWLISTS["plan"])
    elif entity_type == "stage":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            **values("title", "description", "objectives", "position", "status"),
        }
    elif entity_type == "task":
        raw = {
            "stage_ref": _ref(registry, "stage", row.stage_id),
            **values(
                "title",
                "description",
                "kind",
                "status",
                "is_core",
                "evidence_required",
                "estimated_minutes",
                "position",
                "due_at",
                "completed_at",
                "review_due_at",
                "resource_url",
                "task_metadata",
            ),
        }
    elif entity_type == "plan_proposal":
        raw = {
            "session_ref": _ref(registry, "session", row.session_id),
            "run_ref": _ref(registry, "agent_run", row.source_run_id),
            **values(
                "title",
                "rationale",
                "plan_payload",
                "specialist_reports",
                "status",
                "decided_at",
                "created_at",
                "updated_at",
            ),
            "plan_ref": _ref(registry, "plan", row.plan_id),
        }
    elif entity_type == "submission":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "task_ref": _ref(registry, "task", row.task_id),
            "run_ref": _ref(registry, "agent_run", row.run_id),
            **values(
                "submission_type",
                "content",
                "artifacts",
                "status",
                "score",
                "feedback",
                "checked_at",
                "created_at",
            ),
        }
    elif entity_type == "review":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "task_ref": _ref(registry, "task", row.task_id),
            **values("due_at", "review_type", "status", "created_at"),
        }
    elif entity_type == "quiz":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "task_ref": _ref(registry, "task", row.task_id),
            "run_ref": _ref(registry, "agent_run", row.run_id),
            **values(
                "prompt",
                "rubric",
                "answer",
                "score",
                "feedback",
                "evidence",
                "status",
                "created_at",
                "graded_at",
            ),
        }
    elif entity_type == "achievement":
        raw = values(*FIELD_ALLOWLISTS["achievement"])
    elif entity_type == "activity_day":
        raw = values(*FIELD_ALLOWLISTS["activity_day"])
    elif entity_type == "artifact":
        raw = {
            **values(
                "artifact_type",
                "title",
                "content_hash",
                "size_bytes",
                "artifact_metadata",
                "snapshot_sha256",
                "storage_state",
                "envelope_version",
            ),
            "source_identity": _artifact_source_identity(row.source_uri, registry),
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "task_ref": _ref(registry, "task", row.task_id),
            "run_ref": _ref(registry, "agent_run", row.run_id),
            "session_ref": _ref(registry, "session", row.session_id),
            **values("request_digest", "created_at"),
        }
    elif entity_type == "evidence_observation":
        raw = {
            "source_type": row.source_type,
            "source_identity": _source_identity(row, registry),
            "run_ref": _ref(registry, "agent_run", row.run_id),
            "session_ref": _ref(registry, "session", row.session_id),
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "task_ref": _ref(registry, "task", row.task_id),
            "target_observation_ref": _ref(
                registry, "evidence_observation", row.target_observation_id
            ),
            **values(
                "fact_kind",
                "reason_code",
                "evidence_role",
                "eligibility_stage",
                "eligibility_reason",
                "eligibility_policy_version",
                "counts_as_success",
                "outcome",
                "normalized_score",
                "is_correct",
                "assistance_level",
                "transfer_level",
                "rubric_snapshot",
                "evaluator",
                "payload",
                "occurred_at",
                "recorded_at",
                "schema_version",
                "request_digest",
            ),
        }
    elif entity_type == "learning_event":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "task_ref": _ref(registry, "task", row.task_id),
            "run_ref": _ref(registry, "agent_run", row.run_id),
            **values(
                "event_type",
                "payload",
                "schema_version",
                "occurred_at",
                "invalidated_at",
                "invalidation_reason",
                "created_at",
            ),
        }
    elif entity_type == "tool_invocation":
        raw = {
            "run_ref": _ref(registry, "agent_run", row.run_id),
            **values(
                "tool_call_id",
                "tool_name",
                "args_hash",
                "request_digest",
                "canonical_args",
                "effect_kind",
                "status",
                "result_payload",
                "attempt",
                "version",
                "completed_at",
                "created_at",
                "updated_at",
            ),
        }
    elif entity_type == "run_approval":
        remaining = row.remaining_tool_calls
        if not isinstance(remaining, list):
            raise SnapshotCollectionError("snapshot.unsupported_run_approval")
        raw = {
            "run_ref": _ref(registry, "agent_run", row.run_id),
            "invocation_ref": _ref(registry, "tool_invocation", row.invocation_id),
            "tool_call_id": row.tool_call_id,
            "tool_name": row.tool_name,
            "remaining_tool_call_count": len(remaining),
            "reason": row.reason,
            "decision": row.decision,
            "decided_at": row.decided_at,
            "consumed_at": row.consumed_at,
            "created_at": row.created_at,
        }
    elif entity_type == "run_event":
        raw = {
            "run_ref": _ref(registry, "agent_run", row.run_id),
            **values("sequence", "event_type", "payload", "created_at"),
        }
    elif entity_type == "operation":
        target_type = {
            "submission": "submission",
            "plan": "plan",
            "stage": "stage",
            "task": "task",
            "review_schedule": "review",
            "quiz": "quiz",
            "plan_proposal": "plan_proposal",
        }.get(row.entity_type)
        if target_type is None:
            raise SnapshotCollectionError("snapshot.unsupported_operation_entity")
        target_raw: object = (
            int(row.entity_id) if row.entity_id.isdigit() else row.entity_id
        )
        forward_patch = _operation_patch(
            row.forward_patch,
            registry,
            primary_type=target_type,
            primary_raw=target_raw,
        )
        inverse_patch = _operation_patch(
            row.inverse_patch,
            registry,
            primary_type=target_type,
            primary_raw=target_raw,
        )
        raw = {
            "run_ref": _ref(registry, "agent_run", row.run_id),
            "invocation_ref": _ref(registry, "tool_invocation", row.invocation_id),
            "tool_name": row.tool_name,
            "primary_entity_type": target_type,
            "primary_entity_ref": _ref(registry, target_type, target_raw),
            "forward_patch": forward_patch,
            "inverse_patch": inverse_patch,
            "status": row.status,
            "created_at": row.created_at,
            "undone_at": row.undone_at,
        }
    elif entity_type == "proactive_decision":
        raw = {
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "run_ref": _ref(registry, "agent_run", row.source_run_id),
            "invocation_ref": _ref(
                registry, "tool_invocation", row.source_invocation_id
            ),
            **values(
                "candidate_key",
                "candidate_kind",
                "candidate_payload",
                "candidate_digest",
                "policy_version",
                "policy_digest",
                "status",
                "outcome",
                "reason_code",
                "next_eligible_at",
                "decision_payload",
                "decision_digest",
                "decided_at",
                "created_at",
            ),
        }
    elif entity_type == "intervention":
        raw = {
            "decision_ref": _ref(
                registry, "proactive_decision", row.proactive_decision_id
            ),
            "run_ref": _ref(registry, "agent_run", row.source_run_id),
            "invocation_ref": _ref(
                registry, "tool_invocation", row.source_invocation_id
            ),
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "session_ref": _ref(registry, "session", row.session_id),
            **values(
                "title",
                "body",
                "content_digest",
                "reason_code",
                "state",
                "outcome",
                "read_at",
                "archived_at",
                "resolved_at",
                "created_at",
            ),
        }
    elif entity_type == "notification":
        raw = {
            "run_ref": _ref(registry, "agent_run", row.run_id),
            "invocation_ref": _ref(registry, "tool_invocation", row.invocation_id),
            "session_ref": _ref(registry, "session", row.session_id),
            "plan_ref": _ref(registry, "plan", row.plan_id),
            "intervention_ref": _ref(registry, "intervention", row.intervention_id),
            **values(
                "delivery_generation",
                "legacy_unlinked",
                "channel",
                "title",
                "body",
                "status",
                "sent_at",
                "read_at",
                "archived_at",
                "created_at",
            ),
        }
    elif entity_type == "outbox_action":
        raw = {
            "run_ref": _ref(registry, "agent_run", row.run_id),
            "invocation_ref": _ref(registry, "tool_invocation", row.invocation_id),
            "notification_ref": _ref(registry, "notification", row.notification_id),
            "operation_ref": _ref(registry, "operation", row.operation_id),
            **values(
                "action_key",
                "request_digest",
                "effect_kind",
                "destination",
                "status",
                "attempt",
                "version",
                "available_at",
                "completed_at",
                "created_at",
                "updated_at",
            ),
        }
    elif entity_type == "outbox_receipt":
        raw = {
            "outbox_action_ref": _ref(registry, "outbox_action", row.outbox_action_id),
            **values(
                "action_key",
                "status",
                "provider_id",
                "response",
                "accepted_at",
                "created_at",
            ),
        }
    else:
        raise SnapshotCollectionError("snapshot.unsupported_entity")
    normalized = _normalize_references(raw, registry)
    expected = set(FIELD_ALLOWLISTS[entity_type])
    if entity_type != "learner" and set(normalized) != expected:
        raise SnapshotCollectionError("snapshot.field_allowlist_mismatch")
    return normalized


def _declarations(fixture: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    declared: dict[str, list[str]] = {}
    aliases = {"evidence": "evidence_observation"}
    for entity in fixture["state_before"]["logical_entities"]:
        entity_type = aliases.get(entity["entity_type"], entity["entity_type"])
        if entity_type not in FIELD_ALLOWLISTS:
            raise SnapshotCollectionError("snapshot.unsupported_declared_entity")
        declared.setdefault(entity_type, []).append(entity["logical_id"])
    return {key: tuple(values) for key, values in declared.items()}


def identity_registry(
    fixture: dict[str, Any],
    *,
    identity_bindings: list[dict[str, Any]] | None = None,
) -> StableIdentityRegistry:
    """Create the one registry shared by the before and after captures."""

    return StableIdentityRegistry(
        episode_id=fixture["episode_id"],
        declarations=_declarations(fixture),
        identity_bindings=identity_bindings,
    )


async def collect_state_snapshot(
    session_factory: Any,
    fixture: dict[str, Any],
    *,
    registry: StableIdentityRegistry,
    captured_at: str,
    resource_version: str,
    resource_digest: str,
    phase: Literal["before", "after"],
) -> SnapshotCapture:
    """Collect one complete allowlisted snapshot from the temporary database."""

    try:
        rows = await _load_rows(
            session_factory,
            owner_id=fixture["owner_id"],
            run_id=fixture["run_id"],
        )
        if len(rows["learner"]) != 1 or len(rows["profiles"]) != 1:
            raise SnapshotCollectionError("snapshot.learner_scope_incomplete")
        root_runs = [
            row
            for row in rows["agent_run"]
            if row.id == fixture["run_id"] and row.parent_run_id is None
        ]
        if len(root_runs) != 1:
            raise SnapshotCollectionError("snapshot.run_scope_incomplete")
        _register_identities(registry, rows)
        if phase == "before":
            registry.assert_bindings_resolved(
                tuple(
                    entity_type
                    for entity_type in ENTITY_TYPE_ORDER
                    if entity_type not in {"goal", "constraint", "resource"}
                )
            )
        profile = rows["profiles"][0]
        declarations_by_id = {
            item["logical_id"]: item
            for item in fixture["state_before"]["logical_entities"]
        }
        entities: list[dict[str, Any]] = []
        for entity_type in ENTITY_TYPE_ORDER:
            if entity_type in {"goal", "constraint", "resource"}:
                continue
            for row in rows.get(entity_type, []):
                logical_id = registry.resolve(entity_type, _raw_id(entity_type, row))
                if logical_id is None:
                    raise SnapshotCollectionError("snapshot.identity_missing")
                data = _data(entity_type, row, registry)
                if entity_type == "learner":
                    declared = declarations_by_id.get(logical_id, {}).get("data", {})
                    data.update(
                        normalize_json(
                            {
                                "agent_style": profile.agent_style,
                                "preferences": profile.preferences,
                                "quiet_hours": profile.quiet_hours,
                                "daily_notification_limit": profile.daily_notification_limit,
                                "xp": profile.xp,
                                "level": profile.level,
                                "streak_days": profile.streak_days,
                                "follow_up_behavior": profile.follow_up_behavior,
                                "proactive_paused": profile.proactive_paused,
                                "declared_context": declared,
                                "updated_at": profile.updated_at,
                            }
                        )
                    )
                    if set(data) != set(FIELD_ALLOWLISTS["learner"]):
                        raise SnapshotCollectionError(
                            "snapshot.field_allowlist_mismatch"
                        )
                scope_ref = (
                    None
                    if entity_type == "learner"
                    else registry.resolve("learner", fixture["owner_id"])
                )
                entities.append(
                    {
                        "logical_id": logical_id,
                        "entity_type": entity_type,
                        "source": "database",
                        "scope_ref": scope_ref,
                        "data": data,
                    }
                )
        used_ids = {item["logical_id"] for item in entities}
        runtime_only = {"goal", "constraint"}
        stateful = {"learner", "plan", "stage", "task", "submission"}
        for declared in fixture["state_before"]["logical_entities"]:
            entity_type = declared["entity_type"]
            if declared["logical_id"] in used_ids:
                continue
            if entity_type in stateful:
                if phase == "before":
                    raise SnapshotCollectionError(
                        "snapshot.declared_state_entity_missing"
                    )
                continue
            if entity_type not in runtime_only:
                raise SnapshotCollectionError(
                    "snapshot.unsupported_runtime_input_entity"
                )
            entities.append(
                {
                    "logical_id": declared["logical_id"],
                    "entity_type": entity_type,
                    "source": "runtime_input",
                    "scope_ref": registry.resolve("learner", fixture["owner_id"]),
                    "data": {"declared_context": normalize_json(declared["data"])},
                }
            )
        entities.append(
            {
                "logical_id": f"resource:{fixture['episode_id']}:snapshot",
                "entity_type": "resource",
                "source": "resource_snapshot",
                "scope_ref": registry.resolve("learner", fixture["owner_id"]),
                "data": {
                    "snapshot_version": resource_version,
                    "snapshot_digest": resource_digest,
                },
            }
        )
        entities.sort(key=lambda item: (item["entity_type"], item["logical_id"]))
        for ordinal, entity in enumerate(entities, 1):
            entity["ordinal"] = ordinal
            entity["entity_sha256"] = sha256_digest(entity)
        context = dict(fixture["state_before"]["context"])
        context["context_sha256"] = context_summary_digest(context)
        document = {
            "collector_version": COLLECTOR_VERSION,
            "entity_types": list(ENTITY_TYPE_ORDER),
            "field_allowlist_sha256": FIELD_ALLOWLIST_SHA256,
            "capture_status": "complete",
            "captured_at": captured_at,
            "logical_entities": entities,
            "context": context,
            "error_codes": [],
            "snapshot_sha256": "0" * 64,
        }
        document["snapshot_sha256"] = sha256_digest(
            {key: value for key, value in document.items() if key != "snapshot_sha256"}
        )
        return SnapshotCapture(document=document)
    except NormalizationError as exc:
        raise SnapshotCollectionError(exc.code) from exc


def audit_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep the historical E1 engineering-capture shape from one v2 snapshot."""

    by_type: dict[str, list[dict[str, Any]]] = {}
    for entity in snapshot["logical_entities"]:
        by_type.setdefault(entity["entity_type"], []).append(entity)

    def rows(entity_type: str) -> list[dict[str, Any]]:
        return [
            {"logical_id": item["logical_id"], **item["data"]}
            for item in by_type.get(entity_type, [])
        ]

    run_rows = [
        item for item in rows("agent_run") if item.get("parent_run_ref") is None
    ]
    if len(run_rows) != 1:
        raise SnapshotCollectionError("snapshot.audit_run_missing")
    run = run_rows[0]
    return {
        "run": {
            "run_ref": run["logical_id"],
            "status": run["status"],
            "phase": run["phase"],
            "trigger": run["trigger"],
            "output": run["output"],
            "started_at": run["started_at"],
            "completed_at": run["completed_at"],
        },
        "tool_invocations": [
            {
                "logical_id": item["logical_id"],
                "tool_call_id": item["tool_call_id"],
                "tool_name": item["tool_name"],
                "canonical_args": item["canonical_args"] or {},
                "status": item["status"],
                "result": item["result_payload"] or {},
                "completed_at": item["completed_at"],
            }
            for item in rows("tool_invocation")
        ],
        "run_events": [
            {
                "logical_id": item["logical_id"],
                "sequence": item["sequence"],
                "event_type": item["event_type"],
                "summary": item["event_type"],
                "payload": item["payload"],
                "created_at": item["created_at"],
            }
            for item in rows("run_event")
        ],
        "operations": rows("operation"),
        "notifications": sorted(
            rows("notification"),
            key=lambda item: (
                {"in_app": 0, "email": 1, "web_push": 2}.get(item["channel"], 99),
                item["logical_id"],
            ),
        ),
        "outbox_actions": rows("outbox_action"),
        "outbox_receipts": rows("outbox_receipt"),
        "interventions": rows("intervention"),
        "proactive_decisions": rows("proactive_decision"),
        "plan_proposals": rows("plan_proposal"),
        "plans": rows("plan"),
        "submissions": rows("submission"),
    }
