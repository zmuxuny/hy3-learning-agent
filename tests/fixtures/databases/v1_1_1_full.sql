-- Synthetic v1.1.1 database generated from release commit fe3db33.
-- Every application table contains a deterministic, non-personal row.
PRAGMA page_size=4096;
PRAGMA encoding='UTF-8';
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;

CREATE TABLE owners (
    id VARCHAR(64) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE user_profiles (
    owner_id VARCHAR(64) NOT NULL,
    coach_style VARCHAR(64) NOT NULL,
    preferences JSON NOT NULL,
    quiet_hours JSON NOT NULL,
    daily_notification_limit INTEGER NOT NULL,
    xp INTEGER NOT NULL,
    level INTEGER NOT NULL,
    streak_days INTEGER NOT NULL,
    follow_up_behavior VARCHAR(16) NOT NULL,
    proactive_paused BOOLEAN NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (owner_id),
    FOREIGN KEY(owner_id) REFERENCES owners (id)
);

CREATE TABLE plans (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    title VARCHAR(240) NOT NULL,
    description TEXT NOT NULL,
    goal TEXT NOT NULL,
    current_level TEXT NOT NULL,
    deadline DATETIME,
    weekly_minutes INTEGER NOT NULL,
    preferences JSON NOT NULL,
    expected_outcome TEXT NOT NULL,
    available_resources JSON NOT NULL,
    avoid_methods JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    archived_from_status VARCHAR(32),
    version INTEGER NOT NULL,
    progress FLOAT NOT NULL,
    memory_summary TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id)
);
CREATE INDEX ix_plans_owner_id ON plans (owner_id);
CREATE INDEX ix_plans_status ON plans (status);

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
CREATE INDEX ix_memories_supersedes_id ON memories (supersedes_id);
CREATE INDEX ix_memories_scope_id ON memories (scope_id);
CREATE INDEX ix_memories_scope ON memories (scope);
CREATE INDEX ix_memories_status ON memories (status);
CREATE INDEX ix_memories_layer ON memories (layer);
CREATE INDEX ix_memories_owner_id ON memories (owner_id);
CREATE INDEX ix_memories_superseded_by_id ON memories (superseded_by_id);

CREATE TABLE push_subscriptions (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    endpoint TEXT NOT NULL,
    keys JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    UNIQUE (endpoint)
);
CREATE INDEX ix_push_subscriptions_owner_id ON push_subscriptions (owner_id);

CREATE TABLE achievements (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    "key" VARCHAR(120) NOT NULL,
    title VARCHAR(240) NOT NULL,
    description TEXT NOT NULL,
    badge_kind VARCHAR(32) NOT NULL,
    badge_image_url TEXT NOT NULL,
    unlocked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_owner_achievement UNIQUE (owner_id, "key"),
    FOREIGN KEY(owner_id) REFERENCES owners (id)
);
CREATE INDEX ix_achievements_owner_id ON achievements (owner_id);

CREATE TABLE activity_days (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    date VARCHAR(10) NOT NULL,
    xp INTEGER NOT NULL,
    completed_tasks INTEGER NOT NULL,
    passed_quizzes INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_owner_activity_date UNIQUE (owner_id, date),
    FOREIGN KEY(owner_id) REFERENCES owners (id)
);
CREATE INDEX ix_activity_days_owner_id ON activity_days (owner_id);
CREATE INDEX ix_activity_days_date ON activity_days (date);

CREATE TABLE stages (
    id INTEGER NOT NULL,
    plan_id INTEGER NOT NULL,
    title VARCHAR(240) NOT NULL,
    description TEXT NOT NULL,
    objectives JSON NOT NULL,
    position INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE
);
CREATE INDEX ix_stages_plan_id ON stages (plan_id);

CREATE TABLE learning_resources (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER,
    title VARCHAR(300) NOT NULL,
    url TEXT NOT NULL,
    resource_type VARCHAR(32) NOT NULL,
    provider VARCHAR(120) NOT NULL,
    language VARCHAR(32) NOT NULL,
    difficulty VARCHAR(32) NOT NULL,
    summary TEXT NOT NULL,
    why_recommended TEXT NOT NULL,
    source VARCHAR(120) NOT NULL,
    verified_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE
);
CREATE INDEX ix_learning_resources_owner_id ON learning_resources (owner_id);
CREATE INDEX ix_learning_resources_plan_id ON learning_resources (plan_id);

CREATE TABLE sessions (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER,
    parent_session_id VARCHAR(64),
    title VARCHAR(240) NOT NULL,
    summary TEXT NOT NULL,
    handoff_summary TEXT NOT NULL,
    archived_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(parent_session_id) REFERENCES sessions (id) ON DELETE SET NULL
);
CREATE INDEX ix_sessions_archived_at ON sessions (archived_at);
CREATE INDEX ix_sessions_plan_id ON sessions (plan_id);
CREATE INDEX ix_sessions_parent_session_id ON sessions (parent_session_id);
CREATE INDEX ix_sessions_owner_id ON sessions (owner_id);

CREATE TABLE tasks (
    id INTEGER NOT NULL,
    stage_id INTEGER NOT NULL,
    title VARCHAR(300) NOT NULL,
    description TEXT NOT NULL,
    kind VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    is_core BOOLEAN NOT NULL,
    evidence_required BOOLEAN NOT NULL,
    estimated_minutes INTEGER NOT NULL,
    position INTEGER NOT NULL,
    due_at DATETIME,
    completed_at DATETIME,
    review_due_at DATETIME,
    resource_url TEXT NOT NULL,
    task_metadata JSON NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(stage_id) REFERENCES stages (id) ON DELETE CASCADE
);
CREATE INDEX ix_tasks_status ON tasks (status);
CREATE INDEX ix_tasks_stage_id ON tasks (stage_id);
CREATE INDEX ix_tasks_review_due_at ON tasks (review_due_at);

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
CREATE INDEX ix_session_summaries_session_id ON session_summaries (session_id);
CREATE INDEX ix_session_summaries_owner_id ON session_summaries (owner_id);

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
CREATE INDEX ix_agent_runs_plan_id ON agent_runs (plan_id);
CREATE INDEX ix_agent_runs_trigger ON agent_runs ("trigger");
CREATE INDEX ix_agent_runs_created_plan_id ON agent_runs (created_plan_id);
CREATE INDEX ix_agent_runs_owner_id ON agent_runs (owner_id);
CREATE INDEX ix_agent_runs_status ON agent_runs (status);

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
CREATE INDEX ix_queued_messages_session_id ON queued_messages (session_id);
CREATE INDEX ix_queued_messages_owner_id ON queued_messages (owner_id);

CREATE TABLE task_submissions (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    run_id VARCHAR(64),
    submission_type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    artifacts JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    score FLOAT,
    feedback TEXT NOT NULL,
    checked_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_task_submissions_status ON task_submissions (status);
CREATE INDEX ix_task_submissions_plan_id ON task_submissions (plan_id);
CREATE INDEX ix_task_submissions_owner_id ON task_submissions (owner_id);
CREATE INDEX ix_task_submissions_task_id ON task_submissions (task_id);

CREATE TABLE calendar_events (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER,
    task_id INTEGER,
    title VARCHAR(240) NOT NULL,
    description TEXT NOT NULL,
    starts_at DATETIME NOT NULL,
    ends_at DATETIME,
    status VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL
);
CREATE INDEX ix_calendar_events_status ON calendar_events (status);
CREATE INDEX ix_calendar_events_plan_id ON calendar_events (plan_id);
CREATE INDEX ix_calendar_events_starts_at ON calendar_events (starts_at);
CREATE INDEX ix_calendar_events_owner_id ON calendar_events (owner_id);
CREATE INDEX ix_calendar_events_task_id ON calendar_events (task_id);

CREATE TABLE session_plan_links (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    plan_id INTEGER NOT NULL,
    relation_type VARCHAR(32) NOT NULL,
    source_run_id VARCHAR(64),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_session_plan_relation UNIQUE (session_id, plan_id, relation_type),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE,
    FOREIGN KEY(source_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_session_plan_links_owner_id ON session_plan_links (owner_id);
CREATE INDEX ix_session_plan_links_source_run_id ON session_plan_links (source_run_id);
CREATE INDEX ix_session_plan_links_session_id ON session_plan_links (session_id);
CREATE INDEX ix_session_plan_links_relation_type ON session_plan_links (relation_type);
CREATE INDEX ix_session_plan_links_plan_id ON session_plan_links (plan_id);

CREATE TABLE planning_intakes (
    session_id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    source_run_id VARCHAR(64),
    goal TEXT NOT NULL,
    confirmed_facts JSON NOT NULL,
    open_questions JSON NOT NULL,
    readiness VARCHAR(32) NOT NULL,
    readiness_confidence FLOAT NOT NULL,
    rationale TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (session_id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(source_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_planning_intakes_owner_id ON planning_intakes (owner_id);
CREATE INDEX ix_planning_intakes_source_run_id ON planning_intakes (source_run_id);
CREATE INDEX ix_planning_intakes_readiness ON planning_intakes (readiness);

CREATE TABLE plan_proposals (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    source_run_id VARCHAR(64),
    title VARCHAR(240) NOT NULL,
    rationale TEXT NOT NULL,
    plan_payload JSON NOT NULL,
    specialist_reports JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    plan_id INTEGER,
    decided_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY(source_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL
);
CREATE INDEX ix_plan_proposals_owner_id ON plan_proposals (owner_id);
CREATE INDEX ix_plan_proposals_plan_id ON plan_proposals (plan_id);
CREATE INDEX ix_plan_proposals_session_id ON plan_proposals (session_id);
CREATE INDEX ix_plan_proposals_status ON plan_proposals (status);
CREATE INDEX ix_plan_proposals_source_run_id ON plan_proposals (source_run_id);

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
CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);
CREATE INDEX ix_chat_messages_run_id ON chat_messages (run_id);

CREATE TABLE tool_invocations (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    tool_name VARCHAR(120) NOT NULL,
    args_hash VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    result_payload JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_tool_invocations_status ON tool_invocations (status);
CREATE INDEX ix_tool_invocations_owner_id ON tool_invocations (owner_id);
CREATE UNIQUE INDEX ix_tool_invocations_idempotency_key ON tool_invocations (idempotency_key);
CREATE INDEX ix_tool_invocations_run_id ON tool_invocations (run_id);

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
CREATE INDEX ix_run_events_event_type ON run_events (event_type);
CREATE INDEX ix_run_events_run_id ON run_events (run_id);

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
CREATE INDEX ix_run_steer_messages_run_id ON run_steer_messages (run_id);
CREATE INDEX ix_run_steer_messages_owner_id ON run_steer_messages (owner_id);

CREATE TABLE learning_events (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER,
    task_id INTEGER,
    run_id VARCHAR(64),
    event_type VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_learning_events_owner_id ON learning_events (owner_id);
CREATE INDEX ix_learning_events_plan_id ON learning_events (plan_id);
CREATE INDEX ix_learning_events_created_at ON learning_events (created_at);
CREATE INDEX ix_learning_events_task_id ON learning_events (task_id);
CREATE INDEX ix_learning_events_event_type ON learning_events (event_type);

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
CREATE INDEX ix_context_snapshots_run_id ON context_snapshots (run_id);
CREATE INDEX ix_context_snapshots_plan_id ON context_snapshots (plan_id);

CREATE TABLE operations (
    id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64),
    tool_name VARCHAR(120) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    forward_patch JSON NOT NULL,
    inverse_patch JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    undone_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_operations_run_id ON operations (run_id);
CREATE INDEX ix_operations_owner_id ON operations (owner_id);
CREATE INDEX ix_operations_status ON operations (status);

CREATE TABLE notifications (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64),
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
    FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
    UNIQUE (reply_token)
);
CREATE INDEX ix_notifications_plan_id ON notifications (plan_id);
CREATE INDEX ix_notifications_session_id ON notifications (session_id);
CREATE INDEX ix_notifications_owner_id ON notifications (owner_id);
CREATE INDEX ix_notifications_archived_at ON notifications (archived_at);
CREATE INDEX ix_notifications_status ON notifications (status);

CREATE TABLE review_schedules (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER NOT NULL,
    task_id INTEGER,
    due_at DATETIME NOT NULL,
    review_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);
CREATE INDEX ix_review_schedules_plan_id ON review_schedules (plan_id);
CREATE INDEX ix_review_schedules_owner_id ON review_schedules (owner_id);
CREATE INDEX ix_review_schedules_status ON review_schedules (status);
CREATE INDEX ix_review_schedules_due_at ON review_schedules (due_at);
CREATE INDEX ix_review_schedules_task_id ON review_schedules (task_id);

CREATE TABLE quizzes (
    id INTEGER NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    plan_id INTEGER NOT NULL,
    task_id INTEGER,
    run_id VARCHAR(64),
    prompt TEXT NOT NULL,
    rubric JSON NOT NULL,
    answer TEXT NOT NULL,
    score FLOAT,
    feedback TEXT NOT NULL,
    evidence JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    graded_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES owners (id),
    FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE CASCADE,
    FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
    FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_quizzes_status ON quizzes (status);
CREATE INDEX ix_quizzes_owner_id ON quizzes (owner_id);
CREATE INDEX ix_quizzes_plan_id ON quizzes (plan_id);

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

INSERT INTO owners VALUES
    ('fixture-owner', 'Fixture Learner', 'Asia/Shanghai', '2026-01-01 00:00:00.000000');
INSERT INTO user_profiles VALUES
    ('fixture-owner', 'adaptive_study_partner', '{"language":"fixture"}',
     '{"start":"23:00","end":"08:00"}', 3, 120, 2, 4, 'queue', 0,
     '2026-01-01 00:01:00.000000');
INSERT INTO plans VALUES
    (1, 'fixture-owner', 'Fixture Plan', 'Synthetic release fixture', 'Verify upgrade behavior',
     'beginner', '2026-12-31 00:00:00.000000', 180, '{}', 'A deterministic artifact',
     '[]', '[]', 'active', NULL, 3, 0.5, 'Fixture summary',
     '2026-01-01 00:02:00.000000', '2026-01-02 00:02:00.000000');
INSERT INTO stages VALUES
    (1, 1, 'Fixture Stage', 'Synthetic stage', '["verify"]', 0, 'active');
INSERT INTO tasks VALUES
    (1, 1, 'Fixture Task', 'Synthetic task', 'practice', 'completed', 1, 1, 30, 0,
     '2026-01-03 09:00:00.000000', '2026-01-03 09:30:00.000000',
     '2026-01-10 09:00:00.000000', 'https://example.invalid/task', '{"fixture":true}');
INSERT INTO learning_resources VALUES
    (1, 'fixture-owner', 1, 'Fixture Resource', 'https://example.invalid/resource', 'tutorial',
     'Fixture Provider', 'en', 'beginner', 'Synthetic resource', 'Deterministic coverage',
     'fixture', '2026-01-01 00:03:00.000000', '2026-01-01 00:03:00.000000');
INSERT INTO sessions VALUES
    ('fixture-session', 'fixture-owner', 1, NULL, 'Fixture Session', 'Synthetic summary',
     'Frozen fixture handoff', NULL, '2026-01-01 00:04:00.000000',
     '2026-01-02 00:04:00.000000');
INSERT INTO agent_runs VALUES
    ('fixture-run', 'fixture-owner', 'fixture-session', 1, NULL, 'user_message',
     'Verify fixture upgrade', 'completed', 'fixture-model', 0, NULL, NULL,
     '{"model_calls":1}', 'Fixture output', NULL, '2026-01-01 00:05:00.000000',
     '2026-01-01 00:06:00.000000', '2026-01-01 00:05:00.000000');
INSERT INTO memories VALUES
    (1, 'fixture-owner', 'plan', '1', 'semantic', 'Synthetic confirmed memory', 'fixture',
     'fixture-message', 0.9, 'confirmed', NULL, '', NULL, NULL,
     '2026-01-02 00:00:00.000000', 1, '2026-01-02 00:00:00.000000', NULL,
     '[0.0,1.0]', 'fixture-vector', '2026-01-01 00:07:00.000000',
     '2026-01-02 00:07:00.000000');
INSERT INTO push_subscriptions VALUES
    (1, 'fixture-owner', 'https://example.invalid/push/fixture',
     '{"p256dh":"fixture-public","auth":"fixture-value"}',
     '2026-01-01 00:08:00.000000', '2026-01-01 00:08:00.000000');
INSERT INTO achievements VALUES
    (1, 'fixture-owner', 'fixture-achievement', 'Fixture Achievement', 'Synthetic badge',
     'rule', '', '2026-01-01 00:09:00.000000');
INSERT INTO activity_days VALUES
    (1, 'fixture-owner', '2026-01-03', 20, 1, 1);
INSERT INTO session_summaries VALUES
    (1, 'fixture-owner', 'fixture-session', 1, 'Synthetic session summary', 1, '[1]',
     'fallback', '2026-01-01 00:10:00.000000');
INSERT INTO queued_messages VALUES
    ('fixture-queue', 'fixture-owner', 'fixture-session', 1, 'user_message',
     'Synthetic queued objective', 'Synthetic queued content', '{"fixture":true}', 0,
     '2026-01-01 00:11:00.000000', '2026-01-01 00:11:00.000000');
INSERT INTO task_submissions VALUES
    (1, 'fixture-owner', 1, 1, 'fixture-run', 'text', 'Synthetic submission', '[]',
     'accepted', 90.0, 'Synthetic feedback', '2026-01-03 09:29:00.000000',
     '2026-01-03 09:20:00.000000');
INSERT INTO calendar_events VALUES
    (1, 'fixture-owner', 1, 1, 'Fixture Calendar Event', 'Synthetic event',
     '2026-01-03 09:00:00.000000', '2026-01-03 09:30:00.000000', 'completed',
     'fixture', '2026-01-01 00:12:00.000000', '2026-01-03 09:30:00.000000');
INSERT INTO session_plan_links VALUES
    (1, 'fixture-owner', 'fixture-session', 1, 'focused', 'fixture-run',
     '2026-01-01 00:13:00.000000');
INSERT INTO planning_intakes VALUES
    ('fixture-session', 'fixture-owner', 'fixture-run', 'Verify migration',
     '[{"fact":"synthetic"}]', '[]', 'ready', 1.0, 'Fixture is complete',
     '2026-01-01 00:14:00.000000', '2026-01-01 00:14:00.000000');
INSERT INTO plan_proposals VALUES
    ('fixture-proposal', 'fixture-owner', 'fixture-session', 'fixture-run', 'Fixture Proposal',
     'Synthetic proposal', '{"title":"Fixture Plan"}', '[]', 'accepted', 1,
     '2026-01-01 00:15:00.000000', '2026-01-01 00:14:30.000000',
     '2026-01-01 00:15:00.000000');
INSERT INTO chat_messages VALUES
    (1, 'fixture-session', 'fixture-run', 'user', 'Synthetic message', '{"fixture":true}',
     '2026-01-01 00:16:00.000000');
INSERT INTO tool_invocations VALUES
    (1, 'fixture-owner', 'fixture-run', 'fixture-tool-call', 'plan_get',
     '00000000000000000000000000000000', 'committed', '{"ok":true}',
     '2026-01-01 00:17:00.000000', '2026-01-01 00:17:01.000000');
INSERT INTO run_events VALUES
    (1, 'fixture-run', 1, 'run.completed', 'Synthetic completion', '{"fixture":true}',
     '2026-01-01 00:18:00.000000');
INSERT INTO run_steer_messages VALUES
    ('fixture-steer', 'fixture-owner', 'fixture-run', 'Synthetic steer',
     '2026-01-01 00:18:30.000000', '2026-01-01 00:18:20.000000');
INSERT INTO learning_events VALUES
    (1, 'fixture-owner', 1, 1, 'fixture-run', 'task.updated', 'Synthetic task update',
     '{"after":{"status":"completed"},"evidence":[{"kind":"fixture"}]}',
     '2026-01-03 09:30:00.123456');
INSERT INTO context_snapshots VALUES
    (1, 'fixture-owner', 1, 'fixture-run', '# Synthetic context',
     '[{"type":"fixture","id":"one"}]', 8, '2026-01-01 00:19:00.000000');
INSERT INTO operations VALUES
    ('fixture-operation', 'fixture-owner', 'fixture-run', 'task_patch', 'task', '1',
     '{"status":"completed"}', '{"status":"pending"}', 'committed',
     '2026-01-03 09:30:00.000000', NULL);
INSERT INTO notifications VALUES
    (1, 'fixture-owner', 'fixture-run', 'fixture-session', 1, 'in_app', 'Fixture Reminder',
     'Synthetic reminder body', 'sent', 'fixture-reply-token',
     '2026-01-04 09:00:00.000000', NULL, NULL, '2026-01-04 09:00:00.000000');
INSERT INTO review_schedules VALUES
    (1, 'fixture-owner', 1, 1, '2026-01-10 09:00:00.000000', 'spaced', 'pending',
     '2026-01-03 09:31:00.000000');
INSERT INTO quizzes VALUES
    (1, 'fixture-owner', 1, 1, 'fixture-run', 'Explain the fixture invariant',
     '{"criteria":["deterministic"]}', 'Synthetic answer', 95.0, 'Synthetic feedback',
     '[{"kind":"fixture"}]', 'passed', '2026-01-03 09:10:00.000000',
     '2026-01-03 09:25:00.654321');
INSERT INTO chat_message_revisions VALUES
    (1, 1, 'fixture-session', 'fixture-run', 'Previous synthetic message',
     '{"fixture":true}', '2026-01-01 00:20:00.000000');

COMMIT;
PRAGMA foreign_keys=ON;
