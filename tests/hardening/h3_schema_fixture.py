"""Frozen revision-2 runtime schema used only as an H3 migration oracle."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrations import migrate_sqlite_database, schema_checksum


FROZEN_H2_SCHEMA_CHECKSUM = (
    "7f42435d235b1497a358abc4353ff6b52771514a4b252c5ba74acec6e1493de1"
)


FROZEN_H2_RUNTIME_SQL = r"""
PRAGMA foreign_keys=OFF;
DROP TABLE IF EXISTS run_approvals;

CREATE TABLE _h2_agent_runs (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL REFERENCES owners(id),
    session_id VARCHAR(64) REFERENCES sessions(id) ON DELETE SET NULL,
    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
    parent_run_id VARCHAR(64) REFERENCES agent_runs(id) ON DELETE SET NULL,
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    model VARCHAR(120) NOT NULL,
    cancel_requested BOOLEAN NOT NULL,
    checkpoint JSON,
    pending_approval JSON,
    budget_usage JSON,
    output TEXT NOT NULL,
    created_plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
);
INSERT INTO _h2_agent_runs (
    id, owner_id, session_id, plan_id, parent_run_id, "trigger", objective,
    status, model, cancel_requested, checkpoint, pending_approval, budget_usage,
    output, created_plan_id, started_at, completed_at, created_at
)
SELECT id, owner_id, session_id, plan_id, parent_run_id, "trigger", objective,
       status, model, cancel_requested, checkpoint, pending_approval, budget_usage,
       output, created_plan_id, started_at, completed_at, created_at
FROM agent_runs;

CREATE TABLE _h2_chat_messages (
    id INTEGER NOT NULL PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id VARCHAR(64) REFERENCES agent_runs(id) ON DELETE SET NULL,
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    message_metadata JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
);
INSERT INTO _h2_chat_messages
    (id, session_id, run_id, role, content, message_metadata, created_at)
SELECT id, session_id, run_id, role, content, message_metadata, created_at
FROM chat_messages;

CREATE TABLE _h2_tool_invocations (
    id INTEGER NOT NULL PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL REFERENCES owners(id),
    run_id VARCHAR(64) NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(180) NOT NULL,
    tool_name VARCHAR(120) NOT NULL,
    args_hash VARCHAR(64) NOT NULL,
    request_digest VARCHAR(64),
    canonical_args JSON,
    effect_kind VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    result_payload JSON NOT NULL,
    claim_token VARCHAR(64) UNIQUE,
    attempt INTEGER DEFAULT 1 NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    claimed_at DATETIME,
    claim_expires_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_tool_invocation_request_digest
        CHECK (request_digest IS NULL OR length(request_digest) = 64),
    CONSTRAINT ck_tool_invocation_effect_kind
        CHECK (effect_kind IS NULL OR effect_kind IN
               ('pure_read', 'database_write', 'external_read', 'external_write')),
    CONSTRAINT ck_tool_invocation_status
        CHECK (status IN ('running', 'pending_approval', 'pending_delivery',
                          'committed', 'failed', 'needs_reconciliation',
                          'retry_pending', 'cancelled')),
    CONSTRAINT ck_tool_invocation_attempt CHECK (attempt >= 1),
    CONSTRAINT ck_tool_invocation_version CHECK (version >= 1)
);
INSERT INTO _h2_tool_invocations (
    id, owner_id, run_id, idempotency_key, tool_name, args_hash,
    request_digest, canonical_args, effect_kind, status, result_payload,
    claim_token, attempt, version, claimed_at, claim_expires_at,
    completed_at, created_at, updated_at
)
SELECT id, owner_id, run_id, idempotency_key, tool_name, args_hash,
       request_digest, canonical_args, effect_kind, status, result_payload,
       claim_token, attempt, version, claimed_at, claim_expires_at,
       completed_at, created_at, updated_at
FROM tool_invocations;

CREATE TABLE _h2_run_events (
    id INTEGER NOT NULL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT uq_run_event_sequence UNIQUE (run_id, sequence)
);
INSERT INTO _h2_run_events
    (id, run_id, sequence, event_type, summary, payload, created_at)
SELECT id, run_id, sequence, event_type, summary, payload, created_at
FROM run_events;

CREATE TABLE _h2_run_steer_messages (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL REFERENCES owners(id),
    run_id VARCHAR(64) NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    applied_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
);
INSERT INTO _h2_run_steer_messages
    (id, owner_id, run_id, content, applied_at, created_at)
SELECT id, owner_id, run_id, content, applied_at, created_at
FROM run_steer_messages;

CREATE TABLE _h2_queued_messages (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL REFERENCES owners(id),
    session_id VARCHAR(64) REFERENCES sessions(id) ON DELETE CASCADE,
    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    user_content TEXT,
    message_metadata JSON NOT NULL,
    position INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
);
INSERT INTO _h2_queued_messages (
    id, owner_id, session_id, plan_id, "trigger", objective, user_content,
    message_metadata, position, created_at, updated_at
)
SELECT id, owner_id, session_id, plan_id, "trigger", objective, user_content,
       message_metadata, position, created_at, updated_at
FROM queued_messages;

DROP TABLE queued_messages;
DROP TABLE run_steer_messages;
DROP TABLE run_events;
DROP TABLE tool_invocations;
DROP TABLE chat_messages;
DROP TABLE agent_runs;

CREATE TABLE agent_runs (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    plan_id INTEGER,
    parent_run_id VARCHAR(64),
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
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
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(parent_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(created_plan_id) REFERENCES plans (id) ON DELETE SET NULL
);
INSERT INTO agent_runs SELECT * FROM _h2_agent_runs;

CREATE TABLE chat_messages (
    id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64),
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    message_metadata JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
INSERT INTO chat_messages SELECT * FROM _h2_chat_messages;

CREATE TABLE tool_invocations (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(180) NOT NULL,
    tool_name VARCHAR(120) NOT NULL,
    args_hash VARCHAR(64) NOT NULL,
    request_digest VARCHAR(64),
    canonical_args JSON,
    effect_kind VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    result_payload JSON NOT NULL,
    claim_token VARCHAR(64),
    attempt INTEGER DEFAULT 1 NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    claimed_at DATETIME,
    claim_expires_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_tool_invocation_request_digest CHECK (request_digest IS NULL OR length(request_digest) = 64),
    CONSTRAINT ck_tool_invocation_effect_kind CHECK (effect_kind IS NULL OR effect_kind IN ('pure_read', 'database_write', 'external_read', 'external_write')),
    CONSTRAINT ck_tool_invocation_status CHECK (status IN ('running', 'pending_approval', 'pending_delivery', 'committed', 'failed', 'needs_reconciliation', 'retry_pending', 'cancelled')),
    CONSTRAINT ck_tool_invocation_attempt CHECK (attempt >= 1),
    CONSTRAINT ck_tool_invocation_version CHECK (version >= 1),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE,
    UNIQUE (claim_token)
);
INSERT INTO tool_invocations SELECT * FROM _h2_tool_invocations;

CREATE TABLE run_events (
    id INTEGER NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    sequence INTEGER NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_run_event_sequence UNIQUE (run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);
INSERT INTO run_events SELECT * FROM _h2_run_events;

CREATE TABLE run_steer_messages (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    content TEXT NOT NULL,
    applied_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);
INSERT INTO run_steer_messages SELECT * FROM _h2_run_steer_messages;

CREATE TABLE queued_messages (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    plan_id INTEGER,
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    user_content TEXT,
    message_metadata JSON NOT NULL,
    position INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL
);
INSERT INTO queued_messages SELECT * FROM _h2_queued_messages;

DROP TABLE _h2_queued_messages;
DROP TABLE _h2_run_steer_messages;
DROP TABLE _h2_run_events;
DROP TABLE _h2_tool_invocations;
DROP TABLE _h2_chat_messages;
DROP TABLE _h2_agent_runs;

CREATE INDEX ix_agent_runs_created_plan_id ON agent_runs (created_plan_id);
CREATE INDEX ix_agent_runs_owner_id ON agent_runs (owner_id);
CREATE INDEX ix_agent_runs_plan_id ON agent_runs (plan_id);
CREATE INDEX ix_agent_runs_status ON agent_runs (status);
CREATE INDEX ix_agent_runs_trigger ON agent_runs ("trigger");
CREATE INDEX ix_chat_messages_run_id ON chat_messages (run_id);
CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);
CREATE UNIQUE INDEX ix_tool_invocations_idempotency_key ON tool_invocations (idempotency_key);
CREATE INDEX ix_tool_invocations_owner_id ON tool_invocations (owner_id);
CREATE INDEX ix_tool_invocations_run_id ON tool_invocations (run_id);
CREATE INDEX ix_tool_invocations_status ON tool_invocations (status);
CREATE INDEX ix_tool_invocations_request_digest ON tool_invocations (request_digest);
CREATE INDEX ix_tool_invocations_effect_kind ON tool_invocations (effect_kind);
CREATE INDEX ix_tool_invocations_claim_expires_at ON tool_invocations (claim_expires_at);
CREATE INDEX ix_run_events_run_id ON run_events (run_id);
CREATE INDEX ix_run_events_event_type ON run_events (event_type);
CREATE INDEX ix_run_steer_messages_owner_id ON run_steer_messages (owner_id);
CREATE INDEX ix_run_steer_messages_run_id ON run_steer_messages (run_id);
CREATE INDEX ix_queued_messages_owner_id ON queued_messages (owner_id);
CREATE INDEX ix_queued_messages_session_id ON queued_messages (session_id);
DELETE FROM schema_migrations WHERE version > 2;
PRAGMA user_version=2;
PRAGMA foreign_keys=ON;
"""


def materialize_frozen_h2(path: Path, backup_root: Path) -> None:
    """Create a byte-independent revision-2 schema from current migration output."""

    migrate_sqlite_database(
        path,
        backup_root=backup_root,
        database_identity=path.name,
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(FROZEN_H2_RUNTIME_SQL)
    assert schema_checksum(path) == FROZEN_H2_SCHEMA_CHECKSUM
