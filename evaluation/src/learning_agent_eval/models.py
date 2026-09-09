"""Strict Pydantic sources for frozen and versioned evaluation contracts."""

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

from .eligibility import isolation_evidence_protocol_eligible
from .runtime_seed import RuntimeSeed

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
    provider: Literal["none", "tencent-tokenhub", "openai-compatible"]
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
    trigger_type: Literal["user_goal", "heartbeat", "submission", "constraint_change"]
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
    "chat_message",
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


DimensionId = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7"]
JudgeStatus = Literal["complete", "invalid_input", "judge_error"]
EvaluationStatus = Literal["not_a_formal_model_evaluation", "formal_model_evaluation"]


class JudgeDimensionV1(StrictContractModel):
    dimension_id: DimensionId
    level: Literal[0, 1, 2]
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1)]
    reason_code: StableId
    public_summary: NonEmptyText
    specific_issue: NonEmptyText | None

    @model_validator(mode="after")
    def validate_dimension(self) -> Self:
        if len(self.evidence_paths) != len(set(self.evidence_paths)):
            raise ValueError("Judge Evidence Paths must be unique within a dimension")
        if self.level < 2 and self.specific_issue is None:
            raise ValueError("non-full Judge dimensions require a specific issue")
        return self


class JudgeSemanticIssueV1(StrictContractModel):
    issue_id: StableId
    dimension_id: DimensionId | None
    severity: Literal["minor", "major", "critical"]
    reason_code: StableId
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1)]
    public_summary: NonEmptyText


class SuggestedHardGateV1(StrictContractModel):
    suggestion_id: StableId
    reason_code: StableId
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1)]
    public_summary: NonEmptyText


class JudgePublicFactCheckV1(StrictContractModel):
    claim: NonEmptyText
    verification: NonEmptyText
    consistent: bool
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1, max_length=2)]


class JudgeResponsePayloadV1(StrictContractModel):
    """Provider-visible output only; control-plane metadata is never model-authored."""

    audit_checks: list[JudgePublicFactCheckV1] = Field(default_factory=list)
    dimensions: Annotated[list[JudgeDimensionV1], Field(min_length=7, max_length=7)]
    semantic_issues: list[JudgeSemanticIssueV1]
    suggested_hard_gates: list[SuggestedHardGateV1]

    @model_validator(mode="after")
    def validate_dimension_order(self) -> Self:
        expected = ["D1", "D2", "D3", "D4", "D5", "D6", "D7"]
        if [item.dimension_id for item in self.dimensions] != expected:
            raise ValueError("Judge dimensions must use fixed D1-D7 order")
        issue_ids = [item.issue_id for item in self.semantic_issues]
        suggestion_ids = [item.suggestion_id for item in self.suggested_hard_gates]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("semantic issue IDs must be unique")
        if len(suggestion_ids) != len(set(suggestion_ids)):
            raise ValueError("suggested hard-gate IDs must be unique")
        return self


class JudgeResponsePayloadV2(JudgeResponsePayloadV1):
    """Active response contract; historical V1 keeps its optional checks."""

    audit_checks: Annotated[list[JudgePublicFactCheckV1], Field(min_length=1)]


class JudgeResultV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-result-v1.schema.json",
        },
    )

    schema_version: Literal["judge-result-v1"]
    judge_version: StableId
    episode_id: StableId
    episode_sha256: Sha256
    rule_result_sha256: Sha256
    track: Track
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    blind_input_sha256: Sha256
    dimensions: list[JudgeDimensionV1]
    semantic_issues: list[JudgeSemanticIssueV1]
    suggested_hard_gates: list[SuggestedHardGateV1]
    status: JudgeStatus
    judge_mode: Literal["stub", "real"]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    error_code: StableId | None
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_result_state(self) -> Self:
        if self.status == "complete":
            JudgeResponsePayloadV1(
                dimensions=self.dimensions,
                semantic_issues=self.semantic_issues,
                suggested_hard_gates=self.suggested_hard_gates,
            )
            if self.error_code is not None:
                raise ValueError("complete Judge Results cannot contain an error code")
        elif self.dimensions or self.semantic_issues or self.suggested_hard_gates:
            raise ValueError("non-complete Judge Results cannot contain Judge scores")
        elif self.error_code is None:
            raise ValueError("non-complete Judge Results require an error code")
        expected_formal = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected_formal:
            raise ValueError("Judge formal flag and evaluation status must agree")
        if self.status != "complete" and self.formal_evaluation_result:
            raise ValueError("non-complete Judge Results cannot be formal")
        if self.judge_mode == "stub" and self.formal_evaluation_result:
            raise ValueError("stub Judge Results cannot claim formal evaluation")
        return self


class JudgeRunManifestV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-run-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["judge-run-manifest-v1"]
    input_episode_schema_version: Literal["decision-episode-v2"]
    input_rule_schema_version: Literal["rule-result-v1"]
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    repair_limit: Literal[1]
    judge_mode: Literal["stub", "real"]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    input_episode_digests: dict[str, Sha256]
    input_rule_result_digests: dict[str, Sha256]
    blind_input_digests: dict[str, Sha256]
    judge_result_digests: dict[str, Sha256]
    result_statuses: dict[str, JudgeStatus]
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        episode_ids = set(self.episode_ids)
        if len(episode_ids) != len(self.episode_ids):
            raise ValueError("Judge output Episode IDs must be unique")
        if self.episode_ids != sorted(self.episode_ids):
            raise ValueError("Judge output Episode IDs must be sorted")
        if self.requested_episode_ids != sorted(set(self.requested_episode_ids)):
            raise ValueError("requested Judge Episode IDs must be sorted and unique")
        if (
            self.requested_episode_ids
            and set(self.requested_episode_ids) != episode_ids
        ):
            raise ValueError("requested Judge Episode IDs must match selected output")
        mappings = (
            self.input_episode_digests,
            self.input_rule_result_digests,
            self.blind_input_digests,
            self.judge_result_digests,
            self.result_statuses,
        )
        if any(set(mapping) != episode_ids for mapping in mappings):
            raise ValueError("Judge manifest mappings must match Episode IDs")
        expected_formal = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected_formal:
            raise ValueError("Judge manifest formal state is inconsistent")
        if self.judge_mode == "stub" and self.formal_evaluation_result:
            raise ValueError("stub Judge manifests cannot claim formal evaluation")
        return self


class AggregateDimensionV1(StrictContractModel):
    dimension_id: DimensionId
    level: Literal[0, 1, 2]
    weight: Literal[5, 10, 15, 20]
    weighted_signal: Annotated[float, Field(ge=0, le=20)]
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1)]


class AggregateRuleFailureV1(StrictContractModel):
    check_id: StableId
    severity: Literal["minor", "major", "critical"]
    reason_code: StableId
    evidence_paths: list[EvidencePath]


class AppliedScoreCapV1(StrictContractModel):
    cap_id: Literal["critical_hard_gate", "major_rule_fail"]
    maximum_score: Literal[39, 69]
    source_rule_ids: Annotated[list[StableId], Field(min_length=1)]


class AggregateResultV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-result-v1.schema.json",
        },
    )

    schema_version: Literal["aggregate-result-v1"]
    aggregator_version: StableId
    episode_id: StableId
    track: Track
    episode_sha256: Sha256
    rule_result_sha256: Sha256
    judge_result_sha256: Sha256
    status: JudgeStatus
    dimensions: list[AggregateDimensionV1]
    raw_score: Annotated[float, Field(ge=0, le=100)] | None
    rule_failures: list[AggregateRuleFailureV1]
    actual_hard_gates: list[StableId]
    judge_suggested_hard_gates: list[SuggestedHardGateV1]
    applied_caps: list[AppliedScoreCapV1]
    final_score: Annotated[float, Field(ge=0, le=100)] | None
    episode_outcome: Literal["pass", "fail", "invalid_input", "judge_error"]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    invalid_reason_code: StableId | None
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_aggregate_state(self) -> Self:
        expected_order = ["D1", "D2", "D3", "D4", "D5", "D6", "D7"]
        expected_formal = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected_formal:
            raise ValueError("aggregate formal flag and status must agree")
        if self.status == "complete":
            if [item.dimension_id for item in self.dimensions] != expected_order:
                raise ValueError("aggregate dimensions must use fixed D1-D7 order")
            if self.raw_score is None or self.final_score is None:
                raise ValueError("complete aggregates require scores")
            if self.invalid_reason_code is not None:
                raise ValueError("complete aggregates cannot contain invalid reasons")
            expected_outcome = "fail" if self.actual_hard_gates else "pass"
            if self.episode_outcome != expected_outcome:
                raise ValueError("aggregate outcome must preserve Rule hard gates")
        else:
            if (
                self.dimensions
                or self.raw_score is not None
                or self.final_score is not None
            ):
                raise ValueError("invalid aggregates cannot contain scores")
            if self.applied_caps:
                raise ValueError("invalid aggregates cannot apply score caps")
            if self.invalid_reason_code is None:
                raise ValueError("invalid aggregates require a reason code")
            if self.episode_outcome != self.status:
                raise ValueError("invalid aggregate outcome must match its status")
            if self.formal_evaluation_result:
                raise ValueError("invalid aggregates cannot be formal")
        return self


class AggregateTrackResultV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-track-result-v1.schema.json",
        },
    )

    schema_version: Literal["aggregate-track-result-v1"]
    aggregator_version: StableId
    track: Track
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    complete_episode_ids: list[StableId]
    failed_episode_ids: list[StableId]
    invalid_input_episode_ids: list[StableId]
    judge_error_episode_ids: list[StableId]
    score_count: Annotated[int, Field(ge=0)]
    mean_score: Annotated[float, Field(ge=0, le=100)] | None
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_track_summary(self) -> Self:
        episode_ids = set(self.episode_ids)
        groups = (
            self.complete_episode_ids,
            self.invalid_input_episode_ids,
            self.judge_error_episode_ids,
        )
        ordered_groups = (self.episode_ids, *groups, self.failed_episode_ids)
        if any(group != sorted(set(group)) for group in ordered_groups):
            raise ValueError("track aggregate Episode lists must be sorted and unique")
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("track aggregate groups must contain unique IDs")
        if set().union(*(set(group) for group in groups)) != episode_ids:
            raise ValueError("track aggregate groups must partition Episode IDs")
        if any(
            set(groups[index]) & set(groups[other])
            for index in range(3)
            for other in range(index + 1, 3)
        ):
            raise ValueError("track aggregate groups must not overlap")
        if not set(self.failed_episode_ids).issubset(set(self.complete_episode_ids)):
            raise ValueError("failed Episodes must be complete scored Episodes")
        if self.score_count != len(self.complete_episode_ids):
            raise ValueError("track score count must match complete Episodes")
        if (self.mean_score is None) != (self.score_count == 0):
            raise ValueError("track mean exists exactly when scored Episodes exist")
        expected_formal = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected_formal:
            raise ValueError("track aggregate formal state is inconsistent")
        return self


class AggregateRunManifestV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-run-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["aggregate-run-manifest-v1"]
    aggregator_version: StableId
    input_judge_manifest_sha256: Sha256
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    judge_mode: Literal["stub", "real"]
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    input_episode_digests: dict[str, Sha256]
    input_rule_result_digests: dict[str, Sha256]
    input_judge_result_digests: dict[str, Sha256]
    aggregate_result_digests: dict[str, Sha256]
    result_statuses: dict[str, JudgeStatus]
    track_result_digests: dict[str, Sha256]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_aggregate_manifest(self) -> Self:
        episode_ids = set(self.episode_ids)
        if len(episode_ids) != len(self.episode_ids):
            raise ValueError("aggregate Episode IDs must be unique")
        if self.episode_ids != sorted(self.episode_ids):
            raise ValueError("aggregate Episode IDs must be sorted")
        if self.requested_episode_ids != sorted(set(self.requested_episode_ids)):
            raise ValueError(
                "requested aggregate Episode IDs must be sorted and unique"
            )
        if (
            self.requested_episode_ids
            and set(self.requested_episode_ids) != episode_ids
        ):
            raise ValueError(
                "requested aggregate Episode IDs must match selected output"
            )
        mappings = (
            self.input_episode_digests,
            self.input_rule_result_digests,
            self.input_judge_result_digests,
            self.aggregate_result_digests,
            self.result_statuses,
        )
        if any(set(mapping) != episode_ids for mapping in mappings):
            raise ValueError("aggregate manifest mappings must match Episode IDs")
        if not self.track_result_digests:
            raise ValueError("aggregate manifest requires per-track results")
        expected_formal = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected_formal:
            raise ValueError("aggregate manifest formal state is inconsistent")
        if self.judge_mode == "stub" and self.formal_evaluation_result:
            raise ValueError("stub aggregate manifests cannot claim formal evaluation")
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
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(
                ".test"
            ):
                raise ValueError("E1 Mini snapshot URLs must use HTTPS .test domains")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError(
                    "snapshot URLs must not contain credentials or fragments"
                )
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


# E3.1 clean-switch contracts.  The frozen v1/v2 models above remain readable
# engineering history; only the contracts below may represent a formal run.
DatasetRoleV3 = Literal["engineering_mini", "primary_episode", "calibration_output"]
ActiveDatasetRole = Literal["engineering_mini", "primary_episode", "calibration_output", "protocol_pilot"]
ConstraintKindV1 = Literal["must_satisfy", "must_not"]
ConstraintEvaluationV1 = Literal["deterministic_rule", "semantic_judge"]
FailureClassV1 = Literal[
    "fixture_error",
    "provider_error",
    "framework_error",
    "isolation_violation",
]
ProviderScopeV1 = Literal["agent_runtime", "semantic_judge"]
AttributionStatusV1 = Literal["eligible", "ineligible_stub", "invalid"]


def _contains_private_reasoning_key(value: JsonValue) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {
                "analysis",
                "chain_of_thought",
                "private_reasoning",
                "reasoning_content",
            }:
                return True
            if _contains_private_reasoning_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_private_reasoning_key(item) for item in value)
    return False


class IdentityBindingV1(StrictContractModel):
    entity_type: EntityTypeV2
    logical_id: StableId
    identity_fields: Annotated[dict[str, JsonValue], Field(min_length=1)]


class JudgePredicateV1(StrictContractModel):
    path: EvidencePath
    operator: Literal[
        "equals",
        "not_equals",
        "contains",
        "not_contains",
        "exists",
        "not_exists",
        "greater_than_or_equal",
        "less_than_or_equal",
    ]
    expected_value: JsonValue

    @model_validator(mode="after")
    def validate_expected_value(self) -> Self:
        if self.operator in {"exists", "not_exists"} and not isinstance(
            self.expected_value, bool
        ):
            raise ValueError("existence predicates require a boolean expected value")
        return self


class JudgeConstraintV1(StrictContractModel):
    constraint_id: StableId
    kind: ConstraintKindV1
    evaluation: ConstraintEvaluationV1
    public_statement: NonEmptyText
    criticality: Literal["minor", "major", "critical"]
    evidence_paths: Annotated[list[EvidencePath], Field(min_length=1)]
    predicates: list[JudgePredicateV1]

    @model_validator(mode="after")
    def validate_constraint(self) -> Self:
        if len(self.evidence_paths) != len(set(self.evidence_paths)):
            raise ValueError("constraint Evidence Paths must be unique")
        predicate_paths = [item.path for item in self.predicates]
        if len(predicate_paths) != len(set(predicate_paths)):
            raise ValueError("constraint predicate paths must be unique")
        if self.evaluation == "deterministic_rule" and not self.predicates:
            raise ValueError("deterministic constraints require structured predicates")
        if any(path not in self.evidence_paths for path in predicate_paths):
            raise ValueError("predicate paths must be declared as constraint evidence")
        return self


class CaseJudgeCriteriaV1(StrictContractModel):
    allowed_action_classes: Annotated[list[ActionClass], Field(min_length=1)]
    constraints: Annotated[list[JudgeConstraintV1], Field(min_length=1)]
    acceptable_variations: Annotated[list[NonEmptyText], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_criteria(self) -> Self:
        if self.allowed_action_classes != list(
            dict.fromkeys(self.allowed_action_classes)
        ):
            raise ValueError("allowed action classes must be unique and ordered")
        ids = [item.constraint_id for item in self.constraints]
        if len(ids) != len(set(ids)):
            raise ValueError("case constraint IDs must be unique")
        return self


class CasePrivateAnnotationsV1(StrictContractModel):
    quality_label: Literal["good", "mild", "severe"] | None
    mutation_source: NonEmptyText | None
    author_role: RoleName
    reviewer_role: RoleName
    adjudication_note: NonEmptyText | None


class CaseRuntimeSetupV1(StrictContractModel):
    invocation_mode: Literal["stub", "real"]
    frozen_time: Rfc3339
    timezone: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=80,
            pattern=r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+)$",
        ),
    ]
    owner_id: StableId
    run_id: StableId
    session_id: StableId | None
    trigger: Trigger
    state_before: StateBefore
    seed_kind: Track
    seed: dict[str, JsonValue]
    scripted_turns: list[ScriptedModelTurn]
    resource_snapshot_version: StableId

    @model_validator(mode="after")
    def validate_runtime_setup(self) -> Self:
        if self.trigger.triggered_at != self.frozen_time:
            raise ValueError("runtime trigger and frozen time must match")
        if self.invocation_mode == "stub" and not self.scripted_turns:
            raise ValueError("stub CaseSpecs require fixed scripted turns")
        if self.invocation_mode == "real" and self.scripted_turns:
            raise ValueError("real CaseSpecs cannot contain scripted model answers")
        return self


class CaseSpecV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/case-spec-v1.schema.json",
        },
    )

    schema_version: Literal["case-spec-v1"]
    case_id: StableId
    scenario_family_id: StableId
    track: Track
    split: Split
    difficulty: Difficulty
    tags: Annotated[list[Tag], Field(min_length=1)]
    dataset_role: DatasetRoleV3
    runtime_setup: CaseRuntimeSetupV1
    identity_bindings: Annotated[list[IdentityBindingV1], Field(min_length=1)]
    judge_criteria: CaseJudgeCriteriaV1
    private_annotations: CasePrivateAnnotationsV1
    case_spec_sha256: Sha256

    @model_validator(mode="after")
    def validate_case_spec(self) -> Self:
        if self.track != self.runtime_setup.seed_kind:
            raise ValueError("CaseSpec track and runtime seed kind must match")
        expected_trigger = {
            "planning": "user_goal",
            "intervention": "heartbeat",
            "assessment": "submission",
            "revision": "constraint_change",
        }[self.track]
        if self.runtime_setup.trigger.trigger_type != expected_trigger:
            raise ValueError("CaseSpec trigger does not match its track")
        if self.dataset_role == "engineering_mini":
            if self.runtime_setup.invocation_mode != "stub":
                raise ValueError("engineering Mini CaseSpecs must use stub mode")
            if self.private_annotations.quality_label is not None:
                raise ValueError(
                    "engineering Mini CaseSpecs cannot carry quality labels"
                )
        elif self.dataset_role == "calibration_output":
            if self.private_annotations.quality_label is None:
                raise ValueError(
                    "Calibration CaseSpecs require a private quality label"
                )
        elif self.private_annotations.quality_label is not None:
            raise ValueError("Primary CaseSpecs cannot carry Calibration labels")
        binding_ids = [item.logical_id for item in self.identity_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("identity binding logical IDs must be unique")
        return self


class JudgeReferenceV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-reference-v1.schema.json",
        },
    )

    schema_version: Literal["judge-reference-v1"]
    opaque_case_id: StableId
    case_spec_sha256: Sha256
    track: Track
    allowed_action_classes: Annotated[list[ActionClass], Field(min_length=1)]
    constraints: Annotated[list[JudgeConstraintV1], Field(min_length=1)]
    acceptable_variations: Annotated[list[NonEmptyText], Field(min_length=1)]
    reference_sha256: Sha256

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        ids = [item.constraint_id for item in self.constraints]
        if len(ids) != len(set(ids)):
            raise ValueError("JudgeReference constraint IDs must be unique")
        return self


class ProviderCallAttestationV1(StrictContractModel):
    call_id: StableId
    request_model: NonEmptyText
    response_model: NonEmptyText | None
    provider_request_id: NonEmptyText | None
    requested_at: Rfc3339
    responded_at: Rfc3339 | None
    status: Literal["completed", "provider_error", "framework_error"]

    @model_validator(mode="after")
    def validate_call_state(self) -> Self:
        if self.status == "completed" and (
            self.response_model is None or self.responded_at is None
        ):
            raise ValueError("completed provider calls require response attribution")
        if self.responded_at is not None:
            requested = datetime.fromisoformat(
                self.requested_at.replace("Z", "+00:00")
            )
            responded = datetime.fromisoformat(
                self.responded_at.replace("Z", "+00:00")
            )
            if responded < requested:
                raise ValueError("provider response cannot precede its request")
        return self


class ProviderAttestationV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/provider-attestation-v1.schema.json",
        },
    )

    schema_version: Literal["provider-attestation-v1"]
    scope: ProviderScopeV1
    invocation_mode: Literal["stub", "real"]
    provider_id: Literal["none", "tencent-tokenhub", "openai-compatible"]
    api_base: NonEmptyText | None = None
    endpoint_policy_version: StableId
    endpoint_policy_sha256: Sha256
    endpoint_id: StableId | None
    endpoint_origin: NonEmptyText | None
    configured_model: NonEmptyText
    calls: list[ProviderCallAttestationV1]
    configuration_sha256: Sha256
    git_commit: GitCommit
    worktree_clean: bool
    dependency_lock_version: StableId
    dependency_lock_sha256: Sha256
    dependency_lock_verified: bool
    attribution_status: AttributionStatusV1
    reason_codes: list[StableId]
    attestation_sha256: Sha256

    @model_validator(mode="after")
    def validate_attribution(self) -> Self:
        call_ids = [item.call_id for item in self.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("provider call attestation IDs must be unique")
        if self.invocation_mode == "stub":
            if (
                self.provider_id != "none"
                or self.attribution_status != "ineligible_stub"
            ):
                raise ValueError("stub attribution must be explicitly ineligible")
            if self.endpoint_id is not None or self.endpoint_origin is not None:
                raise ValueError("stub attribution cannot claim a provider endpoint")
        else:
            from .runtime_metadata import provider_attribution_reason_codes

            expected_reasons = list(
                provider_attribution_reason_codes(self.model_dump(mode="json"))
            )
            expected_status = "invalid" if expected_reasons else "eligible"
            if self.attribution_status != expected_status:
                raise ValueError("real attribution eligibility was not recomputed")
            if self.reason_codes != expected_reasons:
                raise ValueError("real attribution reason codes are incomplete")
        if self.attribution_status == "eligible" and (
            self.invocation_mode != "real"
            or not self.worktree_clean
            or self.endpoint_id is None
            or self.endpoint_origin is None
            or not self.dependency_lock_verified
            or bool(self.reason_codes)
            or any(item.status != "completed" for item in self.calls)
        ):
            raise ValueError("eligible attribution requires a clean complete real run")
        if self.attribution_status == "invalid" and not self.reason_codes:
            raise ValueError("invalid attribution requires stable reason codes")
        return self


class RuntimeManifestV2(StrictContractModel):
    git_commit: GitCommit
    worktree_clean: bool
    database_mode: Literal["temporary_fixture"]
    fixture_db_sha256: Sha256
    dependency_lock_version: StableId
    dependency_lock_sha256: Sha256


class EnvironmentManifestV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/environment-manifest-v2.schema.json",
        },
    )

    schema_version: Literal["environment-manifest-v2"]
    frozen_time: Rfc3339
    timezone: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=80,
            pattern=r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+)$",
        ),
    ]
    prompt: PromptManifest
    tools: ToolManifest
    policies: PolicyManifest
    resources: ResourceManifest
    runtime: RuntimeManifestV2
    isolation: IsolationManifest
    provider_attestation: ProviderAttestationV1
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_runtime_attribution_linkage(self) -> Self:
        attestation = self.provider_attestation
        if attestation.scope != "agent_runtime":
            raise ValueError("Environment attribution must describe the Agent Runtime")
        if (
            self.runtime.git_commit != attestation.git_commit
            or self.runtime.worktree_clean != attestation.worktree_clean
            or self.runtime.dependency_lock_version
            != attestation.dependency_lock_version
            or self.runtime.dependency_lock_sha256
            != attestation.dependency_lock_sha256
        ):
            raise ValueError("Runtime Manifest and Provider attribution must agree")
        return self


class VisibleMessageV1(StrictContractModel):
    ordinal: Annotated[int, Field(ge=1)]
    role: Literal["system", "developer", "user", "assistant", "tool"]
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def reject_private_reasoning(self) -> Self:
        if _contains_private_reasoning_key(self.payload):
            raise ValueError(
                "model-visible messages cannot retain private reasoning fields"
            )
        return self


class ModelVisibleContextV1(StrictContractModel):
    context_version: Literal["model-visible-context-v1"]
    messages: Annotated[list[VisibleMessageV1], Field(min_length=1)]
    tool_schemas: list[dict[str, JsonValue]]
    context_sha256: Sha256

    @model_validator(mode="after")
    def validate_context_order(self) -> Self:
        ordinals = [item.ordinal for item in self.messages]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("visible message ordinals must be consecutive")
        if _contains_private_reasoning_key(self.tool_schemas):
            raise ValueError("tool schemas cannot contain private reasoning fields")
        return self


CallPurposeV1 = Literal[
    "decision",
    "subagent_decision",
    "subagent_synthesis",
    "session_title",
    "memory_compression",
]


class ModelCallV3(StrictContractModel):
    call_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    run_id: StableId
    parent_run_id: StableId | None
    parent_call_id: StableId | None
    depth: Annotated[int, Field(ge=0)]
    call_purpose: CallPurposeV1
    decision_relevant: bool
    visible_context: ModelVisibleContextV1
    request_model: NonEmptyText
    assistant_text: str | None
    tool_call_refs: list[StableId]
    status: Literal["completed", "provider_error", "framework_error", "cancelled"]
    response_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_call_semantics(self) -> Self:
        decision_purposes = {
            "decision",
            "subagent_decision",
            "subagent_synthesis",
        }
        if self.decision_relevant != (self.call_purpose in decision_purposes):
            raise ValueError("model call purpose and decision relevance must agree")
        if self.depth == 0 and (
            self.parent_run_id is not None or self.parent_call_id is not None
        ):
            raise ValueError("root model calls cannot have parent references")
        if self.depth > 0 and self.parent_run_id is None:
            raise ValueError("nested model calls require a parent run")
        if self.status == "completed" and self.response_sha256 is None:
            raise ValueError("completed model calls require a response digest")
        if self.status != "completed" and self.response_sha256 is not None:
            raise ValueError("failed model calls cannot invent a response digest")
        return self


class ObservableTraceV3(StrictContractModel):
    capture_mode: Literal["runtime_recording"]
    model_calls: Annotated[list[ModelCallV3], Field(min_length=1)]
    decision_call_refs: Annotated[list[StableId], Field(min_length=1)]
    auxiliary_call_refs: list[StableId]
    tool_invocations: list[ToolInvocationV2]
    run_events: list[RunEvent]
    operations: list[OperationV2]
    guard_decisions: list[GuardDecisionV2]

    @model_validator(mode="after")
    def validate_call_partition(self) -> Self:
        call_ids = [item.call_id for item in self.model_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("model call IDs must be unique")
        if [item.ordinal for item in self.model_calls] != list(
            range(1, len(self.model_calls) + 1)
        ):
            raise ValueError("model call ordinals must be consecutive")
        expected_decision = [
            item.call_id for item in self.model_calls if item.decision_relevant
        ]
        expected_auxiliary = [
            item.call_id for item in self.model_calls if not item.decision_relevant
        ]
        if self.decision_call_refs != expected_decision:
            raise ValueError("decision call refs must preserve recorded call order")
        if self.auxiliary_call_refs != expected_auxiliary:
            raise ValueError("auxiliary call refs must preserve recorded call order")
        known = set(call_ids)
        if any(
            item.parent_call_id is not None and item.parent_call_id not in known
            for item in self.model_calls
        ):
            raise ValueError("parent call references must resolve within the trace")
        return self


class EpisodeCompletenessV3(StrictContractModel):
    status: Literal["complete", "invalid"]
    evidence_error_codes: list[StableId]
    verified_evidence_paths: list[EvidencePath]
    decision_correctness_evaluated: Literal[False]
    completeness_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence_status(self) -> Self:
        if (self.status == "complete") != (not self.evidence_error_codes):
            raise ValueError("v3 completeness is determined only by evidence errors")
        return self


class TypedSourceRefV3(StrictContractModel):
    source_type: Literal[
        "runtime",
        "model_call",
        "tool_invocation",
        "guard_decision",
        "operation",
        "run_event",
        "entity",
    ]
    ref: StableId


class ModelAttemptV3(StrictContractModel):
    attempt_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    call_ref: StableId
    attempted_action: Literal["tool_call", "respond", "wait", "no_op"]
    invocation_refs: list[StableId]


class FinalEffectV3(StrictContractModel):
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
    source_refs: Annotated[list[TypedSourceRefV3], Field(min_length=1)]


class DecisionLayersV3(StrictContractModel):
    model_attempts: list[ModelAttemptV3]
    guard_decision_refs: list[StableId]
    final_effects: Annotated[list[FinalEffectV3], Field(min_length=1)]
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


class DecisionResultV3(StrictContractModel):
    action_class: ActionClass
    action_mapping_version: StableId
    action_mapping_sha256: Sha256
    user_visible_output: str | None
    guard: GuardDecisionSummaryV2
    layers: DecisionLayersV3


class ProvenanceV3(StrictContractModel):
    source_type: Literal["runtime_export"]
    construction_method: Literal["runtime_recorded"]
    dataset_role: DatasetRoleV3
    runtime_executed: Literal[True]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    created_at: Rfc3339
    source_refs: list[StableId]
    episode_sha256: Sha256

    @model_validator(mode="after")
    def validate_formal_state(self) -> Self:
        expected = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected:
            raise ValueError("v3 provenance formal flag and status must agree")
        if self.dataset_role != "primary_episode" and self.formal_evaluation_result:
            raise ValueError("only Primary Episodes can be formal results")
        return self


class DecisionEpisodeV3(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/decision-episode-v3.schema.json",
        },
    )

    schema_version: Literal["decision-episode-v3"]
    episode_id: StableId
    case_spec_sha256: Sha256
    judge_reference_sha256: Sha256
    scenario_family_id: StableId
    track: Track
    split: Split
    difficulty: Difficulty
    trigger: Trigger
    state_before: StateSnapshotV2
    state_after: StateSnapshotV2
    state_delta: StateDeltaV2
    environment: EnvironmentManifestV2
    observable_trace: ObservableTraceV3
    result: DecisionResultV3
    completeness: EpisodeCompletenessV3
    isolation_evidence: RuntimeIsolationEvidenceV2
    provenance: ProvenanceV3

    @model_validator(mode="after")
    def validate_episode_formal_state(self) -> Self:
        eligible = (
            self.environment.provider_attestation.attribution_status == "eligible"
        )
        if self.provenance.formal_evaluation_result and not eligible:
            raise ValueError("formal v3 Episodes require eligible provider attribution")
        return self


class RuntimeFailureV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/runtime-failure-v1.schema.json",
        },
    )

    schema_version: Literal["runtime-failure-v1"]
    failure_id: StableId
    case_id: StableId
    case_spec_sha256: Sha256
    stage: StableId
    failure_class: FailureClassV1
    reason_code: StableId
    public_summary: NonEmptyText
    model_calls: list[ModelCallV3]
    provider_attestation: ProviderAttestationV1
    isolation_evidence: RuntimeIsolationEvidenceV2 | None
    started_at: Rfc3339
    failed_at: Rfc3339
    formal_evaluation_result: Literal[False]
    evaluation_status: Literal["not_a_formal_model_evaluation"]
    failure_sha256: Sha256


class RuntimeTerminalRecordV2(StrictContractModel):
    case_id: StableId
    case_spec_sha256: Sha256
    track: Track
    terminal_kind: Literal["episode", "failure"]
    artifact_id: StableId
    artifact_sha256: Sha256
    formal_evaluation_result: bool

    @model_validator(mode="after")
    def validate_terminal_formal_state(self) -> Self:
        if self.terminal_kind == "failure" and self.formal_evaluation_result:
            raise ValueError("Runtime Failures cannot be formal results")
        return self


class RuntimeRunManifestV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/runtime-run-manifest-v2.schema.json",
        },
    )

    schema_version: Literal["runtime-run-manifest-v2"]
    dataset_version: StableId
    case_schema_version: Literal["case-spec-v1"]
    episode_schema_version: Literal["decision-episode-v3"]
    failure_schema_version: Literal["runtime-failure-v1"]
    invocation_mode: Literal["stub", "real"]
    selected_case_ids: Annotated[list[StableId], Field(min_length=1)]
    terminals: Annotated[list[RuntimeTerminalRecordV2], Field(min_length=1)]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    git_commit: GitCommit
    dependency_lock_version: StableId
    dependency_lock_sha256: Sha256
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_terminal_partition(self) -> Self:
        selected = self.selected_case_ids
        terminal_cases = [item.case_id for item in self.terminals]
        if selected != sorted(set(selected)):
            raise ValueError("selected runtime Case IDs must be sorted and unique")
        if terminal_cases != selected:
            raise ValueError(
                "every selected CaseSpec requires exactly one terminal artifact"
            )
        artifact_ids = [item.artifact_id for item in self.terminals]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("runtime terminal artifact IDs must be unique")
        expected = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected:
            raise ValueError("runtime manifest formal state is inconsistent")
        if self.invocation_mode == "stub" and self.formal_evaluation_result:
            raise ValueError("stub runtime manifests cannot be formal")
        if any(item.terminal_kind == "failure" for item in self.terminals) and expected:
            raise ValueError(
                "runtime batches containing failures cannot be formal results"
            )
        terminal_formal = bool(
            self.invocation_mode == "real"
            and all(
                item.terminal_kind == "episode" and item.formal_evaluation_result
                for item in self.terminals
            )
        )
        if self.formal_evaluation_result != terminal_formal:
            raise ValueError("runtime manifest formal state must match all terminals")
        return self


class CaseSuiteManifestV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/case-suite-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["case-suite-manifest-v1"]
    dataset_version: StableId
    case_files: Annotated[list[str], Field(min_length=1)]
    resource_snapshot_file: str
    default_model_mode: Literal["stub", "real"]
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_case_suite(self) -> Self:
        if self.case_files != sorted(set(self.case_files)):
            raise ValueError("CaseSpec paths must be sorted and unique")
        if any(not path or path.startswith(("/", "../")) for path in self.case_files):
            raise ValueError("CaseSpec paths must be contained relative paths")
        if not self.resource_snapshot_file or self.resource_snapshot_file.startswith(
            ("/", "../")
        ):
            raise ValueError("resource Snapshot path must be contained and relative")
        return self


class RuleResultV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/rule-result-v2.schema.json",
        },
    )

    schema_version: Literal["rule-result-v2"]
    evaluator_version: StableId
    episode_id: StableId
    episode_sha256: Sha256
    case_spec_sha256: Sha256
    judge_reference_sha256: Sha256
    rule_pack_version: StableId
    rule_pack_sha256: Sha256
    checks: Annotated[list[RuleCheckV1], Field(min_length=1)]
    hard_gates: list[StableId]
    dimension_signals: dict[str, JsonValue]
    status: Literal["pass", "fail", "invalid_input"]
    formal_evaluation_result: bool
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_rule_result(self) -> Self:
        ids = [item.check_id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule check IDs must be unique")
        expected_gates = [
            item.check_id
            for item in self.checks
            if item.status == "fail" and item.severity == "critical"
        ]
        if self.hard_gates != expected_gates:
            raise ValueError("hard gates must exactly list failed critical checks")
        expected_status = (
            "invalid_input"
            if any(item.status == "invalid_input" for item in self.checks)
            else "fail"
            if any(item.status == "fail" for item in self.checks)
            else "pass"
        )
        if self.status != expected_status:
            raise ValueError("Rule Result status must match its checks")
        return self


class RuleRunManifestV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/rule-run-manifest-v2.schema.json",
        },
    )

    schema_version: Literal["rule-run-manifest-v2"]
    input_episode_schema_version: Literal["decision-episode-v3"]
    input_reference_schema_version: Literal["judge-reference-v1"]
    evaluator_version: StableId
    rule_pack_version: StableId
    rule_pack_sha256: Sha256
    input_runtime_manifest_sha256: Sha256
    invocation_mode: Literal["stub", "real"]
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    runtime_failure_ids: list[StableId]
    input_episode_digests: dict[str, Sha256]
    input_reference_digests: dict[str, Sha256]
    input_runtime_failure_digests: dict[str, Sha256]
    runtime_failure_tracks: dict[str, Track]
    rule_result_digests: dict[str, Sha256]
    formal_evaluation_result: bool
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_rule_manifest(self) -> Self:
        if self.episode_ids != sorted(set(self.episode_ids)):
            raise ValueError("Rule manifest Episode IDs must be sorted and unique")
        if self.requested_episode_ids != sorted(set(self.requested_episode_ids)):
            raise ValueError("requested Rule Episode IDs must be sorted and unique")
        if self.requested_episode_ids and set(self.requested_episode_ids) != set(
            self.episode_ids
        ):
            raise ValueError("requested Rule Episode IDs must match selected output")
        if self.runtime_failure_ids != sorted(set(self.runtime_failure_ids)):
            raise ValueError("Rule runtime Failure IDs must be sorted and unique")
        ids = set(self.episode_ids)
        if any(
            set(mapping) != ids
            for mapping in (
                self.input_episode_digests,
                self.input_reference_digests,
                self.rule_result_digests,
            )
        ):
            raise ValueError("Rule manifest mappings must match Episode IDs")
        failure_ids = set(self.runtime_failure_ids)
        if (
            set(self.input_runtime_failure_digests) != failure_ids
            or set(self.runtime_failure_tracks) != failure_ids
        ):
            raise ValueError("Rule failure mappings must match runtime Failure IDs")
        return self


class JudgeResultV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-result-v2.schema.json",
        },
    )

    schema_version: Literal["judge-result-v2"]
    judge_version: StableId
    episode_id: StableId
    episode_sha256: Sha256
    rule_result_sha256: Sha256
    judge_reference_sha256: Sha256
    track: Track
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    blind_input_sha256: Sha256
    dimensions: list[JudgeDimensionV1]
    semantic_issues: list[JudgeSemanticIssueV1]
    suggested_hard_gates: list[SuggestedHardGateV1]
    status: JudgeStatus
    judge_mode: Literal["stub", "real"]
    provider_attestation: ProviderAttestationV1
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    error_code: StableId | None
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_judge_state(self) -> Self:
        if self.status == "complete":
            JudgeResponsePayloadV1(
                dimensions=self.dimensions,
                semantic_issues=self.semantic_issues,
                suggested_hard_gates=self.suggested_hard_gates,
            )
            if self.error_code is not None:
                raise ValueError("complete Judge Results cannot contain errors")
        elif self.dimensions or self.semantic_issues or self.suggested_hard_gates:
            raise ValueError("non-complete Judge Results cannot contain scores")
        elif self.error_code is None:
            raise ValueError("non-complete Judge Results require a stable error code")
        expected = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected:
            raise ValueError("Judge Result formal state is inconsistent")
        eligible = self.provider_attestation.attribution_status == "eligible"
        if expected and not (self.status == "complete" and eligible):
            raise ValueError(
                "formal Judge Results require eligible complete attribution"
            )
        if self.judge_mode == "stub" and expected:
            raise ValueError("stub Judge Results cannot be formal")
        if self.judge_mode != self.provider_attestation.invocation_mode:
            raise ValueError("Judge mode must match provider attestation")
        if self.provider_attestation.scope != "semantic_judge":
            raise ValueError("Judge attribution must describe the semantic Judge")
        return self


class JudgeRunManifestV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-run-manifest-v2.schema.json",
        },
    )

    schema_version: Literal["judge-run-manifest-v2"]
    input_episode_schema_version: Literal["decision-episode-v3"]
    input_rule_schema_version: Literal["rule-result-v2"]
    input_reference_schema_version: Literal["judge-reference-v1"]
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    repair_limit: Literal[1]
    judge_mode: Literal["stub", "real"]
    input_runtime_manifest_sha256: Sha256
    input_rule_manifest_sha256: Sha256
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    runtime_failure_ids: list[StableId]
    input_episode_digests: dict[str, Sha256]
    input_rule_result_digests: dict[str, Sha256]
    input_reference_digests: dict[str, Sha256]
    input_runtime_failure_digests: dict[str, Sha256]
    runtime_failure_tracks: dict[str, Track]
    blind_input_digests: dict[str, Sha256]
    judge_result_digests: dict[str, Sha256]
    result_statuses: dict[str, JudgeStatus]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_judge_manifest(self) -> Self:
        if self.episode_ids != sorted(set(self.episode_ids)):
            raise ValueError("Judge manifest Episode IDs must be sorted and unique")
        if self.requested_episode_ids != sorted(set(self.requested_episode_ids)):
            raise ValueError("requested Judge Episode IDs must be sorted and unique")
        if self.requested_episode_ids and set(self.requested_episode_ids) != set(
            self.episode_ids
        ):
            raise ValueError("requested Judge Episode IDs must match selected output")
        if self.runtime_failure_ids != sorted(set(self.runtime_failure_ids)):
            raise ValueError("Judge runtime Failure IDs must be sorted and unique")
        ids = set(self.episode_ids)
        mappings = (
            self.input_episode_digests,
            self.input_rule_result_digests,
            self.input_reference_digests,
            self.blind_input_digests,
            self.judge_result_digests,
            self.result_statuses,
        )
        if any(set(mapping) != ids for mapping in mappings):
            raise ValueError("Judge manifest mappings must match Episode IDs")
        failure_ids = set(self.runtime_failure_ids)
        if (
            set(self.input_runtime_failure_digests) != failure_ids
            or set(self.runtime_failure_tracks) != failure_ids
        ):
            raise ValueError("Judge failure mappings must match runtime Failure IDs")
        expected = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected:
            raise ValueError("Judge manifest formal state is inconsistent")
        if self.judge_mode == "stub" and expected:
            raise ValueError("stub Judge manifests cannot be formal")
        return self


class AggregateResultV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-result-v2.schema.json",
        },
    )

    schema_version: Literal["aggregate-result-v2"]
    aggregator_version: StableId
    episode_id: StableId
    track: Track
    episode_sha256: Sha256
    rule_result_sha256: Sha256
    judge_result_sha256: Sha256
    judge_reference_sha256: Sha256
    status: JudgeStatus
    dimensions: list[AggregateDimensionV1]
    raw_score: Annotated[float, Field(ge=0, le=100)] | None
    rule_failures: list[AggregateRuleFailureV1]
    actual_hard_gates: list[StableId]
    judge_suggested_hard_gates: list[SuggestedHardGateV1]
    applied_caps: list[AppliedScoreCapV1]
    final_score: Annotated[float, Field(ge=0, le=100)] | None
    episode_outcome: Literal["pass", "fail", "invalid_input", "judge_error"]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    invalid_reason_code: StableId | None
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        expected_order = ["D1", "D2", "D3", "D4", "D5", "D6", "D7"]
        expected_formal = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected_formal:
            raise ValueError("aggregate formal state is inconsistent")
        if self.status == "complete":
            if [item.dimension_id for item in self.dimensions] != expected_order:
                raise ValueError("aggregate dimensions must use fixed D1-D7 order")
            if self.raw_score is None or self.final_score is None:
                raise ValueError("complete aggregates require scores")
            if self.invalid_reason_code is not None:
                raise ValueError("complete aggregates cannot contain invalid reasons")
            expected_outcome = "fail" if self.actual_hard_gates else "pass"
            if self.episode_outcome != expected_outcome:
                raise ValueError("Rule hard gates determine aggregate outcome")
        else:
            if (
                self.dimensions
                or self.raw_score is not None
                or self.final_score is not None
            ):
                raise ValueError("invalid aggregates cannot contain scores")
            if self.applied_caps:
                raise ValueError("invalid aggregates cannot apply caps")
            if self.invalid_reason_code is None or self.episode_outcome != self.status:
                raise ValueError("invalid aggregate classification must be explicit")
            if self.formal_evaluation_result:
                raise ValueError("invalid aggregates cannot be formal")
        return self


class AggregateTrackResultV2(AggregateTrackResultV1):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-track-result-v2.schema.json",
        },
    )

    schema_version: Literal["aggregate-track-result-v2"]  # type: ignore[assignment]
    runtime_failure_ids: list[StableId]

    @model_validator(mode="after")
    def validate_runtime_failures(self) -> Self:
        if self.runtime_failure_ids != sorted(set(self.runtime_failure_ids)):
            raise ValueError("track runtime Failure IDs must be sorted and unique")
        return self


class AggregateRunManifestV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-run-manifest-v2.schema.json",
        },
    )

    schema_version: Literal["aggregate-run-manifest-v2"]
    aggregator_version: StableId
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    judge_mode: Literal["stub", "real"]
    input_judge_manifest_sha256: Sha256
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: Annotated[list[StableId], Field(min_length=1)]
    runtime_failure_ids: list[StableId]
    input_episode_digests: dict[str, Sha256]
    input_rule_result_digests: dict[str, Sha256]
    input_judge_result_digests: dict[str, Sha256]
    input_reference_digests: dict[str, Sha256]
    input_runtime_failure_digests: dict[str, Sha256]
    runtime_failure_tracks: dict[str, Track]
    aggregate_result_digests: dict[str, Sha256]
    result_statuses: dict[str, JudgeStatus]
    track_result_digests: dict[str, Sha256]
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_aggregate_manifest(self) -> Self:
        if self.episode_ids != sorted(set(self.episode_ids)):
            raise ValueError("aggregate manifest Episode IDs must be sorted and unique")
        if self.requested_episode_ids != sorted(set(self.requested_episode_ids)):
            raise ValueError(
                "requested aggregate Episode IDs must be sorted and unique"
            )
        if self.requested_episode_ids and set(self.requested_episode_ids) != set(
            self.episode_ids
        ):
            raise ValueError(
                "requested aggregate Episode IDs must match selected output"
            )
        if self.runtime_failure_ids != sorted(set(self.runtime_failure_ids)):
            raise ValueError("aggregate runtime Failure IDs must be sorted and unique")
        ids = set(self.episode_ids)
        mappings = (
            self.input_episode_digests,
            self.input_rule_result_digests,
            self.input_judge_result_digests,
            self.input_reference_digests,
            self.aggregate_result_digests,
            self.result_statuses,
        )
        if any(set(mapping) != ids for mapping in mappings):
            raise ValueError("aggregate manifest mappings must match Episode IDs")
        failure_ids = set(self.runtime_failure_ids)
        if (
            set(self.input_runtime_failure_digests) != failure_ids
            or set(self.runtime_failure_tracks) != failure_ids
        ):
            raise ValueError(
                "aggregate failure mappings must match runtime Failure IDs"
            )
        if not self.track_result_digests:
            raise ValueError("aggregate manifest requires per-track results")
        expected = self.evaluation_status == "formal_model_evaluation"
        if self.formal_evaluation_result != expected:
            raise ValueError("aggregate manifest formal state is inconsistent")
        return self


# E3.1.1 clean-switch contracts. Earlier E3.1 contracts remain registered as
# immutable engineering history; the active chain uses the versions below.
PredicateSemanticsV2 = Literal["constraint-proposition-v1"]
ActionDeclarationStatusV1 = Literal[
    "valid", "missing", "invalid", "not_applicable"
]


# E3.1.2 release-governance contracts.  Artifact schema versions remain
# independent from the top-level Evaluation Protocol Release version.
class ActionDeclarationFramingV2(StrictContractModel):
    prefix: Literal["<model-action-v2>"]
    suffix: Literal["</model-action-v2>"]
    position: Literal["first_nonempty_content", "single_frame_anywhere"]
    duplicate_policy: Literal["reject"]


class ActionDeclarationJsonContractV2(StrictContractModel):
    required_fields: list[Literal["action_classes"]]
    allowed_fields: list[Literal["action_classes"]]
    unknown_fields: Literal["reject"]
    insignificant_formatting: list[
        Literal["json_whitespace", "object_key_order"]
    ]
    canonicalize_after_parse: Literal[True]


ActionProtocolInstruction = Annotated[
    str, StringConstraints(min_length=1, max_length=20_000)
]


class ActionMeaningV2(StrictContractModel):
    track: Literal[
        "planning",
        "intervention",
        "assessment",
        "revision",
        "planning_or_intervention",
        "planning_intervention_or_revision",
    ]
    decision_semantics: NonEmptyText
    required_fields: list[Literal["action_classes"]]
    allowed_fields: list[Literal["action_classes"]]
    forbidden_fields: list[StableId]
    boundary: NonEmptyText
    positive_example: NonEmptyText
    negative_example: NonEmptyText
    combinable_with: list[ActionClass]
    missing_declaration_policy: Literal["record_as_scoreable_behavior_failure"]


class InspectionExceptionV1(StrictContractModel):
    assistant_content: Literal["empty"]
    all_tools_in: Annotated[list[StableId], Field(min_length=1)]
    web_search_save_results: Literal[False]
    trajectory_retained: Literal[True]


class ToolActionTransportV1(StrictContractModel):
    field: Literal["evaluation_action_classes"]
    producer: Literal["model_only"]
    strip_before_product_tool: Literal[True]
    stream: Literal[False]
    content_disagreement: Literal["invalid"]
    missing: Literal["retain_and_score_failure"]


class ActionDeclarationProtocolV2(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/action-declaration-protocol-v2.schema.json",
        },
    )

    schema_version: Literal["action-declaration-protocol-v2"]
    version: Literal["model-action-declaration-v2"]
    framing: ActionDeclarationFramingV2
    json_contract: ActionDeclarationJsonContractV2
    actions: dict[ActionClass, ActionMeaningV2]
    allowed_combinations: list[list[ActionClass]]
    missing_or_invalid_policy: Literal["retain_episode_and_score_behavior_failure"]
    inspection_exception: InspectionExceptionV1
    instruction: ActionProtocolInstruction
    tool_argument_transport: ToolActionTransportV1 | None = None

    @model_validator(mode="after")
    def validate_dictionary(self) -> Self:
        from .action_protocol import INSPECTION_TOOLS

        expected = set(ActionClass.__args__)  # type: ignore[attr-defined]
        if set(self.actions) != expected or len(self.actions) != len(expected):
            raise ValueError("action dictionary must contain all 14 canonical actions")
        if self.allowed_combinations != [
            ["INSUFFICIENT_EVIDENCE", "REQUEST_CLARIFICATION"]
        ]:
            raise ValueError("action combinations must match the frozen protocol")
        if self.inspection_exception.all_tools_in != sorted(INSPECTION_TOOLS):
            raise ValueError("inspection exception must match the recorded read tool policy")
        return self


class ActiveArtifactChainV1(StrictContractModel):
    case_spec: Literal["case-spec-v2"]
    decision_episode: Literal["decision-episode-v4"]
    runtime_failure: Literal["runtime-failure-v2"]
    runtime_manifest: Literal["runtime-run-manifest-v3"]
    rule_result: Literal["rule-result-v3"]
    rule_manifest: Literal["rule-run-manifest-v3"]
    judge_result: Literal["judge-result-v3"]
    judge_manifest: Literal["judge-run-manifest-v3"]
    aggregate_result: Literal["aggregate-result-v3"]
    aggregate_track_result: Literal["aggregate-track-result-v3"]
    aggregate_manifest: Literal["aggregate-run-manifest-v3"]


class ProtocolSchemaBindingV1(StrictContractModel):
    schema_version: StableId
    relative_path: NonEmptyText
    schema_id: NonEmptyText
    raw_sha256: Sha256

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("protocol schema paths must be contained and relative")
        return self


class ProtocolSourceBundleBindingV1(StrictContractModel):
    component: Literal["runtime", "rules", "judge", "aggregate"]
    bundle_version: StableId
    bundle_sha256: Sha256


class FormalCapabilityPolicyV1(StrictContractModel):
    single_artifact_claim: Literal["forbidden"]
    trusted_registry_required: Literal[True]
    posthoc_filter_policy: Literal["blocks_formal_capability"]
    runtime_failure_policy: Literal["disclose_and_block_formal_capability"]
    invalid_input_policy: Literal["disclose_exclude_from_mean_and_block"]
    judge_error_policy: Literal["disclose_exclude_from_mean_and_block"]
    track_reporting_policy: Literal["four_tracks_no_overall"]


class EvaluationProtocolReleaseV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/evaluation-protocol-release-v1.schema.json",
        },
    )

    schema_version: Literal["evaluation-protocol-release-v1"]
    protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    protocol_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12", "1.13", "1.14", "1.15"]
    release_status: Literal["active"]
    active_chain: ActiveArtifactChainV1
    artifact_schemas: Annotated[list[ProtocolSchemaBindingV1], Field(min_length=1)]
    schema_lock_version: Literal["evaluation-schema-lock-v1", "evaluation-schema-lock-1.1", "evaluation-schema-lock-1.2", "evaluation-schema-lock-1.3", "evaluation-schema-lock-1.4", "evaluation-schema-lock-1.5", "evaluation-schema-lock-1.6", "evaluation-schema-lock-1.7", "evaluation-schema-lock-1.8", "evaluation-schema-lock-1.9", "evaluation-schema-lock-1.10", "evaluation-schema-lock-1.11", "evaluation-schema-lock-1.12", "evaluation-schema-lock-1.13", "evaluation-schema-lock-1.14", "evaluation-schema-lock-1.15"]
    schema_lock_sha256: Sha256
    canonicalization_version: StableId
    canonicalization_sha256: Sha256
    digest_algorithm: Literal["sha256"]
    action_protocol_version: StableId
    action_protocol_relative_path: Literal[
        "evaluation/releases/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.1/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.2/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.3/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.4/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.5/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.6/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.7/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.8/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.9/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.10/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.11/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.12/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.13/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.14/model-action-declaration-v2.json",
        "evaluation/releases/protocol-1.15/model-action-declaration-v2.json",
    ]
    action_protocol_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    rule_pack_version: StableId
    rule_pack_sha256: Sha256
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    aggregator_version: StableId
    source_bundles: Annotated[
        list[ProtocolSourceBundleBindingV1], Field(min_length=4, max_length=4)
    ]
    blinding_policy_version: StableId
    blinding_policy_sha256: Sha256
    evidence_path_policy_version: StableId
    evidence_path_policy_sha256: Sha256
    formal_capability_policy: FormalCapabilityPolicyV1
    release_sha256: Sha256

    @model_validator(mode="after")
    def validate_protocol_release(self) -> Self:
        if self.protocol_release_id != f"evaluation-protocol-release-{self.protocol_version}":
            raise ValueError("protocol release identity and version must match")
        expected_lock = "evaluation-schema-lock-v1" if self.protocol_version == "1.0" else f"evaluation-schema-lock-{self.protocol_version}"
        expected_action = "evaluation/releases/model-action-declaration-v2.json" if self.protocol_version == "1.0" else f"evaluation/releases/protocol-{self.protocol_version}/model-action-declaration-v2.json"
        if self.schema_lock_version != expected_lock or self.action_protocol_relative_path != expected_action:
            raise ValueError("protocol assets must belong to the same release version")
        schema_versions = [item.schema_version for item in self.artifact_schemas]
        if schema_versions != sorted(set(schema_versions)):
            raise ValueError("protocol schema bindings must be sorted and unique")
        components = [item.component for item in self.source_bundles]
        if components != ["aggregate", "judge", "rules", "runtime"]:
            raise ValueError("protocol source bundles must use fixed component order")
        return self


class BenchmarkCaseBindingV1(StrictContractModel):
    ordinal: Annotated[int, Field(ge=1)]
    relative_path: NonEmptyText
    case_id: StableId
    case_spec_sha256: Sha256
    dataset_role: ActiveDatasetRole
    split: Split
    track: Track

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Benchmark Case paths must be contained and relative")
        return self


class BenchmarkTrackCountsV1(StrictContractModel):
    planning: Annotated[int, Field(ge=0)]
    intervention: Annotated[int, Field(ge=0)]
    assessment: Annotated[int, Field(ge=0)]
    revision: Annotated[int, Field(ge=0)]


class BenchmarkPartitionV1(StrictContractModel):
    dataset_role: ActiveDatasetRole
    case_ids: list[StableId]
    expected_track_counts: BenchmarkTrackCountsV1

    @model_validator(mode="after")
    def validate_case_ids(self) -> Self:
        if self.case_ids != sorted(set(self.case_ids)):
            raise ValueError("Benchmark partition Case IDs must be sorted and unique")
        return self


class BenchmarkResourceBindingV1(StrictContractModel):
    snapshot_version: StableId
    relative_path: NonEmptyText
    snapshot_sha256: Sha256

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Benchmark resource paths must be contained and relative")
        return self


class BenchmarkProtocolBindingsV1(StrictContractModel):
    schema_set_sha256: Sha256
    action_protocol_sha256: Sha256
    rubric_sha256: Sha256
    track_anchor_sha256: Sha256
    rule_pack_sha256: Sha256
    judge_prompt_sha256: Sha256
    runtime_source_bundle_sha256: Sha256
    rules_source_bundle_sha256: Sha256
    judge_source_bundle_sha256: Sha256
    aggregate_source_bundle_sha256: Sha256


class BenchmarkReleaseManifestV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/benchmark-release-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["benchmark-release-manifest-v1"]
    benchmark_release_id: StableId
    benchmark_version: StableId
    release_status: Literal["engineering", "candidate", "released", "retired"]
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    case_schema_version: Literal["case-spec-v2"]
    case_ordering: Literal["fixed_ordinal"]
    case_suite_digest_rule: Literal["ordered-case-bindings-and-resource-v1"]
    cases: Annotated[list[BenchmarkCaseBindingV1], Field(min_length=1)]
    case_suite_sha256: Sha256
    partitions: Annotated[list[BenchmarkPartitionV1], Field(min_length=3, max_length=4)]
    expected_total_cases: Annotated[int, Field(ge=1)]
    mutation_source_lineage_sha256: Sha256
    resource_snapshots: Annotated[
        list[BenchmarkResourceBindingV1], Field(min_length=1)
    ]
    protocol_bindings: BenchmarkProtocolBindingsV1
    canonicalization_version: StableId
    digest_algorithm: Literal["sha256"]
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_benchmark_release(self) -> Self:
        if [item.ordinal for item in self.cases] != list(
            range(1, len(self.cases) + 1)
        ):
            raise ValueError("Benchmark Cases must use contiguous fixed ordinals")
        case_ids = [item.case_id for item in self.cases]
        case_paths = [item.relative_path for item in self.cases]
        if len(case_ids) != len(set(case_ids)) or len(case_paths) != len(
            set(case_paths)
        ):
            raise ValueError("Benchmark Cases and paths must be unique")
        if self.expected_total_cases != len(self.cases):
            raise ValueError("Benchmark expected total must match bound Cases")
        roles = [item.dataset_role for item in self.partitions]
        expected_roles = ["calibration_output", "engineering_mini", "primary_episode"]
        if any(item.dataset_role == "protocol_pilot" for item in self.cases):
            expected_roles.append("protocol_pilot")
            if self.release_status != "engineering":
                raise ValueError("protocol pilots cannot become a released Benchmark")
        if roles != expected_roles:
            raise ValueError("Benchmark partitions must use fixed role order")
        partition_ids = [case_id for item in self.partitions for case_id in item.case_ids]
        if len(partition_ids) != len(set(partition_ids)) or set(partition_ids) != set(
            case_ids
        ):
            raise ValueError("Benchmark partitions must exactly partition Cases")
        cases_by_id = {item.case_id: item for item in self.cases}
        for partition in self.partitions:
            if any(
                cases_by_id[case_id].dataset_role != partition.dataset_role
                for case_id in partition.case_ids
            ):
                raise ValueError("Benchmark partition role differs from Case binding")
            observed = {
                track: sum(
                    cases_by_id[case_id].track == track
                    for case_id in partition.case_ids
                )
                for track in ("planning", "intervention", "assessment", "revision")
            }
            if partition.expected_track_counts.model_dump() != observed:
                raise ValueError("Benchmark partition track counts differ from Cases")
        resource_paths = [item.relative_path for item in self.resource_snapshots]
        if resource_paths != sorted(set(resource_paths)):
            raise ValueError("Benchmark resources must be sorted and unique")
        return self


class SourceBundleFileV1(StrictContractModel):
    relative_path: NonEmptyText
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("source bundle paths must be contained and relative")
        return self


class SourceBundleManifestV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/source-bundle-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["source-bundle-manifest-v1"]
    bundle_version: Literal["evaluation-source-bundle-v2"]
    component: Literal["runtime", "rules", "judge", "aggregate"]
    inclusion_policy: Literal[
        "evaluation-package-conservative-v1",
        "evaluation-and-product-runtime-conservative-v1",
    ]
    files: Annotated[list[SourceBundleFileV1], Field(min_length=1)]
    bundle_sha256: Sha256

    @model_validator(mode="after")
    def validate_bundle_files(self) -> Self:
        paths = [item.relative_path for item in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("source bundle files must be sorted and unique")
        expected_policy = (
            "evaluation-and-product-runtime-conservative-v1"
            if self.component == "runtime"
            else "evaluation-package-conservative-v1"
        )
        if self.inclusion_policy != expected_policy:
            raise ValueError("source bundle component and policy differ")
        return self


class SchemaLockEntryV1(StrictContractModel):
    relative_path: NonEmptyText
    schema_version: StableId
    schema_id: NonEmptyText
    raw_sha256: Sha256
    frozen_in: StableId
    status: Literal["historical_frozen", "active_release"]

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Schema lock paths must be contained and relative")
        return self


class SchemaLockManifestV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/schema-lock-manifest-v1.schema.json",
        },
    )

    schema_version: Literal["schema-lock-manifest-v1"]
    lock_version: Literal["evaluation-schema-lock-v1", "evaluation-schema-lock-1.1", "evaluation-schema-lock-1.2", "evaluation-schema-lock-1.3", "evaluation-schema-lock-1.4", "evaluation-schema-lock-1.5", "evaluation-schema-lock-1.6", "evaluation-schema-lock-1.7", "evaluation-schema-lock-1.8", "evaluation-schema-lock-1.9", "evaluation-schema-lock-1.10", "evaluation-schema-lock-1.11", "evaluation-schema-lock-1.12", "evaluation-schema-lock-1.13", "evaluation-schema-lock-1.14", "evaluation-schema-lock-1.15"]
    entries: Annotated[list[SchemaLockEntryV1], Field(min_length=1)]
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_lock_entries(self) -> Self:
        versions = [item.schema_version for item in self.entries]
        paths = [item.relative_path for item in self.entries]
        if versions != sorted(set(versions)) or paths != sorted(set(paths)):
            raise ValueError("Schema lock entries must be sorted and unique")
        return self


class TrustedBenchmarkReleaseV1(StrictContractModel):
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    manifest_relative_path: NonEmptyText
    case_suite_sha256: Sha256
    expected_total_cases: Annotated[int, Field(ge=1)]
    expected_track_counts: BenchmarkTrackCountsV1
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest_path(self) -> Self:
        path = self.manifest_relative_path
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("trusted Benchmark paths must be contained and relative")
        return self


class TrustedBenchmarkRegistryV1(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/trusted-benchmark-registry-v1.schema.json",
        },
    )

    schema_version: Literal["trusted-benchmark-registry-v1"]
    registry_version: Literal[
        "production-trusted-benchmark-registry-v1",
        "test-only-trusted-benchmark-registry-v1",
    ]
    entries: list[TrustedBenchmarkReleaseV1]
    registry_sha256: Sha256

    @model_validator(mode="after")
    def validate_registry_entries(self) -> Self:
        ids = [item.benchmark_release_id for item in self.entries]
        if ids != sorted(set(ids)):
            raise ValueError("trusted Benchmark registry entries must be sorted and unique")
        return self


class ScriptedModelTurnV2(ScriptedModelTurn):
    declared_actions: list[ActionClass]
    stub_request_user_prefix: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_declared_actions(self) -> Self:
        if self.declared_actions != list(dict.fromkeys(self.declared_actions)):
            raise ValueError("scripted declared actions must be unique and ordered")
        return self


class CaseRuntimeSetupV2(CaseRuntimeSetupV1):
    seed: RuntimeSeed
    scripted_turns: list[ScriptedModelTurnV2]

    @model_validator(mode="after")
    def validate_active_script(self) -> Self:
        if self.seed_kind != "planning" and not self.seed.get("plan_title"):
            raise ValueError("a seeded learning plan requires a title")
        if self.seed_kind == "assessment" and not self.seed.get("task_title"):
            raise ValueError("Assessment requires a seeded task")
        if self.invocation_mode == "real" and self.seed.get("evaluation_injected_failure"):
            raise ValueError("real Cases cannot inject engineering failures")
        ordinals = [turn.ordinal for turn in self.scripted_turns]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("scripted turn ordinals must be consecutive")
        call_ids = [
            call.call_id for turn in self.scripted_turns for call in turn.tool_calls
        ]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("scripted tool call IDs must be unique")
        prefixes = [
            turn.stub_request_user_prefix
            for turn in self.scripted_turns
            if turn.stub_request_user_prefix is not None
        ]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("stub request user prefixes must be unique")
        return self


class CaseJudgeCriteriaV2(CaseJudgeCriteriaV1):
    predicate_semantics: PredicateSemanticsV2

    @model_validator(mode="after")
    def validate_constraint_kinds(self) -> Self:
        kinds = {item.kind for item in self.constraints}
        if kinds != {"must_satisfy", "must_not"}:
            raise ValueError(
                "CaseSpec v2 requires both must_satisfy and must_not constraints"
            )
        return self


class CaseSpecV2(CaseSpecV1):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/case-spec-v2.schema.json",
        },
    )

    schema_version: Literal["case-spec-v2"]  # type: ignore[assignment]
    dataset_role: ActiveDatasetRole
    runtime_setup: CaseRuntimeSetupV2
    judge_criteria: CaseJudgeCriteriaV2


class JudgeReferenceV2(JudgeReferenceV1):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-reference-v2.schema.json",
        },
    )

    schema_version: Literal["judge-reference-v2"]  # type: ignore[assignment]
    predicate_semantics: PredicateSemanticsV2


class ReturnedToolCall(StrictContractModel):
    call_id: str
    name: str
    canonical_arguments: dict[str, JsonValue]
    argument_error: Literal["invalid_json", "non_object_json"] | None = None


class ModelCallV4(ModelCallV3):
    request_config: dict[str, JsonValue] = Field(default_factory=dict)
    token_usage: dict[str, Annotated[int, Field(ge=0)]] | None = None
    response_validation_errors: list[dict[str, str]] = Field(default_factory=list)
    returned_tool_calls: list[ReturnedToolCall] = Field(default_factory=list)
    action_protocol_version: Literal["model-action-declaration-v2"]
    action_protocol_sha256: Sha256
    action_declaration_status: ActionDeclarationStatusV1
    declared_action_classes: list[ActionClass]

    @model_validator(mode="after")
    def validate_action_declaration(self) -> Self:
        if self.declared_action_classes != list(
            dict.fromkeys(self.declared_action_classes)
        ):
            raise ValueError("declared action classes must be unique and ordered")
        from .action_protocol import inspection_only_response

        applicable = (self.call_purpose == "decision" and self.status == "completed"
                      and not inspection_only_response(self.assistant_text or "", [c.model_dump() for c in self.returned_tool_calls]))
        if self.depth > 0 and self.decision_relevant and self.parent_call_id is None:
            raise ValueError("nested decision calls require a parent model call")
        if not applicable:
            if (
                self.action_declaration_status != "not_applicable"
                or self.declared_action_classes
            ):
                raise ValueError(
                    "only completed main decision calls carry action declarations"
                )
        elif self.action_declaration_status == "valid":
            if not self.declared_action_classes:
                raise ValueError("valid action declarations cannot be empty")
        elif (
            self.action_declaration_status not in {"missing", "invalid"}
            or self.declared_action_classes
        ):
            raise ValueError("invalid or missing declarations cannot invent actions")
        return self


class ToolObservationV4(ToolInvocationV2):
    # A pure read resolves to a durable tool.completed RunEvent. It does not
    # claim a business write or an Operation.
    record_source: Literal["tool_invocation", "run_event"]


class ObservableTraceV4(ObservableTraceV3):
    model_calls: Annotated[list[ModelCallV4], Field(min_length=1)]
    tool_invocations: list[ToolObservationV4]


class ModelAttemptV4(ModelAttemptV3):
    declared_action_classes: list[ActionClass]
    action_declaration_status: ActionDeclarationStatusV1


class FinalEffectV4(StrictContractModel):
    effect_id: StableId
    ordinal: Annotated[int, Field(ge=1)]
    effect_type: Literal[
        "plan_proposal",
        "user_input_request",
        "wait",
        "intervention_message",
        "intervention_quiz_or_review",
        "plan_adjustment_proposal",
        "assessment_accept",
        "assessment_revision",
        "insufficient_evidence",
        "clarification_request",
        "no_op",
        "change_proposal",
        "reversible_patch",
        "approval_request",
        "blocked",
        "deferred",
        "failed",
        "unclassified",
    ]
    status: Literal[
        "applied", "blocked", "deferred", "pending", "no_change", "failed"
    ]
    action_classes: list[ActionClass]
    entity_refs: list[StableId]
    source_refs: Annotated[list[TypedSourceRefV3], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_action_classes(self) -> Self:
        if self.action_classes != list(dict.fromkeys(self.action_classes)):
            raise ValueError("effect action classes must be unique and ordered")
        if self.effect_type == "unclassified" and self.action_classes:
            raise ValueError("unclassified effects cannot invent an action class")
        if self.effect_type != "unclassified" and not self.action_classes:
            raise ValueError("classified effects require an action class")
        return self


class DecisionLayersV4(StrictContractModel):
    model_attempts: list[ModelAttemptV4]
    guard_decision_refs: list[StableId]
    final_effects: Annotated[list[FinalEffectV4], Field(min_length=1)]
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
    protocol_eligibility: Literal["eligible", "invalid"]


class DecisionResultV4(StrictContractModel):
    action_class: ActionClass
    action_classes: Annotated[list[ActionClass], Field(min_length=1)]
    classification_issues: list[StableId]
    action_mapping_version: StableId
    action_mapping_sha256: Sha256
    user_visible_output: str | None
    guard: GuardDecisionSummaryV2
    layers: DecisionLayersV4

    @model_validator(mode="after")
    def validate_action_summary(self) -> Self:
        if self.action_classes != list(dict.fromkeys(self.action_classes)):
            raise ValueError("result action classes must be unique and ordered")
        if self.action_class != self.action_classes[0]:
            raise ValueError("primary action class must be the first observed action")
        if self.classification_issues != sorted(set(self.classification_issues)):
            raise ValueError("classification issues must be sorted and unique")
        return self


class ActiveArtifactProvenanceV1(StrictContractModel):
    source_type: Literal["runtime_export"]
    construction_method: Literal["runtime_recorded"]
    dataset_role: ActiveDatasetRole
    runtime_executed: Literal[True]
    protocol_eligible: bool
    provider_eligible: bool
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    runtime_run_id: StableId
    runtime_source_bundle_version: Literal["evaluation-source-bundle-v2"]
    runtime_source_bundle_sha256: Sha256
    formal_evaluation_result: Literal[False]
    evaluation_status: Literal["not_a_formal_model_evaluation"]
    created_at: Rfc3339
    source_refs: list[StableId]
    episode_sha256: Sha256


class DecisionEpisodeV4(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/decision-episode-v4.schema.json",
        },
    )

    schema_version: Literal["decision-episode-v4"]
    episode_id: StableId
    case_spec_sha256: Sha256
    judge_reference_sha256: Sha256
    scenario_family_id: StableId
    track: Track
    split: Split
    difficulty: Difficulty
    trigger: Trigger
    state_before: StateSnapshotV2
    state_after: StateSnapshotV2
    state_delta: StateDeltaV2
    environment: EnvironmentManifestV2
    observable_trace: ObservableTraceV4
    result: DecisionResultV4
    completeness: EpisodeCompletenessV3
    isolation_evidence: RuntimeIsolationEvidenceV2
    provenance: ActiveArtifactProvenanceV1

    @model_validator(mode="after")
    def validate_episode_formal_state(self) -> Self:
        provider_eligible = (
            self.environment.provider_attestation.attribution_status == "eligible"
        )
        if self.provenance.provider_eligible != provider_eligible:
            raise ValueError("Episode provider eligibility must match attestation")
        protocol_eligible = (
            self.completeness.status == "complete"
            and not self.completeness.evidence_error_codes
            and isolation_evidence_protocol_eligible(
                self.isolation_evidence.model_dump(mode="json", by_alias=True)
            )
        )
        if self.provenance.protocol_eligible != protocol_eligible:
            raise ValueError(
                "Episode protocol eligibility must match completeness and isolation"
            )
        expected_layer = "eligible" if protocol_eligible else "invalid"
        if self.result.layers.protocol_eligibility != expected_layer:
            raise ValueError("Episode protocol layer must match isolation evidence")
        return self


class RuntimeFailureV2(RuntimeFailureV1):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/runtime-failure-v2.schema.json",
        },
    )

    schema_version: Literal["runtime-failure-v2"]  # type: ignore[assignment]
    model_calls: list[ModelCallV4]
    protocol_eligible: bool
    provider_eligible: bool
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    runtime_run_id: StableId
    runtime_source_bundle_version: Literal["evaluation-source-bundle-v2"]
    runtime_source_bundle_sha256: Sha256

    @model_validator(mode="after")
    def validate_failure_eligibility(self) -> Self:
        expected = self.provider_attestation.attribution_status == "eligible"
        if self.provider_eligible != expected:
            raise ValueError("Failure provider eligibility must match attestation")
        isolation = (
            self.isolation_evidence.model_dump(mode="json", by_alias=True)
            if self.isolation_evidence is not None
            else None
        )
        expected_protocol = bool(
            self.failure_class != "isolation_violation"
            and isolation_evidence_protocol_eligible(isolation)
        )
        if self.protocol_eligible != expected_protocol:
            raise ValueError("Failure protocol eligibility must match isolation evidence")
        return self


class RuntimeTerminalRecordV3(RuntimeTerminalRecordV2):
    runtime_run_id: StableId
    protocol_eligible: bool
    provider_eligible: bool
    formal_evaluation_result: Literal[False]  # type: ignore[assignment]


class CaseSuiteManifestV2(CaseSuiteManifestV1):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/case-suite-manifest-v2.schema.json",
        },
    )

    schema_version: Literal["case-suite-manifest-v2"]  # type: ignore[assignment]
    case_schema_version: Literal["case-spec-v2"]
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_file: NonEmptyText
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    case_suite_sha256: Sha256
    benchmark_expected_total_cases: Annotated[int, Field(ge=1)]
    benchmark_expected_track_counts: BenchmarkTrackCountsV1

    @model_validator(mode="after")
    def validate_release_path(self) -> Self:
        path = self.benchmark_release_file
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("Benchmark Release path must be contained and relative")
        return self


class RuntimeRunManifestV3(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/runtime-run-manifest-v3.schema.json",
        },
    )

    schema_version: Literal["runtime-run-manifest-v3"]
    runtime_run_id: StableId
    dataset_version: StableId
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    case_suite_sha256: Sha256
    benchmark_expected_total_cases: Annotated[int, Field(ge=1)]
    benchmark_expected_track_counts: BenchmarkTrackCountsV1
    case_schema_version: Literal["case-spec-v2"]
    episode_schema_version: Literal["decision-episode-v4"]
    failure_schema_version: Literal["runtime-failure-v2"]
    invocation_mode: Literal["stub", "real"]
    selection_mode: Literal["unfiltered_suite", "adhoc_filter"]
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    selected_case_ids: Annotated[list[StableId], Field(min_length=1)]
    terminals: Annotated[list[RuntimeTerminalRecordV3], Field(min_length=1)]
    runtime_source_bundle_version: Literal["evaluation-source-bundle-v2"]
    runtime_source_bundle_sha256: Sha256
    protocol_release_verified: bool
    protocol_eligible: bool
    provider_eligible: bool
    benchmark_release_trusted: bool
    suite_complete: bool
    trusted_benchmark_run: bool
    trust_reason_codes: list[StableId]
    formal_evaluation_result: Literal[False]
    evaluation_status: Literal["not_a_formal_model_evaluation"]
    git_commit: GitCommit
    worktree_clean: bool
    dependency_lock_version: StableId
    dependency_lock_sha256: Sha256
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_runtime_manifest(self) -> Self:
        terminal_cases = [item.case_id for item in self.terminals]
        if self.suite_complete:
            counts = {
                track: sum(item.track == track for item in self.terminals)
                for track in ("planning", "intervention", "assessment", "revision")
            }
            if (len(self.terminals) != self.benchmark_expected_total_cases
                    or counts != self.benchmark_expected_track_counts.model_dump()):
                raise ValueError("complete Suite must match declared total and track inventory")
        if self.selected_case_ids != sorted(set(self.selected_case_ids)):
            raise ValueError("selected runtime Case IDs must be sorted and unique")
        if terminal_cases != self.selected_case_ids:
            raise ValueError(
                "every selected CaseSpec requires exactly one terminal artifact"
            )
        artifact_ids = [item.artifact_id for item in self.terminals]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("runtime terminal artifact IDs must be unique")
        if any(item.runtime_run_id != self.runtime_run_id for item in self.terminals):
            raise ValueError("runtime terminals must bind the same Runtime Run")
        if self.requested_episode_ids != sorted(set(self.requested_episode_ids)):
            raise ValueError("requested Episode IDs must be sorted and unique")
        expected_provider = bool(
            self.invocation_mode == "real"
            and all(item.provider_eligible for item in self.terminals)
        )
        if self.provider_eligible != expected_provider:
            raise ValueError("runtime Provider eligibility must match all terminals")
        expected_protocol = bool(
            self.worktree_clean
            and self.protocol_release_verified
            and all(item.protocol_eligible for item in self.terminals)
        )
        if self.protocol_eligible != expected_protocol:
            raise ValueError("runtime protocol eligibility must match its terminals")
        expected_trust = bool(
            self.protocol_eligible
            and self.provider_eligible
            and self.benchmark_release_trusted
            and self.suite_complete
            and self.selection_mode == "unfiltered_suite"
        )
        if self.trusted_benchmark_run != expected_trust:
            raise ValueError("trusted Benchmark Run state is inconsistent")
        if self.trust_reason_codes != sorted(set(self.trust_reason_codes)):
            raise ValueError("Runtime trust reason codes must be sorted and unique")
        if self.selection_mode == "unfiltered_suite" and (
            self.requested_episode_ids or self.selected_track is not None
        ):
            raise ValueError("unfiltered execution cannot claim an ad-hoc filter")
        if self.selection_mode == "adhoc_filter" and not (
            self.requested_episode_ids or self.selected_track is not None
        ):
            raise ValueError("ad-hoc selection requires an explicit filter")
        return self


EvaluationSelectionV1 = Literal["inherited", "adhoc_filter"]


def _selection_mode_matches_filters(
    selection_mode: EvaluationSelectionV1,
    requested_episode_ids: list[str],
    selected_track: str | None,
) -> bool:
    filtered = bool(requested_episode_ids or selected_track is not None)
    if filtered:
        return selection_mode == "adhoc_filter"
    return selection_mode in {"inherited", "adhoc_filter"}


class RuleResultV3(RuleResultV2):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/rule-result-v3.schema.json",
        },
    )

    schema_version: Literal["rule-result-v3"]  # type: ignore[assignment]
    evaluator_implementation_version: StableId
    evaluator_implementation_sha256: Sha256
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    runtime_run_id: StableId
    input_runtime_manifest_sha256: Sha256
    input_runtime_formal_evaluation_result: Literal[False]
    input_episode_formal_evaluation_result: Literal[False]
    input_runtime_trusted_benchmark_run: bool
    selection_mode: EvaluationSelectionV1
    worktree_clean: bool
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    formal_evaluation_result: Literal[False]  # type: ignore[assignment]

    @model_validator(mode="after")
    def validate_formal_inheritance(self) -> Self:
        expected = bool(
            self.input_runtime_trusted_benchmark_run
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
        )
        if self.trusted_benchmark_run != expected:
            raise ValueError("Rule Result trust must monotonically inherit Runtime")
        return self


class RuleRunManifestV3(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/rule-run-manifest-v3.schema.json",
        },
    )

    schema_version: Literal["rule-run-manifest-v3"]
    runtime_run_id: StableId
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    case_suite_sha256: Sha256
    input_episode_schema_version: Literal["decision-episode-v4"]
    input_reference_schema_version: Literal["judge-reference-v2"]
    input_failure_schema_version: Literal["runtime-failure-v2"]
    evaluator_version: StableId
    evaluator_implementation_version: StableId
    evaluator_implementation_sha256: Sha256
    rule_pack_version: StableId
    rule_pack_sha256: Sha256
    input_runtime_manifest_sha256: Sha256
    input_runtime_formal_evaluation_result: Literal[False]
    input_runtime_trusted_benchmark_run: bool
    invocation_mode: Literal["stub", "real"]
    selection_mode: EvaluationSelectionV1
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: list[StableId]
    runtime_failure_ids: list[StableId]
    input_episode_digests: dict[str, Sha256]
    input_reference_digests: dict[str, Sha256]
    input_runtime_failure_digests: dict[str, Sha256]
    runtime_failure_tracks: dict[str, Track]
    rule_result_digests: dict[str, Sha256]
    result_formal_evaluation_states: dict[str, Literal[False]]
    result_trusted_benchmark_states: dict[str, bool]
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    formal_evaluation_result: Literal[False]
    worktree_clean: bool
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_rule_manifest(self) -> Self:
        ordered_lists = (
            self.episode_ids,
            self.requested_episode_ids,
            self.runtime_failure_ids,
        )
        if any(values != sorted(set(values)) for values in ordered_lists):
            raise ValueError("Rule manifest identifiers must be sorted and unique")
        if not self.episode_ids and not self.runtime_failure_ids:
            raise ValueError("Rule manifest requires an Episode or Runtime Failure")
        if self.requested_episode_ids and set(self.requested_episode_ids) != set(
            self.episode_ids
        ):
            raise ValueError("requested Rule Episode IDs must match selected output")
        if not _selection_mode_matches_filters(
            self.selection_mode, self.requested_episode_ids, self.selected_track
        ):
            raise ValueError("Rule selection mode must match its explicit filters")
        episode_ids = set(self.episode_ids)
        episode_mappings = (
            self.input_episode_digests,
            self.input_reference_digests,
            self.rule_result_digests,
            self.result_formal_evaluation_states,
            self.result_trusted_benchmark_states,
        )
        if any(set(mapping) != episode_ids for mapping in episode_mappings):
            raise ValueError("Rule manifest mappings must match Episode IDs")
        failure_ids = set(self.runtime_failure_ids)
        if (
            set(self.input_runtime_failure_digests) != failure_ids
            or set(self.runtime_failure_tracks) != failure_ids
        ):
            raise ValueError("Rule failure mappings must match Runtime Failure IDs")
        expected = bool(
            self.input_runtime_trusted_benchmark_run
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
            and all(self.result_trusted_benchmark_states.values())
        )
        if self.trusted_benchmark_run != expected:
            raise ValueError("Rule manifest trust must monotonically inherit Runtime")
        return self


class JudgeResultV3(JudgeResultV2):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-result-v3.schema.json",
        },
    )

    schema_version: Literal["judge-result-v3"]  # type: ignore[assignment]
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    runtime_run_id: StableId
    judge_source_bundle_version: Literal["evaluation-source-bundle-v2"]
    judge_source_bundle_sha256: Sha256
    input_rule_manifest_sha256: Sha256
    input_rule_manifest_formal_evaluation_result: Literal[False]
    input_episode_formal_evaluation_result: Literal[False]
    input_rule_result_formal_evaluation_result: Literal[False]
    input_rule_manifest_trusted_benchmark_run: bool
    selection_mode: EvaluationSelectionV1
    worktree_clean: bool
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    formal_evaluation_result: Literal[False]  # type: ignore[assignment]

    @model_validator(mode="after")
    def validate_formal_inheritance(self) -> Self:
        expected = bool(
            self.judge_mode == "real"
            and self.provider_attestation.attribution_status == "eligible"
            and self.input_rule_manifest_trusted_benchmark_run
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
        )
        if self.trusted_benchmark_run != expected:
            raise ValueError("Judge Result trust must monotonically inherit Rules")
        return self


class JudgeRunManifestV3(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/judge-run-manifest-v3.schema.json",
        },
    )

    schema_version: Literal["judge-run-manifest-v3"]
    runtime_run_id: StableId
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    case_suite_sha256: Sha256
    input_episode_schema_version: Literal["decision-episode-v4"]
    input_rule_schema_version: Literal["rule-result-v3"]
    input_reference_schema_version: Literal["judge-reference-v2"]
    input_failure_schema_version: Literal["runtime-failure-v2"]
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    repair_limit: Literal[1]
    judge_mode: Literal["stub", "real"]
    judge_source_bundle_version: Literal["evaluation-source-bundle-v2"]
    judge_source_bundle_sha256: Sha256
    input_runtime_manifest_sha256: Sha256
    input_rule_manifest_sha256: Sha256
    input_rule_manifest_formal_evaluation_result: Literal[False]
    input_rule_manifest_trusted_benchmark_run: bool
    selection_mode: EvaluationSelectionV1
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: list[StableId]
    runtime_failure_ids: list[StableId]
    input_episode_digests: dict[str, Sha256]
    input_rule_result_digests: dict[str, Sha256]
    input_reference_digests: dict[str, Sha256]
    input_runtime_failure_digests: dict[str, Sha256]
    episode_tracks: dict[str, Track]
    runtime_failure_tracks: dict[str, Track]
    blind_input_digests: dict[str, Sha256]
    judge_result_digests: dict[str, Sha256]
    result_statuses: dict[str, JudgeStatus]
    result_formal_evaluation_states: dict[str, Literal[False]]
    result_trusted_benchmark_states: dict[str, bool]
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    formal_evaluation_result: Literal[False]
    evaluation_status: Literal["not_a_formal_model_evaluation"]
    worktree_clean: bool
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_judge_manifest(self) -> Self:
        ordered_lists = (
            self.episode_ids,
            self.requested_episode_ids,
            self.runtime_failure_ids,
        )
        if any(values != sorted(set(values)) for values in ordered_lists):
            raise ValueError("Judge manifest identifiers must be sorted and unique")
        if not self.episode_ids and not self.runtime_failure_ids:
            raise ValueError("Judge manifest requires an Episode or Runtime Failure")
        if self.requested_episode_ids and set(self.requested_episode_ids) != set(
            self.episode_ids
        ):
            raise ValueError("requested Judge Episode IDs must match selected output")
        if not _selection_mode_matches_filters(
            self.selection_mode, self.requested_episode_ids, self.selected_track
        ):
            raise ValueError("Judge selection mode must match its explicit filters")
        episode_ids = set(self.episode_ids)
        mappings = (
            self.input_episode_digests,
            self.input_rule_result_digests,
            self.input_reference_digests,
            self.blind_input_digests,
            self.judge_result_digests,
            self.result_statuses,
            self.result_formal_evaluation_states,
            self.result_trusted_benchmark_states,
            self.episode_tracks,
        )
        if any(set(mapping) != episode_ids for mapping in mappings):
            raise ValueError("Judge manifest mappings must match Episode IDs")
        failure_ids = set(self.runtime_failure_ids)
        if (
            set(self.input_runtime_failure_digests) != failure_ids
            or set(self.runtime_failure_tracks) != failure_ids
        ):
            raise ValueError("Judge failure mappings must match Runtime Failure IDs")
        expected = bool(
            self.input_rule_manifest_trusted_benchmark_run
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
            and all(self.result_trusted_benchmark_states.values())
        )
        if self.trusted_benchmark_run != expected:
            raise ValueError("Judge manifest trust must monotonically inherit Rules")
        return self


class AggregateResultV3(AggregateResultV2):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-result-v3.schema.json",
        },
    )

    schema_version: Literal["aggregate-result-v3"]  # type: ignore[assignment]
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    runtime_run_id: StableId
    aggregator_implementation_version: StableId
    aggregator_implementation_sha256: Sha256
    input_judge_manifest_sha256: Sha256
    input_judge_manifest_formal_evaluation_result: Literal[False]
    input_episode_formal_evaluation_result: Literal[False]
    input_rule_result_formal_evaluation_result: Literal[False]
    input_judge_result_formal_evaluation_result: Literal[False]
    input_judge_manifest_trusted_benchmark_run: bool
    selection_mode: EvaluationSelectionV1
    worktree_clean: bool
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    formal_evaluation_result: Literal[False]  # type: ignore[assignment]

    @model_validator(mode="after")
    def validate_formal_inheritance(self) -> Self:
        expected = bool(
            self.input_judge_manifest_trusted_benchmark_run
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
        )
        if self.trusted_benchmark_run != expected:
            raise ValueError("Aggregate Result trust must monotonically inherit Judge")
        return self


class AggregateTrackResultV3(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-track-result-v3.schema.json",
        },
    )

    schema_version: Literal["aggregate-track-result-v3"]
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    runtime_run_id: StableId
    aggregator_version: StableId
    aggregator_implementation_version: StableId
    aggregator_implementation_sha256: Sha256
    track: Track
    episode_ids: list[StableId]
    complete_episode_ids: list[StableId]
    failed_episode_ids: list[StableId]
    invalid_input_episode_ids: list[StableId]
    judge_error_episode_ids: list[StableId]
    runtime_failure_ids: list[StableId]
    score_count: Annotated[int, Field(ge=0)]
    mean_score: Annotated[float, Field(ge=0, le=100)] | None
    input_judge_manifest_formal_evaluation_result: Literal[False]
    input_judge_manifest_trusted_benchmark_run: bool
    result_formal_evaluation_states: dict[str, bool]
    result_trusted_benchmark_states: dict[str, bool]
    selection_mode: EvaluationSelectionV1
    worktree_clean: bool
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    formal_evaluation_result: Literal[False]
    evaluation_status: Literal["not_a_formal_model_evaluation"]
    result_sha256: Sha256

    @model_validator(mode="after")
    def validate_track_summary(self) -> Self:
        ordered = (
            self.episode_ids,
            self.complete_episode_ids,
            self.failed_episode_ids,
            self.invalid_input_episode_ids,
            self.judge_error_episode_ids,
            self.runtime_failure_ids,
        )
        if any(values != sorted(set(values)) for values in ordered):
            raise ValueError("track aggregate identifiers must be sorted and unique")
        if not self.episode_ids and not self.runtime_failure_ids:
            raise ValueError("track aggregate requires an Episode or Runtime Failure")
        groups = (
            set(self.complete_episode_ids),
            set(self.invalid_input_episode_ids),
            set(self.judge_error_episode_ids),
        )
        if set().union(*groups) != set(self.episode_ids):
            raise ValueError("track aggregate groups must partition Episode IDs")
        if any(
            groups[left] & groups[right]
            for left in range(3)
            for right in range(left + 1, 3)
        ):
            raise ValueError("track aggregate groups must not overlap")
        if not set(self.failed_episode_ids).issubset(groups[0]):
            raise ValueError("failed Episodes must be complete scored Episodes")
        if set(self.result_formal_evaluation_states) != set(self.episode_ids):
            raise ValueError("track formal mappings must match Episode IDs")
        if set(self.result_trusted_benchmark_states) != set(self.episode_ids):
            raise ValueError("track trust mappings must match Episode IDs")
        if self.score_count != len(self.complete_episode_ids):
            raise ValueError("track score count must match complete Episodes")
        if (self.mean_score is None) != (self.score_count == 0):
            raise ValueError("track mean exists exactly when scored Episodes exist")
        expected = bool(
            self.input_judge_manifest_trusted_benchmark_run
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
            and all(self.result_trusted_benchmark_states.values())
        )
        if self.trusted_benchmark_run != expected:
            raise ValueError("track trust must monotonically inherit Judge")
        return self


class AggregateRunManifestV3(StrictContractModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$schema": SCHEMA_DIALECT,
            "$id": f"{SCHEMA_BASE_URI}/aggregate-run-manifest-v3.schema.json",
        },
    )

    schema_version: Literal["aggregate-run-manifest-v3"]
    runtime_run_id: StableId
    evaluation_protocol_release_id: Literal["evaluation-protocol-release-1.0", "evaluation-protocol-release-1.1", "evaluation-protocol-release-1.2", "evaluation-protocol-release-1.3", "evaluation-protocol-release-1.4", "evaluation-protocol-release-1.5", "evaluation-protocol-release-1.6", "evaluation-protocol-release-1.7", "evaluation-protocol-release-1.8", "evaluation-protocol-release-1.9", "evaluation-protocol-release-1.10", "evaluation-protocol-release-1.11", "evaluation-protocol-release-1.12", "evaluation-protocol-release-1.13", "evaluation-protocol-release-1.14", "evaluation-protocol-release-1.15"]
    evaluation_protocol_release_sha256: Sha256
    benchmark_release_id: StableId
    benchmark_release_sha256: Sha256
    case_suite_sha256: Sha256
    aggregator_version: StableId
    aggregator_implementation_version: StableId
    aggregator_implementation_sha256: Sha256
    judge_version: StableId
    judge_config_version: StableId
    judge_config_sha256: Sha256
    judge_prompt_version: StableId
    judge_prompt_sha256: Sha256
    rubric_version: StableId
    rubric_sha256: Sha256
    track_anchor_version: StableId
    track_anchor_sha256: Sha256
    judge_mode: Literal["stub", "real"]
    aggregate_source_bundle_version: Literal["evaluation-source-bundle-v2"]
    aggregate_source_bundle_sha256: Sha256
    input_runtime_manifest_sha256: Sha256
    input_judge_manifest_sha256: Sha256
    input_judge_manifest_formal_evaluation_result: Literal[False]
    input_judge_manifest_trusted_benchmark_run: bool
    selection_mode: EvaluationSelectionV1
    requested_episode_ids: list[StableId]
    selected_track: Track | None
    episode_ids: list[StableId]
    runtime_failure_ids: list[StableId]
    runtime_terminals: Annotated[list[RuntimeTerminalRecordV3], Field(min_length=1)]
    input_episode_digests: dict[str, Sha256]
    input_rule_result_digests: dict[str, Sha256]
    input_judge_result_digests: dict[str, Sha256]
    input_reference_digests: dict[str, Sha256]
    input_runtime_failure_digests: dict[str, Sha256]
    episode_tracks: dict[str, Track]
    runtime_failure_tracks: dict[str, Track]
    aggregate_result_digests: dict[str, Sha256]
    result_statuses: dict[str, JudgeStatus]
    result_formal_evaluation_states: dict[str, Literal[False]]
    result_trusted_benchmark_states: dict[str, bool]
    track_result_digests: dict[str, Sha256]
    expected_total_cases: Annotated[int, Field(ge=1)]
    expected_track_counts: BenchmarkTrackCountsV1
    protocol_eligible: bool
    provider_eligible: bool
    trusted_benchmark_run: bool
    capability_blockers: list[StableId]
    formal_capability_result: bool
    formal_evaluation_result: bool
    evaluation_status: EvaluationStatus
    worktree_clean: bool
    git_commit: GitCommit
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_aggregate_manifest(self) -> Self:
        ordered_lists = (
            self.episode_ids,
            self.requested_episode_ids,
            self.runtime_failure_ids,
        )
        if any(values != sorted(set(values)) for values in ordered_lists):
            raise ValueError("Aggregate manifest identifiers must be sorted and unique")
        if not self.episode_ids and not self.runtime_failure_ids:
            raise ValueError("Aggregate manifest requires an Episode or Runtime Failure")
        if self.requested_episode_ids and set(self.requested_episode_ids) != set(
            self.episode_ids
        ):
            raise ValueError("requested Aggregate Episode IDs must match output")
        if not _selection_mode_matches_filters(
            self.selection_mode, self.requested_episode_ids, self.selected_track
        ):
            raise ValueError("Aggregate selection mode must match its explicit filters")
        episode_ids = set(self.episode_ids)
        mappings = (
            self.input_episode_digests,
            self.input_rule_result_digests,
            self.input_judge_result_digests,
            self.input_reference_digests,
            self.aggregate_result_digests,
            self.result_statuses,
            self.result_formal_evaluation_states,
            self.result_trusted_benchmark_states,
        )
        if any(set(mapping) != episode_ids for mapping in mappings):
            raise ValueError("Aggregate manifest mappings must match Episode IDs")
        failure_ids = set(self.runtime_failure_ids)
        if (
            set(self.input_runtime_failure_digests) != failure_ids
            or set(self.runtime_failure_tracks) != failure_ids
        ):
            raise ValueError("Aggregate failure mappings must match Runtime Failure IDs")
        terminal_ids = [item.artifact_id for item in self.runtime_terminals]
        if terminal_ids != sorted(set(terminal_ids)):
            raise ValueError("Aggregate runtime terminals must be sorted and unique")
        if set(terminal_ids) != episode_ids | failure_ids:
            raise ValueError("Aggregate runtime terminals must bind every selected terminal")
        if any(
            item.runtime_run_id != self.runtime_run_id
            for item in self.runtime_terminals
        ):
            raise ValueError("Aggregate runtime terminals must bind the same Runtime Run")
        for terminal in self.runtime_terminals:
            artifact_id = terminal.artifact_id
            if terminal.terminal_kind == "episode":
                if (
                    artifact_id not in episode_ids
                    or terminal.artifact_sha256
                    != self.input_episode_digests[artifact_id]
                    or terminal.track != self.episode_tracks[artifact_id]
                ):
                    raise ValueError("Aggregate Episode terminal binding differs")
            elif (
                artifact_id not in failure_ids
                or terminal.artifact_sha256
                != self.input_runtime_failure_digests[artifact_id]
                or terminal.track != self.runtime_failure_tracks[artifact_id]
            ):
                raise ValueError("Aggregate Failure terminal binding differs")
        if not self.track_result_digests:
            raise ValueError("Aggregate manifest requires per-track results")
        expected_trust = bool(
            self.input_judge_manifest_trusted_benchmark_run
            and self.judge_mode == "real"
            and self.selection_mode == "inherited"
            and self.worktree_clean
            and self.protocol_eligible
            and self.provider_eligible
            and all(self.result_trusted_benchmark_states.values())
            and all(item.protocol_eligible for item in self.runtime_terminals)
            and all(item.provider_eligible for item in self.runtime_terminals)
        )
        if self.trusted_benchmark_run != expected_trust:
            raise ValueError("Aggregate trust must monotonically inherit Judge")
        observed_tracks = {
            track: sum(value == track for value in self.episode_tracks.values())
            + sum(value == track for value in self.runtime_failure_tracks.values())
            for track in ("planning", "intervention", "assessment", "revision")
        }
        if self.trusted_benchmark_run and (
            observed_tracks != self.expected_track_counts.model_dump()
            or len(self.episode_ids) + len(self.runtime_failure_ids)
            != self.expected_total_cases
        ):
            raise ValueError("trusted aggregate inventory differs from Benchmark")
        if self.capability_blockers != sorted(set(self.capability_blockers)):
            raise ValueError("capability blockers must be sorted and unique")
        expected_capability = bool(
            self.trusted_benchmark_run
            and len(self.episode_ids) == self.expected_total_cases
            and not self.runtime_failure_ids
            and all(status == "complete" for status in self.result_statuses.values())
            and set(self.track_result_digests)
            == {"planning", "intervention", "assessment", "revision"}
            and observed_tracks == self.expected_track_counts.model_dump()
            and not self.capability_blockers
        )
        if self.formal_capability_result != expected_capability:
            raise ValueError("formal capability state is inconsistent")
        if self.formal_evaluation_result != self.formal_capability_result:
            raise ValueError("legacy formal alias must equal capability publication")
        if self.evaluation_status != (
            "formal_model_evaluation"
            if expected_capability
            else "not_a_formal_model_evaluation"
        ):
            raise ValueError("Aggregate evaluation status is inconsistent")
        return self


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "decision-episode-v1": DecisionEpisode,
    "decision-episode-v2": DecisionEpisodeV2,
    "acceptable-action-envelope-v1": AcceptableActionEnvelope,
    "environment-manifest-v1": EnvironmentManifest,
    "rule-result-v1": RuleResultV1,
    "judge-result-v1": JudgeResultV1,
    "judge-run-manifest-v1": JudgeRunManifestV1,
    "aggregate-result-v1": AggregateResultV1,
    "aggregate-track-result-v1": AggregateTrackResultV1,
    "aggregate-run-manifest-v1": AggregateRunManifestV1,
    "case-spec-v1": CaseSpecV1,
    "judge-reference-v1": JudgeReferenceV1,
    "provider-attestation-v1": ProviderAttestationV1,
    "environment-manifest-v2": EnvironmentManifestV2,
    "decision-episode-v3": DecisionEpisodeV3,
    "runtime-failure-v1": RuntimeFailureV1,
    "runtime-run-manifest-v2": RuntimeRunManifestV2,
    "case-suite-manifest-v1": CaseSuiteManifestV1,
    "rule-result-v2": RuleResultV2,
    "rule-run-manifest-v2": RuleRunManifestV2,
    "judge-result-v2": JudgeResultV2,
    "judge-run-manifest-v2": JudgeRunManifestV2,
    "aggregate-result-v2": AggregateResultV2,
    "aggregate-track-result-v2": AggregateTrackResultV2,
    "aggregate-run-manifest-v2": AggregateRunManifestV2,
    "case-spec-v2": CaseSpecV2,
    "judge-reference-v2": JudgeReferenceV2,
    "decision-episode-v4": DecisionEpisodeV4,
    "runtime-failure-v2": RuntimeFailureV2,
    "case-suite-manifest-v2": CaseSuiteManifestV2,
    "runtime-run-manifest-v3": RuntimeRunManifestV3,
    "rule-result-v3": RuleResultV3,
    "rule-run-manifest-v3": RuleRunManifestV3,
    "judge-result-v3": JudgeResultV3,
    "judge-run-manifest-v3": JudgeRunManifestV3,
    "aggregate-result-v3": AggregateResultV3,
    "aggregate-track-result-v3": AggregateTrackResultV3,
    "aggregate-run-manifest-v3": AggregateRunManifestV3,
    "evaluation-protocol-release-v1": EvaluationProtocolReleaseV1,
    "benchmark-release-manifest-v1": BenchmarkReleaseManifestV1,
    "source-bundle-manifest-v1": SourceBundleManifestV1,
    "schema-lock-manifest-v1": SchemaLockManifestV1,
    "trusted-benchmark-registry-v1": TrustedBenchmarkRegistryV1,
    "action-declaration-protocol-v2": ActionDeclarationProtocolV2,
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
