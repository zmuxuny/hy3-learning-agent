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
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_artifact_owner_idempotency UNIQUE (owner_id, idempotency_key),
	FOREIGN KEY(owner_id) REFERENCES owners (id),
	FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL
);
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
	assistance_level VARCHAR(24) NOT NULL,
	transfer_level VARCHAR(24) NOT NULL,
	rubric_snapshot JSON NOT NULL,
	evaluator JSON NOT NULL,
	artifact_refs JSON NOT NULL,
	payload JSON NOT NULL,
	occurred_at DATETIME NOT NULL,
	recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	schema_version INTEGER NOT NULL,
	correlation_id VARCHAR(120),
	causation_id VARCHAR(120),
	idempotency_key VARCHAR(180) NOT NULL,
	supersedes_id INTEGER,
	invalidated_at DATETIME,
	invalidation_reason TEXT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES owners (id),
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE SET NULL,
	FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
	FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE SET NULL,
	FOREIGN KEY(supersedes_id) REFERENCES evidence_observations (id) ON DELETE SET NULL
);
CREATE TABLE learning_events (
	id INTEGER NOT NULL,
	owner_id VARCHAR(64) NOT NULL,
	plan_id INTEGER,
	task_id INTEGER,
	run_id VARCHAR(64),
	event_type VARCHAR(64) NOT NULL,
	summary TEXT NOT NULL,
	payload JSON NOT NULL,
	schema_version INTEGER NOT NULL,
	occurred_at DATETIME NOT NULL,
	correlation_id VARCHAR(120),
	causation_id VARCHAR(120),
	idempotency_key VARCHAR(180),
	invalidated_at DATETIME,
	invalidation_reason TEXT NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES owners (id),
	FOREIGN KEY(plan_id) REFERENCES plans (id) ON DELETE SET NULL,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE SET NULL
);
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
CREATE TABLE owners (
	id VARCHAR(64) NOT NULL,
	display_name VARCHAR(120) NOT NULL,
	timezone VARCHAR(64) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
);
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
CREATE INDEX ix_achievements_owner_id ON achievements (owner_id);
CREATE INDEX ix_activity_days_date ON activity_days (date);
CREATE INDEX ix_activity_days_owner_id ON activity_days (owner_id);
CREATE INDEX ix_agent_runs_created_plan_id ON agent_runs (created_plan_id);
CREATE INDEX ix_agent_runs_owner_id ON agent_runs (owner_id);
CREATE INDEX ix_agent_runs_plan_id ON agent_runs (plan_id);
CREATE INDEX ix_agent_runs_status ON agent_runs (status);
CREATE INDEX ix_agent_runs_trigger ON agent_runs ("trigger");
CREATE INDEX ix_artifacts_artifact_type ON artifacts (artifact_type);
CREATE INDEX ix_artifacts_created_at ON artifacts (created_at);
CREATE INDEX ix_artifacts_idempotency_key ON artifacts (idempotency_key);
CREATE INDEX ix_artifacts_owner_id ON artifacts (owner_id);
CREATE INDEX ix_artifacts_plan_id ON artifacts (plan_id);
CREATE INDEX ix_artifacts_run_id ON artifacts (run_id);
CREATE INDEX ix_artifacts_session_id ON artifacts (session_id);
CREATE INDEX ix_artifacts_task_id ON artifacts (task_id);
CREATE INDEX ix_calendar_events_owner_id ON calendar_events (owner_id);
CREATE INDEX ix_calendar_events_plan_id ON calendar_events (plan_id);
CREATE INDEX ix_calendar_events_starts_at ON calendar_events (starts_at);
CREATE INDEX ix_calendar_events_status ON calendar_events (status);
CREATE INDEX ix_calendar_events_task_id ON calendar_events (task_id);
CREATE INDEX ix_chat_message_revisions_message_id ON chat_message_revisions (message_id);
CREATE INDEX ix_chat_message_revisions_previous_run_id ON chat_message_revisions (previous_run_id);
CREATE INDEX ix_chat_message_revisions_session_id ON chat_message_revisions (session_id);
CREATE INDEX ix_chat_messages_run_id ON chat_messages (run_id);
CREATE INDEX ix_chat_messages_session_id ON chat_messages (session_id);
CREATE INDEX ix_competencies_key ON competencies ("key");
CREATE INDEX ix_competencies_owner_id ON competencies (owner_id);
CREATE INDEX ix_competencies_plan_id ON competencies (plan_id);
CREATE INDEX ix_competencies_scope ON competencies (scope);
CREATE INDEX ix_competencies_status ON competencies (status);
CREATE INDEX ix_competency_edges_owner_id ON competency_edges (owner_id);
CREATE INDEX ix_competency_edges_relation ON competency_edges (relation);
CREATE INDEX ix_competency_edges_source_id ON competency_edges (source_id);
CREATE INDEX ix_competency_edges_target_id ON competency_edges (target_id);
CREATE INDEX ix_context_snapshots_owner_id ON context_snapshots (owner_id);
CREATE INDEX ix_context_snapshots_plan_id ON context_snapshots (plan_id);
CREATE INDEX ix_context_snapshots_run_id ON context_snapshots (run_id);
CREATE INDEX ix_evidence_observations_causation_id ON evidence_observations (causation_id);
CREATE INDEX ix_evidence_observations_competency_id ON evidence_observations (competency_id);
CREATE INDEX ix_evidence_observations_competency_key ON evidence_observations (competency_key);
CREATE INDEX ix_evidence_observations_correlation_id ON evidence_observations (correlation_id);
CREATE UNIQUE INDEX ix_evidence_observations_idempotency_key ON evidence_observations (idempotency_key);
CREATE INDEX ix_evidence_observations_occurred_at ON evidence_observations (occurred_at);
CREATE INDEX ix_evidence_observations_outcome ON evidence_observations (outcome);
CREATE INDEX ix_evidence_observations_owner_id ON evidence_observations (owner_id);
CREATE INDEX ix_evidence_observations_plan_id ON evidence_observations (plan_id);
CREATE INDEX ix_evidence_observations_recorded_at ON evidence_observations (recorded_at);
CREATE INDEX ix_evidence_observations_run_id ON evidence_observations (run_id);
CREATE INDEX ix_evidence_observations_session_id ON evidence_observations (session_id);
CREATE INDEX ix_evidence_observations_source_id ON evidence_observations (source_id);
CREATE INDEX ix_evidence_observations_source_type ON evidence_observations (source_type);
CREATE INDEX ix_evidence_observations_supersedes_id ON evidence_observations (supersedes_id);
CREATE INDEX ix_evidence_observations_task_id ON evidence_observations (task_id);
CREATE INDEX ix_learning_events_causation_id ON learning_events (causation_id);
CREATE INDEX ix_learning_events_correlation_id ON learning_events (correlation_id);
CREATE INDEX ix_learning_events_created_at ON learning_events (created_at);
CREATE INDEX ix_learning_events_event_type ON learning_events (event_type);
CREATE INDEX ix_learning_events_idempotency_key ON learning_events (idempotency_key);
CREATE INDEX ix_learning_events_occurred_at ON learning_events (occurred_at);
CREATE INDEX ix_learning_events_owner_id ON learning_events (owner_id);
CREATE INDEX ix_learning_events_plan_id ON learning_events (plan_id);
CREATE INDEX ix_learning_events_task_id ON learning_events (task_id);
CREATE INDEX ix_learning_resources_owner_id ON learning_resources (owner_id);
CREATE INDEX ix_learning_resources_plan_id ON learning_resources (plan_id);
CREATE INDEX ix_memories_layer ON memories (layer);
CREATE INDEX ix_memories_owner_id ON memories (owner_id);
CREATE INDEX ix_memories_scope ON memories (scope);
CREATE INDEX ix_memories_scope_id ON memories (scope_id);
CREATE INDEX ix_memories_status ON memories (status);
CREATE INDEX ix_memories_superseded_by_id ON memories (superseded_by_id);
CREATE INDEX ix_memories_supersedes_id ON memories (supersedes_id);
CREATE INDEX ix_notifications_archived_at ON notifications (archived_at);
CREATE INDEX ix_notifications_owner_id ON notifications (owner_id);
CREATE INDEX ix_notifications_plan_id ON notifications (plan_id);
CREATE INDEX ix_notifications_session_id ON notifications (session_id);
CREATE INDEX ix_notifications_status ON notifications (status);
CREATE INDEX ix_operations_owner_id ON operations (owner_id);
CREATE INDEX ix_operations_run_id ON operations (run_id);
CREATE INDEX ix_operations_status ON operations (status);
CREATE INDEX ix_plan_competency_links_competency_id ON plan_competency_links (competency_id);
CREATE INDEX ix_plan_competency_links_owner_id ON plan_competency_links (owner_id);
CREATE INDEX ix_plan_competency_links_plan_id ON plan_competency_links (plan_id);
CREATE INDEX ix_plan_proposals_owner_id ON plan_proposals (owner_id);
CREATE INDEX ix_plan_proposals_plan_id ON plan_proposals (plan_id);
CREATE INDEX ix_plan_proposals_session_id ON plan_proposals (session_id);
CREATE INDEX ix_plan_proposals_source_run_id ON plan_proposals (source_run_id);
CREATE INDEX ix_plan_proposals_status ON plan_proposals (status);
CREATE INDEX ix_planning_intakes_owner_id ON planning_intakes (owner_id);
CREATE INDEX ix_planning_intakes_readiness ON planning_intakes (readiness);
CREATE INDEX ix_planning_intakes_source_run_id ON planning_intakes (source_run_id);
CREATE INDEX ix_plans_owner_id ON plans (owner_id);
CREATE INDEX ix_plans_status ON plans (status);
CREATE INDEX ix_push_subscriptions_owner_id ON push_subscriptions (owner_id);
CREATE INDEX ix_queued_messages_owner_id ON queued_messages (owner_id);
CREATE INDEX ix_queued_messages_session_id ON queued_messages (session_id);
CREATE INDEX ix_quizzes_owner_id ON quizzes (owner_id);
CREATE INDEX ix_quizzes_plan_id ON quizzes (plan_id);
CREATE INDEX ix_quizzes_status ON quizzes (status);
CREATE INDEX ix_resource_competency_links_competency_id ON resource_competency_links (competency_id);
CREATE INDEX ix_resource_competency_links_owner_id ON resource_competency_links (owner_id);
CREATE INDEX ix_resource_competency_links_resource_id ON resource_competency_links (resource_id);
CREATE INDEX ix_review_schedules_due_at ON review_schedules (due_at);
CREATE INDEX ix_review_schedules_owner_id ON review_schedules (owner_id);
CREATE INDEX ix_review_schedules_plan_id ON review_schedules (plan_id);
CREATE INDEX ix_review_schedules_status ON review_schedules (status);
CREATE INDEX ix_review_schedules_task_id ON review_schedules (task_id);
CREATE INDEX ix_run_events_event_type ON run_events (event_type);
CREATE INDEX ix_run_events_run_id ON run_events (run_id);
CREATE INDEX ix_run_steer_messages_owner_id ON run_steer_messages (owner_id);
CREATE INDEX ix_run_steer_messages_run_id ON run_steer_messages (run_id);
CREATE INDEX ix_session_plan_links_owner_id ON session_plan_links (owner_id);
CREATE INDEX ix_session_plan_links_plan_id ON session_plan_links (plan_id);
CREATE INDEX ix_session_plan_links_relation_type ON session_plan_links (relation_type);
CREATE INDEX ix_session_plan_links_session_id ON session_plan_links (session_id);
CREATE INDEX ix_session_plan_links_source_run_id ON session_plan_links (source_run_id);
CREATE INDEX ix_session_summaries_owner_id ON session_summaries (owner_id);
CREATE INDEX ix_session_summaries_session_id ON session_summaries (session_id);
CREATE INDEX ix_sessions_archived_at ON sessions (archived_at);
CREATE INDEX ix_sessions_owner_id ON sessions (owner_id);
CREATE INDEX ix_sessions_parent_session_id ON sessions (parent_session_id);
CREATE INDEX ix_sessions_plan_id ON sessions (plan_id);
CREATE INDEX ix_stages_plan_id ON stages (plan_id);
CREATE INDEX ix_task_competency_links_competency_id ON task_competency_links (competency_id);
CREATE INDEX ix_task_competency_links_owner_id ON task_competency_links (owner_id);
CREATE INDEX ix_task_competency_links_task_id ON task_competency_links (task_id);
CREATE INDEX ix_task_submissions_owner_id ON task_submissions (owner_id);
CREATE INDEX ix_task_submissions_plan_id ON task_submissions (plan_id);
CREATE INDEX ix_task_submissions_status ON task_submissions (status);
CREATE INDEX ix_task_submissions_task_id ON task_submissions (task_id);
CREATE INDEX ix_tasks_review_due_at ON tasks (review_due_at);
CREATE INDEX ix_tasks_stage_id ON tasks (stage_id);
CREATE INDEX ix_tasks_status ON tasks (status);
CREATE UNIQUE INDEX ix_tool_invocations_idempotency_key ON tool_invocations (idempotency_key);
CREATE INDEX ix_tool_invocations_owner_id ON tool_invocations (owner_id);
CREATE INDEX ix_tool_invocations_run_id ON tool_invocations (run_id);
CREATE INDEX ix_tool_invocations_status ON tool_invocations (status);
