from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    kind: str = "learning"
    is_core: bool = False
    evidence_required: bool = False
    estimated_minutes: int = Field(default=30, ge=1, le=1440)
    due_at: datetime | None = None
    review_due_at: datetime | None = None
    resource_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class StageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    objectives: list[str] = Field(default_factory=list)
    tasks: list[TaskCreate] = Field(default_factory=list)


class PlanCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    goal: str = ""
    current_level: str = ""
    deadline: datetime | None = None
    weekly_minutes: int = Field(default=0, ge=0, le=10080)
    preferences: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = ""
    available_resources: list[str] = Field(default_factory=list)
    avoid_methods: list[str] = Field(default_factory=list)
    stages: list[StageCreate] = Field(default_factory=list)


class TaskRead(APIModel):
    id: int
    stage_id: int
    title: str
    description: str
    kind: str
    status: str
    is_core: bool
    evidence_required: bool
    estimated_minutes: int
    position: int
    due_at: datetime | None
    completed_at: datetime | None
    review_due_at: datetime | None
    resource_url: str
    task_metadata: dict[str, Any]


class StageRead(APIModel):
    id: int
    plan_id: int
    title: str
    description: str
    objectives: list[str]
    position: int
    status: str
    tasks: list[TaskRead]


class PlanRead(APIModel):
    id: int
    owner_id: str
    title: str
    description: str
    goal: str
    current_level: str
    deadline: datetime | None
    weekly_minutes: int
    preferences: dict[str, Any]
    expected_outcome: str
    available_resources: list[str]
    avoid_methods: list[str]
    status: str
    archived_from_status: str | None
    version: int
    progress: float
    memory_summary: str
    created_at: datetime
    updated_at: datetime
    stages: list[StageRead]


class LearningResourceRead(APIModel):
    id: int
    plan_id: int | None
    title: str
    url: str
    resource_type: str
    provider: str
    language: str
    difficulty: str
    summary: str
    why_recommended: str
    source: str
    verified_at: datetime | None
    created_at: datetime


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: Literal["pending", "active", "completed", "blocked", "skipped"] | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    review_due_at: datetime | None = None
    evidence: list[dict[str, Any]] | None = None
    kind: str | None = None
    is_core: bool | None = None
    evidence_required: bool | None = None
    resource_url: str | None = None


class ProfileRead(APIModel):
    owner_id: str
    agent_style: str
    preferences: dict[str, Any]
    quiet_hours: dict[str, Any]
    daily_notification_limit: int
    xp: int
    level: int
    streak_days: int
    updated_at: datetime


class ProfileUpdate(BaseModel):
    agent_style: str | None = None
    preferences: dict[str, Any] | None = None
    quiet_hours: dict[str, Any] | None = None
    daily_notification_limit: int | None = Field(default=None, ge=0, le=20)


class MemoryRead(APIModel):
    id: int
    owner_id: str
    scope: str
    scope_id: str | None
    layer: str
    content: str
    source_type: str
    source_id: str | None
    confidence: float
    status: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MemoryProposalCreate(BaseModel):
    scope: Literal["global", "plan", "session"] = "global"
    scope_id: str | None = None
    layer: Literal["short_term", "long_term", "episodic", "semantic"] = "semantic"
    content: str = Field(min_length=1)
    source_type: str = "user"
    source_id: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: datetime | None = None


class NotificationRead(APIModel):
    id: int
    run_id: str | None
    session_id: str | None
    plan_id: int | None
    channel: str
    title: str
    body: str
    status: str
    sent_at: datetime | None
    read_at: datetime | None
    created_at: datetime


class ContextSnapshotRead(APIModel):
    id: int
    plan_id: int | None
    run_id: str | None
    markdown: str
    source_manifest: list[dict[str, Any]]
    estimated_tokens: int
    created_at: datetime


class AgentRunCreate(BaseModel):
    objective: str = Field(min_length=1)
    session_id: str | None = None
    plan_id: int | None = None
    trigger: Literal["user_message", "heartbeat", "task_event", "review_due", "email_reply"] = "user_message"


class AgentRunRead(APIModel):
    id: str
    owner_id: str
    session_id: str | None
    plan_id: int | None
    parent_run_id: str | None
    trigger: str
    objective: str
    status: str
    model: str
    cancel_requested: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class SessionRead(APIModel):
    id: str
    plan_id: int | None
    parent_session_id: str | None
    title: str
    summary: str
    handoff_summary: str
    archived_at: datetime | None
    linked_plan_ids: list[int]
    message_count: int
    run_count: int
    last_message: str
    last_run_id: str | None
    last_run_status: str | None
    created_at: datetime
    updated_at: datetime


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    archived: bool | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized

    @model_validator(mode="after")
    def require_change(self):
        if self.title is None and self.archived is None:
            raise ValueError("at least one session change is required")
        return self


class SessionHandoffCreate(BaseModel):
    plan_id: int


class PlanningQuestion(APIModel):
    id: str
    prompt: str
    why: str = ""
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True


class PlanningIntakeRead(APIModel):
    session_id: str
    goal: str
    confirmed_facts: list[dict[str, Any]]
    open_questions: list[PlanningQuestion]
    readiness: str
    readiness_confidence: float
    rationale: str
    source_run_id: str | None
    created_at: datetime
    updated_at: datetime


class PlanProposalRead(APIModel):
    id: str
    session_id: str
    source_run_id: str | None
    title: str
    rationale: str
    plan_payload: dict[str, Any]
    specialist_reports: list[dict[str, Any]]
    status: str
    plan_id: int | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PlanningStateRead(BaseModel):
    intake: PlanningIntakeRead | None = None
    proposal: PlanProposalRead | None = None


class PlanningAnswer(BaseModel):
    question_id: str = Field(min_length=1, max_length=80)
    answer: str = Field(min_length=1, max_length=4000)

    @field_validator("answer")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        return value.strip()


class PlanningAnswersSubmit(BaseModel):
    answers: list[PlanningAnswer] = Field(min_length=1, max_length=6)


class PlanProposalDecision(BaseModel):
    accepted: bool


class MessageEdit(BaseModel):
    content: str = Field(min_length=1, max_length=50000)
    rerun: Literal[True] = True

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message cannot be blank")
        return normalized


class PlanArchiveUpdate(BaseModel):
    archived: bool


class ChatMessageRead(APIModel):
    id: int
    session_id: str
    run_id: str | None
    role: str
    content: str
    message_metadata: dict[str, Any]
    created_at: datetime


class RunEventRead(APIModel):
    id: int
    run_id: str
    sequence: int
    event_type: str
    summary: str
    payload: dict[str, Any]
    created_at: datetime


class OperationRead(APIModel):
    id: str
    run_id: str | None
    tool_name: str
    entity_type: str
    entity_id: str
    forward_patch: dict[str, Any]
    inverse_patch: dict[str, Any]
    status: str
    created_at: datetime
    undone_at: datetime | None
