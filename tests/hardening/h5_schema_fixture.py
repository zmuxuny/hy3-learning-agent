"""Frozen revision-4 Context/Intervention schema used by H5 migration tests.

The DDL is intentionally literal.  It captures every revision-4 table and
index changed by H5 and must never be regenerated from current ORM metadata.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrations import migrate_sqlite_database, schema_checksum


FROZEN_H4_SCHEMA_CHECKSUM = (
    "851f34b9c3d455208b73c6815856da70edc57e52d5676f8b6b391e8ddf1b0ace"
)


FROZEN_H4_CONTEXT_SQL = r"""
PRAGMA foreign_keys=OFF;

DROP TABLE IF EXISTS inbound_mail_jobs;
DROP TABLE IF EXISTS context_snapshot_blocks;
DROP TABLE IF EXISTS memory_lifecycle_events;
DROP TABLE IF EXISTS provenance_edges;
DROP TABLE IF EXISTS session_compression_states;
DROP TABLE IF EXISTS session_handoffs;
DROP TABLE IF EXISTS interventions;
DROP TABLE IF EXISTS proactive_decisions;
DROP TABLE IF EXISTS provenance_nodes;
DROP TABLE IF EXISTS context_states;

DROP TABLE IF EXISTS notifications;
DROP TABLE IF EXISTS queued_messages;
DROP TABLE IF EXISTS chat_message_revisions;
DROP TABLE IF EXISTS session_summaries;
DROP TABLE IF EXISTS context_snapshots;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS chat_messages;
DROP TABLE IF EXISTS agent_runs;

CREATE TABLE agent_runs (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    plan_id INTEGER,
    parent_run_id VARCHAR(64),
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    phase VARCHAR(32) DEFAULT 'not_started' NOT NULL,
    state_version INTEGER DEFAULT 1 NOT NULL,
    checkpoint_schema_version INTEGER,
    lease_token VARCHAR(64),
    lease_owner VARCHAR(160),
    lease_acquired_at DATETIME,
    lease_expires_at DATETIME,
    attempt INTEGER DEFAULT 0 NOT NULL,
    retry_count INTEGER DEFAULT 0 NOT NULL,
    available_at DATETIME,
    status_reason VARCHAR(64),
    model VARCHAR(120) NOT NULL,
    cancel_requested BOOLEAN NOT NULL,
    checkpoint JSON,
    pending_approval JSON,
    budget_usage JSON,
    output TEXT NOT NULL,
    created_plan_id INTEGER,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_agent_run_status CHECK (status IN ('queued', 'running', 'waiting_approval', 'retry_wait', 'completed', 'failed', 'cancelled', 'needs_reconciliation')),
    CONSTRAINT ck_agent_run_phase CHECK (phase IN ('not_started', 'starting', 'awaiting_model', 'tool_ready', 'tool_running', 'waiting_approval', 'retry_wait', 'finalizing', 'terminal', 'reconciling')),
    CONSTRAINT ck_agent_run_state_version CHECK (state_version >= 1),
    CONSTRAINT ck_agent_run_attempt CHECK (attempt >= 0),
    CONSTRAINT ck_agent_run_retry_count CHECK (retry_count >= 0),
    CONSTRAINT ck_agent_run_checkpoint_envelope CHECK ((checkpoint IS NULL AND checkpoint_schema_version IS NULL) OR (checkpoint IS NOT NULL AND checkpoint_schema_version = 1 AND CASE WHEN json_valid(checkpoint) THEN coalesce((json_extract(checkpoint, '$.schema_version') = checkpoint_schema_version AND json_extract(checkpoint, '$.kind') IN ('agent', 'subagent') AND json_extract(checkpoint, '$.phase') IN ('not_started', 'starting', 'awaiting_model', 'tool_ready', 'tool_running', 'waiting_approval', 'retry_wait', 'finalizing', 'terminal', 'reconciling') AND json_type(checkpoint, '$.step') = 'integer' AND json_extract(checkpoint, '$.step') >= 0 AND json_type(checkpoint, '$.messages') = 'array' AND json_type(checkpoint, '$.current_tool_call') IN ('null', 'object') AND json_type(checkpoint, '$.remaining_tool_calls') = 'array' AND json_type(checkpoint, '$.current_invocation_id') IN ('null', 'integer') AND (json_type(checkpoint, '$.current_invocation_id') = 'null' OR json_extract(checkpoint, '$.current_invocation_id') > 0) AND json_type(checkpoint, '$.context_snapshot_id') IN ('null', 'integer') AND (json_type(checkpoint, '$.context_snapshot_id') = 'null' OR json_extract(checkpoint, '$.context_snapshot_id') > 0) AND json_type(checkpoint, '$.cards') = 'array' AND json_type(checkpoint, '$.budget_usage') = 'object' AND json_type(checkpoint, '$.state_version') = 'integer' AND json_extract(checkpoint, '$.state_version') >= 1), 0) ELSE 0 END)),
    CONSTRAINT ck_agent_run_lease_shape CHECK ((lease_token IS NULL AND lease_owner IS NULL AND lease_acquired_at IS NULL AND lease_expires_at IS NULL) OR (lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_acquired_at IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CONSTRAINT ck_agent_run_waiting_approval_projection CHECK (status <> 'waiting_approval' OR pending_approval IS NOT NULL),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(parent_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
    UNIQUE (lease_token),
    FOREIGN KEY(created_plan_id) REFERENCES plans (id) ON DELETE SET NULL
);
CREATE INDEX ix_agent_runs_created_plan_id ON agent_runs (created_plan_id);
CREATE INDEX ix_agent_runs_owner_id ON agent_runs (owner_id);
CREATE INDEX ix_agent_runs_parent_status ON agent_runs (parent_run_id, status);
CREATE INDEX ix_agent_runs_plan_id ON agent_runs (plan_id);
CREATE INDEX ix_agent_runs_recovery ON agent_runs (status, available_at, lease_expires_at);
CREATE INDEX ix_agent_runs_status ON agent_runs (status);
CREATE INDEX ix_agent_runs_trigger ON agent_runs ("trigger");
CREATE UNIQUE INDEX uq_agent_runs_active_global_session_root ON agent_runs (owner_id, session_id) WHERE parent_run_id IS NULL AND plan_id IS NULL AND session_id IS NOT NULL AND status IN ('queued', 'running', 'waiting_approval', 'retry_wait');
CREATE UNIQUE INDEX uq_agent_runs_active_plan_root ON agent_runs (owner_id, plan_id) WHERE parent_run_id IS NULL AND plan_id IS NOT NULL AND status IN ('queued', 'running', 'waiting_approval', 'retry_wait');
CREATE UNIQUE INDEX uq_agent_runs_active_stateless_root ON agent_runs (owner_id) WHERE parent_run_id IS NULL AND plan_id IS NULL AND session_id IS NULL AND trigger <> 'subagent' AND status IN ('queued', 'running', 'waiting_approval', 'retry_wait');

CREATE TABLE chat_messages (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64),
    message_key VARCHAR(180),
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    message_metadata JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_chat_messages_run_id ON chat_messages (run_id);
CREATE INDEX ix_chat_messages_run_role ON chat_messages (run_id, role);
CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);
CREATE UNIQUE INDEX uq_chat_messages_session_message_key ON chat_messages (session_id, message_key) WHERE message_key IS NOT NULL;

CREATE TABLE chat_message_revisions (
    id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    previous_run_id VARCHAR(64),
    content TEXT NOT NULL,
    message_metadata JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(previous_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_chat_message_revisions_message_id ON chat_message_revisions (message_id);
CREATE INDEX ix_chat_message_revisions_previous_run_id ON chat_message_revisions (previous_run_id);
CREATE INDEX ix_chat_message_revisions_session_id ON chat_message_revisions (session_id);

CREATE TABLE queued_messages (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    plan_id INTEGER,
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    user_content TEXT,
    message_metadata JSON NOT NULL,
    source_steer_id VARCHAR(64),
    source_message_id INTEGER,
    position INTEGER NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_queued_message_position CHECK (position >= 0),
    CONSTRAINT ck_queued_message_version CHECK (version >= 1),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(source_steer_id) REFERENCES run_steer_messages (id) ON DELETE SET NULL,
    FOREIGN KEY(source_message_id) REFERENCES chat_messages (id) ON DELETE SET NULL
);
CREATE INDEX ix_queued_messages_owner_id ON queued_messages (owner_id);
CREATE INDEX ix_queued_messages_session_dequeue ON queued_messages (owner_id, session_id, position, created_at, id) WHERE session_id IS NOT NULL;
CREATE INDEX ix_queued_messages_session_id ON queued_messages (session_id);
CREATE INDEX ix_queued_messages_stateless_dequeue ON queued_messages (owner_id, position, created_at, id) WHERE session_id IS NULL;
CREATE UNIQUE INDEX uq_queued_messages_session_position ON queued_messages (owner_id, session_id, position) WHERE session_id IS NOT NULL;
CREATE UNIQUE INDEX uq_queued_messages_source_steer ON queued_messages (source_steer_id) WHERE source_steer_id IS NOT NULL;
CREATE UNIQUE INDEX uq_queued_messages_stateless_position ON queued_messages (owner_id, position) WHERE session_id IS NULL;

CREATE TABLE session_summaries (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    covered_through_message_id INTEGER,
    source_message_ids JSON NOT NULL,
    method VARCHAR(32) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_session_summary_version UNIQUE (session_id, version),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE INDEX ix_session_summaries_owner_id ON session_summaries (owner_id);
CREATE INDEX ix_session_summaries_session_id ON session_summaries (session_id);

CREATE TABLE memories (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    scope_id VARCHAR(64),
    layer VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    source_type VARCHAR(64) NOT NULL,
    source_id VARCHAR(64),
    confidence FLOAT NOT NULL,
    status VARCHAR(32) NOT NULL,
    archived_from_status VARCHAR(32),
    archived_reason TEXT NOT NULL,
    supersedes_id INTEGER,
    superseded_by_id INTEGER,
    last_accessed_at DATETIME,
    access_count INTEGER NOT NULL,
    last_reinforced_at DATETIME,
    expires_at DATETIME,
    embedding JSON,
    embedding_provider VARCHAR(64),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(supersedes_id) REFERENCES memories (id) ON DELETE SET NULL,
    FOREIGN KEY(superseded_by_id) REFERENCES memories (id) ON DELETE SET NULL
);
CREATE INDEX ix_memories_layer ON memories (layer);
CREATE INDEX ix_memories_owner_id ON memories (owner_id);
CREATE INDEX ix_memories_scope ON memories (scope);
CREATE INDEX ix_memories_scope_id ON memories (scope_id);
CREATE INDEX ix_memories_status ON memories (status);
CREATE INDEX ix_memories_superseded_by_id ON memories (superseded_by_id);
CREATE INDEX ix_memories_supersedes_id ON memories (supersedes_id);

CREATE TABLE context_snapshots (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER,
    run_id VARCHAR(64),
    markdown TEXT NOT NULL,
    source_manifest JSON NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_context_snapshots_owner_id ON context_snapshots (owner_id);
CREATE INDEX ix_context_snapshots_plan_id ON context_snapshots (plan_id);
CREATE INDEX ix_context_snapshots_run_id ON context_snapshots (run_id);

CREATE TABLE notifications (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64),
    invocation_id INTEGER,
    session_id VARCHAR(64),
    plan_id INTEGER,
    channel VARCHAR(32) NOT NULL,
    title VARCHAR(240) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    reply_token VARCHAR(64) NOT NULL,
    sent_at DATETIME,
    read_at DATETIME,
    archived_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(invocation_id) REFERENCES tool_invocations (id) ON DELETE SET NULL,
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    UNIQUE (reply_token)
);
CREATE INDEX ix_notifications_archived_at ON notifications (archived_at);
CREATE INDEX ix_notifications_invocation_id ON notifications (invocation_id);
CREATE INDEX ix_notifications_owner_id ON notifications (owner_id);
CREATE INDEX ix_notifications_plan_id ON notifications (plan_id);
CREATE INDEX ix_notifications_session_id ON notifications (session_id);
CREATE INDEX ix_notifications_status ON notifications (status);

DELETE FROM schema_migrations WHERE version > 4;
PRAGMA user_version=4;
PRAGMA foreign_keys=ON;
"""


def materialize_frozen_h4(path: Path, bootstrap_backup_root: Path) -> None:
    """Create an empty exact revision-4 database without current ORM drift."""

    migrate_sqlite_database(path, backup_root=bootstrap_backup_root)
    with sqlite3.connect(path) as connection:
        connection.executescript(FROZEN_H4_CONTEXT_SQL)
    assert schema_checksum(path) == FROZEN_H4_SCHEMA_CHECKSUM
