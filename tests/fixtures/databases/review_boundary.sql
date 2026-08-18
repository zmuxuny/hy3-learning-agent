-- Deterministic review-scale fixture. Recursive CTEs keep the source auditable.
-- Evidence cardinalities are isolated by plan: 0, 1, 500, 501, and 10,000.
PRAGMA page_size=4096;
PRAGMA encoding='UTF-8';
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;

CREATE TABLE owners (
    id VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(120) NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL,
    title VARCHAR(240) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id)
);

CREATE TABLE stages (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL,
    title VARCHAR(240) NOT NULL,
    position INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL,
    title VARCHAR(300) NOT NULL,
    status VARCHAR(32) NOT NULL,
    position INTEGER NOT NULL,
    FOREIGN KEY(stage_id) REFERENCES stages(id) ON DELETE CASCADE
);

CREATE TABLE sessions (
    id VARCHAR(64) PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER,
    title VARCHAR(240) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id),
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE SET NULL
);

CREATE TABLE agent_runs (
    id VARCHAR(64) PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64),
    plan_id INTEGER,
    "trigger" VARCHAR(40) NOT NULL,
    objective TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(id),
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE SET NULL
);

CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64),
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    message_metadata JSON NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE SET NULL
);

CREATE TABLE competencies (
    id INTEGER PRIMARY KEY,
    owner_id VARCHAR(64) NOT NULL,
    "key" VARCHAR(160) NOT NULL,
    title VARCHAR(240) NOT NULL,
    scope VARCHAR(16) NOT NULL,
    plan_id INTEGER,
    status VARCHAR(24) NOT NULL,
    version INTEGER NOT NULL,
    CONSTRAINT uq_boundary_competency UNIQUE(owner_id, "key"),
    FOREIGN KEY(owner_id) REFERENCES owners(id),
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
);

CREATE TABLE evidence_observations (
    id INTEGER PRIMARY KEY,
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
    assistance_level VARCHAR(24) NOT NULL DEFAULT 'unknown',
    transfer_level VARCHAR(24) NOT NULL DEFAULT 'unknown',
    rubric_snapshot JSON NOT NULL DEFAULT '{}',
    evaluator JSON NOT NULL DEFAULT '{}',
    artifact_refs JSON NOT NULL DEFAULT '[]',
    payload JSON NOT NULL DEFAULT '{}',
    occurred_at DATETIME NOT NULL,
    recorded_at DATETIME NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    correlation_id VARCHAR(120),
    causation_id VARCHAR(120),
    idempotency_key VARCHAR(180) NOT NULL UNIQUE,
    supersedes_id INTEGER,
    invalidated_at DATETIME,
    invalidation_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(owner_id) REFERENCES owners(id),
    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE SET NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE SET NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL,
    FOREIGN KEY(competency_id) REFERENCES competencies(id) ON DELETE SET NULL,
    FOREIGN KEY(supersedes_id) REFERENCES evidence_observations(id) ON DELETE SET NULL
);

CREATE INDEX ix_boundary_messages_session ON chat_messages(session_id, id);
CREATE INDEX ix_boundary_evidence_plan ON evidence_observations(owner_id, plan_id, occurred_at);
CREATE INDEX ix_boundary_evidence_task ON evidence_observations(owner_id, task_id, occurred_at);
CREATE INDEX ix_boundary_evidence_competency ON evidence_observations(competency_id);

INSERT INTO owners VALUES
    ('fixture-owner', 'Boundary Fixture', 'Asia/Shanghai', '2026-01-01T00:00:00.000000Z');

INSERT INTO plans(id, owner_id, title, status, created_at) VALUES
    (100, 'fixture-owner', 'Boundary 0', 'active', '2026-01-01T00:00:00.000000Z'),
    (101, 'fixture-owner', 'Boundary 1', 'active', '2026-01-01T00:00:00.000000Z'),
    (102, 'fixture-owner', 'Boundary 500', 'active', '2026-01-01T00:00:00.000000Z'),
    (103, 'fixture-owner', 'Boundary 501', 'active', '2026-01-01T00:00:00.000000Z'),
    (104, 'fixture-owner', 'Boundary 10000', 'active', '2026-01-01T00:00:00.000000Z');

INSERT INTO stages(id, plan_id, title, position, status) VALUES
    (100, 100, 'Boundary Stage 0', 0, 'active'),
    (101, 101, 'Boundary Stage 1', 0, 'active'),
    (102, 102, 'Boundary Stage 500', 0, 'active'),
    (103, 103, 'Boundary Stage 501', 0, 'active'),
    (104, 104, 'Boundary Stage 10000', 0, 'active');

INSERT INTO tasks(id, stage_id, title, status, position) VALUES
    (100, 100, 'Boundary Task 0', 'pending', 0),
    (101, 101, 'Boundary Task 1', 'completed', 0),
    (102, 102, 'Boundary Task 500', 'completed', 0),
    (103, 103, 'Boundary Task 501', 'completed', 0),
    (104, 104, 'Boundary Task 10000', 'completed', 0);

INSERT INTO competencies(id, owner_id, "key", title, scope, plan_id, status, version) VALUES
    (100, 'fixture-owner', 'boundary-0', 'Boundary Competency 0', 'plan', 100, 'active', 1),
    (101, 'fixture-owner', 'boundary-1', 'Boundary Competency 1', 'plan', 101, 'active', 1),
    (102, 'fixture-owner', 'boundary-500', 'Boundary Competency 500', 'plan', 102, 'active', 1),
    (103, 'fixture-owner', 'boundary-501', 'Boundary Competency 501', 'plan', 103, 'active', 1),
    (104, 'fixture-owner', 'boundary-10000', 'Boundary Competency 10000', 'plan', 104, 'active', 1);

INSERT INTO sessions VALUES
    ('boundary-long-session', 'fixture-owner', 104, 'Boundary Long Session',
     '2026-01-01T00:00:00.000000Z', '2026-01-02T00:00:00.000000Z');
INSERT INTO agent_runs VALUES
    ('boundary-run', 'fixture-owner', 'boundary-long-session', 104, 'user_message',
     'Generate deterministic boundary data', 'completed', '2026-01-01T00:00:00.000000Z');

WITH RECURSIVE message_seq(n) AS (
    VALUES(1)
    UNION ALL
    SELECT n + 1 FROM message_seq WHERE n < 10000
)
INSERT INTO chat_messages(id, session_id, run_id, role, content, message_metadata, created_at)
SELECT
    n,
    'boundary-long-session',
    'boundary-run',
    CASE WHEN n % 2 = 1 THEN 'user' ELSE 'assistant' END,
    printf('Synthetic boundary message %05d', n),
    json_object('fixture', 1, 'sequence', n),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-01-01', '+' || n || ' seconds')
FROM message_seq;

INSERT INTO evidence_observations(
    id, owner_id, source_type, source_id, plan_id, task_id, competency_id,
    competency_key, outcome, normalized_score, is_correct, assistance_level,
    transfer_level, rubric_snapshot, evaluator, artifact_refs, payload,
    occurred_at, recorded_at, schema_version, idempotency_key, invalidation_reason
) VALUES (
    1, 'fixture-owner', 'quiz', 'boundary-1-00001', 101, 101, 101,
    'boundary-1', 'passed', 1.0, 1, 'independent', 'same_task', '{}',
    '{"type":"fixture"}', '[]', '{"fixture":true}',
    '2026-01-01T09:00:00.123456Z', '2026-01-01T09:00:01.654321Z', 1,
    'boundary:1:00001', ''
);

WITH RECURSIVE evidence_500(n) AS (
    VALUES(1)
    UNION ALL
    SELECT n + 1 FROM evidence_500 WHERE n < 500
)
INSERT INTO evidence_observations(
    id, owner_id, source_type, source_id, plan_id, task_id, competency_id,
    competency_key, outcome, normalized_score, is_correct, assistance_level,
    transfer_level, rubric_snapshot, evaluator, artifact_refs, payload,
    occurred_at, recorded_at, schema_version, idempotency_key, invalidation_reason
)
SELECT
    100000 + n, 'fixture-owner', 'quiz', printf('boundary-500-%05d', n),
    102, 102, 102, 'boundary-500',
    CASE WHEN n % 7 = 0 THEN 'needs_revision' ELSE 'passed' END,
    CASE WHEN n % 7 = 0 THEN 0.4 ELSE 0.9 END,
    CASE WHEN n % 7 = 0 THEN 0 ELSE 1 END,
    'independent', 'same_task', '{}', '{"type":"fixture"}', '[]',
    json_object('fixture', 1, 'sequence', n),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-02-01', '+' || n || ' seconds'),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-02-01', '+' || (n + 1) || ' seconds'),
    1, printf('boundary:500:%05d', n), ''
FROM evidence_500;

WITH RECURSIVE evidence_501(n) AS (
    VALUES(1)
    UNION ALL
    SELECT n + 1 FROM evidence_501 WHERE n < 501
)
INSERT INTO evidence_observations(
    id, owner_id, source_type, source_id, plan_id, task_id, competency_id,
    competency_key, outcome, normalized_score, is_correct, assistance_level,
    transfer_level, rubric_snapshot, evaluator, artifact_refs, payload,
    occurred_at, recorded_at, schema_version, idempotency_key, invalidation_reason
)
SELECT
    200000 + n, 'fixture-owner', 'quiz', printf('boundary-501-%05d', n),
    103, 103, 103, 'boundary-501',
    CASE WHEN n % 11 = 0 THEN 'needs_revision' ELSE 'passed' END,
    CASE WHEN n % 11 = 0 THEN 0.3 ELSE 0.95 END,
    CASE WHEN n % 11 = 0 THEN 0 ELSE 1 END,
    'independent', 'variant', '{}', '{"type":"fixture"}', '[]',
    json_object('fixture', 1, 'sequence', n),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-03-01', '+' || n || ' seconds'),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-03-01', '+' || (n + 1) || ' seconds'),
    1, printf('boundary:501:%05d', n), ''
FROM evidence_501;

WITH RECURSIVE evidence_10000(n) AS (
    VALUES(1)
    UNION ALL
    SELECT n + 1 FROM evidence_10000 WHERE n < 10000
)
INSERT INTO evidence_observations(
    id, owner_id, source_type, source_id, plan_id, task_id, competency_id,
    competency_key, outcome, normalized_score, is_correct, assistance_level,
    transfer_level, rubric_snapshot, evaluator, artifact_refs, payload,
    occurred_at, recorded_at, schema_version, idempotency_key, invalidation_reason
)
SELECT
    300000 + n, 'fixture-owner', 'quiz', printf('boundary-10000-%05d', n),
    104, 104, 104, 'boundary-10000',
    CASE WHEN n % 13 = 0 THEN 'needs_revision' ELSE 'passed' END,
    CASE WHEN n % 13 = 0 THEN 0.2 ELSE 0.98 END,
    CASE WHEN n % 13 = 0 THEN 0 ELSE 1 END,
    CASE WHEN n % 5 = 0 THEN 'light_hint' ELSE 'independent' END,
    CASE WHEN n % 3 = 0 THEN 'transfer' ELSE 'variant' END,
    '{}', '{"type":"fixture"}', '[]', json_object('fixture', 1, 'sequence', n),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-04-01', '+' || n || ' seconds'),
    strftime('%Y-%m-%dT%H:%M:%fZ', '2026-04-01', '+' || (n + 1) || ' seconds'),
    1, printf('boundary:10000:%05d', n), ''
FROM evidence_10000;

COMMIT;
PRAGMA foreign_keys=ON;
