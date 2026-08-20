"""Frozen revision-3 Evidence/Competency schema used by H4 migration tests.

The SQL below is intentionally literal.  It is the exact revision-3 shape of
the tables changed by H4; it must not be derived from current ORM metadata.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrations import migrate_sqlite_database, schema_checksum


FROZEN_H3_SCHEMA_CHECKSUM = (
    "b69ed9f0844106e54936e008c22b4d4ebd7e089a8cb38989a4306ad25c7239de"
)


FROZEN_H3_EVIDENCE_COMPETENCY_SQL = r"""
PRAGMA foreign_keys=OFF;

DROP TABLE IF EXISTS evidence_projection_states;
DROP TABLE IF EXISTS competency_graph_mutations;
DROP TABLE IF EXISTS competency_graph_states;
DROP TABLE IF EXISTS operation_dependencies;
DROP TABLE IF EXISTS operation_evidence_links;
DROP TABLE IF EXISTS evidence_competency_links;
DROP TABLE IF EXISTS evidence_artifact_links;

DROP TABLE IF EXISTS resource_competency_links;
DROP TABLE IF EXISTS task_competency_links;
DROP TABLE IF EXISTS plan_competency_links;
DROP TABLE IF EXISTS competency_edges;
DROP TABLE IF EXISTS evidence_observations;
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS competencies;

CREATE TABLE artifacts (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    artifact_type VARCHAR(32) NOT NULL,
    source_uri VARCHAR(500) NOT NULL,
    title VARCHAR(300) NOT NULL,
    content_hash VARCHAR(128) NOT NULL,
    size_bytes INTEGER,
    metadata JSON NOT NULL,
    plan_id INTEGER,
    task_id INTEGER,
    run_id VARCHAR(64),
    session_id VARCHAR(64),
    idempotency_key VARCHAR(180) NOT NULL,
    request_digest VARCHAR(64),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_artifact_owner_idempotency UNIQUE (owner_id, idempotency_key),
    CONSTRAINT ck_artifact_request_digest CHECK (request_digest IS NULL OR length(request_digest) = 64),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL
);
CREATE INDEX ix_artifacts_artifact_type ON artifacts (artifact_type);
CREATE INDEX ix_artifacts_created_at ON artifacts (created_at);
CREATE INDEX ix_artifacts_idempotency_key ON artifacts (idempotency_key);
CREATE INDEX ix_artifacts_owner_id ON artifacts (owner_id);
CREATE INDEX ix_artifacts_owner_plan ON artifacts (owner_id, plan_id, created_at);
CREATE INDEX ix_artifacts_plan_id ON artifacts (plan_id);
CREATE INDEX ix_artifacts_request_digest ON artifacts (request_digest);
CREATE INDEX ix_artifacts_run_id ON artifacts (run_id);
CREATE INDEX ix_artifacts_session_id ON artifacts (session_id);
CREATE INDEX ix_artifacts_task_id ON artifacts (task_id);

CREATE TABLE competencies (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    "key" VARCHAR(160) NOT NULL,
    title VARCHAR(240) NOT NULL,
    description TEXT NOT NULL,
    competency_type VARCHAR(32) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    plan_id INTEGER,
    status VARCHAR(24) NOT NULL,
    version INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_competency_owner_key UNIQUE (owner_id, "key"),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE
);
CREATE INDEX ix_competencies_key ON competencies ("key");
CREATE INDEX ix_competencies_owner_id ON competencies (owner_id);
CREATE INDEX ix_competencies_owner_scope ON competencies (owner_id, scope, plan_id);
CREATE INDEX ix_competencies_plan_id ON competencies (plan_id);
CREATE INDEX ix_competencies_scope ON competencies (scope);
CREATE INDEX ix_competencies_status ON competencies (status);

CREATE TABLE competency_edges (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation VARCHAR(24) NOT NULL,
    version INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_competency_edge UNIQUE (owner_id, source_id, target_id, relation),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(source_id) REFERENCES competencies (id) ON DELETE CASCADE,
    FOREIGN KEY(target_id) REFERENCES competencies (id) ON DELETE CASCADE
);
CREATE INDEX ix_competency_edges_owner_id ON competency_edges (owner_id);
CREATE INDEX ix_competency_edges_relation ON competency_edges (relation);
CREATE INDEX ix_competency_edges_source_id ON competency_edges (source_id);
CREATE INDEX ix_competency_edges_target_id ON competency_edges (target_id);

CREATE TABLE evidence_observations (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_id VARCHAR(120) NOT NULL,
    run_id VARCHAR(64),
    session_id VARCHAR(64),
    plan_id INTEGER,
    task_id INTEGER,
    competency_id INTEGER,
    competency_key VARCHAR(160),
    outcome VARCHAR(40) NOT NULL,
    normalized_score FLOAT,
    is_correct BOOLEAN,
    assistance_level VARCHAR(24) DEFAULT 'unknown' NOT NULL,
    transfer_level VARCHAR(24) DEFAULT 'unknown' NOT NULL,
    rubric_snapshot JSON DEFAULT '{}' NOT NULL,
    evaluator JSON DEFAULT '{}' NOT NULL,
    artifact_refs JSON DEFAULT '[]' NOT NULL,
    payload JSON DEFAULT '{}' NOT NULL,
    occurred_at DATETIME NOT NULL,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    schema_version INTEGER DEFAULT 1 NOT NULL,
    correlation_id VARCHAR(120),
    causation_id VARCHAR(120),
    idempotency_key VARCHAR(180) NOT NULL,
    request_digest VARCHAR(64),
    supersedes_id INTEGER,
    invalidated_at DATETIME,
    invalidation_reason TEXT DEFAULT '' NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_evidence_observation_request_digest CHECK (request_digest IS NULL OR length(request_digest) = 64),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
    FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE SET NULL,
    FOREIGN KEY(supersedes_id) REFERENCES evidence_observations (id) ON DELETE SET NULL
);
CREATE INDEX ix_evidence_observations_causation_id ON evidence_observations (causation_id);
CREATE INDEX ix_evidence_observations_competency_id ON evidence_observations (competency_id);
CREATE INDEX ix_evidence_observations_competency_key ON evidence_observations (competency_key);
CREATE INDEX ix_evidence_observations_correlation_id ON evidence_observations (correlation_id);
CREATE UNIQUE INDEX ix_evidence_observations_idempotency_key ON evidence_observations (idempotency_key);
CREATE INDEX ix_evidence_observations_occurred_at ON evidence_observations (occurred_at);
CREATE INDEX ix_evidence_observations_outcome ON evidence_observations (outcome);
CREATE INDEX ix_evidence_observations_owner_id ON evidence_observations (owner_id);
CREATE INDEX ix_evidence_observations_owner_plan ON evidence_observations (owner_id, plan_id, recorded_at);
CREATE INDEX ix_evidence_observations_plan_id ON evidence_observations (plan_id);
CREATE INDEX ix_evidence_observations_recorded_at ON evidence_observations (recorded_at);
CREATE INDEX ix_evidence_observations_request_digest ON evidence_observations (request_digest);
CREATE INDEX ix_evidence_observations_run_id ON evidence_observations (run_id);
CREATE INDEX ix_evidence_observations_session_id ON evidence_observations (session_id);
CREATE INDEX ix_evidence_observations_source ON evidence_observations (owner_id, source_type, source_id);
CREATE INDEX ix_evidence_observations_source_id ON evidence_observations (source_id);
CREATE INDEX ix_evidence_observations_source_type ON evidence_observations (source_type);
CREATE INDEX ix_evidence_observations_supersedes_id ON evidence_observations (supersedes_id);
CREATE INDEX ix_evidence_observations_task ON evidence_observations (owner_id, task_id, occurred_at);
CREATE INDEX ix_evidence_observations_task_id ON evidence_observations (task_id);

CREATE TABLE plan_competency_links (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER NOT NULL,
    competency_id INTEGER NOT NULL,
    target_stage VARCHAR(24) NOT NULL,
    relation VARCHAR(24) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_plan_competency_link UNIQUE (owner_id, plan_id, competency_id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE,
    FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE CASCADE
);
CREATE INDEX ix_plan_competency_links_competency_id ON plan_competency_links (competency_id);
CREATE INDEX ix_plan_competency_links_owner_id ON plan_competency_links (owner_id);
CREATE INDEX ix_plan_competency_links_plan_id ON plan_competency_links (plan_id);

CREATE TABLE task_competency_links (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    task_id INTEGER NOT NULL,
    competency_id INTEGER NOT NULL,
    relation VARCHAR(24) NOT NULL,
    target_stage VARCHAR(24) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_task_competency_link UNIQUE (owner_id, task_id, competency_id, relation),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE,
    FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE CASCADE
);
CREATE INDEX ix_task_competency_links_competency_id ON task_competency_links (competency_id);
CREATE INDEX ix_task_competency_links_owner_id ON task_competency_links (owner_id);
CREATE INDEX ix_task_competency_links_task_id ON task_competency_links (task_id);

CREATE TABLE resource_competency_links (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    resource_id INTEGER NOT NULL,
    competency_id INTEGER NOT NULL,
    depth VARCHAR(24) NOT NULL,
    relation VARCHAR(24) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_resource_competency_link UNIQUE (owner_id, resource_id, competency_id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(resource_id) REFERENCES learning_resources (id) ON DELETE CASCADE,
    FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE CASCADE
);
CREATE INDEX ix_resource_competency_links_competency_id ON resource_competency_links (competency_id);
CREATE INDEX ix_resource_competency_links_owner_id ON resource_competency_links (owner_id);
CREATE INDEX ix_resource_competency_links_resource_id ON resource_competency_links (resource_id);

DELETE FROM schema_migrations WHERE version > 3;
PRAGMA user_version=3;
PRAGMA foreign_keys=ON;
"""


def downgrade_current_to_frozen_h3(connection: sqlite3.Connection) -> None:
    connection.executescript(FROZEN_H3_EVIDENCE_COMPETENCY_SQL)


def materialize_frozen_h3(path: Path, backup_root: Path) -> None:
    """Create a byte-independent revision-3 schema from current output."""

    migrate_sqlite_database(
        path,
        backup_root=backup_root,
        database_identity=path.name,
    )
    with sqlite3.connect(path) as connection:
        downgrade_current_to_frozen_h3(connection)
    assert schema_checksum(path) == FROZEN_H3_SCHEMA_CHECKSUM
