"""H2 schema revision and bounded SQLite Unit-of-Work contracts.

Every database in this module lives under ``tmp_path``.  The frozen H1 table
fragments below are an upgrade oracle, not a second production migration path:
their combined schema digest must equal the immutable revision-1 checksum
before the public migrator is allowed to consume them.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.migrations import (
    MIGRATION_REGISTRY,
    migrate_sqlite_database,
    schema_checksum,
    verify_sqlite_database,
)
from app.db.uow import DatabaseBusyError, run_short_transaction
from hardening.h3_schema_fixture import materialize_frozen_h2


FROZEN_H1_SCHEMA_CHECKSUM = (
    "e7130a9013e7bd4754c3318520d9101f18d18b88c11e8ebbe7c965ead4c5e293"
)
FROZEN_H2_SCHEMA_CHECKSUM = (
    "7f42435d235b1497a358abc4353ff6b52771514a4b252c5ba74acec6e1493de1"
)
FROZEN_H3_SCHEMA_CHECKSUM = (
    "b69ed9f0844106e54936e008c22b4d4ebd7e089a8cb38989a4306ad25c7239de"
)
FROZEN_H4_SCHEMA_CHECKSUM = (
    "851f34b9c3d455208b73c6815856da70edc57e52d5676f8b6b391e8ddf1b0ace"
)
FROZEN_H5_SCHEMA_CHECKSUM = (
    "878d69dc13324434678716be6c4d05bfa77ff80c25e16d157576ec6a5458ec45"
)


FROZEN_H1_CHANGED_SCHEMA_SQL = """
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
CREATE UNIQUE INDEX ix_tool_invocations_idempotency_key ON tool_invocations (idempotency_key);
CREATE INDEX ix_tool_invocations_owner_id ON tool_invocations (owner_id);
CREATE INDEX ix_tool_invocations_run_id ON tool_invocations (run_id);
CREATE INDEX ix_tool_invocations_status ON tool_invocations (status);

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
    supersedes_id INTEGER,
    invalidated_at DATETIME,
    invalidation_reason TEXT DEFAULT '' NOT NULL,
    PRIMARY KEY (id),
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
CREATE INDEX ix_evidence_observations_run_id ON evidence_observations (run_id);
CREATE INDEX ix_evidence_observations_session_id ON evidence_observations (session_id);
CREATE INDEX ix_evidence_observations_source ON evidence_observations (owner_id, source_type, source_id);
CREATE INDEX ix_evidence_observations_source_id ON evidence_observations (source_id);
CREATE INDEX ix_evidence_observations_source_type ON evidence_observations (source_type);
CREATE INDEX ix_evidence_observations_supersedes_id ON evidence_observations (supersedes_id);
CREATE INDEX ix_evidence_observations_task ON evidence_observations (owner_id, task_id, occurred_at);
CREATE INDEX ix_evidence_observations_task_id ON evidence_observations (task_id);

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
CREATE INDEX ix_artifacts_artifact_type ON artifacts (artifact_type);
CREATE INDEX ix_artifacts_created_at ON artifacts (created_at);
CREATE INDEX ix_artifacts_idempotency_key ON artifacts (idempotency_key);
CREATE INDEX ix_artifacts_owner_id ON artifacts (owner_id);
CREATE INDEX ix_artifacts_owner_plan ON artifacts (owner_id, plan_id, created_at);
CREATE INDEX ix_artifacts_plan_id ON artifacts (plan_id);
CREATE INDEX ix_artifacts_run_id ON artifacts (run_id);
CREATE INDEX ix_artifacts_session_id ON artifacts (session_id);
CREATE INDEX ix_artifacts_task_id ON artifacts (task_id);

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
CREATE INDEX ix_operations_owner_id ON operations (owner_id);
CREATE INDEX ix_operations_run_id ON operations (run_id);
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
CREATE INDEX ix_notifications_archived_at ON notifications (archived_at);
CREATE INDEX ix_notifications_owner_id ON notifications (owner_id);
CREATE INDEX ix_notifications_plan_id ON notifications (plan_id);
CREATE INDEX ix_notifications_session_id ON notifications (session_id);
CREATE INDEX ix_notifications_status ON notifications (status);
"""


def _materialize_frozen_h1(path: Path, bootstrap_backups: Path) -> None:
    materialize_frozen_h2(path, bootstrap_backups)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            DROP TABLE outbox_receipts;
            DROP TABLE operations;
            DROP TABLE notifications;
            DROP TABLE outbox_actions;
            DROP TABLE tool_invocations;
            DROP TABLE evidence_observations;
            DROP TABLE artifacts;
            """
            + FROZEN_H1_CHANGED_SCHEMA_SQL
            + """
            DELETE FROM schema_migrations WHERE version > 1;
            PRAGMA user_version=1;
            """
        )
    assert schema_checksum(path) == FROZEN_H1_SCHEMA_CHECKSUM


def _seed_h1_rows(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES (?, ?, ?)",
            ("local", "H1 upgrade fixture", "Asia/Shanghai"),
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, owner_id, trigger, objective, status, model,
                cancel_requested, output
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("h1-run", "local", "user_message", "upgrade", "running", "fixture", 0, ""),
        )
        connection.executemany(
            """
            INSERT INTO tool_invocations(
                id, owner_id, run_id, idempotency_key, tool_name,
                args_hash, status, result_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (1, "local", "h1-run", "h1-running", "file_write", "a" * 32, "running", "{}"),
                (2, "local", "h1-run", "h1-committed", "task_patch", "b" * 32, "committed", '{"task_id":1}'),
            ),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                id, owner_id, artifact_type, source_uri, title, content_hash,
                metadata, run_id, idempotency_key
            ) VALUES (1, 'local', 'text', 'memory://h1', 'legacy', ?, '{}',
                      'h1-run', 'h1-artifact')
            """,
            ("c" * 64,),
        )
        connection.execute(
            """
            INSERT INTO evidence_observations(
                id, owner_id, source_type, source_id, run_id, outcome,
                occurred_at, idempotency_key
            ) VALUES (1, 'local', 'quiz', 'attempt-1', 'h1-run', 'passed',
                      '2026-08-19T00:00:00.000000Z', 'h1-evidence')
            """
        )
        connection.execute(
            """
            INSERT INTO operations(
                id, owner_id, run_id, tool_name, entity_type, entity_id,
                forward_patch, inverse_patch, status
            ) VALUES ('h1-operation', 'local', 'h1-run', 'task.patch', 'task',
                      '1', '{}', '{}', 'committed')
            """
        )
        connection.execute(
            """
            INSERT INTO notifications(
                id, owner_id, run_id, channel, title, body, status, reply_token
            ) VALUES (1, 'local', 'h1-run', 'email', 'legacy', 'body', 'queued',
                      'h1-reply-token')
            """
        )


def test_revision_one_upgrades_to_h2_without_inventing_request_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "h1-to-h2.sqlite3"
    _materialize_frozen_h1(database, tmp_path / "bootstrap-backups")
    _seed_h1_rows(database)

    report = migrate_sqlite_database(
        database,
        backup_root=tmp_path / "upgrade-backups",
        database_identity=database.name,
    )

    assert report.applied is True
    assert report.source_kind == "versioned"
    assert report.version == 5
    assert report.source_schema_checksum == FROZEN_H1_SCHEMA_CHECKSUM
    assert report.target_schema_checksum == FROZEN_H5_SCHEMA_CHECKSUM
    assert report.backup_path is not None
    assert verify_sqlite_database(
        database,
        expected_schema_checksum=FROZEN_H5_SCHEMA_CHECKSUM,
    ) == {
        "integrity": ["ok"],
        "foreign_key_violation_count": 0,
        "user_version": 5,
        "schema_checksum": FROZEN_H5_SCHEMA_CHECKSUM,
    }

    with sqlite3.connect(database) as connection:
        history = connection.execute(
            "SELECT version, name, checksum, result FROM schema_migrations ORDER BY version"
        ).fetchall()
        invocations = connection.execute(
            """
            SELECT id, status, request_digest, canonical_args, effect_kind,
                   attempt, version, claim_expires_at
            FROM tool_invocations ORDER BY id
            """
        ).fetchall()
        artifact_digest = connection.execute(
            "SELECT request_digest FROM artifacts WHERE id=1"
        ).fetchone()
        evidence_digest = connection.execute(
            "SELECT request_digest FROM evidence_observations WHERE id=1"
        ).fetchone()
        operation_links = connection.execute(
            "SELECT invocation_id FROM operations WHERE id='h1-operation'"
        ).fetchone()
        notification_state = connection.execute(
            "SELECT invocation_id, status FROM notifications WHERE id=1"
        ).fetchone()
        outbox_counts = (
            connection.execute("SELECT count(*) FROM outbox_actions").fetchone()[0],
            connection.execute("SELECT count(*) FROM outbox_receipts").fetchone()[0],
        )

    assert history == [
        (item.version, item.name, item.checksum, "applied") for item in MIGRATION_REGISTRY
    ]
    assert invocations == [
        (1, "needs_reconciliation", None, None, None, 1, 1, None),
        (2, "committed", None, None, None, 1, 1, None),
    ]
    assert artifact_digest == (None,)
    assert evidence_digest == (None,)
    assert operation_links == (None,)
    assert notification_state == (None, "needs_reconciliation")
    assert outbox_counts == (0, 0)

    second = migrate_sqlite_database(
        database,
        backup_root=tmp_path / "upgrade-backups",
        database_identity=database.name,
    )
    assert second.applied is False
    assert second.backup_path is None


def test_h2_schema_enforces_digest_effect_destination_and_status_constraints(
    tmp_path: Path,
) -> None:
    database = tmp_path / "h2-constraints.sqlite3"
    migrate_sqlite_database(
        database,
        backup_root=tmp_path / "constraint-backups",
        database_identity=database.name,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'H2', 'UTC')"
        )
        connection.execute(
            """
            INSERT INTO operations(
                id, owner_id, tool_name, entity_type, entity_id,
                forward_patch, inverse_patch, status
            ) VALUES ('operation-1', 'local', 'file.write', 'workspace_file',
                      'fixture.txt', '{}', '{}', 'pending_delivery')
            """
        )
        connection.execute(
            """
            INSERT INTO notifications(
                id, owner_id, channel, title, body, status, reply_token
            ) VALUES (1, 'local', 'email', 'H2', 'body', 'queued', 'reply-1')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO outbox_actions(
                    id, owner_id, action_key, request_digest, effect_kind,
                    destination, payload, status
                ) VALUES ('bad-digest', 'local', 'bad-digest', 'short',
                          'external_write', 'smtp', '{}', 'queued')
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO outbox_actions(
                    id, owner_id, action_key, request_digest, effect_kind,
                    destination, payload, status
                ) VALUES ('bad-destination', 'local', 'bad-destination', ?,
                          'external_write', 'unknown', '{}', 'queued')
                """,
                ("d" * 64,),
            )
        connection.execute(
            """
            INSERT INTO outbox_actions(
                id, owner_id, action_key, request_digest, effect_kind,
                destination, payload, status
            ) VALUES ('subprocess-ok', 'local', 'subprocess-ok', ?,
                      'external_write', 'subprocess', '{}', 'queued')
            """,
            ("e" * 64,),
        )
        connection.execute(
            """
            INSERT INTO outbox_actions(
                id, owner_id, operation_id, action_key, request_digest,
                effect_kind, destination, payload, status
            ) VALUES ('workspace-ok', 'local', 'operation-1', 'workspace-ok', ?,
                      'external_write', 'workspace_file', '{}', 'queued')
            """,
            ("f" * 64,),
        )
        connection.execute(
            """
            INSERT INTO outbox_actions(
                id, owner_id, notification_id, action_key, request_digest,
                effect_kind, destination, payload, status
            ) VALUES ('smtp-ok', 'local', 1, 'smtp-ok', ?,
                      'external_write', 'smtp', '{}', 'queued')
            """,
            ("1" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO outbox_actions(
                    id, owner_id, operation_id, action_key, request_digest,
                    effect_kind, destination, payload, status
                ) VALUES ('orphan-operation', 'local', 'missing',
                          'orphan-operation', ?, 'external_write',
                          'workspace_file', '{}', 'queued')
                """,
                ("2" * 64,),
            )
        foreign_keys = {
            row[3]: (row[2], row[4], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(outbox_actions)")
        }
        assert foreign_keys["notification_id"] == ("notifications", "id", "SET NULL")
        assert foreign_keys["operation_id"] == ("operations", "id", "SET NULL")
        assert foreign_keys["invocation_id"] == ("tool_invocations", "id", "SET NULL")
        connection.execute(
            """
            INSERT INTO outbox_receipts(
                outbox_action_id, action_key, status, response
            ) VALUES ('subprocess-ok', 'subprocess-ok', 'accepted', '{}')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            connection.execute(
                """
                INSERT INTO outbox_receipts(
                    outbox_action_id, action_key, status, response
                ) VALUES ('subprocess-ok', 'different-key', 'accepted', '{}')
                """
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("record_kind", "column", "invalid_value"),
    [
        pytest.param("invocation", "request_digest", "short", id="invocation-digest"),
        pytest.param("invocation", "effect_kind", "unknown", id="invocation-effect"),
        pytest.param("invocation", "status", "unknown", id="invocation-status"),
        pytest.param("invocation", "attempt", 0, id="invocation-attempt"),
        pytest.param("invocation", "version", 0, id="invocation-version"),
        pytest.param("outbox", "effect_kind", "database_write", id="outbox-effect"),
        pytest.param("outbox", "status", "unknown", id="outbox-status"),
        pytest.param("outbox", "attempt", -1, id="outbox-attempt"),
        pytest.param("outbox", "version", 0, id="outbox-version"),
        pytest.param("receipt", "status", "unknown", id="receipt-status"),
    ],
)
def test_h2_schema_rejects_each_invalid_protocol_state(
    tmp_path: Path,
    record_kind: str,
    column: str,
    invalid_value,
) -> None:
    """Each H2 state-machine constraint has an independent executable oracle."""

    database = tmp_path / f"invalid-{record_kind}-{column}.sqlite3"
    migrate_sqlite_database(
        database,
        backup_root=tmp_path / "invalid-state-backups",
        database_identity=database.name,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'H2', 'UTC')"
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, owner_id, trigger, objective, status, model,
                cancel_requested, output
            ) VALUES ('constraint-run', 'local', 'user_message', 'constraint',
                      'running', 'fixture', 0, '')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            if record_kind == "invocation":
                values = {
                    "request_digest": "a" * 64,
                    "effect_kind": "database_write",
                    "status": "running",
                    "attempt": 1,
                    "version": 1,
                }
                values[column] = invalid_value
                connection.execute(
                    """
                    INSERT INTO tool_invocations(
                        owner_id, run_id, idempotency_key, tool_name, args_hash,
                        request_digest, canonical_args, effect_kind, status,
                        result_payload, attempt, version
                    ) VALUES (
                        'local', 'constraint-run', 'constraint-invocation',
                        'constraint_probe', ?, ?, '{}', ?, ?, '{}', ?, ?
                    )
                    """,
                    (
                        "b" * 64,
                        values["request_digest"],
                        values["effect_kind"],
                        values["status"],
                        values["attempt"],
                        values["version"],
                    ),
                )
            elif record_kind == "outbox":
                values = {
                    "effect_kind": "external_write",
                    "status": "queued",
                    "attempt": 0,
                    "version": 1,
                }
                values[column] = invalid_value
                connection.execute(
                    """
                    INSERT INTO outbox_actions(
                        id, owner_id, action_key, request_digest, effect_kind,
                        destination, payload, status, attempt, version
                    ) VALUES (
                        'constraint-action', 'local', 'constraint-action', ?, ?,
                        'smtp', '{}', ?, ?, ?
                    )
                    """,
                    (
                        "c" * 64,
                        values["effect_kind"],
                        values["status"],
                        values["attempt"],
                        values["version"],
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO outbox_actions(
                        id, owner_id, action_key, request_digest, effect_kind,
                        destination, payload, status
                    ) VALUES (
                        'constraint-action', 'local', 'constraint-action', ?,
                        'external_write', 'smtp', '{}', 'delivered'
                    )
                    """,
                    ("d" * 64,),
                )
                connection.execute(
                    """
                    INSERT INTO outbox_receipts(
                        outbox_action_id, action_key, status, response
                    ) VALUES ('constraint-action', 'constraint-action', ?, '{}')
                    """,
                    (invalid_value,),
                )


def _locked_session_factory(
    database: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _zero_busy_timeout(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=0")
        cursor.close()

    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_short_transaction_waits_within_budget_then_commits_once(
    tmp_path: Path,
) -> None:
    engine, factory = _locked_session_factory(tmp_path / "budget-success.sqlite3")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE uow_probe (id INTEGER PRIMARY KEY)"))
        holder = await engine.connect()
        await holder.execute(text("BEGIN IMMEDIATE"))

        async def release_writer() -> None:
            await asyncio.sleep(0.3)
            await holder.rollback()

        async def insert_once(db: AsyncSession) -> int:
            await db.execute(text("INSERT INTO uow_probe(id) VALUES (1)"))
            return 1

        release_task = asyncio.create_task(release_writer())
        started = time.monotonic()
        result = await run_short_transaction(factory, insert_once)
        elapsed = time.monotonic() - started
        await release_task
        await holder.close()

        async with factory() as db:
            count = await db.scalar(text("SELECT count(*) FROM uow_probe"))
        assert result == 1
        assert count == 1
        assert 0.3 <= elapsed < 1.5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_short_transaction_budget_exhaustion_is_typed_and_exactly_retryable(
    tmp_path: Path,
) -> None:
    engine, factory = _locked_session_factory(tmp_path / "budget-exhausted.sqlite3")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE uow_probe (id INTEGER PRIMARY KEY)"))
        holder = await engine.connect()
        await holder.execute(text("BEGIN IMMEDIATE"))

        async def insert_once(db: AsyncSession) -> int:
            await db.execute(text("INSERT INTO uow_probe(id) VALUES (1)"))
            return 1

        started = time.monotonic()
        with pytest.raises(DatabaseBusyError) as raised:
            await run_short_transaction(factory, insert_once)
        elapsed = time.monotonic() - started
        payload = raised.value.as_result()
        assert payload["error_code"] == "database_busy"
        assert payload["retryable"] is True
        assert payload["status"] == "database_busy"
        assert payload["attempts"] == 5
        assert 0.4 <= elapsed < 1.5

        await holder.rollback()
        await holder.close()
        assert await run_short_transaction(factory, insert_once) == 1
        async with factory() as db:
            count = await db.scalar(text("SELECT count(*) FROM uow_probe"))
        assert count == 1
    finally:
        await engine.dispose()
