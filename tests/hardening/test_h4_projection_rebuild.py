"""H4 failure baselines for complete and restart-safe Evidence projections.

Every database in this module is disposable.  The HTTP checks exercise the
real ASGI router with only the database dependency replaced by the per-test
session factory; they do not accept an in-process tool call as an API proxy.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import app.context.assembler as context_assembler_module
from app.context.assembler import ContextAssembler
from app.db.database import Base, get_db
from app.main import app
from app.models import (
    AgentRun,
    EvidenceObservation,
    EvidenceProjectionState,
    Owner,
    UserProfile,
)
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import evidence as evidence_service
from app.services import plans as plan_service
from app.services.evidence import (
    append_observation,
    build_evidence_state,
    build_plan_evidence_state,
)
from app.tools import ToolContext, execute_tool


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class IsolatedDatabase:
    url: str
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    """Override the repository fixture; this module owns a separate temp DB."""

    yield


def _new_database(database_path: Path) -> IsolatedDatabase:
    url = f"sqlite+aiosqlite:///{database_path}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return IsolatedDatabase(
        url=url,
        engine=engine,
        sessions=async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )


@pytest_asyncio.fixture
async def isolated_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(context_assembler_module, "PROJECT_ROOT", tmp_path)
    database = _new_database(tmp_path / "h4-projection.db")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.sessions() as db:
        db.add(Owner(id="local", display_name="H4 projection learner", timezone="Asia/Shanghai"))
        db.add(UserProfile(owner_id="local"))
        await db.commit()
    try:
        yield database
    finally:
        await database.engine.dispose()


async def _create_plan(db: AsyncSession, title: str) -> tuple[int, int]:
    plan = await plan_service.create_plan(
        db,
        "local",
        PlanCreate(
            title=title,
            goal="验证完整 Evidence 投影",
            current_level="入门",
            expected_outcome="所有读取面返回同一 digest",
            stages=[
                StageCreate(
                    title="投影",
                    tasks=[TaskCreate(title="建立确定性账本", evidence_required=True)],
                )
            ],
        ),
    )
    return plan.id, plan.stages[0].tasks[0].id


async def _seed_ledger(db: AsyncSession, total: int) -> int:
    plan_id, task_id = await _create_plan(db, f"H4 ledger {total}")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(total):
        await append_observation(
            db,
            owner_id="local",
            source_type="manual",
            source_id=f"h4-projection:{total}:{index}",
            plan_id=plan_id,
            task_id=task_id,
            outcome="submitted",
            idempotency_key=f"h4-projection:{total}:{index}",
            payload={"index": index},
            occurred_at=start + timedelta(seconds=index),
        )
    await db.commit()
    return plan_id


def _cli_projection(database_url: str, plan_id: int) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment["ENABLE_SCHEDULER"] = "false"
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "rebuild-evidence.py"),
            "--plan-id",
            str(plan_id),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(completed.stdout)["projection"]


async def _incremental_projection(
    database: IsolatedDatabase,
    plan_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    builder = getattr(evidence_service, "build_incremental_plan_evidence_state", None)
    if not callable(builder):
        return None, None
    async with database.sessions() as db:
        value = builder(db, "local", plan_id)
        if inspect.isawaitable(value):
            value = await value
        await db.commit()
    if isinstance(value, dict) and isinstance(value.get("projection"), dict):
        value = value["projection"]
    projection = value if isinstance(value, dict) else None

    async with database.sessions() as db:
        state = await db.scalar(
            select(EvidenceProjectionState).where(
                EvidenceProjectionState.owner_id == "local",
                EvidenceProjectionState.plan_id == plan_id,
            )
        )
        max_observation_id = await db.scalar(
            select(func.max(EvidenceObservation.id)).where(
                EvidenceObservation.plan_id == plan_id
            )
        )
    if state is None:
        return projection, None
    checkpoint = {
        "watermark": state.watermark,
        "max_observation_id": max_observation_id or 0,
        "projection_digest": state.projection_digest,
        "projection": state.projection,
        "algorithm_version": state.algorithm_version,
    }
    return projection, checkpoint


async def _http_projection(
    database: IsolatedDatabase,
    plan_id: int,
) -> tuple[int, dict[str, Any] | None]:
    async def _override_database():
        async with database.sessions() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_database
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://h4.test",
        ) as client:
            response = await client.get(f"/api/v1/plans/{plan_id}/evidence")
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous

    if response.status_code != 200:
        return response.status_code, None
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("evidence_state"), dict):
        payload = payload["evidence_state"]
    return response.status_code, payload if isinstance(payload, dict) else None


async def _runtime_surfaces(
    database: IsolatedDatabase,
    plan_id: int,
) -> dict[str, Any]:
    cli = _cli_projection(database.url, plan_id)
    async with database.sessions() as db:
        records = list(
            (
                await db.execute(
                    select(EvidenceObservation)
                    .where(EvidenceObservation.plan_id == plan_id)
                    .order_by(EvidenceObservation.id)
                )
            ).scalars()
        )
        full = build_evidence_state(records)
        online = await build_plan_evidence_state(db, "local", plan_id)
        run = await db.scalar(
            select(AgentRun)
            .where(
                AgentRun.owner_id == "local",
                AgentRun.plan_id == plan_id,
                AgentRun.session_id.is_(None),
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(1)
        )
        if run is None:
            run = AgentRun(
                owner_id="local",
                plan_id=plan_id,
                trigger="user_message",
                objective="读取 H4 Evidence digest",
            )
            db.add(run)
            await db.commit()
        context = ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            trigger="user_message",
            plan_id=plan_id,
        )
        tool_result = await execute_tool(
            "study_state_get",
            json.dumps({"plan_id": plan_id}),
            context,
        )
        if tool_result.get("ok") is not True:
            raise RuntimeError(f"study_state_get fixture failed: {tool_result}")
        tool = tool_result["data"]["evidence_state"]
        snapshot = await ContextAssembler(db).build(
            "local",
            plan_id=plan_id,
            run_id=run.id,
            objective="核对 H4 Evidence digest",
        )
        context_digests = {
            item["digest"]
            for item in snapshot.source_manifest
            if item.get("type") == "evidence_state"
        }

    incremental, checkpoint = await _incremental_projection(database, plan_id)
    http_status, http = await _http_projection(database, plan_id)
    return {
        "full": full,
        "incremental": incremental,
        "checkpoint": checkpoint,
        "cli": cli,
        "online": online,
        "tool": tool,
        "context_digests": context_digests,
        "http_status": http_status,
        "http": http,
    }


def _assert_all_surfaces(surfaces: dict[str, Any], expected_count: int) -> None:
    projection_names = ("full", "incremental", "cli", "online", "tool", "http")
    projections = {name: surfaces[name] for name in projection_names}
    assert surfaces["http_status"] == 200
    assert all(isinstance(value, dict) for value in projections.values())
    assert {
        name: value["observation_count"]
        for name, value in projections.items()
    } == {name: expected_count for name in projection_names}
    assert {
        value["digest"]
        for value in projections.values()
    } == {surfaces["full"]["digest"]}
    assert surfaces["context_digests"] == {surfaces["full"]["digest"]}
    assert surfaces["checkpoint"] is not None
    assert surfaces["checkpoint"]["watermark"] == surfaces["checkpoint"]["max_observation_id"]
    assert surfaces["checkpoint"]["projection_digest"] == surfaces["full"]["digest"]
    assert surfaces["checkpoint"]["projection"] == surfaces["full"]
    assert surfaces["checkpoint"]["algorithm_version"] == surfaces["full"]["algorithm_version"]


@pytest.mark.parametrize(
    "total",
    [0, 1, 500, 501, 10_000],
    ids=["0", "1", "500", "501", "10000"],
)
@pytest.mark.asyncio
async def test_full_incremental_cli_context_and_http_use_the_complete_ledger(
    isolated_database: IsolatedDatabase,
    total: int,
):
    async with isolated_database.sessions() as db:
        plan_id = await _seed_ledger(db, total)

    surfaces = await _runtime_surfaces(isolated_database, plan_id)

    _assert_all_surfaces(surfaces, total)


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError("H4 projection fixture requires a SQLite file URL")
    return Path(database_url.removeprefix(prefix))


def _sqlite_backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


@pytest.mark.asyncio
async def test_digest_survives_close_reopen_and_sqlite_backup_restore(
    isolated_database: IsolatedDatabase,
    tmp_path: Path,
):
    async with isolated_database.sessions() as db:
        plan_id = await _seed_ledger(db, 501)

    before = await _runtime_surfaces(isolated_database, plan_id)
    await isolated_database.engine.dispose()

    reopened = _new_database(_sqlite_path(isolated_database.url))
    backup = _new_database(tmp_path / "restored-h4-projection.db")
    try:
        reopened_surfaces = await _runtime_surfaces(reopened, plan_id)
        await reopened.engine.dispose()
        _sqlite_backup(_sqlite_path(reopened.url), _sqlite_path(backup.url))
        restored_surfaces = await _runtime_surfaces(backup, plan_id)
    finally:
        await reopened.engine.dispose()
        await backup.engine.dispose()

    for surfaces in (before, reopened_surfaces, restored_surfaces):
        _assert_all_surfaces(surfaces, 501)
    assert {
        before["full"]["digest"],
        reopened_surfaces["full"]["digest"],
        restored_surfaces["full"]["digest"],
    } == {before["full"]["digest"]}
