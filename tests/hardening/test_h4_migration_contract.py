"""Revision-3 to revision-4 Evidence/Competency migration contracts."""

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

from app.db.database import Base
from app.db.migrations import (
    MIGRATION_REGISTRY,
    MigrationError,
    migrate_sqlite_database,
    schema_checksum,
    verify_sqlite_database,
)
from hardening.h4_schema_fixture import (
    FROZEN_H3_SCHEMA_CHECKSUM,
    materialize_frozen_h3,
)
import app.models  # noqa: F401,E402 - registers tables and create_all triggers


H5_CHECKSUM = "878d69dc13324434678716be6c4d05bfa77ff80c25e16d157576ec6a5458ec45"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


H4_MIGRATION_KILL_PROGRAM = r"""
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


def _insert_plan_scope(connection: sqlite3.Connection, *, plan_id: int = 1) -> None:
    connection.execute(
        """
        INSERT INTO plans(
            id, owner_id, title, description, goal, current_level,
            weekly_minutes, preferences, expected_outcome,
            available_resources, avoid_methods, status, version, progress,
            memory_summary
        ) VALUES (?, 'local', 'H4 fixture', '', '', '', 0, '{}', '', '[]',
                  '[]', 'active', 1, 0, '')
        """,
        (plan_id,),
    )
    connection.execute(
        """
        INSERT INTO stages(id, plan_id, title, description, objectives, position, status)
        VALUES (?, ?, 'stage', '', '[]', 0, 'active')
        """,
        (plan_id, plan_id),
    )
    connection.execute(
        """
        INSERT INTO tasks(
            id, stage_id, title, description, kind, status, is_core,
            evidence_required, estimated_minutes, position, resource_url,
            task_metadata
        ) VALUES (?, ?, 'task', '', 'learning', 'completed', 1, 1, 30, 0,
                  '', '{}')
        """,
        (plan_id, plan_id),
    )


def _seed_revision_three_facts(path: Path) -> None:
    body = "print('durable')"
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    artifact_refs = json.dumps([
        {
            "artifact_id": 1,
            "kind": "submission",
            "uri": "submission:1",
            "content_hash": body_hash,
        }
    ])
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'H4 fixture', 'UTC')"
        )
        _insert_plan_scope(connection)
        connection.execute(
            """
            INSERT INTO sessions(id, owner_id, plan_id, title, summary, handoff_summary)
            VALUES ('session-h4', 'local', 1, 'H4', '', '')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, owner_id, session_id, plan_id, trigger, objective, status,
                model, cancel_requested, output
            ) VALUES ('run-h4', 'local', 'session-h4', 1, 'user_message',
                      'H4 fixture', 'queued', 'fixture', 0, '')
            """
        )
        connection.execute(
            """
            INSERT INTO task_submissions(
                id, owner_id, plan_id, task_id, run_id, submission_type,
                content, artifacts, status, score, feedback
            ) VALUES (1, 'local', 1, 1, 'run-h4', 'code', ?, '[]',
                      'accepted', 95, 'ok')
            """,
            (body,),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                id, owner_id, artifact_type, source_uri, title, content_hash,
                size_bytes, metadata, plan_id, task_id, run_id, session_id,
                idempotency_key
            ) VALUES (1, 'local', 'submission', 'submission:1', 'submission', ?,
                      ?, ?, 1, 1, 'run-h4', 'session-h4', 'artifact:stored')
            """,
            (body_hash, len(body.encode()), json.dumps({"language": "python"})),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                id, owner_id, artifact_type, source_uri, title, content_hash,
                metadata, plan_id, task_id, run_id, session_id, idempotency_key
            ) VALUES (2, 'local', 'file', 'file:///deleted-answer.py', 'file', ?,
                      '{}', 1, 1, 'run-h4', 'session-h4', 'artifact:missing')
            """,
            ("b" * 64,),
        )
        connection.execute(
            """
            INSERT INTO competencies(
                id, owner_id, "key", title, description, competency_type,
                scope, plan_id, status, version
            ) VALUES (1, 'local', 'python.async', 'Async', '', 'skill',
                      'plan', 1, 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO task_competency_links(
                id, owner_id, task_id, competency_id, relation, target_stage,
                created_at
            ) VALUES (1, 'local', 1, 1, 'assesses', 'demonstrated',
                      '2026-01-01T00:00:00.000000Z')
            """
        )
        connection.execute(
            """
            INSERT INTO evidence_observations(
                id, owner_id, source_type, source_id, run_id, session_id,
                plan_id, task_id, competency_id, competency_key, outcome,
                normalized_score, is_correct, assistance_level, transfer_level,
                artifact_refs, payload, occurred_at, recorded_at,
                idempotency_key
            ) VALUES (1, 'local', 'submission', '1:check', 'run-h4',
                      'session-h4', 1, 1, 1, 'python.async', 'accepted', .95,
                      1, 'independent', 'same_task', ?, '{}',
                      '2026-01-02T00:00:00.000000Z',
                      '2026-01-02T00:00:00.000000Z', 'evidence:one')
            """,
            (artifact_refs,),
        )
        connection.execute(
            """
            INSERT INTO evidence_observations(
                id, owner_id, source_type, source_id, run_id, session_id,
                plan_id, task_id, outcome, normalized_score, is_correct,
                assistance_level, transfer_level, artifact_refs, payload,
                occurred_at, recorded_at, idempotency_key, supersedes_id,
                invalidated_at, invalidation_reason
            ) VALUES (2, 'local', 'quiz', 'grade:replacement', 'run-h4',
                      'session-h4', 1, 1, 'passed', 1, 1, 'independent',
                      'transfer', ?, '{}', '2026-01-03T00:00:00.000000Z',
                      '2026-01-03T00:00:00.000000Z', 'evidence:two', 1,
                      '2026-01-04T00:00:00.000000Z', 'retracted')
            """,
            (artifact_refs,),
        )
        connection.execute(
            """
            INSERT INTO operations(
                id, owner_id, run_id, tool_name, entity_type, entity_id,
                forward_patch, inverse_patch, status
            ) VALUES ('operation-submission', 'local', 'run-h4',
                      'submission.check', 'submission', '1', '{}', '{}',
                      'committed')
            """
        )


def _schema_rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()


def test_fresh_and_revision_three_upgrade_have_identical_sqlite_schema(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh.sqlite3"
    upgraded = tmp_path / "upgraded.sqlite3"
    migrate_sqlite_database(fresh, backup_root=tmp_path / "fresh-backups")
    materialize_frozen_h3(upgraded, tmp_path / "bootstrap-backups")
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
    assert history[:3] == prior_history
    assert [row[:3] for row in history] == [
        (revision.version, revision.name, revision.checksum)
        for revision in MIGRATION_REGISTRY
    ]


def test_metadata_create_all_installs_the_canonical_trigger_set(tmp_path: Path) -> None:
    metadata_database = tmp_path / "metadata.sqlite3"
    migrated_database = tmp_path / "migrated.sqlite3"
    engine = create_engine(f"sqlite:///{metadata_database}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    migrate_sqlite_database(
        migrated_database,
        backup_root=tmp_path / "migrated-backups",
    )

    def triggers(path: Path) -> list[tuple[str, str]]:
        with sqlite3.connect(path) as connection:
            return connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='trigger' ORDER BY name"
            ).fetchall()

    assert schema_checksum(metadata_database) == H5_CHECKSUM
    assert triggers(metadata_database) == triggers(migrated_database)


def test_h4_scope_triggers_reject_cross_owner_source_and_graph_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scope-triggers.sqlite3"
    migrate_sqlite_database(database, backup_root=tmp_path / "backups")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executemany(
            "INSERT INTO owners(id, display_name, timezone) VALUES (?, ?, 'UTC')",
            (("local", "local"), ("foreign", "foreign")),
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id, owner_id, trigger, objective, status, model,
                cancel_requested, output
            ) VALUES ('local-run', 'local', 'user_message', 'scope', 'queued',
                      'fixture', 0, '')
            """
        )
        connection.execute(
            """
            INSERT INTO operations(
                id, owner_id, run_id, tool_name, entity_type, entity_id,
                forward_patch, inverse_patch, status
            ) VALUES ('local-operation', 'local', 'local-run', 'fixture',
                      'fixture', '1', '{}', '{}', 'committed')
            """
        )
        connection.execute(
            "INSERT INTO competency_graph_states(owner_id, revision) VALUES ('foreign', 0)"
        )

        with pytest.raises(sqlite3.IntegrityError, match="evidence source scope mismatch"):
            connection.execute(
                """
                INSERT INTO evidence_observations(
                    owner_id, source_type, source_id, run_id, outcome,
                    occurred_at, idempotency_key
                ) VALUES ('foreign', 'manual', 'cross-owner', 'local-run',
                          'observed', '2026-01-01T00:00:00.000000Z',
                          'cross-owner-evidence')
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="artifact source scope mismatch"):
            connection.execute(
                """
                INSERT INTO artifacts(
                    owner_id, artifact_type, source_uri, title, content_hash,
                    metadata, run_id, idempotency_key
                ) VALUES ('foreign', 'link', 'https://example.invalid', 'x', ?,
                          '{}', 'local-run', 'cross-owner-artifact')
                """,
                ("a" * 64,),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="competency graph mutation owner mismatch",
        ):
            connection.execute(
                """
                INSERT INTO competency_graph_mutations(
                    owner_id, revision, operation_id, action_key, action,
                    entity_type, entity_id
                ) VALUES ('foreign', 1, 'local-operation', 'foreign:bad',
                          'apply', 'competency', '1')
                """
            )


def test_revision_three_semantic_backfill_is_conservative_and_complete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "h3-facts.sqlite3"
    materialize_frozen_h3(database, tmp_path / "bootstrap-backups")
    _seed_revision_three_facts(database)

    report = migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")

    assert report.version == 5
    assert report.target_schema_checksum == H5_CHECKSUM
    assert verify_sqlite_database(database, expected_schema_checksum=H5_CHECKSUM) == {
        "integrity": ["ok"],
        "foreign_key_violation_count": 0,
        "user_version": 5,
        "schema_checksum": H5_CHECKSUM,
    }
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        artifacts = connection.execute(
            """
            SELECT id, content_hash, snapshot_bytes, snapshot_sha256,
                   storage_state, envelope_version
            FROM artifacts ORDER BY id
            """
        ).fetchall()
        facts = connection.execute(
            """
            SELECT id, fact_kind, target_observation_id, evidence_role,
                   eligibility_stage, counts_as_success
            FROM evidence_observations ORDER BY id
            """
        ).fetchall()
        artifact_links = connection.execute(
            "SELECT observation_id, artifact_id FROM evidence_artifact_links ORDER BY observation_id"
        ).fetchall()
        competency_links = connection.execute(
            """
            SELECT observation_id, competency_id, association_kind
            FROM evidence_competency_links ORDER BY observation_id
            """
        ).fetchall()
        operation_links = connection.execute(
            """
            SELECT operation_id, observation_id, generation, role
            FROM operation_evidence_links
            """
        ).fetchall()
        graph = connection.execute(
            "SELECT owner_id, revision FROM competency_graph_states"
        ).fetchone()
        projection = connection.execute(
            """
            SELECT owner_id, plan_id, watermark, algorithm_version
            FROM evidence_projection_states
            """
        ).fetchone()

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE evidence_observations SET outcome='failed' WHERE id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM artifacts WHERE id=1")

    expected_envelope = hashlib.sha256(json.dumps(
        {"content": "print('durable')", "metadata": {"language": "python"}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    assert tuple(artifacts[0]) == (
        1,
        expected_envelope,
        b"print('durable')",
        hashlib.sha256(b"print('durable')").hexdigest(),
        "stored",
        1,
    )
    assert tuple(artifacts[1]) == (2, "b" * 64, None, None, "legacy_unavailable", 0)
    assert [tuple(row) for row in facts] == [
        (1, "observation", None, "primary", "demonstrated", 1),
        (2, "amendment", 1, "primary", "demonstrated", 1),
        (3, "invalidation", 2, "control", "unknown", 0),
    ]
    assert [tuple(row) for row in artifact_links] == [(1, 1), (2, 1)]
    assert [tuple(row) for row in competency_links] == [
        (1, 1, "legacy"),
        (2, 1, "task_assesses"),
    ]
    assert tuple(operation_links[0]) == ("operation-submission", 1, 0, "produced")
    assert tuple(graph) == ("local", 1)
    assert tuple(projection) == ("local", 1, 0, "evidence-projection-v1")


def test_revision_three_assessment_backfill_uses_occurrence_time_not_record_time(
    tmp_path: Path,
) -> None:
    database = tmp_path / "h3-assessment-time-boundary.sqlite3"
    materialize_frozen_h3(database, tmp_path / "bootstrap-backups")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'H4 fixture', 'UTC')"
        )
        _insert_plan_scope(connection)
        connection.execute(
            """
            INSERT INTO competencies(
                id, owner_id, "key", title, description, competency_type,
                scope, plan_id, status, version
            ) VALUES (1, 'local', 'python.temporal', 'Temporal', '', 'skill',
                      'plan', 1, 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO task_competency_links(
                id, owner_id, task_id, competency_id, relation, target_stage,
                created_at
            ) VALUES (1, 'local', 1, 1, 'assesses', 'demonstrated',
                      '2026-01-02T00:00:00.000000Z')
            """
        )
        connection.execute(
            """
            INSERT INTO evidence_observations(
                id, owner_id, source_type, source_id, plan_id, task_id,
                outcome, artifact_refs, payload, occurred_at, recorded_at,
                idempotency_key
            ) VALUES (1, 'local', 'quiz', 'temporal:grade', 1, 1,
                      'passed', '[]', '{}',
                      '2026-01-01T00:00:00.000000Z',
                      '2026-01-03T00:00:00.000000Z', 'evidence:temporal')
            """
        )

    migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT observation_id, competency_id FROM evidence_competency_links"
        ).fetchall() == []


def test_revision_three_branch_conflict_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "h3-branch.sqlite3"
    materialize_frozen_h3(database, tmp_path / "bootstrap-backups")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'branch', 'UTC')"
        )
        rows = [
            (1, "root", None),
            (2, "branch-a", 1),
            (3, "branch-b", 1),
        ]
        connection.executemany(
            """
            INSERT INTO evidence_observations(
                id, owner_id, source_type, source_id, outcome, occurred_at,
                idempotency_key, supersedes_id
            ) VALUES (?, 'local', 'quiz', ?, 'passed',
                      '2026-01-01T00:00:00.000000Z', ?, ?)
            """,
            [(row_id, source_id, f"branch:{row_id}", target) for row_id, source_id, target in rows],
        )

    with pytest.raises(MigrationError) as caught:
        migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")
    assert caught.value.code == "legacy_evidence_branch_conflict"
    assert schema_checksum(database) == FROZEN_H3_SCHEMA_CHECKSUM


def test_revision_three_cross_plan_edge_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "h3-cross-plan-edge.sqlite3"
    materialize_frozen_h3(database, tmp_path / "bootstrap-backups")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'edge', 'UTC')"
        )
        _insert_plan_scope(connection, plan_id=1)
        _insert_plan_scope(connection, plan_id=2)
        connection.executemany(
            """
            INSERT INTO competencies(
                id, owner_id, "key", title, description, competency_type,
                scope, plan_id, status, version
            ) VALUES (?, 'local', ?, ?, '', 'skill', 'plan', ?, 'active', 1)
            """,
            ((1, "plan.one", "one", 1), (2, "plan.two", "two", 2)),
        )
        connection.execute(
            """
            INSERT INTO competency_edges(
                id, owner_id, source_id, target_id, relation, version
            ) VALUES (1, 'local', 1, 2, 'related_to', 1)
            """
        )

    with pytest.raises(MigrationError) as caught:
        migrate_sqlite_database(database, backup_root=tmp_path / "upgrade-backups")
    assert caught.value.code == "legacy_competency_edge_scope_conflict"
    assert schema_checksum(database) == FROZEN_H3_SCHEMA_CHECKSUM


@pytest.mark.parametrize(
    "phase",
    (
        "after_h4_semantic_backfill",
        "after_history",
        "after_main_replace",
        "after_publish_verify",
    ),
)
def test_revision_three_sigkill_converges_without_duplicate_derived_facts(
    phase: str,
    tmp_path: Path,
) -> None:
    database = tmp_path / f"h3-kill-{phase}.sqlite3"
    materialize_frozen_h3(database, tmp_path / "bootstrap-backups")
    _seed_revision_three_facts(database)
    backup_root = tmp_path / "upgrade-backups"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            H4_MIGRATION_KILL_PROGRAM,
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
        f"H4 migration did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    observed = verify_sqlite_database(database)
    assert observed["schema_checksum"] in {FROZEN_H3_SCHEMA_CHECKSUM, H5_CHECKSUM}

    migrate_sqlite_database(database, backup_root=backup_root)
    with sqlite3.connect(database) as connection:
        derived = connection.execute(
            """
            SELECT count(*) FROM evidence_observations
            WHERE fact_kind='invalidation' AND target_observation_id=2
            """
        ).fetchone()[0]
        history = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert derived == 1
    assert history == [
        (revision.version, revision.name, revision.checksum)
        for revision in MIGRATION_REGISTRY
    ]
