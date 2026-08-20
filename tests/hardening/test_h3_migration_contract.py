"""H3 revision-2 to revision-3 durable Runtime migration contracts."""

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

from app.db.migrations import (
    MIGRATION_REGISTRY,
    migrate_sqlite_database,
    schema_checksum,
    verify_sqlite_database,
)
from hardening.h3_schema_fixture import FROZEN_H2_SCHEMA_CHECKSUM, materialize_frozen_h2


H3_CHECKSUM = "b69ed9f0844106e54936e008c22b4d4ebd7e089a8cb38989a4306ad25c7239de"
H5_CHECKSUM = "878d69dc13324434678716be6c4d05bfa77ff80c25e16d157576ec6a5458ec45"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


H3_MIGRATION_KILL_PROGRAM = r"""
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


def _h2_idempotency_key(run_id: str, tool_name: str, tool_call_id: str) -> str:
    digest = hashlib.sha256(
        f"{run_id}|{tool_name}|provider:{tool_call_id}".encode("utf-8")
    ).hexdigest()
    return f"{run_id[:8]}:{tool_name}:{digest[:48]}"


def _seed_revision_two_runtime(path: Path) -> None:
    safe_checkpoint = json.dumps({
        "step": 2,
        "messages": [{"role": "user", "content": "resume safely"}],
        "pending_tool_calls": [],
    })
    ambiguous_checkpoint = json.dumps({
        "step": 2,
        "messages": [{"role": "user", "content": "which call was current?"}],
        "pending_tool_calls": [{"id": "later", "name": "plan_list", "arguments": "{}"}],
    })
    later_call = {"id": "later", "name": "plan_list", "arguments": "{}"}
    current_checkpoint = json.dumps({
        "step": 3,
        "messages": [{
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "current", "type": "function", "function": {
                    "name": "plan_create", "arguments": "{}",
                }},
                {"id": "later", "type": "function", "function": {
                    "name": "plan_list", "arguments": "{}",
                }},
            ],
        }],
        "pending_tool_calls": [later_call],
    })
    after_result_checkpoint = json.dumps({
        "step": 3,
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "current", "type": "function", "function": {
                        "name": "plan_create", "arguments": "{}",
                    }},
                    {"id": "later", "type": "function", "function": {
                        "name": "plan_list", "arguments": "{}",
                    }},
                ],
            },
            {"role": "tool", "tool_call_id": "current", "content": "{}"},
        ],
        "pending_tool_calls": [later_call],
    })
    invalid_versioned_checkpoint = json.dumps({"schema_version": 1, "step": 7})
    approval = {
        "tool_call": {"id": "approval-call", "name": "plan_create", "arguments": "{}"},
        "remaining_tool_calls": [],
        "reason": "explicit approval required",
        "step": 1,
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'H2 fixture', 'UTC')"
        )
        rows = [
            ("fresh", "queued", None, None, ""),
            ("running-missing", "running", None, None, ""),
            ("running-safe", "running", safe_checkpoint, None, ""),
            ("running-current", "running", current_checkpoint, None, ""),
            ("running-after-result", "running", after_result_checkpoint, None, ""),
            ("running-ambiguous", "running", ambiguous_checkpoint, None, ""),
            ("versioned-invalid", "running", invalid_versioned_checkpoint, None, ""),
            ("approval-wait", "waiting_approval", safe_checkpoint, json.dumps(approval), ""),
            ("approval-missing", "waiting_approval", safe_checkpoint, None, ""),
            ("approval-unproven", "waiting_approval", safe_checkpoint, json.dumps(approval), ""),
            ("approval-queued", "queued", safe_checkpoint, json.dumps(approval), ""),
            ("split-final", "running", None, None, "durable answer"),
        ]
        connection.executemany(
            "INSERT INTO sessions(id, owner_id, title, summary, handoff_summary, created_at, updated_at) "
            "VALUES (?, 'local', 'fixture', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            [(f"session-{run_id}",) for run_id, *_ in rows],
        )
        connection.executemany(
            """
            INSERT INTO agent_runs(
                id, owner_id, session_id, trigger, objective, status, model,
                cancel_requested, checkpoint, pending_approval, output, created_at
            ) VALUES (?, 'local', ?, 'user_message', ?, ?, 'h2', 0, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                (run_id, f"session-{run_id}", run_id, status, checkpoint, pending, output)
                for run_id, status, checkpoint, pending, output in rows
            ],
        )
        connection.execute(
            "INSERT INTO sessions(id, owner_id, title, summary, handoff_summary, created_at, updated_at) "
            "VALUES ('session-conflict', 'local', 'conflict', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.executemany(
            """
            INSERT INTO agent_runs(
                id, owner_id, session_id, trigger, objective, status, model,
                cancel_requested, output, created_at
            ) VALUES (?, 'local', ?, ?, ?, 'queued', 'h2', 0, '', CURRENT_TIMESTAMP)
            """,
            (
                ("session-conflict-a", "session-conflict", "user_message", "first ambiguous root"),
                ("session-conflict-b", "session-conflict", "email_reply", "second ambiguous root"),
                ("stateless-conflict-a", None, "heartbeat", "first ambiguous stateless root"),
                ("stateless-conflict-b", None, "manual_heartbeat", "second ambiguous stateless root"),
            ),
        )
        connection.execute(
            """
            INSERT INTO tool_invocations(
                owner_id, run_id, idempotency_key, tool_name, args_hash,
                request_digest, canonical_args, effect_kind, status,
                result_payload, attempt, version
            ) VALUES ('local', 'approval-wait', ?, 'plan_create', ?, ?, '{}',
                      'database_write', 'pending_approval', '{}', 1, 2)
            """,
            (
                _h2_idempotency_key("approval-wait", "plan_create", "approval-call"),
                hashlib.sha256(b"{}").hexdigest(),
                hashlib.sha256(b"{}").hexdigest(),
            ),
        )
        connection.execute(
            """
            INSERT INTO tool_invocations(
                owner_id, run_id, idempotency_key, tool_name, args_hash,
                request_digest, canonical_args, effect_kind, status,
                result_payload, attempt, version
            ) VALUES ('local', 'approval-unproven', 'wrong-provider-identity',
                      'plan_create', ?, ?, '{}', 'database_write',
                      'pending_approval', '{}', 1, 2)
            """,
            (hashlib.sha256(b"{}").hexdigest(), hashlib.sha256(b"{}").hexdigest()),
        )
        connection.execute(
            """
            INSERT INTO chat_messages(session_id, run_id, role, content, message_metadata)
            VALUES ('session-split-final', 'split-final', 'assistant', 'durable answer', '{}')
            """
        )
        connection.executemany(
            """
            INSERT INTO queued_messages(
                id, owner_id, session_id, trigger, objective, message_metadata, position
            ) VALUES (?, 'local', 'session-fresh', 'user_message', ?, '{}', ?)
            """,
            (("queue-b", "second", 7), ("queue-a", "first", 7)),
        )


def test_revision_two_runtime_migrates_conservatively_and_repeatably(tmp_path: Path) -> None:
    database = tmp_path / "h2-runtime.sqlite3"
    materialize_frozen_h2(database, tmp_path / "bootstrap-backups")
    _seed_revision_two_runtime(database)

    report = migrate_sqlite_database(
        database,
        backup_root=tmp_path / "upgrade-backups",
        database_identity=database.name,
    )

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
        runs = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT id, status, phase, status_reason, checkpoint, checkpoint_schema_version "
                "FROM agent_runs ORDER BY id"
            )
        }
        approval = connection.execute(
            "SELECT run_id, tool_call_id, decision, decided_at FROM run_approvals"
        ).fetchone()
        invocation = connection.execute(
            "SELECT tool_call_id, status FROM tool_invocations WHERE run_id='approval-wait'"
        ).fetchone()
        final_message = connection.execute(
            "SELECT message_key FROM chat_messages WHERE run_id='split-final'"
        ).fetchone()
        queue = connection.execute(
            "SELECT id, position, version FROM queued_messages ORDER BY position"
        ).fetchall()
        history = connection.execute(
            "SELECT version, name, checksum, result FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert (runs["fresh"]["status"], runs["fresh"]["phase"]) == ("queued", "not_started")
    assert (runs["running-missing"]["status"], runs["running-missing"]["status_reason"]) == (
        "needs_reconciliation",
        "legacy_running_without_checkpoint",
    )
    assert runs["running-missing"]["phase"] == "reconciling"
    safe = json.loads(runs["running-safe"]["checkpoint"])
    assert (runs["running-safe"]["status"], safe["phase"], safe["messages"][0]["content"]) == (
        "queued",
        "awaiting_model",
        "resume safely",
    )
    assert runs["running-ambiguous"]["status_reason"] == "legacy_current_tool_ambiguous"
    current = json.loads(runs["running-current"]["checkpoint"])
    assert (runs["running-current"]["status"], current["phase"]) == ("queued", "tool_ready")
    assert current["current_tool_call"] == {
        "id": "current", "name": "plan_create", "arguments": "{}",
    }
    expected_later_call = {"id": "later", "name": "plan_list", "arguments": "{}"}
    assert current["remaining_tool_calls"] == [expected_later_call]
    after_result = json.loads(runs["running-after-result"]["checkpoint"])
    assert after_result["current_tool_call"] == expected_later_call
    assert after_result["remaining_tool_calls"] == []
    assert (
        runs["versioned-invalid"]["status"],
        runs["versioned-invalid"]["phase"],
        runs["versioned-invalid"]["status_reason"],
    ) == ("needs_reconciliation", "reconciling", "legacy_checkpoint_envelope_invalid")
    invalid_envelope = json.loads(runs["versioned-invalid"]["checkpoint"])
    assert set(invalid_envelope) >= {
        "kind", "phase", "messages", "current_tool_call", "remaining_tool_calls",
        "cards", "budget_usage", "state_version",
    }
    assert approval[:3] == ("approval-wait", "approval-call", "pending")
    assert approval[3] is None
    assert tuple(invocation) == ("approval-call", "pending_approval")
    assert (
        runs["approval-missing"]["status"],
        runs["approval-missing"]["status_reason"],
    ) == ("needs_reconciliation", "legacy_approval_missing")
    assert (
        runs["approval-unproven"]["status"],
        runs["approval-unproven"]["status_reason"],
    ) == ("needs_reconciliation", "legacy_approval_invocation_unproven")
    assert runs["approval-queued"]["status_reason"] == "legacy_approval_decision_missing"
    for run_id in (
        "session-conflict-a",
        "session-conflict-b",
        "stateless-conflict-a",
        "stateless-conflict-b",
    ):
        assert (runs[run_id]["status"], runs[run_id]["status_reason"]) == (
            "needs_reconciliation",
            "legacy_active_root_conflict",
        )
    final_checkpoint = json.loads(runs["split-final"]["checkpoint"])
    assert (runs["split-final"]["status"], final_checkpoint["phase"], final_checkpoint["final_text"]) == (
        "queued",
        "finalizing",
        "durable answer",
    )
    assert tuple(final_message) == ("run:split-final:final",)
    assert [tuple(row) for row in queue] == [("queue-a", 0, 1), ("queue-b", 1, 1)]
    assert [tuple(row) for row in history] == [
        (revision.version, revision.name, revision.checksum, "applied")
        for revision in MIGRATION_REGISTRY
    ]

    second = migrate_sqlite_database(
        database,
        backup_root=tmp_path / "upgrade-backups",
        database_identity=database.name,
    )
    assert second.applied is False


def test_revision_three_schema_constraints_and_queue_indexes_are_executable(tmp_path: Path) -> None:
    database = tmp_path / "h3-constraints.sqlite3"
    migrate_sqlite_database(database, backup_root=tmp_path / "constraint-backups")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO owners(id, display_name, timezone) VALUES ('local', 'constraints', 'UTC')"
        )
        connection.execute(
            "INSERT INTO sessions(id, owner_id, title, summary, handoff_summary, created_at, updated_at) "
            "VALUES ('session', 'local', 'constraints', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        base = """
            INSERT INTO agent_runs(
                id, owner_id, session_id, trigger, objective, status, phase,
                state_version, model, cancel_requested, attempt, retry_count,
                output, created_at, updated_at
            ) VALUES (?, 'local', 'session', 'user_message', 'probe', ?, ?,
                      1, 'h3', 0, 0, 0, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(base, ("bad-phase", "completed", "invented_phase"))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, owner_id, session_id, trigger, objective, status, phase,
                    state_version, model, cancel_requested, attempt, retry_count,
                    output, lease_token, created_at, updated_at
                ) VALUES ('bad-lease', 'local', 'session', 'user_message', 'probe',
                          'completed', 'terminal', 1, 'h3', 0, 0, 0, '',
                          'partial-lease', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, owner_id, session_id, trigger, objective, status, phase,
                    state_version, checkpoint, checkpoint_schema_version, model,
                    cancel_requested, attempt, retry_count, output, created_at, updated_at
                ) VALUES ('bad-checkpoint', 'local', 'session', 'user_message', 'probe',
                          'queued', 'awaiting_model', 1, '{"schema_version":1}', 1,
                          'h3', 0, 0, 0, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        valid_checkpoint = json.dumps({
            "schema_version": 1,
            "kind": "agent",
            "phase": "awaiting_model",
            "step": 0,
            "messages": [],
            "current_tool_call": None,
            "remaining_tool_calls": [],
            "current_invocation_id": None,
            "context_snapshot_id": None,
            "cards": [],
            "budget_usage": {},
            "state_version": 1,
        })
        connection.execute(base, ("checkpoint-consistency", "completed", "terminal"))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "UPDATE agent_runs SET checkpoint=? WHERE id='checkpoint-consistency'",
                (valid_checkpoint,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "UPDATE agent_runs SET checkpoint_schema_version=1 "
                "WHERE id='checkpoint-consistency'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(base, ("bad-wait", "waiting_approval", "waiting_approval"))

        connection.execute(base, ("approval-run", "queued", "not_started"))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO run_approvals(
                    id, owner_id, run_id, tool_call_id, tool_name, tool_call,
                    remaining_tool_calls, reason, decision, decided_at, created_at
                ) VALUES ('bad-approval', 'local', 'approval-run', 'call', 'plan_create',
                          '{}', '[]', '', 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO run_approvals(
                    id, owner_id, run_id, tool_call_id, tool_name, tool_call,
                    remaining_tool_calls, reason, decision, answer, decided_at, created_at
                ) VALUES ('bad-answer', 'local', 'approval-run', 'answer-call', 'ask_user',
                          '{}', '[]', '', 'answer', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        queue_insert = """
            INSERT INTO queued_messages(
                id, owner_id, session_id, trigger, objective, message_metadata,
                position, version, created_at, updated_at
            ) VALUES (?, 'local', ?, 'user_message', 'queued', '{}',
                      0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
        connection.execute(queue_insert, ("session-queue-a", "session"))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            connection.execute(queue_insert, ("session-queue-b", "session"))
        connection.execute(queue_insert, ("stateless-queue-a", None))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            connection.execute(queue_insert, ("stateless-queue-b", None))
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(queued_messages)")
        }
    assert {
        "uq_queued_messages_session_position",
        "uq_queued_messages_stateless_position",
        "ix_queued_messages_session_dequeue",
        "ix_queued_messages_stateless_dequeue",
    }.issubset(indexes)


def test_fresh_and_revision_two_upgrade_have_identical_sqlite_schema(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.sqlite3"
    upgraded = tmp_path / "upgraded.sqlite3"
    migrate_sqlite_database(fresh, backup_root=tmp_path / "fresh-backups")
    materialize_frozen_h2(upgraded, tmp_path / "h2-backups")
    migrate_sqlite_database(upgraded, backup_root=tmp_path / "upgrade-backups")

    def schema_rows(path: Path) -> list[tuple]:
        with sqlite3.connect(path) as connection:
            return connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()

    assert schema_checksum(fresh) == schema_checksum(upgraded) == H5_CHECKSUM
    assert schema_rows(fresh) == schema_rows(upgraded)


@pytest.mark.parametrize(
    "phase",
    ("after_copy", "after_history", "after_main_replace", "after_publish_verify"),
)
def test_frozen_h2_sigkill_converges_with_history_and_classification(
    phase: str,
    tmp_path: Path,
) -> None:
    database = tmp_path / f"h2-kill-{phase}.sqlite3"
    materialize_frozen_h2(database, tmp_path / "bootstrap-backups")
    _seed_revision_two_runtime(database)
    with sqlite3.connect(database) as connection:
        prior_history = connection.execute(
            "SELECT version, name, checksum, applied_at, result "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()

    backup_root = tmp_path / "upgrade-backups"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            H3_MIGRATION_KILL_PROGRAM,
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
        f"H3 migration did not reach {phase}; stdout={completed.stdout!r}, "
        f"stderr={completed.stderr!r}"
    )
    observed = verify_sqlite_database(database)
    assert observed["schema_checksum"] in {FROZEN_H2_SCHEMA_CHECKSUM, H5_CHECKSUM}
    report = migrate_sqlite_database(
        database,
        backup_root=backup_root,
        database_identity=database.name,
    )
    assert report.target_schema_checksum == H5_CHECKSUM
    with sqlite3.connect(database) as connection:
        final_history = connection.execute(
            "SELECT version, name, checksum, applied_at, result "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
        current = json.loads(connection.execute(
            "SELECT checkpoint FROM agent_runs WHERE id='running-current'"
        ).fetchone()[0])
    assert final_history[:2] == prior_history
    assert [row[:3] for row in final_history] == [
        (revision.version, revision.name, revision.checksum)
        for revision in MIGRATION_REGISTRY
    ]
    assert current["current_tool_call"]["id"] == "current"
