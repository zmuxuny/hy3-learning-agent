"""Strict public schemas for the Evidence tool boundary.

The persistence layer deliberately keeps several JSON envelopes flexible while
the public tool contract names every structural level that callers depend on.
This prevents an accidental ORM/service field from leaking into the tool
protocol and makes nested artifact/competency references independently
validated.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceArtifactRef(StrictEvidenceModel):
    artifact_id: int = Field(gt=0)
    kind: str
    uri: str
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=0)


class EvidenceCompetencyRef(StrictEvidenceModel):
    competency_id: int = Field(gt=0)
    competency_key: str
    association_kind: Literal["explicit", "task_assesses", "legacy"]
    task_competency_link_id_snapshot: int | None = Field(default=None, gt=0)


class EvidenceCheck(StrictEvidenceModel):
    key: str | None = None
    kind: str | None = None
    passed: bool | None = None
    score: float | None = None
    message: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceRubricEnvelope(StrictEvidenceModel):
    rubric_version: str = "unspecified"
    pass_threshold: float | None = None
    checks: list[EvidenceCheck] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceEvaluatorEnvelope(StrictEvidenceModel):
    type: str = "unknown"
    run_id: str | None = None
    model: str | None = None
    evaluator_version: str = "unspecified"
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceClaim(StrictEvidenceModel):
    kind: str
    verified: bool | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EvidencePayloadEnvelope(StrictEvidenceModel):
    feedback: str | None = None
    answer_length: int | None = Field(default=None, ge=0)
    artifact_count: int | None = Field(default=None, ge=0)
    has_text: bool | None = None
    submission_type: str | None = None
    backfilled: bool | None = None
    evidence: list[EvidenceClaim] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceObservationOutput(StrictEvidenceModel):
    id: int = Field(gt=0)
    source_type: str
    source_id: str
    run_id: str | None
    session_id: str | None
    plan_id: int | None = Field(gt=0)
    task_id: int | None = Field(gt=0)
    competency_refs: list[EvidenceCompetencyRef] = Field(default_factory=list)
    fact_kind: Literal["observation", "amendment", "invalidation", "reinstatement"]
    target_observation_id: int | None = Field(gt=0)
    reason_code: str
    evidence_role: Literal["primary", "supporting", "control"]
    eligibility_stage: Literal["unknown", "exposed", "practicing", "demonstrated"]
    eligibility_reason: str
    eligibility_policy_version: str
    counts_as_success: bool
    outcome: str
    normalized_score: float | None = Field(ge=0, le=1)
    is_correct: bool | None
    assistance_level: str
    transfer_level: str
    rubric_snapshot: EvidenceRubricEnvelope
    evaluator: EvidenceEvaluatorEnvelope
    artifact_refs: list[EvidenceArtifactRef]
    payload: EvidencePayloadEnvelope
    occurred_at: str
    recorded_at: str
    schema_version: int = Field(ge=1)
    correlation_id: str | None
    causation_id: str | None
    idempotency_key: str


class EvidenceListInput(StrictEvidenceModel):
    plan_id: int | None = Field(default=None, gt=0)
    task_id: int | None = Field(default=None, gt=0)
    competency_id: int | None = Field(default=None, gt=0)
    limit: int = Field(default=30, ge=1, le=200)


class EvidenceListOutput(StrictEvidenceModel):
    observations: list[EvidenceObservationOutput]
