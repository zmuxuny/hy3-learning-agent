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
    },
    "agent_runs": {
        "checkpoint": "JSON",
        "pending_approval": "JSON",
        "budget_usage": "JSON",
        "output": "TEXT NOT NULL DEFAULT ''",
        "created_plan_id": "INTEGER",
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
