from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.context.assembler as context_assembler_module
import app.models as models_module
from app.api.operations import undo_operation
from app.context.assembler import ContextAssembler
from app.db.database import Base, get_db
from app.db.uow import DatabaseBusyError
from app.main import app
from app.models import AgentRun, Artifact, EvidenceObservation, Owner, UserProfile
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.services import evidence as evidence_service
from app.services.evidence import (
    append_observation,
    build_evidence_state,
    build_plan_evidence_state,
    create_artifact,
)
from app.tools import ToolContext, execute_tool


PROJECT_ROOT = Path(__file__).resolve().parents[2]

def _require_fixture(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@dataclass(frozen=True)
class IsolatedDatabase:
    url: str
    sessions: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    """Override the repository-wide fixed-file fixture for this module.

    Every test below owns a separate SQLite file under pytest's ``tmp_path``.
    """

    yield


@pytest_asyncio.fixture
async def isolated_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(context_assembler_module, "PROJECT_ROOT", tmp_path)
    database_path = tmp_path / "h0-evidence.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    engine = create_async_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(Owner(id="local", display_name="H0 learner", timezone="Asia/Shanghai"))
        db.add(UserProfile(owner_id="local"))
        await db.commit()

    try:
        yield IsolatedDatabase(url=database_url, sessions=sessions)
    finally:
        await engine.dispose()


def _plan_payload(title: str) -> PlanCreate:
    return PlanCreate(
        title=title,
        goal="建立可验证的学习证据",
        current_level="入门",
        expected_outcome="完成一次可审计的练习",
        stages=[
            StageCreate(
                title="证据阶段",
                tasks=[
                    TaskCreate(
                        title="提交并验收练习",
                        is_core=True,
                        evidence_required=True,
                    )
                ],
            )
        ],
    )


async def _create_plan(db: AsyncSession, title: str):
    plan = await plan_service.create_plan(db, "local", _plan_payload(title))
    return plan, plan.stages[0].tasks[0]


async def _tool_context(
    db: AsyncSession,
    plan_id: int,
    objective: str,
    *,
    tool_call_id: str | None = None,
) -> ToolContext:
    run = AgentRun(
        owner_id="local",
        plan_id=plan_id,
        trigger="user_message",
        objective=objective,
    )
    db.add(run)
    await db.commit()
    return ToolContext(
        db=db,
        owner_id="local",
        run_id=run.id,
        trigger="user_message",
        plan_id=plan_id,
        tool_call_id=tool_call_id,
    )


def _fact(
    fact_id: int,
    *,
    occurred_at: datetime,
    source_type: str = "quiz",
    outcome: str = "passed",
    payload: dict[str, Any] | None = None,
):
    return SimpleNamespace(
        id=fact_id,
        source_type=source_type,
        source_id=f"fact:{fact_id}",
        task_id=1,
        outcome=outcome,
        normalized_score=1.0,
        is_correct=True,
        assistance_level="independent",
        transfer_level="same_task",
        fact_kind="observation",
        target_observation_id=None,
        reason_code="",
        evidence_role="primary",
        eligibility_stage=None,
        eligibility_reason="",
        eligibility_policy_version="",
        counts_as_success=False,
        _evidence_artifact_refs=[],
        _evidence_competency_refs=[],
        occurred_at=occurred_at,
        payload=payload or {},
    )


def _task_projection(state: dict[str, Any], task_id: int) -> dict[str, Any] | None:
    return next((item for item in state["by_task"] if item["task_id"] == task_id), None)


async def _observations(db: AsyncSession, plan_id: int) -> list[EvidenceObservation]:
    return list(
        (
            await db.execute(
                select(EvidenceObservation)
                .where(EvidenceObservation.plan_id == plan_id)
                .order_by(EvidenceObservation.id)
            )
        ).scalars()
    )


def _freeze_persisted_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return value


def _immutable_signature(item: EvidenceObservation) -> tuple[tuple[str, Any], ...]:
    """Freeze every persisted Evidence column, including mutable JSON values."""

    return tuple(
        (column.key, _freeze_persisted_value(getattr(item, column.key)))
        for column in EvidenceObservation.__table__.columns
    )


def _expected_artifact_envelope_hash(
    content: str,
    metadata: dict[str, Any],
) -> str:
    canonical_envelope = json.dumps(
        {"content": content, "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_envelope).hexdigest()


@pytest.mark.asyncio
async def test_h1_time_001_sqlite_roundtrip_preserves_utc_digest(isolated_database):
    occurred_at = datetime(
        2026,
        8,
        18,
        20,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    async with isolated_database.sessions() as db:
        observation, _ = await append_observation(
            db,
            owner_id="local",
            source_type="quiz",
            source_id="timezone-roundtrip",
            outcome="passed",
            idempotency_key="h1-time-001:roundtrip",
            occurred_at=occurred_at,
        )
        digest_before_restart = build_evidence_state([observation])["digest"]
        await db.commit()

    async with isolated_database.sessions() as db:
        reloaded = (await db.execute(select(EvidenceObservation))).scalar_one()
        digest_after_restart = build_evidence_state([reloaded])["digest"]

    expected = (
        datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc),
        digest_before_restart,
    )
    actual = (reloaded.occurred_at, digest_after_restart)
    assert actual == expected


def test_h1_time_001_projection_orders_mixed_naive_and_aware_history():
    records = [
        _fact(
            1,
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            outcome="needs_revision",
        ),
        _fact(
            2,
            occurred_at=datetime(2026, 1, 1),
            outcome="passed",
        ),
    ]

    try:
        projection = build_evidence_state(records)
    except TypeError as exc:
        actual = {
            "projection_created": False,
            "error_type": type(exc).__name__,
            "latest_outcome": None,
            "last_observed_year": None,
        }
    else:
        actual = {
            "projection_created": True,
            "error_type": None,
            "latest_outcome": projection["by_task"][0]["latest_outcome"],
            "last_observed_year": projection["by_task"][0]["last_observed_at"][:4],
        }

    assert actual == {
        "projection_created": True,
        "error_type": None,
        "latest_outcome": "passed",
        "last_observed_year": "2026",
    }


async def _seed_ledger(
    db: AsyncSession,
    *,
    total: int,
    title: str,
) -> tuple[int, int]:
    plan, task = await _create_plan(db, title)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {
            "owner_id": "local",
            "source_type": "manual",
            "source_id": f"bulk:{index}",
            "plan_id": plan.id,
            "task_id": task.id,
            "fact_kind": "observation",
            "target_observation_id": None,
            "reason_code": "",
            "evidence_role": "primary",
            "eligibility_stage": "practicing",
            "eligibility_reason": "ATTEMPT_RECORDED",
            "eligibility_policy_version": "evidence-eligibility-v1",
            "counts_as_success": False,
            "outcome": "submitted",
            "normalized_score": None,
            "is_correct": None,
            "assistance_level": "unknown",
            "transfer_level": "unknown",
            "rubric_snapshot": {},
            "evaluator": {},
            "payload": {"index": index},
            "occurred_at": start + timedelta(seconds=index),
            "recorded_at": start + timedelta(seconds=index),
            "schema_version": 2,
            "idempotency_key": f"h4-evid-001:{total}:{index}",
        }
        for index in range(total)
    ]
    if rows:
        await db.execute(insert(EvidenceObservation), rows)
    await db.commit()
    return plan.id, task.id


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


async def _evidence_surfaces(
    isolated_database: IsolatedDatabase,
    *,
    plan_id: int,
) -> dict[str, Any]:
    cli_projection = _cli_projection(isolated_database.url, plan_id)
    async with isolated_database.sessions() as db:
        all_records = list(
            (
                await db.execute(
                    select(EvidenceObservation)
                    .where(EvidenceObservation.plan_id == plan_id)
                    .order_by(EvidenceObservation.id)
                )
            ).scalars()
        )
        full_projection = build_evidence_state(all_records)
        online_projection = await build_plan_evidence_state(db, "local", plan_id)

        ctx = await _tool_context(db, plan_id, "读取完整 Evidence digest")
        tool_result = await execute_tool(
            "study_state_get",
            json.dumps({"plan_id": plan_id}),
            ctx,
        )
        if not tool_result.get("ok"):
            raise RuntimeError(f"study_state_get failed during H0 setup: {tool_result}")
        tool_projection = tool_result["data"]["evidence_state"]

        snapshot = await ContextAssembler(db).build(
            "local",
            plan_id=plan_id,
            run_id=ctx.run_id,
            objective="解释当前 Evidence digest",
        )
        context_digests = {
            item["digest"]
            for item in snapshot.source_manifest
            if item.get("type") == "evidence_state"
        }

    return {
        "full": full_projection,
        "cli": cli_projection,
        "online": online_projection,
        "tool": tool_projection,
        "context_digests": context_digests,
    }


def _assert_complete_surfaces(surfaces: dict[str, Any], expected_count: int) -> None:
    counts = {
        name: surfaces[name]["observation_count"]
        for name in ("full", "cli", "online", "tool")
    }
    digests = {
        name: surfaces[name]["digest"]
        for name in ("full", "cli", "online", "tool")
    }
    actual = {
        "counts": counts,
        "digest_count": len(set(digests.values())),
        "context_digests": surfaces["context_digests"],
    }
    expected = {
        "counts": {name: expected_count for name in counts},
        "digest_count": 1,
        "context_digests": {surfaces["full"]["digest"]},
    }
    assert actual == expected


@pytest.mark.parametrize("total", [0, 1, 500], ids=["0", "1", "500"])
@pytest.mark.asyncio
async def test_h4_evid_001_projection_keeps_complete_passing_boundaries(
    isolated_database,
    total,
):
    async with isolated_database.sessions() as db:
        plan_id, _ = await _seed_ledger(
            db,
            total=total,
            title=f"{total} evidence passing boundary",
        )

    surfaces = await _evidence_surfaces(isolated_database, plan_id=plan_id)

    _assert_complete_surfaces(surfaces, total)


@pytest.mark.parametrize("total", [501, 10_000], ids=["501", "10000"])
@pytest.mark.asyncio
async def test_h4_evid_001_projection_does_not_truncate_large_ledgers(
    isolated_database,
    total,
):
    async with isolated_database.sessions() as db:
        plan_id, _ = await _seed_ledger(
            db,
            total=total,
            title=f"{total} evidence boundary",
        )

    surfaces = await _evidence_surfaces(isolated_database, plan_id=plan_id)

    _assert_complete_surfaces(surfaces, total)


async def _successful_operation(
    db: AsyncSession,
    *,
    case: str,
    plan_id: int,
    task_id: int,
) -> str:
    ctx = await _tool_context(db, plan_id, f"{case} success before undo")
    if case == "task":
        result = await execute_tool(
            "task_patch",
            json.dumps(
                {
                    "task_id": task_id,
                    "changes": {
                        "status": "completed",
                        "evidence": [
                            {
                                "kind": "repository",
                                "value": "snapshot://task-proof",
                            }
                        ],
                    },
                    "reason": "完成带证据任务",
                }
            ),
            ctx,
        )
        if not result.get("ok"):
            raise RuntimeError(f"task_patch failed during H0 setup: {result}")
        return result["data"]["operation_id"]

    if case == "submission":
        submitted = await execute_tool(
            "submission_create",
            json.dumps(
                {
                    "task_id": task_id,
                    "submission_type": "code",
                    "content": "print('immutable evidence')",
                }
            ),
            ctx,
        )
        if not submitted.get("ok"):
            raise RuntimeError(f"submission_create failed during H0 setup: {submitted}")
        checked = await execute_tool(
            "submission_check",
            json.dumps(
                {
                    "submission_id": submitted["data"]["submission_id"],
                    "score": 90,
                    "feedback": "运行与解释均通过",
                    "checks": [{"name": "运行", "passed": True}],
                }
            ),
            ctx,
        )
        if not checked.get("ok"):
            raise RuntimeError(f"submission_check failed during H0 setup: {checked}")
        return checked["data"]["operation_id"]

    if case == "quiz":
        created = await execute_tool(
            "quiz_create",
            json.dumps(
                {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "prompt": "解释事件循环",
                    "rubric": {"threshold": 70},
                }
            ),
            ctx,
        )
        if not created.get("ok"):
            raise RuntimeError(f"quiz_create failed during H0 setup: {created}")
        graded = await execute_tool(
            "quiz_grade",
            json.dumps(
                {
                    "quiz_id": created["data"]["quiz_id"],
                    "answer": "事件循环调度就绪协程并处理 IO 完成事件。",
                    "score": 90,
                    "feedback": "解释完整",
                    "evidence": [{"criterion": "调度", "passed": True}],
                }
            ),
            ctx,
        )
        if not graded.get("ok"):
            raise RuntimeError(f"quiz_grade failed during H0 setup: {graded}")
        return graded["data"]["operation_id"]

    raise RuntimeError(f"Unsupported Evidence undo case: {case}")


@pytest.mark.parametrize("case", ["submission", "quiz", "task"], ids=lambda value: value)
@pytest.mark.asyncio
async def test_h4_evid_002_undo_appends_invalidation_and_removes_active_success(
    isolated_database,
    case,
):
    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, f"{case} Evidence undo")
        plan_id = plan.id
        task_id = task.id
        operation_id = await _successful_operation(
            db,
            case=case,
            plan_id=plan_id,
            task_id=task_id,
        )

    async with isolated_database.sessions() as db:
        before_rows = await _observations(db, plan_id)
        before_projection = build_evidence_state(before_rows)
        before_task = _task_projection(before_projection, task_id)
        if before_task is None or before_task["evidence_stage"] != "demonstrated":
            raise RuntimeError(
                f"{case} setup did not produce demonstrated Evidence: {before_projection}"
            )
        original_signatures = {
            item.id: _immutable_signature(item)
            for item in before_rows
        }

        await undo_operation(operation_id, db)

        after_rows = await _observations(db, plan_id)
        after_projection = build_evidence_state(after_rows)
        after_task = _task_projection(after_projection, task_id)
        after_by_id = {item.id: item for item in after_rows}
        unchanged_originals = all(
            item_id in after_by_id
            and _immutable_signature(after_by_id[item_id]) == signature
            for item_id, signature in original_signatures.items()
        )
        actual = {
            "original_ids_preserved": set(original_signatures) <= set(after_by_id),
            "originals_unchanged": unchanged_originals,
            "history_grew": len(after_rows) > len(before_rows),
            "success_is_inactive": (
                after_task is None
                or after_task["evidence_stage"] != "demonstrated"
            ),
        }
        assert actual == {
            "original_ids_preserved": True,
            "originals_unchanged": True,
            "history_grew": True,
            "success_is_inactive": True,
        }


async def _operation_http_action(
    db: AsyncSession,
    operation_id: str,
    action: str,
) -> tuple[int, dict[str, Any] | None]:
    async def _override_database():
        yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_database
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.post(
                f"/api/v1/operations/{operation_id}/{action}"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous

    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    return response.status_code, payload if isinstance(payload, dict) else None


async def _operation_evidence_links(
    db: AsyncSession,
    operation_id: str,
) -> list[Any] | None:
    link_model = getattr(models_module, "OperationEvidenceLink", None)
    if link_model is None:
        return None
    return list(
        (
            await db.execute(
                select(link_model)
                .where(link_model.operation_id == operation_id)
                .order_by(link_model.generation, link_model.id)
            )
        ).scalars()
    )


@pytest.mark.parametrize("case", ["submission", "quiz", "task"], ids=lambda value: value)
@pytest.mark.asyncio
async def test_h4_evid_002_commit_undo_redo_undo_is_append_only(
    isolated_database,
    case,
):
    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, f"{case} Evidence redo")
        operation_id = await _successful_operation(
            db,
            case=case,
            plan_id=plan.id,
            task_id=task.id,
        )
        plan_id = plan.id
        original_ids = {item.id for item in await _observations(db, plan_id)}

    async with isolated_database.sessions() as db:
        undo_status, _ = await _operation_http_action(db, operation_id, "undo")
        redo_status, _ = await _operation_http_action(db, operation_id, "redo")
        second_undo_status, _ = await _operation_http_action(db, operation_id, "undo")
        rows = await _observations(db, plan_id)
        links = await _operation_evidence_links(db, operation_id)

    assert (undo_status, redo_status, second_undo_status) == (200, 200, 200)
    assert original_ids <= {item.id for item in rows}
    assert links is not None
    assert [(item.generation, item.role) for item in links] == [
        (0, "produced"),
        (0, "invalidation"),
        (1, "produced"),
        (1, "invalidation"),
    ]
    projection = build_evidence_state(rows)
    task_projection = _task_projection(projection, task.id)
    assert task_projection is None or task_projection["evidence_stage"] != "demonstrated"


@pytest.mark.asyncio
async def test_h4_evid_002_undo_commit_failure_is_atomic_and_retryable(
    isolated_database,
):
    class InjectedCommitFailure(RuntimeError):
        pass

    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, "Evidence undo commit failure")
        operation_id = await _successful_operation(
            db,
            case="task",
            plan_id=plan.id,
            task_id=task.id,
        )
        plan_id = plan.id

    async with isolated_database.sessions() as db:
        before_rows = await _observations(db, plan_id)
        before_signatures = {
            item.id: _immutable_signature(item)
            for item in before_rows
        }
        fired = False

        def _fail_before_commit(_session):
            nonlocal fired
            fired = True
            raise InjectedCommitFailure("H4 undo commit kill point")

        event.listen(db.sync_session, "before_commit", _fail_before_commit)
        try:
            with pytest.raises(InjectedCommitFailure):
                await undo_operation(operation_id, db)
        finally:
            event.remove(db.sync_session, "before_commit", _fail_before_commit)
            await db.rollback()
        assert fired is True

    async with isolated_database.sessions() as db:
        after_failure = await _observations(db, plan_id)
        operation = await db.get(models_module.Operation, operation_id)
        assert operation is not None and operation.status == "committed"
        assert {
            item.id: _immutable_signature(item)
            for item in after_failure
        } == before_signatures
        await undo_operation(operation_id, db)

    async with isolated_database.sessions() as db:
        rows = await _observations(db, plan_id)
        links = await _operation_evidence_links(db, operation_id)

    assert set(before_signatures) <= {item.id for item in rows}
    assert links is not None
    assert [item.role for item in links].count("invalidation") == 1


@pytest.mark.asyncio
async def test_h4_evid_002_two_concurrent_undo_requests_append_one_invalidation(
    isolated_database,
):
    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, "Concurrent Evidence undo")
        operation_id = await _successful_operation(
            db,
            case="task",
            plan_id=plan.id,
            task_id=task.id,
        )
        plan_id = plan.id
        original_ids = {item.id for item in await _observations(db, plan_id)}

    async with (
        isolated_database.sessions() as first,
        isolated_database.sessions() as second,
    ):
        results = await asyncio.gather(
            undo_operation(operation_id, first),
            undo_operation(operation_id, second),
        )

    async with isolated_database.sessions() as db:
        rows = await _observations(db, plan_id)
        links = await _operation_evidence_links(db, operation_id)

    assert [item.status for item in results] == ["undone", "undone"]
    assert original_ids <= {item.id for item in rows}
    assert links is not None
    assert [item.role for item in links].count("invalidation") == 1


@pytest.mark.asyncio
async def test_h4_evid_002_sqlite_busy_retry_appends_one_invalidation(
    isolated_database,
):
    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, "Locked Evidence undo")
        operation_id = await _successful_operation(
            db,
            case="task",
            plan_id=plan.id,
            task_id=task.id,
        )
        plan_id = plan.id
        original_ids = {item.id for item in await _observations(db, plan_id)}

    database_path = Path(
        isolated_database.url.removeprefix("sqlite+aiosqlite:///")
    )
    locker = sqlite3.connect(database_path, timeout=0.05)
    try:
        locker.execute("PRAGMA journal_mode=WAL")
        locker.execute("BEGIN IMMEDIATE")
        async with isolated_database.sessions() as db:
            await db.execute(text("PRAGMA busy_timeout=50"))
            with pytest.raises(DatabaseBusyError):
                await undo_operation(operation_id, db)
    finally:
        locker.rollback()
        locker.close()

    async with isolated_database.sessions() as db:
        operation = await undo_operation(operation_id, db)
        rows = await _observations(db, plan_id)
        links = await _operation_evidence_links(db, operation_id)

    assert operation.status == "undone"
    assert original_ids <= {item.id for item in rows}
    assert links is not None
    assert [item.role for item in links].count("invalidation") == 1


@pytest.mark.asyncio
async def test_h4_evid_003_submission_success_is_weighted_once(isolated_database):
    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, "single primary submission observation")
        await _successful_operation(
            db,
            case="submission",
            plan_id=plan.id,
            task_id=task.id,
        )
        plan_id = plan.id
        task_id = task.id

    async with isolated_database.sessions() as db:
        projection = build_evidence_state(await _observations(db, plan_id))
        task_projection = _task_projection(projection, task_id)

    _require_fixture(task_projection is not None, "submission projection fixture is missing")
    assert task_projection["success_count"] == 1


@pytest.mark.parametrize(
    ("source_type", "payload"),
    [
        pytest.param("self_report", {"text": "我已经掌握了"}, id="self-report"),
        pytest.param(
            "task_completion",
            {"evidence": [{"kind": "checkbox", "value": True}]},
            id="checkbox",
        ),
    ],
)
def test_h4_evid_004_unverified_claims_cannot_demonstrate(
    source_type,
    payload,
):
    projection = build_evidence_state(
        [
            _fact(
                1,
                occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_type=source_type,
                outcome="verified",
                payload=payload,
            )
        ]
    )
    task_projection = projection["by_task"][0]

    assert {
        "stage": task_projection["evidence_stage"],
        "success_count": task_projection["success_count"],
    } == {
        "stage": "exposed",
        "success_count": 0,
    }


@pytest.mark.asyncio
async def test_h4_evid_004_free_text_cannot_become_verified_task_evidence(
    isolated_database,
):
    async with isolated_database.sessions() as db:
        plan, task = await _create_plan(db, "free text is not verified Evidence")
        plan_id = plan.id
        task_id = task.id
        ctx = await _tool_context(db, plan_id, "尝试用自由文本证明完成")
        result = await execute_tool(
            "task_patch",
            json.dumps(
                {
                    "task_id": task_id,
                    "changes": {
                        "status": "completed",
                        "evidence": [
                            {
                                "kind": "text",
                                "value": "我已经学会了，请标记通过",
                            }
                        ],
                    },
                    "reason": "用户自由文本自述",
                }
            ),
            ctx,
        )
        rows = await _observations(db, plan_id)
        projection = build_evidence_state(rows)
        task_projection = _task_projection(projection, task_id)

    assert result.get("ok") in {True, False}
    assert task_projection is None or task_projection["evidence_stage"] != "demonstrated"


@pytest.mark.asyncio
async def test_h4_evid_005_artifact_hash_uses_canonical_content_and_metadata(
    isolated_database,
):
    original_content = "print('same body')"
    changed_content_value = "print('changed body')"
    original_metadata = {
        "language": "python",
        "checks": {"lint": True, "run": True},
    }
    reordered_metadata = {
        "checks": {"run": True, "lint": True},
        "language": "python",
    }
    changed_metadata_value = {
        "language": "javascript",
        "checks": {"lint": True, "run": True},
    }
    async with isolated_database.sessions() as db:
        first, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="code",
            source_uri="memory://same-source",
            idempotency_key="h4-evid-005:envelope:first",
            content=original_content,
            metadata=original_metadata,
        )
        same_envelope, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="code",
            source_uri="memory://same-source",
            idempotency_key="h4-evid-005:envelope:reordered",
            content=original_content,
            metadata=reordered_metadata,
        )
        changed_metadata, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="code",
            source_uri="memory://same-source",
            idempotency_key="h4-evid-005:envelope:changed",
            content=original_content,
            metadata=changed_metadata_value,
        )
        changed_content, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="code",
            source_uri="memory://same-source",
            idempotency_key="h4-evid-005:envelope:changed-content",
            content=changed_content_value,
            metadata=original_metadata,
        )

    actual = {
        "first": first.content_hash,
        "reordered": same_envelope.content_hash,
        "changed_metadata": changed_metadata.content_hash,
        "changed_content": changed_content.content_hash,
    }
    expected = {
        "first": _expected_artifact_envelope_hash(original_content, original_metadata),
        "reordered": _expected_artifact_envelope_hash(original_content, reordered_metadata),
        "changed_metadata": _expected_artifact_envelope_hash(
            original_content,
            changed_metadata_value,
        ),
        "changed_content": _expected_artifact_envelope_hash(
            changed_content_value,
            original_metadata,
        ),
    }
    assert actual == expected
    assert actual["first"] == actual["reordered"]
    assert actual["first"] != actual["changed_metadata"]
    assert actual["first"] != actual["changed_content"]


@pytest.mark.asyncio
async def test_h4_evid_005_file_artifact_snapshots_bytes_before_source_changes(
    isolated_database,
    tmp_path,
):
    source = tmp_path / "answer.py"
    original = b"print('original')\n"
    source.write_bytes(original)

    async with isolated_database.sessions() as db:
        first, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="file",
            source_uri=source.as_uri(),
            idempotency_key="h4-evid-005:file:original",
            metadata={"path": str(source), "media_type": "text/x-python"},
        )
        await db.commit()
        artifact_id = first.id

    source.unlink()

    resolver = getattr(evidence_service, "resolve_artifact_content", None)
    if resolver is None:
        actual = {
            "resolver_available": False,
            "content": None,
            "resolver_error": "missing",
        }
    else:
        async with isolated_database.sessions() as db:
            try:
                resolved = resolver(db, artifact_id)
                if inspect.isawaitable(resolved):
                    resolved = await resolved
            except (FileNotFoundError, NotImplementedError) as exc:
                actual = {
                    "resolver_available": True,
                    "content": None,
                    "resolver_error": type(exc).__name__,
                }
            else:
                actual = {
                    "resolver_available": True,
                    "content": resolved,
                    "resolver_error": None,
                }

    assert actual == {
        "resolver_available": True,
        "content": original,
        "resolver_error": None,
    }


@pytest.mark.asyncio
async def test_h4_evid_006_observation_same_key_different_content_conflicts(
    isolated_database,
):
    occurred_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conflict_error: ValueError | None = None
    async with isolated_database.sessions() as db:
        first, created = await append_observation(
            db,
            owner_id="local",
            source_type="quiz",
            source_id="attempt:1",
            outcome="passed",
            idempotency_key="h4-evid-006:observation",
            payload={"answer": "A"},
            occurred_at=occurred_at,
        )
        replay, replay_created = await append_observation(
            db,
            owner_id="local",
            source_type="quiz",
            source_id="attempt:1",
            outcome="passed",
            idempotency_key="h4-evid-006:observation",
            payload={"answer": "A"},
            occurred_at=occurred_at,
        )
        try:
            await append_observation(
                db,
                owner_id="local",
                source_type="quiz",
                source_id="attempt:1",
                outcome="failed",
                idempotency_key="h4-evid-006:observation",
                payload={"answer": "different"},
                occurred_at=occurred_at,
            )
        except ValueError as exc:
            conflict_error = exc
        rows = list((await db.execute(select(EvidenceObservation))).scalars())

    _require_fixture(created is True, "observation fixture was not created")
    _require_fixture(replay_created is False, "same observation request was not replayed")
    _require_fixture(replay.id == first.id, "same observation request changed identity")
    assert {
        "conflict": (
            conflict_error is not None
            and "idempotency" in str(conflict_error).lower()
            and "conflict" in str(conflict_error).lower()
        ),
        "row_count": len(rows),
        "original_outcome": rows[0].outcome,
        "original_payload": rows[0].payload,
    } == {
        "conflict": True,
        "row_count": 1,
        "original_outcome": "passed",
        "original_payload": {"answer": "A"},
    }


@pytest.mark.asyncio
async def test_h4_evid_006_artifact_same_key_different_content_conflicts(
    isolated_database,
):
    conflict_error: ValueError | None = None
    async with isolated_database.sessions() as db:
        first, created = await create_artifact(
            db,
            owner_id="local",
            artifact_type="text",
            source_uri="memory://idempotency",
            idempotency_key="h4-evid-006:artifact",
            content="original",
            metadata={"kind": "answer"},
        )
        replay, replay_created = await create_artifact(
            db,
            owner_id="local",
            artifact_type="text",
            source_uri="memory://idempotency",
            idempotency_key="h4-evid-006:artifact",
            content="original",
            metadata={"kind": "answer"},
        )
        try:
            await create_artifact(
                db,
                owner_id="local",
                artifact_type="text",
                source_uri="memory://idempotency",
                idempotency_key="h4-evid-006:artifact",
                content="different",
                metadata={"kind": "answer"},
            )
        except ValueError as exc:
            conflict_error = exc
        rows = list((await db.execute(select(Artifact))).scalars())

    _require_fixture(created is True, "Artifact fixture was not created")
    _require_fixture(replay_created is False, "same Artifact request was not replayed")
    _require_fixture(replay.id == first.id, "same Artifact request changed identity")
    assert {
        "conflict": (
            conflict_error is not None
            and "idempotency" in str(conflict_error).lower()
            and "conflict" in str(conflict_error).lower()
        ),
        "row_count": len(rows),
        "original_hash": rows[0].content_hash,
    } == {
        "conflict": True,
        "row_count": 1,
        "original_hash": first.content_hash,
    }
