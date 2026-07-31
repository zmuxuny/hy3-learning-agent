from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    version: int
    progress: float
    memory_summary: str
    created_at: datetime
    updated_at: datetime
    stages: list[StageRead]


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: Literal["pending", "active", "completed", "blocked", "skipped"] | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    review_due_at: datetime | None = None
    evidence: list[dict[str, Any]] | None = None


class ProfileRead(APIModel):
    owner_id: str
    coach_style: str
    preferences: dict[str, Any]
    quiet_hours: dict[str, Any]
    daily_notification_limit: int
    xp: int
    level: int
    streak_days: int
    updated_at: datetime


class ProfileUpdate(BaseModel):
    coach_style: str | None = None
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
