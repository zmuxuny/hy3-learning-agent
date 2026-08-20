"""Revision-4 to revision-5 Context/Intervention migration contracts."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.context.memory import MemoryManager
from app.db.database import Base
from app.db.migrations import (
    MIGRATION_REGISTRY,
    migrate_sqlite_database,
    schema_checksum,
    verify_sqlite_database,
)
from hardening.h5_schema_fixture import (
    FROZEN_H4_SCHEMA_CHECKSUM,
    materialize_frozen_h4,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
H5_CHECKSUM = "878d69dc13324434678716be6c4d05bfa77ff80c25e16d157576ec6a5458ec45"


H5_MIGRATION_KILL_PROGRAM = r"""
import os
import signal
import sys
from pathlib import Path
from app.db.migrations import migrate_sqlite_database

database, backup_root, phase = sys.argv[1:]

def terminate_at(point: str) -> None:
    if point == phase:
        os.kill(os.getpid(), signal.SIGKILL)

migrate_sqlite_database(
    Path(database),
    backup_root=Path(backup_root),
    database_identity=Path(database).name,
    fault_injector=terminate_at,
)
raise SystemExit(97)
"""


def _schema_rows(path: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        ).fetchall()


def _seed_revision_four_context(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'Learner', 'Asia/Shanghai')"
        )
        connection.execute(
            """
            INSERT INTO plans(
                id, owner_id, title, description, goal, current_level,
                weekly_minutes, preferences, expected_outcome,
                available_resources, avoid_methods, status, version, progress,
                memory_summary
            ) VALUES (1, 'local', 'H5 fixture', '', '', '', 0, '{}', '', '[]',
                      '[]', 'active', 1, 0, '')
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(
                id, owner_id, plan_id, parent_session_id, title, summary,
                handoff_summary
            ) VALUES ('source', 'local', NULL, NULL, 'Source', 'legacy summary', '')
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(
                id, owner_id, plan_id, parent_session_id, title, summary,
                handoff_summary
            ) VALUES ('target', 'local', 1, 'source', 'Target', '', 'FROZEN LEGACY HANDOFF')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, owner_id, session_id, plan_id, parent_run_id, "trigger",
                objective, status, phase, state_version, attempt, retry_count,
                model, cancel_requested, output
            ) VALUES (
                'heartbeat-run', 'local', 'target', 1, NULL, 'heartbeat',
                'legacy candidate is unavailable', 'failed', 'terminal', 1, 0, 0,
                'hy3', 0, ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO chat_messages(
                id, session_id, run_id, role, content, message_metadata
            ) VALUES (1, 'target', 'heartbeat-run', 'user', 'current content', '{}')
            """
        )
        connection.executemany(
            """
            INSERT INTO chat_message_revisions(
                id, message_id, session_id, previous_run_id, content,
                message_metadata
            ) VALUES (?, 1, 'target', 'heartbeat-run', ?, '{}')
            """,
            [(1, "first content"), (2, "second content")],
        )
        connection.execute(
            """
            INSERT INTO session_summaries(
                id, owner_id, session_id, version, content,
                covered_through_message_id, source_message_ids, method
            ) VALUES (1, 'local', 'target', 1, 'legacy compressed', 1, '[1]', 'model')
            """
        )
        connection.execute(
            """
            INSERT INTO memories(
                id, owner_id, scope, scope_id, layer, content, source_type,
                source_id, confidence, status, archived_reason, access_count
            ) VALUES (
                1, 'local', 'plan', '1', 'semantic', 'legacy memory', 'run',
                'heartbeat-run', 0.8, 'confirmed', '', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memories(
                id, owner_id, scope, scope_id, layer, content, source_type,
                source_id, confidence, status, archived_reason, access_count
            ) VALUES (
                2, 'local', 'global', NULL, 'semantic', 'unverified legacy memory',
                'user', NULL, 0.5, 'confirmed', '', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO context_snapshots(
                id, owner_id, plan_id, run_id, markdown, source_manifest,
                estimated_tokens
            ) VALUES (
                1, 'local', 1, 'heartbeat-run', 'legacy context',
                '[{"type":"message","id":1}]', 4
            )
            """
        )
        connection.execute(
            """
            INSERT INTO notifications(
                id, owner_id, run_id, session_id, plan_id, channel, title,
                body, status, reply_token
            ) VALUES (
                1, 'local', 'heartbeat-run', 'target', 1, 'in_app',
                'Same title', 'Same body', 'sent',
                '00000000-0000-4000-8000-000000000001'
            )
            """
        )
        connection.commit()


def test_fresh_and_revision_four_upgrade_have_identical_sqlite_schema(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh.sqlite3"
    upgraded = tmp_path / "upgraded.sqlite3"
    migrate_sqlite_database(fresh, backup_root=tmp_path / "fresh-backups")
    materialize_frozen_h4(upgraded, tmp_path / "bootstrap-backups")
    with sqlite3.connect(upgraded) as connection:
        prior_history = connection.execute(
            "SELECT version, name, checksum, applied_at, result "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    migrate_sqlite_database(upgraded, backup_root=tmp_path / "upgrade-backups")

    assert schema_checksum(fresh) == schema_checksum(upgraded) == H5_CHECKSUM
    assert _schema_rows(fresh) == _schema_rows(upgraded)
    with sqlite3.connect(upgraded) as connection:
        history = connection.execute(
            "SELECT version, name, checksum, applied_at, result "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert history[:4] == prior_history
    assert [row[:3] for row in history] == [
        (revision.version, revision.name, revision.checksum)
        for revision in MIGRATION_REGISTRY
    ]


def test_metadata_create_all_matches_revision_five_schema(tmp_path: Path) -> None:
    metadata_database = tmp_path / "metadata.sqlite3"
    migrated_database = tmp_path / "migrated.sqlite3"
    engine = create_engine(f"sqlite:///{metadata_database}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    migrate_sqlite_database(migrated_database, backup_root=tmp_path / "backups")

    assert schema_checksum(metadata_database) == H5_CHECKSUM
    assert _schema_rows(metadata_database) == _schema_rows(migrated_database)


def test_revision_four_backfill_preserves_bytes_without_inventing_provenance(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade.sqlite3"
    materialize_frozen_h4(database, tmp_path / "bootstrap-backups")
    _seed_revision_four_context(database)

    report = migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")

    assert report.target_schema_checksum == H5_CHECKSUM
    verify_sqlite_database(database, expected_schema_checksum=H5_CHECKSUM)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        message = connection.execute(
            "SELECT version, content_hash FROM chat_messages WHERE id=1"
        ).fetchone()
        revisions = connection.execute(
            "SELECT version, content, content_hash FROM chat_message_revisions ORDER BY version"
        ).fetchall()
        summary = connection.execute(
            """
            SELECT content, coverage_count, validity_state, provenance_node_id,
                   source_digest
            FROM session_summaries WHERE id=1
            """
        ).fetchone()
        memory = connection.execute(
            """
            SELECT content, validity_state, content_hash, provenance_node_id,
                   provenance_digest, lifecycle_reason_code
            FROM memories WHERE id=1
            """
        ).fetchone()
        lifecycle = connection.execute(
            """
            SELECT event_type, action_key, to_status, to_validity_state, reason_code
            FROM memory_lifecycle_events WHERE memory_id=1
            """
        ).fetchone()
        invalid_legacy_imports = connection.execute(
            """
            SELECT count(*)
            FROM memory_lifecycle_events AS event
            JOIN memories AS memory ON memory.id=event.memory_id
            WHERE event.event_type='legacy_import'
              AND NOT (
                event.version=memory.lifecycle_version
                AND event.to_status=memory.status
                AND event.to_validity_state=memory.validity_state
                AND event.reason_code=memory.lifecycle_reason_code
                AND event.expires_at_after IS memory.expires_at
                AND event.version=1
                AND event.from_status IS NULL
                AND event.from_validity_state IS NULL
                AND (
                  (event.to_validity_state='legacy_unverified'
                   AND event.reason_code='legacy_unverified')
                  OR (event.to_validity_state='valid'
                      AND event.reason_code='legacy_exact_source')
                )
              )
            """
        ).fetchone()[0]
        memory_source_edges = connection.execute(
            """
            SELECT count(*) FROM provenance_edges
            WHERE target_node_id=? AND relation='memory_source'
            """,
            (memory["provenance_node_id"],),
        ).fetchone()[0]
        unverified_memory = connection.execute(
            """
            SELECT validity_state, provenance_node_id, lifecycle_reason_code
            FROM memories WHERE id=2
            """
        ).fetchone()
        snapshot = connection.execute(
            """
            SELECT markdown, session_id, validity_state, dropped_source_manifest,
                   budget_breakdown, provenance_node_id
            FROM context_snapshots WHERE id=1
            """
        ).fetchone()
        handoff = connection.execute(
            """
            SELECT content, content_hash, provenance_state, validity_state
            FROM session_handoffs WHERE target_session_id='target'
            """
        ).fetchone()
        notification = connection.execute(
            "SELECT intervention_id, legacy_unlinked FROM notifications WHERE id=1"
        ).fetchone()
        decision_count = connection.execute(
            "SELECT count(*) FROM proactive_decisions"
        ).fetchone()[0]
        intervention_count = connection.execute(
            "SELECT count(*) FROM interventions"
        ).fetchone()[0]
        run_state = connection.execute(
            "SELECT proactive_candidate_state FROM agent_runs WHERE id='heartbeat-run'"
        ).fetchone()[0]
        context_state = connection.execute(
            "SELECT generation FROM context_states WHERE owner_id='local'"
        ).fetchone()[0]
        compression = connection.execute(
            """
            SELECT covered_through_message_id, generation, claim_token
            FROM session_compression_states WHERE session_id='target'
            """
        ).fetchone()

    assert tuple(message) == (
        3,
        hashlib.sha256(b"current content").hexdigest(),
    )
    assert [tuple(row) for row in revisions] == [
        (1, "first content", hashlib.sha256(b"first content").hexdigest()),
        (2, "second content", hashlib.sha256(b"second content").hexdigest()),
    ]
    assert summary["content"] == "legacy compressed"
    assert summary["coverage_count"] == 0
    assert summary["validity_state"] == "legacy_unverified"
    assert summary["provenance_node_id"] is None
    assert len(summary["source_digest"]) == 64
    assert memory["content"] == "legacy memory"
    assert memory["validity_state"] == "valid"
    assert memory["content_hash"] == hashlib.sha256(b"legacy memory").hexdigest()
    assert memory["provenance_node_id"] is not None
    assert len(memory["provenance_digest"]) == 64
    assert memory["lifecycle_reason_code"] == "legacy_exact_source"
    assert memory_source_edges == 1
    assert tuple(unverified_memory) == (
        "legacy_unverified",
        None,
        "legacy_unverified",
    )
    assert tuple(lifecycle) == (
        "legacy_import",
        "h5:legacy-memory:1",
        "confirmed",
        "valid",
        "legacy_exact_source",
    )
    assert invalid_legacy_imports == 0
    assert snapshot["markdown"] == "legacy context"
    assert snapshot["session_id"] == "target"
    assert snapshot["validity_state"] == "legacy_unverified"
    assert json.loads(snapshot["dropped_source_manifest"]) == []
    assert json.loads(snapshot["budget_breakdown"]) == {}
    assert snapshot["provenance_node_id"] is None
    assert handoff["content"] == "FROZEN LEGACY HANDOFF"
    assert handoff["content_hash"] == hashlib.sha256(
        b"FROZEN LEGACY HANDOFF"
    ).hexdigest()
    assert handoff["provenance_state"] == "legacy_unverified"
    assert handoff["validity_state"] == "legacy_unverified"
    assert tuple(notification) == (None, 1)
    assert decision_count == intervention_count == 0
    assert run_state == "legacy_unavailable"
    assert context_state == 0
    assert tuple(compression) == (None, 0, None)


@pytest.mark.asyncio
async def test_exact_legacy_memory_can_advance_through_current_atomic_lifecycle(
    tmp_path: Path,
) -> None:
    """A verified migration fact must remain usable by the current H5 CAS path."""

    database = tmp_path / "exact-memory-runtime.sqlite3"
    materialize_frozen_h4(database, tmp_path / "bootstrap-backups")
    _seed_revision_four_context(database)
    migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")

    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            async with session.begin():
                archived = await MemoryManager(session).archive("local", 1)
                assert archived.status == "archived"
                assert archived.lifecycle_version == 2
                assert archived.provenance_node_id is not None
    finally:
        await engine.dispose()

    with sqlite3.connect(database) as connection:
        memory = connection.execute(
            """
            SELECT status, lifecycle_version, lifecycle_reason_code,
                   provenance_node_id, provenance_digest
            FROM memories WHERE id=1
            """
        ).fetchone()
        node = connection.execute(
            """
            SELECT kind, entity_key, entity_version, content_digest
            FROM provenance_nodes WHERE id=?
            """,
            (memory[3],),
        ).fetchone()
        edge_count = connection.execute(
            """
            SELECT count(*) FROM provenance_edges
            WHERE target_node_id=? AND relation='memory_source'
            """,
            (memory[3],),
        ).fetchone()[0]
        lifecycle = connection.execute(
            """
            SELECT version, event_type, to_status, to_validity_state, reason_code
            FROM memory_lifecycle_events WHERE memory_id=1 ORDER BY version
            """
        ).fetchall()

    assert memory[:3] == ("archived", 2, "manual_archive")
    assert len(memory[4]) == 64
    assert node[:3] == ("memory", "1", 2)
    assert node[3] == hashlib.sha256(b"legacy memory").hexdigest()
    assert edge_count == 1
    assert lifecycle == [
        (1, "legacy_import", "confirmed", "valid", "legacy_exact_source"),
        (2, "archived", "archived", "valid", "manual_archive"),
    ]


def test_revision_five_update_guards_close_scope_and_identity_escapes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guards.sqlite3"
    materialize_frozen_h4(database, tmp_path / "bootstrap-backups")
    _seed_revision_four_context(database)
    migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")

    digest = hashlib.sha256(b"Same title\0Same body").hexdigest()
    payload_digest = hashlib.sha256(b"mail payload").hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, owner_id, session_id, plan_id, "trigger", objective,
                status, phase, state_version, attempt, retry_count, model,
                cancel_requested, output
            ) VALUES (
                'other-run', 'local', 'target', 1, 'user_message', '',
                'completed', 'terminal', 1, 0, 0, 'hy3', 0, ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tool_invocations(
                id, owner_id, run_id, idempotency_key, tool_name, args_hash,
                status, result_payload, attempt, version
            ) VALUES (
                99, 'local', 'other-run', 'h5-other-invocation', 'notification_send',
                ?, 'committed', '{}', 1, 1
            )
            """,
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="proactive decision run scope mismatch"):
            connection.execute(
                """
                INSERT INTO proactive_decisions(
                    id, owner_id, plan_id, source_run_id, source_invocation_id,
                    candidate_key, candidate_kind, candidate_payload,
                    candidate_digest, policy_version, status, decision_payload,
                    decision_digest
                ) VALUES (
                    'cross-invocation', 'local', 1, 'heartbeat-run', 99,
                    'candidate', 'due_review', '{}', ?, 'v1', 'building', '{}', ?
                )
                """,
                ("1" * 64, "2" * 64),
            )

        connection.execute(
            """
            INSERT INTO interventions(
                id, owner_id, source_run_id, plan_id, session_id, reply_token,
                title, body, content_digest, state
            ) VALUES (
                'building-intervention', 'local', 'heartbeat-run', 1, 'target',
                '00000000-0000-4000-8000-000000000002', 'Same title',
                'Same body', ?, 'building'
            )
            """,
            (digest,),
        )
        connection.execute(
            """
            INSERT INTO interventions(
                id, owner_id, source_run_id, source_invocation_id, plan_id,
                session_id, reply_token, title, body, content_digest, state
            ) VALUES (
                'invocation-intervention', 'local', 'other-run', 99, 1,
                'target', '00000000-0000-4000-8000-000000000099',
                'Same title', 'Same body', ?, 'building'
            )
            """,
            (digest,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="source_invocation"):
            connection.execute(
                """
                INSERT INTO interventions(
                    id, owner_id, source_run_id, source_invocation_id, plan_id,
                    session_id, reply_token, title, body, content_digest, state
                ) VALUES (
                    'duplicate-invocation-intervention', 'local', 'other-run', 99, 1,
                    'target', '00000000-0000-4000-8000-000000000098',
                    'Same title', 'Same body', ?, 'building'
                )
                """,
                (digest,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="intervention identity"):
            connection.execute(
                """
                UPDATE interventions
                SET owner_id='foreign', state='active', canonical_message_id=1
                WHERE id='building-intervention'
                """
            )

        connection.execute(
            """
            INSERT INTO chat_messages(
                id, session_id, run_id, role, content, version, content_hash,
                validity_state, invalidation_reason, message_metadata
            ) VALUES (
                3, 'target', 'heartbeat-run', 'assistant', 'Same body', 1, ?,
                'active', '', '{}'
            )
            """,
            (hashlib.sha256(b"Same body").hexdigest(),),
        )
        with pytest.raises(sqlite3.IntegrityError, match="intervention scope mismatch"):
            connection.execute(
                """
                INSERT INTO interventions(
                    id, owner_id, source_run_id, plan_id, session_id,
                    canonical_message_id, reply_token, title, body, content_digest,
                    state
                ) VALUES (
                    'wrong-canonical', 'local', 'heartbeat-run', 1, 'target', 1,
                    '00000000-0000-4000-8000-000000000004', 'Same title',
                    'current content', ?, 'active'
                )
                """,
                (digest,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="intervention scope mismatch"):
            connection.execute(
                """
                INSERT INTO interventions(
                    id, owner_id, source_run_id, plan_id, session_id,
                    canonical_message_id, reply_token, title, body, content_digest,
                    state
                ) VALUES (
                    'wrong-canonical-body', 'local', 'heartbeat-run', 1, 'target', 3,
                    '00000000-0000-4000-8000-000000000005', 'Same title',
                    'forged body', ?, 'active'
                )
                """,
                (digest,),
            )
        connection.execute(
            """
            INSERT INTO interventions(
                id, owner_id, source_run_id, plan_id, session_id,
                canonical_message_id, reply_token, title, body, content_digest,
                state
            ) VALUES (
                'intervention', 'local', 'heartbeat-run', 1, 'target', 3,
                '00000000-0000-4000-8000-000000000003', 'Same title',
                'Same body', ?, 'active'
            )
            """,
            (digest,),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="canonical intervention message is immutable",
        ):
            connection.execute(
                """
                UPDATE chat_messages
                SET content='mutated canonical', version=2, content_hash=?
                WHERE id=3
                """,
                (hashlib.sha256(b"mutated canonical").hexdigest(),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_intervention_resolved_shape"):
            connection.execute(
                """
                UPDATE interventions
                SET state='resolved', resolved_at=CURRENT_TIMESTAMP
                WHERE id='intervention'
                """
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="notification intervention scope mismatch",
        ):
            connection.execute(
                """
                UPDATE notifications
                SET intervention_id='intervention', legacy_unlinked=0,
                    reply_token='00000000-0000-4000-8000-000000000006'
                WHERE id=1
                """
            )
        connection.execute(
            """
            UPDATE notifications
            SET intervention_id='intervention', legacy_unlinked=0,
                reply_token='00000000-0000-4000-8000-000000000003'
            WHERE id=1
            """
        )
        connection.execute(
            """
            INSERT INTO notifications(
                id, owner_id, run_id, session_id, plan_id, intervention_id,
                delivery_generation, legacy_unlinked, channel, title, body,
                status, reply_token
            ) VALUES (
                2, 'local', 'heartbeat-run', 'target', 1, 'intervention',
                1, 0, 'email', 'Same title', 'Same body', 'sent',
                '00000000-0000-4000-8000-000000000003'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="linked notification identity"):
            connection.execute("UPDATE notifications SET title='escaped' WHERE id=1")

        with pytest.raises(sqlite3.IntegrityError, match="ck_agent_run_proactive_candidate_shape"):
            connection.execute(
                "UPDATE agent_runs SET proactive_candidate_key='forged' WHERE id='other-run'"
            )
        connection.execute(
            """
            UPDATE agent_runs
            SET proactive_candidate_state='captured',
                proactive_candidate_key='due-review',
                proactive_candidate_kind='due_review',
                proactive_candidate_payload='{}',
                proactive_candidate_digest=?,
                proactive_source_watermark=1,
                proactive_source_projection_digest=?,
                proactive_detected_at=CURRENT_TIMESTAMP
            WHERE id='heartbeat-run'
            """,
            ("3" * 64, "4" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="captured proactive candidate"):
            connection.execute(
                """
                UPDATE agent_runs SET proactive_candidate_key='mutated'
                WHERE id='heartbeat-run'
                """
            )

        connection.execute(
            """
            INSERT INTO chat_messages(
                id, session_id, role, content, version, content_hash,
                validity_state, invalidation_reason, message_metadata
            ) VALUES (2, 'target', 'user', 'reply', 1, ?, 'active', '', '{}')
            """,
            (hashlib.sha256(b"reply").hexdigest(),),
        )
        connection.execute(
            "UPDATE chat_messages SET reply_to_intervention_id='intervention' WHERE id=2"
        )
        with pytest.raises(sqlite3.IntegrityError, match="reply target is immutable"):
            connection.execute(
                "UPDATE chat_messages SET reply_to_intervention_id=NULL WHERE id=2"
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="chat message content requires next version and digest",
        ):
            connection.execute("UPDATE chat_messages SET content='unversioned edit' WHERE id=2")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="chat message revision scope or version mismatch",
        ):
            connection.execute(
                """
                INSERT INTO chat_message_revisions(
                    message_id, session_id, previous_run_id, version, content,
                    content_hash, message_metadata
                ) VALUES (2, 'target', NULL, 1, 'forged old bytes', ?, '{}')
                """,
                (hashlib.sha256(b"forged old bytes").hexdigest(),),
            )
        connection.execute(
            """
            INSERT INTO chat_message_revisions(
                message_id, session_id, previous_run_id, version, content,
                content_hash, message_metadata
            ) VALUES (2, 'target', NULL, 1, 'reply', ?, '{}')
            """,
            (hashlib.sha256(b"reply").hexdigest(),),
        )
        with pytest.raises(sqlite3.IntegrityError, match="chat message revisions are immutable"):
            connection.execute("UPDATE chat_message_revisions SET content='escaped'")
        with pytest.raises(sqlite3.IntegrityError, match="chat message revisions are immutable"):
            connection.execute("DELETE FROM chat_message_revisions")
        connection.execute(
            """
            UPDATE chat_messages
            SET content='versioned edit', version=2, content_hash=?
            WHERE id=2
            """,
            (hashlib.sha256(b"versioned edit").hexdigest(),),
        )

        connection.execute(
            "UPDATE agent_runs SET reply_to_intervention_id='intervention' WHERE id='heartbeat-run'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="reply target is immutable"):
            connection.execute(
                "UPDATE agent_runs SET reply_to_intervention_id=NULL WHERE id='heartbeat-run'"
            )

        connection.execute(
            """
            INSERT INTO queued_messages(
                id, owner_id, session_id, plan_id, "trigger", execution_mode,
                objective, message_metadata, position, version
            ) VALUES (
                'queued-reply', 'local', 'target', 1, 'email_reply', 'read_only',
                'reply', '{}', 0, 1
            )
            """
        )
        connection.execute(
            """
            UPDATE queued_messages
            SET reply_to_intervention_id='intervention'
            WHERE id='queued-reply'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="reply target is immutable"):
            connection.execute(
                """
                UPDATE queued_messages SET reply_to_intervention_id=NULL
                WHERE id='queued-reply'
                """
            )

        connection.execute(
            """
            INSERT INTO inbound_mail_jobs(
                id, owner_id, intervention_id, source_notification_id,
                session_id, plan_id, mailbox_key, uidvalidity, uid, subject,
                body, payload_digest, state, execution_mode, outcome,
                ack_state, ack_attempt
            ) VALUES (
                'mail-job', 'local', 'intervention', 1, 'target', 1,
                'mailbox', 7, 11, 'subject', 'body', ?, 'queued', 'read_only',
                '', 'pending', 0
            )
            """,
            (payload_digest,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="inbound mail identity"):
            connection.execute("UPDATE inbound_mail_jobs SET uid=12 WHERE id='mail-job'")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid inbound mail acknowledgement transition",
        ):
            connection.execute(
                """
                UPDATE inbound_mail_jobs
                SET ack_state='acked', acked_at=CURRENT_TIMESTAMP
                WHERE id='mail-job'
                """
            )
        connection.execute(
            """
            UPDATE inbound_mail_jobs
            SET ack_state='claimed', ack_attempt=1,
                ack_claim_token='mail-claim-live',
                ack_claim_expires_at='2999-01-01T00:00:00+00:00'
            WHERE id='mail-job'
            """
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid inbound mail acknowledgement transition",
        ):
            connection.execute(
                """
                UPDATE inbound_mail_jobs
                SET ack_attempt=2, ack_claim_token='mail-claim-stolen',
                    ack_claim_expires_at='2999-02-01T00:00:00+00:00'
                WHERE id='mail-job'
                """
            )
        connection.execute(
            """
            INSERT INTO inbound_mail_jobs(
                id, owner_id, intervention_id, source_notification_id,
                session_id, plan_id, mailbox_key, uidvalidity, uid, subject,
                body, payload_digest, state, execution_mode, outcome,
                ack_state, ack_attempt, ack_claim_token, ack_claim_expires_at
            ) VALUES (
                'expired-mail-job', 'local', 'intervention', 1, 'target', 1,
                'mailbox', 7, 12, 'subject', 'body', ?, 'queued', 'read_only',
                '', 'claimed', 1, 'mail-claim-expired',
                '2000-01-01T00:00:00+00:00'
            )
            """,
            (payload_digest,),
        )
        connection.execute(
            """
            UPDATE inbound_mail_jobs
            SET ack_attempt=2, ack_claim_token='mail-claim-recovered',
                ack_claim_expires_at='2999-02-01T00:00:00+00:00'
            WHERE id='expired-mail-job'
            """
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="inbound_mail_jobs.owner_id",
        ):
            connection.execute(
                """
                INSERT INTO inbound_mail_jobs(
                    id, owner_id, intervention_id, source_notification_id,
                    session_id, plan_id, mailbox_key, uidvalidity, uid, subject,
                    body, payload_digest, state, execution_mode, outcome,
                    ack_state, ack_attempt
                ) VALUES (
                    'duplicate-mail-uid', 'local', 'intervention', 1, 'target', 1,
                    'mailbox', 7, 12, 'subject', 'body', ?, 'queued', 'read_only',
                    '', 'pending', 0
                )
                """,
                (payload_digest,),
            )
        connection.execute(
            """
            INSERT INTO inbound_mail_jobs(
                id, owner_id, intervention_id, source_notification_id,
                session_id, plan_id, mailbox_key, uidvalidity, uid, subject,
                body, payload_digest, state, execution_mode, outcome,
                ack_state, ack_attempt
            ) VALUES (
                'new-uidvalidity-mail', 'local', 'intervention', 1, 'target', 1,
                'mailbox', 8, 12, 'subject', 'body', ?, 'queued', 'read_only',
                '', 'pending', 0
            )
            """,
            (payload_digest,),
        )

        with pytest.raises(sqlite3.IntegrityError, match="compression identity"):
            connection.execute(
                """
                UPDATE session_compression_states SET owner_id='foreign'
                WHERE session_id='target'
                """
            )

        # The durable Memory row cannot be edited, deleted, advanced while
        # retaining a stale provenance version, or paired with a forged event.
        with pytest.raises(sqlite3.IntegrityError, match="memory identity and content"):
            connection.execute("UPDATE memories SET content='escaped' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError, match="memory provenance pointer mismatch"):
            connection.execute(
                """
                UPDATE memories
                SET status='archived', archived_from_status='confirmed',
                    archived_reason='manual', lifecycle_reason_code='manual_archive',
                    lifecycle_version=2
                WHERE id=1
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="memory lifecycle fact or scope"):
            connection.execute(
                """
                INSERT INTO memory_lifecycle_events(
                    owner_id, memory_id, version, event_type, action_key,
                    from_status, to_status, from_validity_state,
                    to_validity_state, reason_code
                ) VALUES (
                    'local', 1, 2, 'archived', 'forged-memory-event',
                    'confirmed', 'archived', 'valid', 'valid', 'manual_archive'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="durable lifecycle facts"):
            connection.execute("DELETE FROM memories WHERE id=2")
        connection.execute(
            """
            INSERT INTO memories(
                id, owner_id, scope, scope_id, layer, content, source_type,
                source_id, confidence, status, archived_reason,
                lifecycle_reason_code, validity_state, content_hash,
                provenance_digest, lifecycle_version, access_count
            ) VALUES (
                3, 'local', 'global', NULL, 'semantic', 'legacy raw memory',
                'legacy', NULL, 0.5, 'confirmed', '', 'legacy_unverified',
                'legacy_unverified', ?, '', 1, 0
            )
            """,
            (hashlib.sha256(b"legacy raw memory").hexdigest(),),
        )
        connection.execute(
            """
            INSERT INTO memory_lifecycle_events(
                owner_id, memory_id, version, event_type, action_key,
                from_status, to_status, from_validity_state,
                to_validity_state, reason_code
            ) VALUES (
                'local', 3, 1, 'legacy_import', 'raw-legacy-memory-event',
                NULL, 'confirmed', NULL, 'legacy_unverified', 'legacy_unverified'
            )
            """
        )

        # Compression cursor and claim message identities are session-bound.
        connection.execute(
            """
            INSERT INTO chat_messages(
                id, session_id, role, content, version, content_hash,
                validity_state, invalidation_reason, message_metadata
            ) VALUES (4, 'source', 'user', 'foreign cursor', 1, ?, 'active', '', '{}')
            """,
            (hashlib.sha256(b"foreign cursor").hexdigest(),),
        )
        with pytest.raises(sqlite3.IntegrityError, match="compression target scope"):
            connection.execute(
                """
                UPDATE session_compression_states
                SET covered_through_message_id=4, covered_through_message_version=1
                WHERE session_id='target'
                """
            )
        foreign_manifest = json.dumps(
            [
                {
                    "id": 4,
                    "version": 1,
                    "role": "user",
                    "content_digest": hashlib.sha256(b"foreign cursor").hexdigest(),
                    "source_bytes": len(b"foreign cursor"),
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="compression target scope"):
            connection.execute(
                """
                UPDATE session_compression_states
                SET claim_token='cross-session-claim', claim_owner='worker',
                    claim_generation=generation, claim_start_message_id=4,
                    claim_end_message_id=4, claim_source_manifest=?,
                    claim_source_digest=?, claim_algorithm_version='h5-test-v1',
                    claim_started_at='2026-01-01T00:00:00+00:00',
                    claim_expires_at='2026-01-01T00:05:00+00:00'
                WHERE session_id='target'
                """,
                (foreign_manifest, hashlib.sha256(foreign_manifest.encode()).hexdigest()),
            )

        # A node cannot pair plan A with a plan B Session. A global Session
        # remains a valid global provenance scope.
        connection.execute(
            """
            INSERT INTO plans(
                id, owner_id, title, description, goal, current_level,
                weekly_minutes, preferences, expected_outcome,
                available_resources, avoid_methods, status, version, progress,
                memory_summary
            ) VALUES (
                2, 'local', 'Other plan', '', '', '', 0, '{}', '', '[]',
                '[]', 'active', 1, 0, ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(
                id, owner_id, plan_id, parent_session_id, title, summary,
                handoff_summary
            ) VALUES ('other-plan-session', 'local', 2, NULL, 'Other', '', '')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="provenance node scope mismatch"):
            connection.execute(
                """
                INSERT INTO provenance_nodes(
                    id, owner_id, plan_id, session_id, kind, entity_key,
                    entity_version, content_digest, metadata
                ) VALUES (
                    'cross-plan-node', 'local', 1, 'other-plan-session',
                    'message', 'cross-plan', 1, ?, '{}'
                )
                """,
                ("5" * 64,),
            )
        connection.execute(
            """
            INSERT INTO provenance_nodes(
                id, owner_id, plan_id, session_id, kind, entity_key,
                entity_version, content_digest, metadata
            ) VALUES (
                'global-session-node', 'local', NULL, 'source',
                'message', 'global-message', 1, ?, '{}'
            )
            """,
            ("6" * 64,),
        )

        # New H5 snapshot blocks are fully normalized; NULL source identity is
        # never a compatibility escape hatch.
        with pytest.raises(
            sqlite3.IntegrityError,
            match="context_snapshot_blocks.source_node_id",
        ):
            connection.execute(
                """
                INSERT INTO context_snapshot_blocks(
                    owner_id, snapshot_id, source_node_id, disposition, ordinal,
                    block_type, source_type, source_id, source_version,
                    source_digest, block_digest, token_count, priority,
                    reason_code, metadata
                ) VALUES (
                    'local', 1, NULL, 'retained', 0, 'profile', 'profile',
                    'local', 1, '', ?, 1, 0, '', '{}'
                )
                """,
                ("7" * 64,),
            )


@pytest.mark.parametrize(
    "phase",
    (
        "after_h5_summary_handoff_backfill",
        "after_h5_intervention_backfill",
        "after_h5_semantic_backfill",
        "after_h5_schema_triggers",
        "after_history",
        "after_main_replace",
        "after_publish_verify",
    ),
)
def test_revision_four_sigkill_converges_without_duplicate_h5_facts(
    phase: str,
    tmp_path: Path,
) -> None:
    database = tmp_path / f"h4-kill-{phase}.sqlite3"
    materialize_frozen_h4(database, tmp_path / "bootstrap-backups")
    _seed_revision_four_context(database)
    backup_root = tmp_path / "upgrade-backups"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            H5_MIGRATION_KILL_PROGRAM,
            str(database),
            str(backup_root),
            phase,
        ],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "backend")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL, (
        f"H5 migration did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    observed = verify_sqlite_database(database)
    assert observed["schema_checksum"] in {FROZEN_H4_SCHEMA_CHECKSUM, H5_CHECKSUM}

    migrate_sqlite_database(database, backup_root=backup_root)
    with sqlite3.connect(database) as connection:
        lifecycle_count = connection.execute(
            "SELECT count(*) FROM memory_lifecycle_events WHERE memory_id=1"
        ).fetchone()[0]
        handoff_count = connection.execute(
            "SELECT count(*) FROM session_handoffs WHERE target_session_id='target'"
        ).fetchone()[0]
        history = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert lifecycle_count == 1
    assert handoff_count == 1
    assert history == [
        (revision.version, revision.name, revision.checksum)
        for revision in MIGRATION_REGISTRY
    ]
