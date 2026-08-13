from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


SQLITE_COLUMNS: dict[str, dict[str, str]] = {
    "plans": {
        "archived_from_status": "VARCHAR(32)",
    },
    "sessions": {
        "parent_session_id": "VARCHAR(64)",
        "handoff_summary": "TEXT NOT NULL DEFAULT ''",
        "archived_at": "DATETIME",
    },
    "notifications": {
        "session_id": "VARCHAR(64)",
        "archived_at": "DATETIME",
    },
    "learning_resources": {
        "provider": "VARCHAR(120) NOT NULL DEFAULT ''",
        "language": "VARCHAR(32) NOT NULL DEFAULT ''",
        "difficulty": "VARCHAR(32) NOT NULL DEFAULT 'mixed'",
        "why_recommended": "TEXT NOT NULL DEFAULT ''",
        "verified_at": "DATETIME",
    },
    "memories": {
        "embedding": "JSON",
        "embedding_provider": "VARCHAR(64)",
        "archived_from_status": "VARCHAR(32)",
        "archived_reason": "TEXT NOT NULL DEFAULT ''",
        "supersedes_id": "INTEGER",
        "superseded_by_id": "INTEGER",
        "last_accessed_at": "DATETIME",
        "access_count": "INTEGER NOT NULL DEFAULT 0",
        "last_reinforced_at": "DATETIME",
    },
    "agent_runs": {
        "checkpoint": "JSON",
        "pending_approval": "JSON",
        "budget_usage": "JSON",
        "output": "TEXT NOT NULL DEFAULT ''",
        "created_plan_id": "INTEGER",
    },
    "user_profiles": {
        "follow_up_behavior": "VARCHAR(16) NOT NULL DEFAULT 'steer'",
        "proactive_paused": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "queued_messages": {
        "trigger": "VARCHAR(40) NOT NULL DEFAULT 'user_message'",
        "user_content": "TEXT",
        "message_metadata": "JSON NOT NULL DEFAULT '{}'",
    },
    "learning_events": {
        "schema_version": "INTEGER NOT NULL DEFAULT 1",
        "occurred_at": "DATETIME",
        "correlation_id": "VARCHAR(120)",
        "causation_id": "VARCHAR(120)",
        "idempotency_key": "VARCHAR(180)",
        "invalidated_at": "DATETIME",
        "invalidation_reason": "TEXT NOT NULL DEFAULT ''",
    },
}


async def migrate_sqlite_schema(connection: AsyncConnection) -> None:
    """Apply additive migrations for personal SQLite installs created before Alembic."""
    if connection.dialect.name != "sqlite":
        return
    for table, columns in SQLITE_COLUMNS.items():
        existing = {
            row[1]
            for row in (await connection.execute(text(f'PRAGMA table_info("{table}")'))).all()
        }
        for column, definition in columns.items():
            if column not in existing:
                await connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_sessions_archived_at ON sessions (archived_at)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_sessions_parent_session_id ON sessions (parent_session_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_notifications_session_id ON notifications (session_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_notifications_archived_at ON notifications (archived_at)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_memories_supersedes_id ON memories (supersedes_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_memories_superseded_by_id ON memories (superseded_by_id)"
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS session_summaries (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(64) NOT NULL,
            version INTEGER NOT NULL,
            content TEXT NOT NULL,
            covered_through_message_id INTEGER,
            source_message_ids JSON NOT NULL DEFAULT '[]',
            method VARCHAR(32) NOT NULL DEFAULT 'model',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_session_summary_version UNIQUE (session_id, version)
        )
        """
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_session_summaries_owner_id ON session_summaries (owner_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_session_summaries_session_id ON session_summaries (session_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_context_snapshots_run_id ON context_snapshots (run_id)"
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS queued_messages (
            id VARCHAR(64) PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            session_id VARCHAR(64),
            plan_id INTEGER,
            trigger VARCHAR(40) NOT NULL DEFAULT 'user_message',
            objective TEXT NOT NULL,
            user_content TEXT,
            message_metadata JSON NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS run_steer_messages (
            id VARCHAR(64) PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            run_id VARCHAR(64) NOT NULL,
            content TEXT NOT NULL,
            applied_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_queued_messages_session_id ON queued_messages (session_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_run_steer_messages_run_id ON run_steer_messages (run_id)"
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS evidence_observations (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            source_type VARCHAR(32) NOT NULL,
            source_id VARCHAR(120) NOT NULL,
            run_id VARCHAR(64),
            session_id VARCHAR(64),
            plan_id INTEGER,
            task_id INTEGER,
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
            recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
            FOREIGN KEY(supersedes_id) REFERENCES evidence_observations(id) ON DELETE SET NULL
        )
        """
    ))
    evidence_columns = {
        row[1]
        for row in (await connection.execute(text('PRAGMA table_info("evidence_observations")'))).all()
    }
    evidence_additive_columns = {
        "artifact_refs": "JSON NOT NULL DEFAULT '[]'",
        "competency_key": "VARCHAR(160)",
        "normalized_score": "FLOAT",
        "is_correct": "BOOLEAN",
        "assistance_level": "VARCHAR(24) NOT NULL DEFAULT 'unknown'",
        "transfer_level": "VARCHAR(24) NOT NULL DEFAULT 'unknown'",
        "rubric_snapshot": "JSON NOT NULL DEFAULT '{}'",
        "evaluator": "JSON NOT NULL DEFAULT '{}'",
        "payload": "JSON NOT NULL DEFAULT '{}'",
        "occurred_at": "DATETIME",
        "recorded_at": "DATETIME",
        "schema_version": "INTEGER NOT NULL DEFAULT 1",
        "correlation_id": "VARCHAR(120)",
        "causation_id": "VARCHAR(120)",
        "idempotency_key": "VARCHAR(180)",
        "supersedes_id": "INTEGER",
        "invalidated_at": "DATETIME",
        "invalidation_reason": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in evidence_additive_columns.items():
        if column not in evidence_columns:
            await connection.execute(text(
                f'ALTER TABLE "evidence_observations" ADD COLUMN "{column}" {definition}'
            ))
    await connection.execute(text(
        "UPDATE evidence_observations SET recorded_at = occurred_at WHERE recorded_at IS NULL"
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            artifact_type VARCHAR(32) NOT NULL,
            source_uri VARCHAR(500) NOT NULL,
            title VARCHAR(300) NOT NULL DEFAULT '',
            content_hash VARCHAR(128) NOT NULL DEFAULT '',
            size_bytes INTEGER,
            metadata JSON NOT NULL DEFAULT '{}',
            plan_id INTEGER,
            task_id INTEGER,
            run_id VARCHAR(64),
            session_id VARCHAR(64),
            idempotency_key VARCHAR(180) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_artifact_owner_idempotency UNIQUE (owner_id, idempotency_key),
            FOREIGN KEY(owner_id) REFERENCES owners(id),
            FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE SET NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL,
            FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL
        )
        """
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS competencies (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            key VARCHAR(160) NOT NULL,
            title VARCHAR(240) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            competency_type VARCHAR(32) NOT NULL DEFAULT 'concept',
            scope VARCHAR(16) NOT NULL DEFAULT 'global',
            plan_id INTEGER,
            status VARCHAR(24) NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_competency_owner_key UNIQUE (owner_id, key),
            FOREIGN KEY(owner_id) REFERENCES owners(id),
            FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
        )
        """
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS competency_edges (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation VARCHAR(24) NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_competency_edge UNIQUE (owner_id, source_id, target_id, relation),
            FOREIGN KEY(owner_id) REFERENCES owners(id),
            FOREIGN KEY(source_id) REFERENCES competencies(id) ON DELETE CASCADE,
            FOREIGN KEY(target_id) REFERENCES competencies(id) ON DELETE CASCADE
        )
        """
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS plan_competency_links (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            plan_id INTEGER NOT NULL,
            competency_id INTEGER NOT NULL,
            target_stage VARCHAR(24) NOT NULL DEFAULT 'practicing',
            relation VARCHAR(24) NOT NULL DEFAULT 'targets',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_plan_competency_link UNIQUE (owner_id, plan_id, competency_id),
            FOREIGN KEY(owner_id) REFERENCES owners(id),
            FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE,
            FOREIGN KEY(competency_id) REFERENCES competencies(id) ON DELETE CASCADE
        )
        """
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS task_competency_links (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            task_id INTEGER NOT NULL,
            competency_id INTEGER NOT NULL,
            relation VARCHAR(24) NOT NULL DEFAULT 'teaches',
            target_stage VARCHAR(24) NOT NULL DEFAULT 'practicing',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_task_competency_link UNIQUE (owner_id, task_id, competency_id, relation),
            FOREIGN KEY(owner_id) REFERENCES owners(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY(competency_id) REFERENCES competencies(id) ON DELETE CASCADE
        )
        """
    ))
    await connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS resource_competency_links (
            id INTEGER PRIMARY KEY,
            owner_id VARCHAR(64) NOT NULL,
            resource_id INTEGER NOT NULL,
            competency_id INTEGER NOT NULL,
            depth VARCHAR(24) NOT NULL DEFAULT 'overview',
            relation VARCHAR(24) NOT NULL DEFAULT 'covers',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_resource_competency_link UNIQUE (owner_id, resource_id, competency_id),
            FOREIGN KEY(owner_id) REFERENCES owners(id),
            FOREIGN KEY(resource_id) REFERENCES learning_resources(id) ON DELETE CASCADE,
            FOREIGN KEY(competency_id) REFERENCES competencies(id) ON DELETE CASCADE
        )
        """
    ))
    evidence_columns = {
        row[1]
        for row in (await connection.execute(text('PRAGMA table_info("evidence_observations")'))).all()
    }
    if "competency_id" not in evidence_columns:
        await connection.execute(text(
            'ALTER TABLE "evidence_observations" ADD COLUMN "competency_id" INTEGER'
        ))
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_artifacts_owner_plan ON artifacts (owner_id, plan_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_competencies_owner_scope ON competencies (owner_id, scope, plan_id)",
        "CREATE INDEX IF NOT EXISTS ix_competency_edges_source ON competency_edges (source_id)",
        "CREATE INDEX IF NOT EXISTS ix_competency_edges_target ON competency_edges (target_id)",
        "CREATE INDEX IF NOT EXISTS ix_plan_competency_links_plan ON plan_competency_links (plan_id)",
        "CREATE INDEX IF NOT EXISTS ix_task_competency_links_task ON task_competency_links (task_id)",
        "CREATE INDEX IF NOT EXISTS ix_resource_competency_links_resource ON resource_competency_links (resource_id)",
        "CREATE INDEX IF NOT EXISTS ix_evidence_observations_competency ON evidence_observations (competency_id)",
    ):
        await connection.execute(text(statement))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_evidence_observations_owner_plan ON evidence_observations (owner_id, plan_id, recorded_at)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_evidence_observations_task ON evidence_observations (owner_id, task_id, occurred_at)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_evidence_observations_source ON evidence_observations (owner_id, source_type, source_id)"
    ))
    await connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_learning_events_occurred_at ON learning_events (occurred_at)"
    ))
    await connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_events_idempotency_key ON learning_events (idempotency_key) WHERE idempotency_key IS NOT NULL"
    ))
    await connection.execute(text(
        "UPDATE learning_events SET occurred_at = created_at WHERE occurred_at IS NULL"
    ))
    # Older builds did not enable SQLite foreign-key enforcement. Repair the
    # known SET NULL edge before new writes rely on the declared relationship.
    await connection.execute(text(
        """
        UPDATE context_snapshots
        SET run_id = NULL
        WHERE run_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM agent_runs WHERE agent_runs.id = context_snapshots.run_id)
        """
    ))
    await connection.execute(text(
        """
        INSERT OR IGNORE INTO session_plan_links
            (owner_id, session_id, plan_id, relation_type, source_run_id, created_at)
        SELECT owner_id, id, plan_id, 'focused', NULL, created_at
        FROM sessions
        WHERE plan_id IS NOT NULL
        """
    ))
    await connection.execute(text(
        """
        INSERT OR IGNORE INTO session_plan_links
            (owner_id, session_id, plan_id, relation_type, source_run_id, created_at)
        SELECT runs.owner_id, runs.session_id,
               CAST(json_extract(events.payload, '$.result.data.plan_id') AS INTEGER),
               'created', runs.id, events.created_at
        FROM run_events AS events
        JOIN agent_runs AS runs ON runs.id = events.run_id
        WHERE runs.session_id IS NOT NULL
          AND events.event_type = 'tool.completed'
          AND json_extract(events.payload, '$.name') = 'plan_create'
          AND json_extract(events.payload, '$.result.ok') = 1
          AND json_extract(events.payload, '$.result.data.plan_id') IS NOT NULL
        """
    ))
    await connection.execute(text(
        """
        UPDATE notifications
        SET session_id = (
            SELECT agent_runs.session_id FROM agent_runs WHERE agent_runs.id = notifications.run_id
        )
        WHERE session_id IS NULL AND run_id IS NOT NULL
        """
    ))
    await connection.execute(text(
        """
        UPDATE learning_resources
        SET provider = CASE
            WHEN url LIKE '%runoob.com%' THEN '菜鸟教程'
            WHEN url LIKE '%coursera.org%' THEN 'Coursera'
            WHEN url LIKE '%huggingface.co%' THEN 'Hugging Face'
            WHEN url LIKE '%kaggle.com%' THEN 'Kaggle'
            WHEN url LIKE '%fastapi.tiangolo.com%' THEN 'FastAPI'
            WHEN url LIKE '%realpython.com%' THEN 'Real Python'
            WHEN url LIKE '%github.com%' THEN 'GitHub'
            ELSE provider
        END
        WHERE provider = ''
        """
    ))
    await connection.execute(text(
        """
        UPDATE learning_resources
        SET resource_type = CASE
            WHEN url LIKE '%coursera.org/%' OR url LIKE '%huggingface.co/learn/%' THEN 'course'
            WHEN url LIKE '%runoob.com/%' OR url LIKE '%realpython.com/%' THEN 'tutorial'
            WHEN url LIKE '%github.com/%' THEN 'repository'
            WHEN url LIKE '%docs%' OR url LIKE '%documentation%' OR url LIKE '%fastapi.tiangolo.com/%' THEN 'documentation'
            ELSE 'web'
        END
        WHERE resource_type = 'web'
        """
    ))
    await connection.execute(text(
        """
        UPDATE learning_resources
        SET resource_type = 'web'
        WHERE (source = 'web_search' OR source LIKE 'web_search:%')
          AND provider = ''
          AND resource_type = 'tutorial'
        """
    ))
