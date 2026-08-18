from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.database import Base
from app.db.migrations import migrate_sqlite_schema
from app.models import EvidenceObservation, Owner
from app.services.evidence import build_evidence_state


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "databases"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
FIXTURE_NAMES = ("empty", "v1_1_1_full", "review_boundary")

M13_TABLES = {
    "evidence_observations",
    "artifacts",
    "competencies",
    "competency_edges",
    "plan_competency_links",
    "task_competency_links",
    "resource_competency_links",
}

M13_EVIDENCE_TABLE_SQL = """
CREATE TABLE evidence_observations (
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


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _materialize_sql(name: str, target: Path) -> Path:
    source = FIXTURE_ROOT / f"{name}.sql"
    source_hash = _sha256(source)
    if not source.is_file():
        raise RuntimeError(f"fixture source is missing: {source}")
    if target.exists():
        raise RuntimeError(f"fixture target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as connection:
        connection.executescript(source.read_text(encoding="utf-8"))
        connection.execute("PRAGMA foreign_keys=ON")
    if _sha256(source) != source_hash:
        raise RuntimeError(f"fixture source changed while materializing: {source}")
    return target


def _schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, coalesce(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    payload = "\n".join("|".join(map(str, row)) for row in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_schema(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        result: dict[str, dict] = {}
        for table_name in table_names:
            quoted_table = table_name.replace('"', '""')
            columns = [tuple(row) for row in connection.execute(
                f'PRAGMA table_xinfo("{quoted_table}")'
            )]
            foreign_keys = sorted(
                (row[2], row[3], row[4], row[5], row[6], row[7])
                for row in connection.execute(f'PRAGMA foreign_key_list("{quoted_table}")')
            )
            indexes = []
            for index_row in connection.execute(f'PRAGMA index_list("{quoted_table}")'):
                index_name = index_row[1]
                quoted_index = index_name.replace('"', '""')
                index_columns = [
                    (row[0], row[1], row[2], row[3], row[4], row[5])
                    for row in connection.execute(f'PRAGMA index_xinfo("{quoted_index}")')
                ]
                index_sql_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (index_name,),
                ).fetchone()
                index_sql = " ".join((index_sql_row[0] or "").split()) if index_sql_row else ""
                indexes.append({
                    "name": index_name,
                    "unique": index_row[2],
                    "origin": index_row[3],
                    "partial": index_row[4],
                    "columns": index_columns,
                    "sql": index_sql,
                })
            result[table_name] = {
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": sorted(indexes, key=lambda value: value["name"]),
            }
        return result


def _sqlite_value_token(value: object) -> list[object]:
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    return [type(value).__name__, value]


def _business_data_snapshot(
    connection: sqlite3.Connection,
    table_names: tuple[str, ...],
    columns_by_table: dict[str, tuple[str, ...]] | None = None,
) -> tuple[dict[str, tuple[str, ...]], dict[str, int], dict[str, str]]:
    resolved_columns: dict[str, tuple[str, ...]] = {}
    cardinalities: dict[str, int] = {}
    content_digests: dict[str, str] = {}
    for table_name in table_names:
        quoted_table = table_name.replace('"', '""')
        if columns_by_table is None:
            columns = tuple(
                row[1]
                for row in connection.execute(f'PRAGMA table_xinfo("{quoted_table}")')
                if row[6] == 0
            )
        else:
            columns = columns_by_table[table_name]
        if not columns:
            raise RuntimeError(f"business table has no selectable columns: {table_name}")
        resolved_columns[table_name] = columns

        quoted_columns = (column.replace('"', '""') for column in columns)
        selected_columns = ", ".join(f'"{column}"' for column in quoted_columns)
        rows = connection.execute(
            f'SELECT {selected_columns} FROM "{quoted_table}"'
        ).fetchall()
        encoded_rows = sorted(
            json.dumps(
                [_sqlite_value_token(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for row in rows
        )
        cardinalities[table_name] = len(rows)
        content_digests[table_name] = hashlib.sha256(
            "\n".join(encoded_rows).encode("utf-8")
        ).hexdigest()
    return resolved_columns, cardinalities, content_digests


def _sqlite_health(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    connection.execute("PRAGMA foreign_keys=ON")
    return {
        "integrity_check": [tuple(row) for row in connection.execute("PRAGMA integrity_check")],
        "foreign_key_check": [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")],
    }


def _test_engine(path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=1000")
        cursor.close()

    return engine


async def _upgrade_current_schema(path: Path) -> None:
    engine = _test_engine(path)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await migrate_sqlite_schema(connection)
    finally:
        await engine.dispose()


def _subprocess_environment(database_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
        "OPENAI_API_KEY": "test-key",
        "ENABLE_SCHEDULER": "false",
        "SMTP_HOST": "",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "IMAP_HOST": "",
        "IMAP_USERNAME": "",
        "IMAP_PASSWORD": "",
        "VAPID_PRIVATE_KEY": "",
    }


def test_database_fixture_manifest_hashes_and_public_content():
    manifest = _manifest()
    assert manifest["manifest_version"] == 1
    assert manifest["contains_personal_data"] is False
    assert set(manifest["fixtures"]) == set(FIXTURE_NAMES)

    forbidden_patterns = (
        re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"(?i)(?:api[_-]?key|password|authorization[_-]?code)\s*[:=]"),
        re.compile(r"(?:/root/|/home/|[A-Za-z]:\\\\Users\\\\)"),
        re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    )
    for name, metadata in manifest["fixtures"].items():
        source = FIXTURE_ROOT / metadata["file"]
        content = source.read_text(encoding="utf-8")
        assert source.suffix == ".sql"
        assert source.stat().st_size == metadata["size_bytes"]
        assert _sha256(source) == metadata["sha256"]
        for pattern in forbidden_patterns:
            assert pattern.search(content) is None, f"{name} matched {pattern.pattern}"
        for url in re.findall(r"https?://[^'\"\s]+", content):
            assert url.startswith("https://example.invalid/")


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_database_fixture_materializes_in_two_isolated_temp_copies(name: str, tmp_path: Path):
    source = FIXTURE_ROOT / f"{name}.sql"
    before = _sha256(source)
    first = _materialize_sql(name, tmp_path / "first" / f"{name}.sqlite3")
    second = _materialize_sql(name, tmp_path / "second" / f"{name}.sqlite3")

    assert first != second
    assert first.is_relative_to(tmp_path)
    assert second.is_relative_to(tmp_path)
    assert _sha256(source) == before
    assert not list(FIXTURE_ROOT.glob("*.db"))
    assert not list(FIXTURE_ROOT.glob("*.sqlite3"))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_database_fixture_integrity_schema_and_counts(name: str, tmp_path: Path):
    metadata = _manifest()["fixtures"][name]
    database_path = _materialize_sql(name, tmp_path / f"{name}.sqlite3")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _schema_digest(connection) == metadata["schema_sha256"]
        for table_name, expected_count in metadata["table_counts"].items():
            quoted_table = table_name.replace('"', '""')
            actual_count = connection.execute(
                f'SELECT count(*) FROM "{quoted_table}"'
            ).fetchone()[0]
            assert actual_count == expected_count


def test_v1_1_1_fixture_covers_every_release_business_table(tmp_path: Path):
    metadata = _manifest()["fixtures"]["v1_1_1_full"]
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "v1.sqlite3")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == set(metadata["table_counts"])
    assert len(tables) == 30
    assert all(count > 0 for count in metadata["table_counts"].values())


def test_review_boundary_fixture_has_exact_isolated_cardinalities(tmp_path: Path):
    metadata = _manifest()["fixtures"]["review_boundary"]
    database_path = _materialize_sql("review_boundary", tmp_path / "boundary.sqlite3")
    with sqlite3.connect(database_path) as connection:
        actual = {
            str(plan_id): count
            for plan_id, count in connection.execute(
                """
                SELECT plans.id, count(evidence_observations.id)
                FROM plans
                LEFT JOIN evidence_observations ON evidence_observations.plan_id = plans.id
                GROUP BY plans.id
                ORDER BY plans.id
                """
            )
        }
        message_count = connection.execute("SELECT count(*) FROM chat_messages").fetchone()[0]
    assert actual == metadata["evidence_counts_by_plan"]
    assert message_count == 10000


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1-MIG-001: create_schema relies on callers importing app.models before fresh initialization",
)
def test_create_schema_registers_models_without_import_side_effects(tmp_path: Path):
    database_path = tmp_path / "fresh.sqlite3"
    command = (
        "import asyncio; "
        "from app.db.database import create_schema; "
        "asyncio.run(create_schema())"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(database_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    missing_plans_observed = (
        result.returncode != 0
        and "sqlite3.OperationalError: no such table: plans" in result.stderr
    )
    if result.returncode != 0 and not missing_plans_observed:
        raise RuntimeError(
            "isolated create_schema subprocess failed outside H1-MIG-001: "
            f"returncode={result.returncode}; stderr={result.stderr[-2000:]}"
        )
    with sqlite3.connect(database_path) as connection:
        owners_table_present = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='owners'"
        ).fetchone()[0] == 1

    observation = {
        "completed": result.returncode == 0,
        "missing_plans_error": missing_plans_observed,
        "owners_table_present": owners_table_present,
    }
    assert observation == {
        "completed": True,
        "missing_plans_error": False,
        "owners_table_present": True,
    }


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1-MIG-002: additive v1.1.1 upgrade leaves nullability, defaults, indexes, and order divergent from fresh schema",
)
@pytest.mark.asyncio
async def test_v1_1_1_upgrade_schema_matches_fresh_schema(tmp_path: Path):
    fresh_path = _materialize_sql("empty", tmp_path / "fresh.sqlite3")
    upgraded_path = _materialize_sql("v1_1_1_full", tmp_path / "upgraded.sqlite3")
    manifest_counts = _manifest()["fixtures"]["v1_1_1_full"]["table_counts"]
    business_tables = tuple(sorted(manifest_counts))
    with sqlite3.connect(upgraded_path) as connection:
        before_health = _sqlite_health(connection)
        columns_by_table, before_counts, before_digests = _business_data_snapshot(
            connection,
            business_tables,
        )
    if before_health != {"integrity_check": [("ok",)], "foreign_key_check": []}:
        raise RuntimeError(f"v1.1.1 fixture is unhealthy before migration: {before_health}")
    if before_counts != manifest_counts:
        raise RuntimeError(
            "v1.1.1 fixture cardinalities disagree with its manifest: "
            f"snapshot={before_counts}; manifest={manifest_counts}"
        )

    await _upgrade_current_schema(fresh_path)
    await _upgrade_current_schema(upgraded_path)
    with sqlite3.connect(upgraded_path) as connection:
        after_health = _sqlite_health(connection)
        _, after_counts, after_digests = _business_data_snapshot(
            connection,
            business_tables,
            columns_by_table,
        )

    observation = {
        "schema_matches_fresh": _canonical_schema(upgraded_path) == _canonical_schema(fresh_path),
        "cardinality_mismatches": {
            table_name: [before_counts[table_name], after_counts[table_name]]
            for table_name in business_tables
            if before_counts[table_name] != after_counts[table_name]
        },
        "content_digest_mismatches": [
            table_name
            for table_name in business_tables
            if before_digests[table_name] != after_digests[table_name]
        ],
        "integrity_check": after_health["integrity_check"],
        "foreign_key_check": after_health["foreign_key_check"],
    }
    assert observation == {
        "schema_matches_fresh": True,
        "cardinality_mismatches": {},
        "content_digest_mismatches": [],
        "integrity_check": [("ok",)],
        "foreign_key_check": [],
    }


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1-MIG-003: ALTER-added evidence competency_id has no declared competencies foreign key",
)
@pytest.mark.asyncio
async def test_partial_m13_upgrade_adds_competency_foreign_key(tmp_path: Path):
    database_path = tmp_path / "partial-m13.sqlite3"
    engine = _test_engine(database_path)
    try:
        async with engine.begin() as connection:
            legacy_tables = [
                table for name, table in Base.metadata.tables.items() if name not in M13_TABLES
            ]
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=legacy_tables,
                )
            )
            await connection.execute(text(M13_EVIDENCE_TABLE_SQL))
            await connection.run_sync(Base.metadata.create_all)
            await migrate_sqlite_schema(connection)
            foreign_keys = [
                tuple(row)
                for row in (
                    await connection.execute(text(
                        'PRAGMA foreign_key_list("evidence_observations")'
                    ))
                ).all()
            ]
            evidence_columns = {
                row[1]
                for row in (
                    await connection.execute(text(
                        'PRAGMA table_info("evidence_observations")'
                    ))
                ).all()
            }
            if "competency_id" not in evidence_columns:
                raise RuntimeError("partial M13 migration did not add competency_id")
            await connection.execute(text(
                """
                INSERT INTO owners (id, display_name, timezone)
                VALUES ('fixture-owner', 'Fixture Learner', 'Asia/Shanghai')
                """
            ))
            invalid_write_rejected = False
            try:
                await connection.execute(text(
                    """
                    INSERT INTO evidence_observations
                        (id, owner_id, source_type, source_id, competency_id, outcome,
                         occurred_at, idempotency_key)
                    VALUES
                        (1, 'fixture-owner', 'quiz', 'invalid-competency', 999999,
                         'passed', '2026-08-18 09:23:45.123456',
                         'invalid-competency-write')
                    """
                ))
            except IntegrityError as exc:
                if "FOREIGN KEY constraint failed" not in str(exc):
                    raise RuntimeError(
                        "invalid competency write failed for a non-FK reason"
                    ) from exc
                invalid_write_rejected = True
            invalid_row_present = (
                await connection.execute(text(
                    """
                    SELECT count(*) FROM evidence_observations
                    WHERE idempotency_key = 'invalid-competency-write'
                    """
                ))
            ).scalar_one() == 1
    finally:
        await engine.dispose()

    observation = {
        "declared_competency_fk": any(
            row[2] == "competencies" and row[3] == "competency_id" and row[4] == "id"
            for row in foreign_keys
        ),
        "invalid_write_rejected": invalid_write_rejected,
        "invalid_row_present": invalid_row_present,
    }
    assert observation == {
        "declared_competency_fk": True,
        "invalid_write_rejected": True,
        "invalid_row_present": False,
    }


async def _roundtrip_evidence(path: Path, occurred_at: datetime) -> tuple[datetime, str]:
    engine = _test_engine(path)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await migrate_sqlite_schema(connection)
        async with session_factory() as session:
            session.add(Owner(
                id="fixture-owner",
                display_name="Fixture Learner",
                timezone="Asia/Shanghai",
            ))
            await session.flush()
            session.add(EvidenceObservation(
                id=1,
                owner_id="fixture-owner",
                source_type="quiz",
                source_id="timezone-boundary",
                outcome="passed",
                normalized_score=1.0,
                is_correct=True,
                occurred_at=occurred_at,
                recorded_at=datetime(2026, 8, 18, 9, 24, 0, 654321, tzinfo=timezone.utc),
                idempotency_key="timezone-boundary",
            ))
            await session.commit()
        async with session_factory() as session:
            observation = (
                await session.execute(select(EvidenceObservation).where(EvidenceObservation.id == 1))
            ).scalar_one()
            return observation.occurred_at, build_evidence_state([observation])["digest"]
    finally:
        await engine.dispose()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1-TIME-002: SQLite stores offset wall time without UTC canonicalization, so equal instants diverge",
)
@pytest.mark.asyncio
async def test_equal_instants_with_different_offsets_roundtrip_identically(tmp_path: Path):
    utc_value = datetime(2026, 8, 18, 9, 23, 45, 123456, tzinfo=timezone.utc)
    shanghai_value = utc_value.astimezone(ZoneInfo("Asia/Shanghai"))
    loaded_utc, utc_digest = await _roundtrip_evidence(tmp_path / "utc.sqlite3", utc_value)
    loaded_shanghai, shanghai_digest = await _roundtrip_evidence(
        tmp_path / "shanghai.sqlite3",
        shanghai_value,
    )

    assert loaded_utc.tzinfo is not None
    assert loaded_shanghai.tzinfo is not None
    assert loaded_utc == loaded_shanghai == utc_value
    assert utc_digest == shanghai_digest


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1-AUDIT-001: rebuild-evidence --audit calls create_schema and changes the source before backup",
)
def test_evidence_audit_is_byte_for_byte_read_only(tmp_path: Path):
    database_path = _materialize_sql("v1_1_1_full", tmp_path / "audit-source.sqlite3")
    before = database_path.read_bytes()
    result = subprocess.run(
        [sys.executable, "scripts/rebuild-evidence.py", "--audit"],
        cwd=PROJECT_ROOT,
        env=_subprocess_environment(database_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "evidence audit subprocess did not complete successfully: "
            f"returncode={result.returncode}; stderr={result.stderr[-2000:]}"
        )
    try:
        audit_output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("evidence audit subprocess returned invalid JSON") from exc
    if not isinstance(audit_output.get("audit"), dict):
        raise RuntimeError("evidence audit subprocess omitted its audit result")

    after = database_path.read_bytes()
    sidecars = [
        path for path in (
            database_path.with_name(database_path.name + "-wal"),
            database_path.with_name(database_path.name + "-shm"),
        ) if path.exists()
    ]

    observation = {
        "source_bytes_unchanged": after == before,
        "sidecars": [path.name for path in sidecars],
    }
    assert observation == {
        "source_bytes_unchanged": True,
        "sidecars": [],
    }


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H1-SCHEMA-001: verify_database_writable leaves a permanent _write_probe table",
)
@pytest.mark.asyncio
async def test_writability_probe_leaves_no_schema_object(tmp_path: Path, monkeypatch):
    from app import main as main_module

    database_path = tmp_path / "probe.sqlite3"
    await _upgrade_current_schema(database_path)
    engine = _test_engine(database_path)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main_module, "AsyncSessionLocal", session_factory)
    try:
        await main_module.verify_database_writable()
    finally:
        await engine.dispose()

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert "_write_probe" not in table_names
