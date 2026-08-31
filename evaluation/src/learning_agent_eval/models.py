"""Strict Pydantic source models for the three E0 JSON contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
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


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "decision-episode-v1": DecisionEpisode,
    "acceptable-action-envelope-v1": AcceptableActionEnvelope,
    "environment-manifest-v1": EnvironmentManifest,
}
