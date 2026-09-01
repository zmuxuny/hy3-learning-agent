"""Strict Pydantic sources for frozen E0 and versioned E2 contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

SCHEMA_BASE_URI = "https://zmuxuny.github.io/hy3-learning-agent/evaluation/schemas"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

StableId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9._:/-]*$",
    ),
]
RoleName = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


def _validate_rfc3339(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid RFC3339 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError("RFC3339 timestamp must include an offset")
    return value


Rfc3339 = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
        )
    ),
    AfterValidator(_validate_rfc3339),
]
EvidencePath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=300,
        pattern=(
            r"^[A-Za-z_][A-Za-z0-9_-]*(?:\[(?:0|[1-9]\d*)\])?"
            r"(?:\.[A-Za-z_][A-Za-z0-9_-]*(?:\[(?:0|[1-9]\d*)\])?)*$"
        ),
    ),
]
DeltaFieldPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=300,
        pattern=(
            r"^(?:\$entity|data(?:\.[A-Za-z_][A-Za-z0-9_-]*"
            r"(?:\[(?:0|[1-9]\d*)\])?)*)$"
        ),
    ),
]
Tag = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]

Track = Literal["planning", "intervention", "assessment", "revision"]
Split = Literal["dev", "test"]
Difficulty = Literal["standard", "hard", "adversarial"]
ActionClass = Literal[
    "PROPOSE_PLAN",
    "REQUEST_USER_INPUT",
    "WAIT",
    "INTERVENE_MESSAGE",
    "INTERVENE_QUIZ_OR_REVIEW",
    "PROPOSE_PLAN_ADJUSTMENT",
    "ACCEPT",
    "REVISION_REQUIRED",
    "INSUFFICIENT_EVIDENCE",
    "REQUEST_CLARIFICATION",
    "NO_OP",
    "PROPOSE_CHANGE",
    "APPLY_REVERSIBLE_PATCH",
    "REQUEST_APPROVAL",
]


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelManifest(StrictContractModel):
    provider: Literal["none", "tencent-tokenhub"]
    name: NonEmptyText
    invocation_mode: Literal["not_invoked", "stub", "real"]
    temperature: Annotated[float, Field(ge=0, le=2)]
    reasoning_effort: Literal[
        "not_applicable", "none", "low", "medium", "high", "xhigh"
    ]
    max_tokens: Annotated[int, Field(ge=0, le=1_000_000)]


class PromptManifest(StrictContractModel):
    version: StableId
    digest: Sha256


class ToolManifest(StrictContractModel):
    allowlist: list[StableId]
    schema_digest: Sha256


class PolicyManifest(StrictContractModel):
    proactive_version: StableId
    proactive_digest: Sha256
    evidence_version: StableId
    evidence_digest: Sha256
    approval_version: StableId
    approval_digest: Sha256


class ResourceManifest(StrictContractModel):
    snapshot_version: StableId
    snapshot_digest: Sha256


class RuntimeManifest(StrictContractModel):
    git_commit: GitCommit
    database_mode: Literal["none", "temporary_fixture"]
    fixture_db_sha256: Sha256 | None


class IsolationManifest(StrictContractModel):
    production_database_access: Literal[False]
    network_access: Literal[
        "disabled", "model_provider_only", "resource_snapshots_only"
    ]
    notification_mode: Literal["disabled", "fake_outbox"]


class EnvironmentManifest(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/environment-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["environment-manifest-v1"]
    frozen_time: Rfc3339
    timezone: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=80,
            pattern=r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+)$",
        ),
    ]
    model: ModelManifest
    prompt: PromptManifest
    tools: ToolManifest
    policies: PolicyManifest
    resources: ResourceManifest
    runtime: RuntimeManifest
    isolation: IsolationManifest
    manifest_sha256: Sha256


class OracleRequirement(StrictContractModel):
    id: StableId
    statement: NonEmptyText
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1)]


class OracleProhibition(StrictContractModel):
    id: StableId
    statement: NonEmptyText
    criticality: Literal["minor", "major", "critical"]


class ExpectedEffect(StrictContractModel):
    path: EvidencePath
    relation: Literal["equals", "contains", "unchanged", "bounded_change"]
    value: JsonValue


class AcceptableActionEnvelope(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/acceptable-action-envelope-v1.schema.json",
        },
    )

    schema_version: Literal["acceptable-action-envelope-v1"]
    allowed_action_classes: Annotated[list[ActionClass], Field(min_length=1)]
    must_satisfy: Annotated[list[OracleRequirement], Field(min_length=1)]
    must_not: Annotated[list[OracleProhibition], Field(min_length=1)]
    expected_effects: Annotated[list[ExpectedEffect], Field(min_length=1)]
    acceptable_variations: Annotated[list[NonEmptyText], Field(min_length=1)]
    critical_failures: Annotated[list[NonEmptyText], Field(min_length=1)]
    oracle_author: RoleName
    oracle_reviewer: RoleName
    adjudication_note: NonEmptyText | None
    envelope_sha256: Sha256


class Trigger(StrictContractModel):
    trigger_id: StableId
    trigger_type: Literal[
        "user_goal", "heartbeat", "submission", "constraint_change"
    ]
    objective: NonEmptyText
    triggered_at: Rfc3339
    target_refs: Annotated[list[StableId], Field(min_length=1)]
    source_event_refs: list[StableId]
    payload: dict[str, JsonValue]


class LogicalEntity(StrictContractModel):
    logical_id: StableId
    entity_type: Literal[
        "learner",
        "goal",
        "plan",
        "stage",
        "task",
        "submission",
        "evidence",
        "review",
        "intervention",
        "resource",
        "rubric",
        "constraint",
        "calendar",
    ]
    data: dict[str, JsonValue]


class StateContext(StrictContractModel):
    public_summary: NonEmptyText
    source_refs: Annotated[list[StableId], Field(min_length=1)]
    context_sha256: Sha256


class StateBefore(StrictContractModel):
    snapshot_type: Literal["synthetic_fixture", "runtime_snapshot"]
    logical_entities: Annotated[list[LogicalEntity], Field(min_length=1)]
    facts: dict[str, JsonValue]
    context: StateContext


class ModelTurn(StrictContractModel):
    turn_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    visible_input_digest: Sha256
    assistant_text: str | None
    tool_call_refs: list[StableId]


class ToolInvocation(StrictContractModel):
    invocation_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    tool_name: StableId
    canonical_args: dict[str, JsonValue]
    status: Literal["succeeded", "failed", "blocked", "not_executed"]
    result: dict[str, JsonValue]
    result_digest: Sha256


class RunEvent(StrictContractModel):
    event_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    event_type: StableId
    payload: dict[str, JsonValue]
    payload_digest: Sha256


class Operation(StrictContractModel):
    operation_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    tool_name: StableId
    entity_ref: StableId
    forward_patch: dict[str, JsonValue]
    inverse_patch: dict[str, JsonValue]
    patch_digest: Sha256


class ObservableTrace(StrictContractModel):
    capture_mode: Literal["manual_protocol_fixture", "runtime_recording"]
    model_turns: list[ModelTurn]
    tool_invocations: list[ToolInvocation]
    run_events: list[RunEvent]
    operations: list[Operation]


class StateChange(StrictContractModel):
    change_id: StableId
    path: EvidencePath
    before: JsonValue
    after: JsonValue
    operation_ref: StableId | None


class GuardResult(StrictContractModel):
    status: Literal["not_evaluated", "allowed", "blocked"]
    reason_code: StableId | None
    blocked_effect_refs: list[StableId]


class DecisionResult(StrictContractModel):
    action_class: ActionClass
    user_visible_output: str | None
    state_changes: list[StateChange]
    guard: GuardResult


class Provenance(StrictContractModel):
    source_type: Literal["manual_protocol_fixture", "runtime_export"]
    construction_method: Literal["hand_authored", "runtime_recorded"]
    author_role: RoleName
    reviewer_role: RoleName
    purpose: NonEmptyText
    dataset_role: Literal[
        "protocol_mini_fixture", "primary_episode", "calibration_output"
    ]
    runtime_executed: bool
    formal_evaluation_result: bool
    evaluation_status: Literal[
        "not_a_formal_model_evaluation", "formal_model_evaluation"
    ]
    created_at: Rfc3339
    source_refs: list[StableId]
    episode_sha256: Sha256


class DecisionEpisode(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/decision-episode-v1.schema.json",
        },
    )

    schema_version: Literal["decision-episode-v1"]
    episode_id: StableId
    scenario_family_id: StableId
    track: Track
    split: Split
    difficulty: Difficulty
    tags: Annotated[list[Tag], Field(min_length=1)]
    trigger: Trigger
    state_before: StateBefore
    environment: EnvironmentManifest
    observable_trace: ObservableTrace
    result: DecisionResult
    oracle: AcceptableActionEnvelope
    provenance: Provenance


EntityTypeV2 = Literal[
    "learner",
    "goal",
    "constraint",
    "resource",
    "session",
    "agent_run",
    "context_snapshot",
    "planning_intake",
    "plan",
    "stage",
    "task",
    "plan_proposal",
    "submission",
    "review",
    "quiz",
    "achievement",
    "activity_day",
    "artifact",
    "evidence_observation",
    "learning_event",
    "intervention",
    "proactive_decision",
    "notification",
    "outbox_action",
    "outbox_receipt",
    "operation",
    "tool_invocation",
    "run_approval",
    "run_event",
]


class SnapshotEntityV2(StrictContractModel):
    logical_id: StableId
    entity_type: EntityTypeV2
    ordinal: Annotated[int, Field(ge=1)]
    source: Literal["database", "runtime_input", "resource_snapshot"]
    scope_ref: StableId | None
    data: dict[str, JsonValue]
    entity_sha256: Sha256


class StateSnapshotV2(StrictContractModel):
    collector_version: StableId
    entity_types: list[EntityTypeV2]
    field_allowlist_sha256: Sha256
    capture_status: Literal["complete", "incomplete", "failed"]
    captured_at: Rfc3339
    logical_entities: list[SnapshotEntityV2]
    context: StateContext
    error_codes: list[StableId]
    snapshot_sha256: Sha256

    @model_validator(mode="after")
    def validate_capture_shape(self) -> Self:
        if (self.capture_status == "complete") != (not self.error_codes):
            raise ValueError("complete snapshots cannot contain capture errors")
        return self


class PresenceValueV2(StrictContractModel):
    presence: Literal["missing", "present"]
    value: JsonValue

    @model_validator(mode="after")
    def validate_missing_value(self) -> Self:
        if self.presence == "missing" and self.value is not None:
            raise ValueError("missing values must use a null payload")
        return self


class TypedSourceRefV2(StrictContractModel):
    source_type: Literal[
        "runtime",
        "model_turn",
        "tool_invocation",
        "guard_decision",
        "operation",
        "run_event",
        "entity",
    ]
    ref: StableId


class StateDeltaChangeV2(StrictContractModel):
    change_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    kind: Literal["added", "removed", "changed", "unchanged"]
    entity_ref: StableId
    field_path: DeltaFieldPath
    before_path: EvidencePath | None
    after_path: EvidencePath | None
    before: PresenceValueV2
    after: PresenceValueV2
    source_refs: Annotated[list[TypedSourceRefV2], Field(min_length=1)]
    operation_refs: list[StableId]
    operation_alignment: Literal[
        "matched", "not_applicable", "unattributed", "ambiguous", "mismatch"
    ]


class StateDeltaV2(StrictContractModel):
    capture_status: Literal["complete", "incomplete", "failed"]
    before_snapshot_sha256: Sha256
    after_snapshot_sha256: Sha256
    changes: list[StateDeltaChangeV2]
    compared_entity_refs: list[StableId]
    unchanged_entity_refs: list[StableId]
    error_codes: list[StableId]
    delta_sha256: Sha256


class ToolInvocationV2(StrictContractModel):
    invocation_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    tool_call_id: StableId | None
    tool_name: StableId
    canonical_args: dict[str, JsonValue]
    execution_status: Literal["completed", "failed", "blocked", "not_executed"]
    observation_status: Literal[
        "succeeded",
        "failed",
        "blocked",
        "deferred",
        "pending_approval",
        "pending_delivery",
        "needs_reconciliation",
        "retry_pending",
        "not_executed",
    ]
    durable_status: Literal[
        "running",
        "pending_approval",
        "pending_delivery",
        "committed",
        "failed",
        "rejected",
        "needs_reconciliation",
        "retry_pending",
        "cancelled",
    ]
    result: dict[str, JsonValue]
    result_digest: Sha256
    operation_refs: list[StableId]


class OperationV2(StrictContractModel):
    operation_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    invocation_ref: StableId | None
    tool_name: StableId
    status: Literal[
        "committed",
        "undo_pending",
        "undone",
        "redo_pending",
        "needs_reconciliation",
    ]
    primary_entity_ref: StableId
    affected_entity_refs: Annotated[list[StableId], Field(min_length=1)]
    forward_patch: dict[str, JsonValue]
    inverse_patch: dict[str, JsonValue]
    patch_digest: Sha256


class GuardDecisionV2(StrictContractModel):
    guard_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    policy: StableId
    status: Literal["not_evaluated", "allowed", "blocked", "deferred"]
    reason_code: StableId | None
    invocation_ref: StableId | None
    decision_ref: StableId | None
    attempted_effect_refs: list[StableId]
    final_effect_refs: list[StableId]


class ObservableTraceV2(StrictContractModel):
    capture_mode: Literal["runtime_recording"]
    model_turns: list[ModelTurn]
    tool_invocations: list[ToolInvocationV2]
    run_events: list[RunEvent]
    operations: list[OperationV2]
    guard_decisions: list[GuardDecisionV2]


class ModelAttemptV2(StrictContractModel):
    attempt_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    turn_ref: StableId
    attempted_action: Literal["tool_call", "respond", "wait", "no_op"]
    invocation_refs: list[StableId]


class FinalEffectV2(StrictContractModel):
    effect_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    effect_type: Literal[
        "plan_proposal",
        "intervention",
        "assessment_verdict",
        "reversible_patch",
        "approval_request",
        "wait",
        "no_op",
        "blocked",
        "deferred",
        "failed",
    ]
    status: Literal["applied", "blocked", "deferred", "pending", "no_change", "failed"]
    entity_refs: list[StableId]
    source_refs: Annotated[list[TypedSourceRefV2], Field(min_length=1)]


class DecisionLayersV2(StrictContractModel):
    model_attempts: list[ModelAttemptV2]
    guard_decision_refs: list[StableId]
    final_effects: Annotated[list[FinalEffectV2], Field(min_length=1)]
    run_status: Literal[
        "queued",
        "running",
        "waiting_approval",
        "retry_wait",
        "completed",
        "failed",
        "cancelled",
        "needs_reconciliation",
    ]
    durable_status: Literal[
        "committed",
        "pending",
        "partial",
        "blocked",
        "deferred",
        "failed",
        "needs_reconciliation",
    ]
    formal_evaluation_eligibility: Literal[
        "eligible", "ineligible_stub", "ineligible_engineering", "invalid"
    ]


class GuardDecisionSummaryV2(StrictContractModel):
    status: Literal["not_evaluated", "allowed", "blocked", "deferred", "mixed"]
    reason_code: StableId | None
    blocked_effect_refs: list[StableId]


class DecisionResultV2(StrictContractModel):
    action_class: ActionClass
    action_mapping_version: StableId
    action_mapping_sha256: Sha256
    user_visible_output: str | None
    guard: GuardDecisionSummaryV2
    layers: DecisionLayersV2


class EpisodeCompletenessV2(StrictContractModel):
    status: Literal["complete", "invalid"]
    error_codes: list[StableId]
    verified_evidence_paths: list[EvidencePath]
    completeness_sha256: Sha256


class SnapshotProviderCallsV2(StrictContractModel):
    search: Annotated[int, Field(ge=0)]
    open: Annotated[int, Field(ge=0)]
    validation_calls: Annotated[int, Field(ge=0, alias="validate")]


class RuntimeIsolationEvidenceV2(StrictContractModel):
    temporary_database: Literal[True]
    database_inside_worker_root: Literal[True]
    env_file_read: Literal[False]
    repository_runtime_data_access: Literal[False]
    background_services_started: Literal[False]
    network_calls: Annotated[int, Field(ge=0)]
    smtp_calls: Annotated[int, Field(ge=0)]
    smtp_ssl_calls: Annotated[int, Field(ge=0)]
    web_push_calls: Annotated[int, Field(ge=0)]
    imap_calls: Annotated[int, Field(ge=0)]
    imap_ssl_calls: Annotated[int, Field(ge=0)]
    prohibited_file_access: Annotated[int, Field(ge=0)]
    outside_sqlite_access: Annotated[int, Field(ge=0)]
    subprocess_calls: Annotated[int, Field(ge=0)]
    published_sqlite_files: Literal[0]
    routing_material_exported: Literal[False]
    snapshot_provider_calls: SnapshotProviderCallsV2
    recording_sink_attempts: Annotated[int, Field(ge=0)]
    outbox_replay_confirmed: bool
    agent_observed_pending_delivery: bool
    agent_observed_emulated_receipt: Literal[False]


class DecisionEpisodeV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/decision-episode-v2.schema.json",
        },
    )

    schema_version: Literal["decision-episode-v2"]
    episode_id: StableId
    scenario_family_id: StableId
    track: Track
    split: Split
    difficulty: Difficulty
    tags: Annotated[list[Tag], Field(min_length=1)]
    trigger: Trigger
    state_before: StateSnapshotV2
    state_after: StateSnapshotV2
    state_delta: StateDeltaV2
    environment: EnvironmentManifest
    observable_trace: ObservableTraceV2
    result: DecisionResultV2
    oracle: AcceptableActionEnvelope
    completeness: EpisodeCompletenessV2
    isolation_evidence: RuntimeIsolationEvidenceV2
    provenance: Provenance


class RuleCheckV1(StrictContractModel):
    check_id: StableId
    rule_pack: Literal[
        "common",
        "planning",
        "intervention",
        "assessment",
        "revision",
        "trace",
        "isolation",
    ]
    status: Literal["pass", "fail", "not_applicable", "invalid_input"]
    severity: Literal["minor", "major", "critical"]
    evidence_paths: list[EvidencePath]
    observed: JsonValue
    expected: JsonValue
    reason_code: StableId
    message: NonEmptyText


class RuleResultV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/rule-result-v1.schema.json",
        },
    )

    schema_version: Literal["rule-result-v1"]
    evaluator_version: StableId
    episode_id: StableId
    episode_sha256: Sha256
    rule_pack_version: StableId
    rule_pack_sha256: Sha256
    checks: Annotated[list[RuleCheckV1], Field(min_length=1)]
    hard_gates: list[StableId]
    dimension_signals: dict[str, JsonValue]
    status: Literal["pass", "fail", "invalid_input"]
    formal_evaluation_result: bool
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_hard_gates(self) -> Self:
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("Rule check IDs must be unique")
        pack_order = {
            name: index
            for index, name in enumerate(
                (
                    "common",
                    "planning",
                    "intervention",
                    "assessment",
                    "revision",
                    "trace",
                    "isolation",
                )
            )
        }
        order = [
            (pack_order.get(check.rule_pack, len(pack_order)), check.check_id)
            for check in self.checks
        ]
        if order != sorted(order):
            raise ValueError("Rule checks must use stable pack and check ordering")
        if any(
            token in key.casefold()
            for key in self.dimension_signals
            for token in ("score", "rank", "judge")
        ):
            raise ValueError("dimension signals cannot contain scores or Judge output")
        expected = [
            check.check_id
            for check in self.checks
            if check.status == "fail" and check.severity == "critical"
        ]
        if self.hard_gates != expected:
            raise ValueError("hard gates must exactly list failed critical checks")
        expected_status = (
            "invalid_input"
            if any(check.status == "invalid_input" for check in self.checks)
            else "fail"
            if any(check.status == "fail" for check in self.checks)
            else "pass"
        )
        if self.status != expected_status:
            raise ValueError("Rule Result status must match its ordered checks")
        return self


class IntegrityErrorV1(StrictContractModel):
    error_code: StableId
    stage: StableId
    episode_id: StableId
    message: NonEmptyText
    evidence_path: EvidencePath | None


class IntegrityResultV1(StrictContractModel):
    schema_version: Literal["integrity-result-v1"]
    episode_id: StableId
    episode_sha256: Sha256
    status: Literal["valid", "invalid"]
    errors: list[IntegrityErrorV1]
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.status == "valid") != (not self.errors):
            raise ValueError("integrity status must match the error list")
        return self


class E2RunOutputManifest(StrictContractModel):
    schema_version: Literal["e2-run-output-manifest-v1"]
    dataset_version: StableId
    episode_schema_version: Literal["decision-episode-v2"]
    invocation_mode: Literal["stub", "real"]
    formal_evaluation_result: bool
    evaluation_status: Literal[
        "not_a_formal_model_evaluation", "formal_model_evaluation"
    ]
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    episode_digests: dict[str, Sha256]
    capture_digests: dict[str, Sha256]
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_output_semantics(self) -> Self:
        episode_ids = set(self.episode_ids)
        if len(episode_ids) != len(self.episode_ids):
            raise ValueError("output Episode IDs must be unique")
        if episode_ids != set(self.episode_digests) or episode_ids != set(
            self.capture_digests
        ):
            raise ValueError("output digest keys must match Episode IDs")
        if self.invocation_mode == "stub" and (
            self.formal_evaluation_result
            or self.evaluation_status != "not_a_formal_model_evaluation"
        ):
            raise ValueError("stub outputs cannot claim a formal evaluation")
        return self


class RuleRunManifestV1(StrictContractModel):
    schema_version: Literal["rule-run-manifest-v1"]
    input_episode_schema_version: Literal["decision-episode-v2"]
    evaluator_version: StableId
    rule_pack_version: StableId
    rule_pack_sha256: Sha256
    invocation_mode: Literal["stub", "real", "mixed"]
    formal_evaluation_result: bool
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    input_episode_digests: dict[str, Sha256]
    rule_result_digests: dict[str, Sha256]
    integrity_result_digests: dict[str, Sha256]
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_result_keys(self) -> Self:
        episode_ids = set(self.episode_ids)
        if len(episode_ids) != len(self.episode_ids):
            raise ValueError("rule output Episode IDs must be unique")
        mappings = (
            self.input_episode_digests,
            self.rule_result_digests,
            self.integrity_result_digests,
        )
        if any(set(mapping) != episode_ids for mapping in mappings):
            raise ValueError("rule output digest keys must match Episode IDs")
        if self.invocation_mode == "stub" and self.formal_evaluation_result:
            raise ValueError("stub Rule Results cannot claim formal evaluation")
        return self


class ScriptedFunctionCall(StrictContractModel):
    call_id: StableId
    name: StableId
    arguments: dict[str, JsonValue]


class ScriptedModelTurn(StrictContractModel):
    ordinal: Annotated[int, Field(ge=1)]
    delivery: Literal["stream", "nonstream"]
    assistant_text: str
    tool_calls: list[ScriptedFunctionCall]


class RuntimeMiniFixture(StrictContractModel):
    schema_version: Literal["e1-runtime-mini-fixture-v1"]
    episode_id: StableId
    scenario_family_id: StableId
    track: Track
    split: Split
    difficulty: Difficulty
    frozen_time: Rfc3339
    timezone: str
    owner_id: StableId
    run_id: StableId
    session_id: StableId | None
    trigger: Trigger
    state_before: StateBefore
    seed_kind: Literal["planning", "intervention", "assessment", "revision"]
    seed: dict[str, JsonValue]
    scripted_turns: Annotated[list[ScriptedModelTurn], Field(min_length=1)]
    oracle_file: str
    resource_snapshot_version: StableId
    engineering_only: Literal[True]
    fixture_sha256: Sha256

    @model_validator(mode="after")
    def validate_fixture_semantics(self) -> Self:
        if self.track != self.seed_kind:
            raise ValueError("fixture track and seed kind must match")
        expected_trigger = {
            "planning": "user_goal",
            "intervention": "heartbeat",
            "assessment": "submission",
            "revision": "constraint_change",
        }[self.track]
        if self.trigger.trigger_type != expected_trigger:
            raise ValueError("fixture trigger does not match its track")
        if self.trigger.triggered_at != self.frozen_time:
            raise ValueError("fixture trigger and frozen time must match")
        ordinals = [turn.ordinal for turn in self.scripted_turns]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("scripted turn ordinals must be consecutive")
        call_ids = [
            call.call_id for turn in self.scripted_turns for call in turn.tool_calls
        ]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("scripted tool call IDs must be unique")
        return self


class SnapshotResult(StrictContractModel):
    title: NonEmptyText
    url: NonEmptyText


class SnapshotQuery(StrictContractModel):
    query: NonEmptyText
    results: Annotated[list[SnapshotResult], Field(min_length=1)]


class SnapshotPage(StrictContractModel):
    url: NonEmptyText
    title: NonEmptyText
    content: NonEmptyText


class ResourceSnapshot(StrictContractModel):
    schema_version: Literal["e1-resource-snapshot-v1"]
    snapshot_version: StableId
    provenance: Literal["public_and_fully_synthetic"]
    queries: Annotated[list[SnapshotQuery], Field(min_length=1)]
    pages: Annotated[list[SnapshotPage], Field(min_length=1)]
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_snapshot_semantics(self) -> Self:
        queries = [item.query for item in self.queries]
        page_urls = [item.url for item in self.pages]
        if len(queries) != len(set(queries)) or len(page_urls) != len(set(page_urls)):
            raise ValueError("snapshot queries and pages must be unique")
        registered_pages = set(page_urls)
        referenced_urls = {
            result.url for query in self.queries for result in query.results
        }
        if not referenced_urls.issubset(registered_pages):
            raise ValueError("snapshot search results must reference registered pages")
        for url in registered_pages:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".test"):
                raise ValueError("E1 Mini snapshot URLs must use HTTPS .test domains")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("snapshot URLs must not contain credentials or fragments")
        return self


class E1RunManifest(StrictContractModel):
    schema_version: Literal["e1-run-manifest-v1"]
    dataset_version: StableId
    fixture_files: Annotated[list[str], Field(min_length=1)]
    resource_snapshot_file: str
    default_model_mode: Literal["stub"]
    formal_evaluation_result: Literal[False]
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest_semantics(self) -> Self:
        if len(self.fixture_files) != len(set(self.fixture_files)):
            raise ValueError("runtime fixture paths must be unique")
        return self


class E1CaptureArtifact(StrictContractModel):
    schema_version: Literal["e1-capture-artifact-v1"]
    episode_id: StableId
    invocation_mode: Literal["stub", "real"]
    model_records: list[dict[str, JsonValue]]
    database_projection: dict[str, JsonValue]
    database_projection_sha256: Sha256
    delivery_attempts: list[dict[str, JsonValue]]
    isolation_evidence: dict[str, JsonValue]
    capture_sha256: Sha256


class E1RunOutputManifest(StrictContractModel):
    schema_version: Literal["e1-run-output-manifest-v1"]
    dataset_version: StableId
    invocation_mode: Literal["stub", "real"]
    formal_evaluation_result: Literal[False]
    evaluation_status: Literal["not_a_formal_model_evaluation"]
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    episode_digests: dict[str, Sha256]
    capture_digests: dict[str, Sha256]
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_output_semantics(self) -> Self:
        episode_ids = set(self.episode_ids)
        if len(episode_ids) != len(self.episode_ids):
            raise ValueError("output Episode IDs must be unique")
        if episode_ids != set(self.episode_digests) or episode_ids != set(
            self.capture_digests
        ):
            raise ValueError("output digest keys must match Episode IDs")
        return self


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "decision-episode-v1": DecisionEpisode,
    "decision-episode-v2": DecisionEpisodeV2,
    "acceptable-action-envelope-v1": AcceptableActionEnvelope,
    "environment-manifest-v1": EnvironmentManifest,
    "rule-result-v1": RuleResultV1,
}

DATASET_DOCUMENT_MODELS: dict[str, type[BaseModel]] = {
    **SCHEMA_MODELS,
    "e1-runtime-mini-fixture-v1": RuntimeMiniFixture,
    "e1-resource-snapshot-v1": ResourceSnapshot,
    "e1-run-manifest-v1": E1RunManifest,
    "e1-capture-artifact-v1": E1CaptureArtifact,
    "e1-run-output-manifest-v1": E1RunOutputManifest,
    "e2-run-output-manifest-v1": E2RunOutputManifest,
    "integrity-result-v1": IntegrityResultV1,
    "rule-run-manifest-v1": RuleRunManifestV1,
}
