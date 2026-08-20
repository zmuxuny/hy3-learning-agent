"""H1 acceptance tests for the canonical persisted-time contract.

H1-TIME-001 requires one stable UTC representation for persistence, reload,
digests, and JSON-facing serialization.  H1-TIME-002 applies that contract to
every persisted datetime column and makes it independent of the host timezone.
All databases in this module are disposable files owned by ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, event, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.time import canonical_utc, coerce_legacy_utc, parse_legacy_datetime, require_aware_utc
from app.db.database import Base
from app.db.types import UTCDateTime
from app.models import Owner, Plan, SchemaMigration, UserProfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER_ID = "h1-time-owner"

# This is intentionally an exact inventory rather than a count-only assertion:
# exchanging one omitted datetime for a newly added one must not preserve a
# misleading aggregate count.
DOMAIN_DATETIME_COLUMNS = {
    "owners": ("created_at",),
    "user_profiles": ("updated_at",),
    "plans": ("deadline", "created_at", "updated_at"),
    "tasks": ("due_at", "completed_at", "review_due_at"),
    "learning_resources": ("verified_at", "created_at"),
    "task_submissions": ("checked_at", "created_at"),
    "calendar_events": ("starts_at", "ends_at", "created_at", "updated_at"),
    "sessions": ("archived_at", "created_at", "updated_at"),
    "session_plan_links": ("created_at",),
    "planning_intakes": ("created_at", "updated_at"),
    "plan_proposals": ("decided_at", "created_at", "updated_at"),
    "chat_messages": ("invalidated_at", "created_at"),
    "session_summaries": ("invalidated_at", "created_at"),
    "chat_message_revisions": ("created_at",),
    "agent_runs": (
        "lease_acquired_at",
        "lease_expires_at",
        "available_at",
        "proactive_detected_at",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "tool_invocations": (
        "claimed_at",
        "claim_expires_at",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "run_events": ("created_at",),
    "queued_messages": ("created_at", "updated_at"),
    "run_steer_messages": ("applied_at", "disposed_at", "created_at"),
    "run_approvals": ("decided_at", "consumed_at", "created_at"),
    "learning_events": ("occurred_at", "invalidated_at", "created_at"),
    "evidence_observations": ("occurred_at", "recorded_at"),
    "artifacts": ("created_at",),
    "competencies": ("created_at", "updated_at"),
    "competency_edges": ("created_at",),
    "plan_competency_links": ("created_at",),
    "task_competency_links": ("created_at",),
    "resource_competency_links": ("created_at",),
    "competency_graph_states": ("updated_at",),
    "context_states": ("updated_at",),
    "evidence_projection_states": ("updated_at",),
    "evidence_artifact_links": ("created_at",),
    "evidence_competency_links": ("created_at",),
    "competency_graph_mutations": ("created_at",),
    "operation_dependencies": ("created_at",),
    "operation_evidence_links": ("created_at",),
    "provenance_nodes": ("created_at",),
    "provenance_edges": ("created_at",),
    "session_handoffs": ("invalidated_at", "created_at"),
    "context_snapshot_blocks": ("created_at",),
    "memory_lifecycle_events": (
        "expires_at_before",
        "expires_at_after",
        "created_at",
    ),
    "proactive_decisions": ("next_eligible_at", "decided_at", "created_at"),
    "interventions": ("read_at", "archived_at", "resolved_at", "created_at"),
    "session_compression_states": (
        "claim_started_at",
        "claim_expires_at",
        "updated_at",
    ),
    "inbound_mail_jobs": (
        "ack_claim_expires_at",
        "acked_at",
        "created_at",
        "updated_at",
    ),
    "memories": (
        "last_accessed_at",
        "last_reinforced_at",
        "expires_at",
        "invalidated_at",
        "created_at",
        "updated_at",
    ),
    "context_snapshots": ("invalidated_at", "created_at"),
    "outbox_actions": (
        "available_at",
        "claimed_at",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "outbox_receipts": ("accepted_at", "created_at"),
    "operations": ("created_at", "undone_at"),
    "notifications": ("sent_at", "read_at", "archived_at", "created_at"),
    "push_subscriptions": ("created_at", "updated_at"),
    "review_schedules": ("due_at", "created_at"),
    "quizzes": ("created_at", "graded_at"),
    "achievements": ("unlocked_at",),
}


@dataclass(frozen=True)
class IsolatedDatabase:
    path: Path
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    """Replace the repository fixture so this module never uses its shared DB."""

    yield


@pytest_asyncio.fixture
async def isolated_database(tmp_path: Path) -> AsyncIterator[IsolatedDatabase]:
    database_path = tmp_path / "h1-time.sqlite3"
    engine = _create_sqlite_engine(database_path)

    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield IsolatedDatabase(path=database_path, engine=engine, sessions=sessions)
    finally:
        await engine.dispose()


def _create_sqlite_engine(database_path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine


def _assert_aware_utc(value: datetime) -> None:
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


def test_h1_time_002_all_persisted_datetime_columns_use_utc_datetime() -> None:
    actual_domain_columns = {
        table.name: tuple(
            column.name for column in table.columns if isinstance(column.type, UTCDateTime)
        )
        for table in Base.metadata.sorted_tables
        if table.name != "schema_migrations"
        and any(isinstance(column.type, UTCDateTime) for column in table.columns)
    }
    naked_datetime_columns = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, DateTime)
    ]

    assert actual_domain_columns == DOMAIN_DATETIME_COLUMNS
    assert len(actual_domain_columns) == 55
    assert sum(map(len, actual_domain_columns.values())) == 122
    assert tuple(
        column.name
        for column in Base.metadata.tables["schema_migrations"].columns
        if isinstance(column.type, UTCDateTime)
    ) == ("applied_at",)
    assert sum(
        isinstance(column.type, UTCDateTime)
        for table in Base.metadata.sorted_tables
        for column in table.columns
    ) == 123
    assert naked_datetime_columns == []


def test_h1_time_001_canonical_utc_has_fixed_microseconds_and_orders_one_microsecond() -> None:
    center = datetime(2026, 8, 19, 4, 5, 6, 500_000, tzinfo=timezone.utc)
    instants = (center - timedelta(microseconds=1), center, center + timedelta(microseconds=1))

    encoded = tuple(canonical_utc(value) for value in instants)

    assert encoded == (
        "2026-08-19T04:05:06.499999Z",
        "2026-08-19T04:05:06.500000Z",
        "2026-08-19T04:05:06.500001Z",
    )
    assert encoded == tuple(sorted(encoded))
    assert canonical_utc(center.replace(microsecond=0)) == "2026-08-19T04:05:06.000000Z"


def test_h1_time_001_legacy_naive_values_are_interpreted_as_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5, 6)
    expected = naive.replace(tzinfo=timezone.utc)

    assert coerce_legacy_utc(naive) == expected
    assert parse_legacy_datetime("2026-01-02 03:04:05.000006") == expected
    assert parse_legacy_datetime("2026-01-02T03:04:05.000006Z") == expected
    assert canonical_utc(naive) == "2026-01-02T03:04:05.000006Z"


@pytest.mark.parametrize(
    "naive_value",
    [
        pytest.param(datetime(2026, 8, 19, 12, 0), id="ordinary-naive"),
        pytest.param(datetime(2026, 3, 8, 2, 30), id="new-york-dst-gap-naive"),
    ],
)
async def test_h1_time_002_new_naive_orm_writes_are_rejected(
    isolated_database: IsolatedDatabase,
    naive_value: datetime,
) -> None:
    with pytest.raises(ValueError, match="must include a UTC offset"):
        require_aware_utc(naive_value)

    async with isolated_database.sessions() as db:
        db.add(Owner(id=OWNER_ID, display_name="H1 UTC learner"))
        await db.commit()
        db.add(Plan(owner_id=OWNER_ID, title="naive timestamp must fail", deadline=naive_value))

        with pytest.raises(StatementError) as raised:
            await db.flush()

        assert isinstance(raised.value.orig, ValueError)
        assert "must include a UTC offset" in str(raised.value.orig)
        await db.rollback()
        assert await db.scalar(select(Plan).where(Plan.title == "naive timestamp must fail")) is None


async def test_h1_time_001_equal_utc_and_shanghai_instants_round_trip_identically(
    isolated_database: IsolatedDatabase,
) -> None:
    utc_value = datetime(2026, 8, 18, 9, 23, 45, 123_456, tzinfo=timezone.utc)
    shanghai_value = datetime(
        2026,
        8,
        18,
        17,
        23,
        45,
        123_456,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert utc_value == shanghai_value

    async with isolated_database.sessions() as db:
        db.add(Owner(id=OWNER_ID, display_name="H1 UTC learner"))
        db.add_all(
            [
                Plan(owner_id=OWNER_ID, title="UTC instant", deadline=utc_value),
                Plan(owner_id=OWNER_ID, title="Shanghai instant", deadline=shanghai_value),
            ]
        )
        await db.commit()

    async with isolated_database.engine.connect() as connection:
        raw_values = tuple(
            row[0]
            for row in (
                await connection.exec_driver_sql("SELECT deadline FROM plans ORDER BY id")
            ).all()
        )
    assert raw_values == (
        "2026-08-18 09:23:45.123456",
        "2026-08-18 09:23:45.123456",
    )

    async with isolated_database.sessions() as db:
        loaded_values = tuple(
            await db.scalars(select(Plan.deadline).order_by(Plan.id))
        )
    assert loaded_values == (utc_value, utc_value)
    for value in loaded_values:
        assert value is not None
        _assert_aware_utc(value)
        assert canonical_utc(value) == "2026-08-18T09:23:45.123456Z"


def test_h1_time_002_new_york_fold_values_are_distinct_utc_instants() -> None:
    new_york = ZoneInfo("America/New_York")
    repeated_wall_time = datetime(2026, 11, 1, 1, 30)
    fold_zero = repeated_wall_time.replace(tzinfo=new_york, fold=0)
    fold_one = repeated_wall_time.replace(tzinfo=new_york, fold=1)

    normalized_fold_zero = require_aware_utc(fold_zero)
    normalized_fold_one = require_aware_utc(fold_one)

    assert fold_zero.utcoffset() == timedelta(hours=-4)
    assert fold_one.utcoffset() == timedelta(hours=-5)
    assert normalized_fold_zero == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert normalized_fold_one == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    assert normalized_fold_one - normalized_fold_zero == timedelta(hours=1)
    assert canonical_utc(normalized_fold_zero) == "2026-11-01T05:30:00.000000Z"
    assert canonical_utc(normalized_fold_one) == "2026-11-01T06:30:00.000000Z"


async def test_h1_time_001_legacy_sqlite_row_reopens_as_aware_utc(
    isolated_database: IsolatedDatabase,
) -> None:
    async with isolated_database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, display_name, timezone, created_at) "
                "VALUES (:id, :display_name, :timezone, :created_at)"
            ),
            {
                "id": OWNER_ID,
                "display_name": "legacy learner",
                "timezone": "Asia/Shanghai",
                "created_at": "2024-05-06 07:08:09.123456",
            },
        )

    async with isolated_database.sessions() as db:
        owner = await db.get(Owner, OWNER_ID)

    assert owner is not None
    assert owner.created_at == datetime(2024, 5, 6, 7, 8, 9, 123_456, tzinfo=timezone.utc)
    _assert_aware_utc(owner.created_at)
    assert canonical_utc(owner.created_at) == "2024-05-06T07:08:09.123456Z"


async def test_h1_time_002_server_defaults_and_onupdate_reopen_as_aware_utc(
    isolated_database: IsolatedDatabase,
) -> None:
    # Use raw SQL for these rows so the database server defaults, rather than
    # the ORM's Python-side ``utc_now`` defaults, are the code under test.
    async with isolated_database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, display_name, timezone) "
                "VALUES (:id, :display_name, :timezone)"
            ),
            {
                "id": OWNER_ID,
                "display_name": "H1 defaults learner",
                "timezone": "Asia/Shanghai",
            },
        )
        await connection.execute(
            text(
                "INSERT INTO schema_migrations (version, name, checksum, result) "
                "VALUES (:version, :name, :checksum, :result)"
            ),
            {
                "version": 1,
                "name": "h1-time-contract-probe",
                "checksum": "0" * 64,
                "result": "applied",
            },
        )

    # UserProfile has application defaults that intentionally are not all SQL
    # server defaults; create it normally so the later onupdate path is valid.
    async with isolated_database.sessions() as db:
        db.add(UserProfile(owner_id=OWNER_ID))
        await db.commit()

    await isolated_database.engine.dispose()
    reopened_engine = _create_sqlite_engine(isolated_database.path)
    reopened_sessions = async_sessionmaker(
        reopened_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with reopened_sessions() as db:
            owner = await db.get(Owner, OWNER_ID)
            migration = await db.get(SchemaMigration, 1)
        assert owner is not None
        assert migration is not None
        _assert_aware_utc(owner.created_at)
        _assert_aware_utc(migration.applied_at)
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000000Z",
            canonical_utc(owner.created_at),
        )
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000000Z",
            canonical_utc(migration.applied_at),
        )

        legacy_updated_at = "2000-01-01 00:00:00.000000"
        async with reopened_engine.begin() as connection:
            await connection.execute(
                text("UPDATE user_profiles SET updated_at=:updated_at WHERE owner_id=:owner_id"),
                {"updated_at": legacy_updated_at, "owner_id": OWNER_ID},
            )

        async with reopened_sessions() as db:
            profile = await db.get(UserProfile, OWNER_ID)
            assert profile is not None
            assert profile.updated_at == datetime(2000, 1, 1, tzinfo=timezone.utc)
            profile.agent_style = "direct"
            await db.commit()
    finally:
        await reopened_engine.dispose()

    final_engine = _create_sqlite_engine(isolated_database.path)
    final_sessions = async_sessionmaker(final_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with final_sessions() as db:
            updated_profile = await db.get(UserProfile, OWNER_ID)
        assert updated_profile is not None
        _assert_aware_utc(updated_profile.updated_at)
        assert updated_profile.updated_at > datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
            canonical_utc(updated_profile.updated_at),
        )
    finally:
        await final_engine.dispose()


def test_h1_time_002_sqlite_round_trip_is_independent_of_process_timezone(tmp_path: Path) -> None:
    probe = textwrap.dedent(
        """
        import json
        import sys
        import time
        from datetime import datetime

        from sqlalchemy import String, create_engine, text
        from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

        from app.core.time import canonical_utc
        from app.db.types import UTCDateTime

        if hasattr(time, "tzset"):
            time.tzset()

        class ProbeBase(DeclarativeBase):
            pass

        class TimeProbe(ProbeBase):
            __tablename__ = "time_probes"

            id: Mapped[str] = mapped_column(String(32), primary_key=True)
            occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())

        engine = create_engine(f"sqlite:///{sys.argv[1]}")
        ProbeBase.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                TimeProbe(
                    id="probe",
                    occurred_at=datetime.fromisoformat("2026-08-18T17:23:45.123456+08:00"),
                )
            )
            session.commit()
        engine.dispose()

        engine = create_engine(f"sqlite:///{sys.argv[1]}")
        with engine.connect() as connection:
            raw = connection.execute(text("SELECT occurred_at FROM time_probes")).scalar_one()
        with Session(engine) as session:
            loaded = session.get(TimeProbe, "probe")
            output = {
                "canonical": canonical_utc(loaded.occurred_at),
                "offset_seconds": int(loaded.occurred_at.utcoffset().total_seconds()),
                "raw": raw,
            }
        engine.dispose()
        print(json.dumps(output, sort_keys=True))
        """
    )
    expected = {
        "canonical": "2026-08-18T09:23:45.123456Z",
        "offset_seconds": 0,
        "raw": "2026-08-18 09:23:45.123456",
    }
    observations = []

    for timezone_name in ("UTC", "Asia/Shanghai", "America/New_York"):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "backend")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["TZ"] = timezone_name
        database_path = tmp_path / f"process-{timezone_name.replace('/', '-')}.sqlite3"
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(database_path)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        observations.append(json.loads(completed.stdout))

    assert observations == [expected, expected, expected]
