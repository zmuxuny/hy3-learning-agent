from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.database import Base
from app.core.time import utc_now
from app.db.types import UTCDateTime


def uuid_string() -> str:
    return str(uuid4())


class SchemaMigration(Base):
    """Verified canonical schema revision applied to this database."""

    __tablename__ = "schema_migrations"
    __table_args__ = (
        CheckConstraint("result = 'applied'", name="ck_schema_migration_applied"),
    )

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    checksum: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    result: Mapped[str] = mapped_column(String(24))


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="local")
    display_name: Mapped[str] = mapped_column(String(120), default="Learner")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class UserProfile(Base):
    __tablename__ = "user_profiles"

    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    # Keep the existing column name for local database compatibility while the
    # product language uses "agent style" instead of the retired coach label.
    agent_style: Mapped[str] = mapped_column("coach_style", String(64), default="adaptive_study_partner")
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    quiet_hours: Mapped[dict] = mapped_column(JSON, default=lambda: {"start": "23:00", "end": "08:00"})
    daily_notification_limit: Mapped[int] = mapped_column(Integer, default=3)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    follow_up_behavior: Mapped[str] = mapped_column(String(16), default="steer")
    proactive_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    current_level: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    weekly_minutes: Mapped[int] = mapped_column(Integer, default=0)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_outcome: Mapped[str] = mapped_column(Text, default="")
    available_resources: Mapped[list] = mapped_column(JSON, default=list)
    avoid_methods: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    archived_from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    memory_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now)

    stages: Mapped[list[Stage]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="Stage.position", lazy="selectin"
    )


class Stage(Base):
    __tablename__ = "stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    objectives: Mapped[list] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")

    plan: Mapped[Plan] = relationship(back_populates="stages")
    tasks: Mapped[list[Task]] = relationship(
        back_populates="stage", cascade="all, delete-orphan", order_by="Task.position", lazy="selectin"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(32), default="learning")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    is_core: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_required: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=30)
    position: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    review_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    resource_url: Mapped[str] = mapped_column(Text, default="")
    task_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    stage: Mapped[Stage] = relationship(back_populates="tasks")


class LearningResource(Base):
    __tablename__ = "learning_resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text, default="")
    resource_type: Mapped[str] = mapped_column(String(32), default="web")
    provider: Mapped[str] = mapped_column(String(120), default="")
    language: Mapped[str] = mapped_column(String(32), default="")
    difficulty: Mapped[str] = mapped_column(String(32), default="mixed")
    summary: Mapped[str] = mapped_column(Text, default="")
    why_recommended: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(120), default="agent")
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class TaskSubmission(Base):
    __tablename__ = "task_submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    submission_type: Mapped[str] = mapped_column(String(32), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="submitted", index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    source: Mapped[str] = mapped_column(String(64), default="agent")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(240), default="新对话")
    summary: Mapped[str] = mapped_column(Text, default="")
    handoff_summary: Mapped[str] = mapped_column(Text, default="")
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now)

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at", lazy="selectin"
    )


class SessionPlanLink(Base):
    __tablename__ = "session_plan_links"
    __table_args__ = (
        UniqueConstraint("session_id", "plan_id", "relation_type", name="uq_session_plan_relation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    relation_type: Mapped[str] = mapped_column(String(32), default="discussed", index=True)
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class PlanningIntake(Base):
    """Durable requirement discovery state for one planning conversation."""

    __tablename__ = "planning_intakes"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    goal: Mapped[str] = mapped_column(Text, default="")
    confirmed_facts: Mapped[list] = mapped_column(JSON, default=list)
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    readiness: Mapped[str] = mapped_column(String(32), default="collecting", index=True)
    readiness_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class PlanProposal(Base):
    """Reviewable plan draft that must be accepted before a Plan is created."""

    __tablename__ = "plan_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(240))
    rationale: Mapped[str] = mapped_column(Text, default="")
    plan_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    specialist_reports: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index(
            "uq_chat_messages_session_message_key",
            "session_id",
            "message_key",
            unique=True,
            sqlite_where=text("message_key IS NOT NULL"),
        ),
        Index("ix_chat_messages_run_role", "run_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    message_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, default="")
    message_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())

    session: Mapped[Session] = relationship(back_populates="messages")


class SessionSummary(Base):
    """Immutable provenance for every durable session compression."""

    __tablename__ = "session_summaries"
    __table_args__ = (
        UniqueConstraint("session_id", "version", name="uq_session_summary_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    covered_through_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    method: Mapped[str] = mapped_column(String(32), default="model")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class ChatMessageRevision(Base):
    """Immutable audit copy created whenever a visible user message is revised."""

    __tablename__ = "chat_message_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    previous_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text)
    message_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_approval', 'retry_wait', "
            "'completed', 'failed', 'cancelled', 'needs_reconciliation')",
            name="ck_agent_run_status",
        ),
        CheckConstraint(
            "phase IN ('not_started', 'starting', 'awaiting_model', 'tool_ready', "
            "'tool_running', 'waiting_approval', 'retry_wait', 'finalizing', "
            "'terminal', 'reconciling')",
            name="ck_agent_run_phase",
        ),
        CheckConstraint("state_version >= 1", name="ck_agent_run_state_version"),
        CheckConstraint("attempt >= 0", name="ck_agent_run_attempt"),
        CheckConstraint("retry_count >= 0", name="ck_agent_run_retry_count"),
        CheckConstraint(
            "(checkpoint IS NULL AND checkpoint_schema_version IS NULL) OR "
            "(checkpoint IS NOT NULL AND checkpoint_schema_version = 1 AND "
            "CASE WHEN json_valid(checkpoint) THEN coalesce(("
            "json_extract(checkpoint, '$.schema_version') = checkpoint_schema_version "
            "AND json_extract(checkpoint, '$.kind') IN ('agent', 'subagent') "
            "AND json_extract(checkpoint, '$.phase') IN ("
            "'not_started', 'starting', 'awaiting_model', 'tool_ready', "
            "'tool_running', 'waiting_approval', 'retry_wait', 'finalizing', "
            "'terminal', 'reconciling') "
            "AND json_type(checkpoint, '$.step') = 'integer' "
            "AND json_extract(checkpoint, '$.step') >= 0 "
            "AND json_type(checkpoint, '$.messages') = 'array' "
            "AND json_type(checkpoint, '$.current_tool_call') IN ('null', 'object') "
            "AND json_type(checkpoint, '$.remaining_tool_calls') = 'array' "
            "AND json_type(checkpoint, '$.current_invocation_id') IN ('null', 'integer') "
            "AND (json_type(checkpoint, '$.current_invocation_id') = 'null' "
            "OR json_extract(checkpoint, '$.current_invocation_id') > 0) "
            "AND json_type(checkpoint, '$.context_snapshot_id') IN ('null', 'integer') "
            "AND (json_type(checkpoint, '$.context_snapshot_id') = 'null' "
            "OR json_extract(checkpoint, '$.context_snapshot_id') > 0) "
            "AND json_type(checkpoint, '$.cards') = 'array' "
            "AND json_type(checkpoint, '$.budget_usage') = 'object' "
            "AND json_type(checkpoint, '$.state_version') = 'integer' "
            "AND json_extract(checkpoint, '$.state_version') >= 1), 0) "
            "ELSE 0 END)",
            name="ck_agent_run_checkpoint_envelope",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_owner IS NULL "
            "AND lease_acquired_at IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_owner IS NOT NULL "
            "AND lease_acquired_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_agent_run_lease_shape",
        ),
        CheckConstraint(
            "status <> 'waiting_approval' OR pending_approval IS NOT NULL",
            name="ck_agent_run_waiting_approval_projection",
        ),
        Index(
            "uq_agent_runs_active_plan_root",
            "owner_id",
            "plan_id",
            unique=True,
            sqlite_where=text(
                "parent_run_id IS NULL AND plan_id IS NOT NULL AND "
                "status IN ('queued', 'running', 'waiting_approval', 'retry_wait')"
            ),
        ),
        Index(
            "uq_agent_runs_active_global_session_root",
            "owner_id",
            "session_id",
            unique=True,
            sqlite_where=text(
                "parent_run_id IS NULL AND plan_id IS NULL AND session_id IS NOT NULL AND "
                "status IN ('queued', 'running', 'waiting_approval', 'retry_wait')"
            ),
        ),
        Index(
            "uq_agent_runs_active_stateless_root",
            "owner_id",
            unique=True,
            sqlite_where=text(
                "parent_run_id IS NULL AND plan_id IS NULL AND session_id IS NULL AND "
                "trigger <> 'subagent' AND "
                "status IN ('queued', 'running', 'waiting_approval', 'retry_wait')"
            ),
        ),
        Index("ix_agent_runs_recovery", "status", "available_at", "lease_expires_at"),
        Index("ix_agent_runs_parent_status", "parent_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(40), default="user_message", index=True)
    objective: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(32), default="not_started", server_default=text("'not_started'"))
    state_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    checkpoint_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_acquired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="hy3")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    pending_approval: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    budget_usage: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    created_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )

    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.sequence", lazy="selectin"
    )


class ToolInvocation(Base):
    """Persisted idempotency record for write tools inside one Agent run."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        CheckConstraint(
            "request_digest IS NULL OR length(request_digest) = 64",
            name="ck_tool_invocation_request_digest",
        ),
        CheckConstraint(
            "effect_kind IS NULL OR effect_kind IN "
            "('pure_read', 'database_write', 'external_read', 'external_write')",
            name="ck_tool_invocation_effect_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'pending_approval', 'pending_delivery', "
            "'committed', 'failed', 'rejected', 'needs_reconciliation', 'retry_pending', 'cancelled')",
            name="ck_tool_invocation_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_tool_invocation_attempt"),
        CheckConstraint("version >= 1", name="ck_tool_invocation_version"),
        Index(
            "uq_tool_invocations_run_tool_call",
            "run_id",
            "tool_call_id",
            unique=True,
            sqlite_where=text("tool_call_id IS NOT NULL"),
        ),
        Index("ix_tool_invocations_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    tool_call_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    args_hash: Mapped[str] = mapped_column(String(64))
    # ``NULL`` is intentionally reserved for pre-H2 rows whose original
    # validated request cannot be reconstructed.  Coordinators must fail
    # closed instead of inventing a digest and replaying uncertain work.
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    canonical_args: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    effect_kind: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_run_event_sequence"),
        Index(
            "uq_run_events_run_event_key",
            "run_id",
            "event_key",
            unique=True,
            sqlite_where=text("event_key IS NOT NULL"),
        ),
        Index("ix_run_events_run_type", "run_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    event_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())

    run: Mapped[AgentRun] = relationship(back_populates="events")


class QueuedMessage(Base):
    """Messages the user queued while the Agent is busy; run after the current run."""

    __tablename__ = "queued_messages"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_queued_message_position"),
        CheckConstraint("version >= 1", name="ck_queued_message_version"),
        Index(
            "uq_queued_messages_source_steer",
            "source_steer_id",
            unique=True,
            sqlite_where=text("source_steer_id IS NOT NULL"),
        ),
        Index(
            "uq_queued_messages_session_position",
            "owner_id",
            "session_id",
            "position",
            unique=True,
            sqlite_where=text("session_id IS NOT NULL"),
        ),
        Index(
            "uq_queued_messages_stateless_position",
            "owner_id",
            "position",
            unique=True,
            sqlite_where=text("session_id IS NULL"),
        ),
        Index(
            "ix_queued_messages_session_dequeue",
            "owner_id",
            "session_id",
            "position",
            "created_at",
            "id",
            sqlite_where=text("session_id IS NOT NULL"),
        ),
        Index(
            "ix_queued_messages_stateless_dequeue",
            "owner_id",
            "position",
            "created_at",
            "id",
            sqlite_where=text("session_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(40), default="user_message")
    objective: Mapped[str] = mapped_column(Text)
    user_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    source_steer_id: Mapped[str | None] = mapped_column(
        ForeignKey("run_steer_messages.id", ondelete="SET NULL"), nullable=True
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class RunSteerMessage(Base):
    """Mid-turn steering: a user message injected into a running Run without stopping it."""

    __tablename__ = "run_steer_messages"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('pending', 'applied', 'queued')",
            name="ck_run_steer_disposition",
        ),
        CheckConstraint(
            "(disposition = 'pending' AND applied_at IS NULL AND disposed_at IS NULL) OR "
            "(disposition = 'applied' AND applied_at IS NOT NULL AND disposed_at IS NOT NULL) OR "
            "(disposition = 'queued' AND applied_at IS NULL AND disposed_at IS NOT NULL)",
            name="ck_run_steer_disposition_times",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(String(24), default="pending", server_default=text("'pending'"))
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    disposed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class RunApproval(Base):
    """Durable user decision for one blocking tool call.

    ``AgentRun.pending_approval`` remains the current request projection used
    by API clients.  This table is the immutable decision/audit identity and
    supports more than one approval during a single Run.
    """

    __tablename__ = "run_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_run_approval_tool_call"),
        CheckConstraint(
            "decision IN ('pending', 'approve', 'reject', 'answer')",
            name="ck_run_approval_decision",
        ),
        CheckConstraint(
            "(decision = 'pending' AND decided_at IS NULL) OR "
            "(decision <> 'pending' AND decided_at IS NOT NULL)",
            name="ck_run_approval_decision_time",
        ),
        CheckConstraint(
            "decision <> 'answer' OR (answer IS NOT NULL AND length(answer) > 0)",
            name="ck_run_approval_answer",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(180))
    tool_name: Mapped[str] = mapped_column(String(120))
    tool_call: Mapped[dict] = mapped_column(JSON)
    remaining_tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class LearningEvent(Base):
    __tablename__ = "learning_events"
    __table_args__ = (
        Index(
            "uq_learning_events_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    causation_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True, default=uuid_string)
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    invalidation_reason: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), index=True)


class EvidenceObservation(Base):
    """An append-only, structured observation about a learning attempt.

    ``LearningEvent`` remains the operational event stream.  This table is the
    v2 fact layer used to rebuild learner state.  There is deliberately no
    update/delete service for observations; a correction is represented by a
    later observation that supersedes or invalidates the earlier one.
    """

    __tablename__ = "evidence_observations"
    __table_args__ = (
        Index("ix_evidence_observations_owner_plan", "owner_id", "plan_id", "recorded_at"),
        Index("ix_evidence_observations_task", "owner_id", "task_id", "occurred_at"),
        Index("ix_evidence_observations_source", "owner_id", "source_type", "source_id"),
        CheckConstraint(
            "request_digest IS NULL OR length(request_digest) = 64",
            name="ck_evidence_observation_request_digest",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(120), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    competency_id: Mapped[int | None] = mapped_column(ForeignKey("competencies.id", ondelete="SET NULL"), nullable=True, index=True)
    competency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(40), index=True)
    normalized_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    assistance_level: Mapped[str] = mapped_column(
        String(24), default="unknown", server_default=text("'unknown'")
    )
    transfer_level: Mapped[str] = mapped_column(
        String(24), default="unknown", server_default=text("'unknown'")
    )
    rubric_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    evaluator: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    artifact_refs: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    payload: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    correlation_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    causation_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence_observations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    invalidation_reason: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))


class Artifact(Base):
    """Immutable source reference used to support an evidence observation."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_artifact_owner_idempotency"),
        Index("ix_artifacts_owner_plan", "owner_id", "plan_id", "created_at"),
        CheckConstraint(
            "request_digest IS NULL OR length(request_digest) = 64",
            name="ck_artifact_request_digest",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(32), index=True)
    source_uri: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(300), default="")
    content_hash: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), index=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), index=True)


class Competency(Base):
    """An explicitly named skill/concept node; keys never merge silently."""

    __tablename__ = "competencies"
    __table_args__ = (
        UniqueConstraint("owner_id", "key", name="uq_competency_owner_key"),
        Index("ix_competencies_owner_scope", "owner_id", "scope", "plan_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    key: Mapped[str] = mapped_column(String(160), index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    competency_type: Mapped[str] = mapped_column(String(32), default="concept")
    scope: Mapped[str] = mapped_column(String(16), default="global", index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now)


class CompetencyEdge(Base):
    __tablename__ = "competency_edges"
    __table_args__ = (
        UniqueConstraint("owner_id", "source_id", "target_id", "relation", name="uq_competency_edge"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("competencies.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("competencies.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(24), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class PlanCompetencyLink(Base):
    __tablename__ = "plan_competency_links"
    __table_args__ = (
        UniqueConstraint("owner_id", "plan_id", "competency_id", name="uq_plan_competency_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id", ondelete="CASCADE"), index=True)
    target_stage: Mapped[str] = mapped_column(String(24), default="practicing")
    relation: Mapped[str] = mapped_column(String(24), default="targets")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class TaskCompetencyLink(Base):
    __tablename__ = "task_competency_links"
    __table_args__ = (
        UniqueConstraint("owner_id", "task_id", "competency_id", "relation", name="uq_task_competency_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(24), default="teaches")
    target_stage: Mapped[str] = mapped_column(String(24), default="practicing")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class ResourceCompetencyLink(Base):
    __tablename__ = "resource_competency_links"
    __table_args__ = (
        UniqueConstraint("owner_id", "resource_id", "competency_id", name="uq_resource_competency_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("learning_resources.id", ondelete="CASCADE"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id", ondelete="CASCADE"), index=True)
    depth: Mapped[str] = mapped_column(String(24), default="overview")
    relation: Mapped[str] = mapped_column(String(24), default="covers")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    scope: Mapped[str] = mapped_column(String(32), default="global", index=True)
    scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    layer: Mapped[str] = mapped_column(String(32), default="short_term", index=True)
    content: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(64), default="user")
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    archived_from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    archived_reason: Mapped[str] = mapped_column(Text, default="")
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reinforced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    # Access counters are updated during retrieval and must not refresh semantic
    # freshness. Lifecycle code updates this timestamp explicitly when the fact changes.
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())

    @property
    def restorable(self) -> bool:
        if self.status == "expired":
            return True
        if self.status != "archived":
            return False
        return self.archived_reason not in {
            "来源消息已被用户修订",
            "关联计划已不存在",
            "同一旧认识已有其他纠正被确认",
        }


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    markdown: Mapped[str] = mapped_column(Text)
    source_manifest: Mapped[list] = mapped_column(JSON, default=list)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class OutboxAction(Base):
    """Durable intent and uncertainty fence for one external write."""

    __tablename__ = "outbox_actions"
    __table_args__ = (
        CheckConstraint("length(request_digest) = 64", name="ck_outbox_action_request_digest"),
        CheckConstraint("effect_kind = 'external_write'", name="ck_outbox_action_effect_kind"),
        CheckConstraint(
            "destination IN ('smtp', 'web_push', 'workspace_file', 'subprocess')",
            name="ck_outbox_action_destination",
        ),
        CheckConstraint(
            "status IN ('queued', 'delivering', 'needs_reconciliation', "
            "'retry_pending', 'delivered', 'failed', 'cancelled')",
            name="ck_outbox_action_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_outbox_action_attempt"),
        CheckConstraint("version >= 1", name="ck_outbox_action_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), index=True)
    effect_kind: Mapped[str] = mapped_column(
        String(32), default="external_write", server_default=text("'external_write'")
    )
    destination: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(
        String(32), default="queued", server_default=text("'queued'"), index=True
    )
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class OutboxReceipt(Base):
    """Provider acknowledgement for an outbox action, written at most once."""

    __tablename__ = "outbox_receipts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted', 'delivered', 'reconciled')",
            name="ck_outbox_receipt_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    outbox_action_id: Mapped[str] = mapped_column(
        ForeignKey("outbox_actions.id", ondelete="CASCADE"), unique=True, index=True
    )
    action_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="accepted", server_default=text("'accepted'"))
    provider_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    response: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    accepted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now()
    )


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_string)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    forward_patch: Mapped[dict] = mapped_column(JSON, default=dict)
    inverse_patch: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="committed", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="in_app")
    title: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    reply_token: Mapped[str] = mapped_column(String(64), default=uuid_string, unique=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    keys: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class ReviewSchedule(Base):
    __tablename__ = "review_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    review_type: Mapped[str] = mapped_column(String(32), default="quiz")
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())


class Quiz(Base):
    __tablename__ = "quizzes"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    prompt: Mapped[str] = mapped_column(Text)
    rubric: Mapped[dict] = mapped_column(JSON, default=dict)
    answer: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())
    graded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    badge_kind: Mapped[str] = mapped_column(String(32), default="rule")
    badge_image_url: Mapped[str] = mapped_column(Text, default="")
    unlocked_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, server_default=func.now())

    __table_args__ = (UniqueConstraint("owner_id", "key", name="uq_owner_achievement"),)


class ActivityDay(Base):
    __tablename__ = "activity_days"
    __table_args__ = (UniqueConstraint("owner_id", "date", name="uq_owner_activity_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"), index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    passed_quizzes: Mapped[int] = mapped_column(Integer, default=0)
