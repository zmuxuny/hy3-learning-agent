"""H0 failure baselines for the H2 transaction and side-effect gate.

Each test states an invariant first captured against the reviewed legacy
implementation.  H2 now keeps the former failure baselines as ordinary passing
regressions for Unit of Work ownership, short CAS claims, request-digest
conflicts, and the durable outbox.  The domain digest portion of H4-EVID-006 was
closed early by the same request-identity work and is verified separately.

Defect mapping:

* H2-TXN-001: transaction ownership and tool effect classification
* H2-TXN-002: atomic concurrent ToolInvocation claim
* H2-TXN-003: stable action key plus canonical request digest
* H2-TXN-004: one Unit of Work for domain/result/event persistence
* H2-TXN-005: no external await while a SQLite writer lock is held
* H2-TXN-006: final-model waits and child cancellation outside write locks
* H2-TXN-007: outbox and needs_reconciliation for uncertain delivery
* H2-TXN-008: recoverable filesystem side effects
* H2-TXN-009: real SQLite contention across main, child, and heartbeat actors
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.runtime.agent as agent_runtime_module
import app.runtime.scheduler as scheduler_module
import app.runtime.subagents as runtime_subagents
import app.outbox as outbox_module
import app.tools.registry as tool_registry
import app.tools.web as web_tools
import app.tools.workspace as workspace_tools
from app.context.memory import MemoryManager
from app.db.database import Base
from app.models import (
    AgentRun,
    CalendarEvent,
    Notification,
    Operation,
    Owner,
    RunEvent,
    Session,
    ToolInvocation,
    UserProfile,
)
from app.notifications.service import NotificationService
from app.outbox import dispatch_once
from app.runtime.agent import AgentRuntime
from app.runtime.events import emit_event
from app.runtime.scheduler import ProactiveScheduler
from app.tools import ToolContext, execute_tool
from app.tools.base import ToolDefinition


OWNER_ID = "local"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ProbeArgs(BaseModel):
    value: int = 1


class ProbeOutput(BaseModel):
    value: int


class InjectedProcessCrash(BaseException):
    """A kill-point that production ``except Exception`` blocks cannot absorb."""


class HarnessInvariantError(RuntimeError):
    """A broken test precondition, distinct from a protocol assertion."""


def _require_harness(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessInvariantError(message)


@pytest_asyncio.fixture
async def sqlite_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create a per-test WAL database with a deliberately short lock timeout."""

    database_path = tmp_path / "h0-transactions.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_guards(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=75")
        cursor.close()

    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.commit()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add(Owner(id=OWNER_ID, display_name="H0 transaction fixture"))
            db.add(UserProfile(owner_id=OWNER_ID))
            await db.commit()
        yield session_factory
    finally:
        await engine.dispose()


async def _create_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    trigger: str = "user_message",
    session_id: str | None = None,
    parent_run_id: str | None = None,
    status: str = "queued",
) -> str:
    async with session_factory() as db:
        run = AgentRun(
            owner_id=OWNER_ID,
            session_id=session_id,
            parent_run_id=parent_run_id,
            trigger=trigger,
            objective=f"H0 transaction probe: {trigger}",
            status=status,
            started_at=datetime.now(timezone.utc) if status == "running" else None,
        )
        db.add(run)
        await db.commit()
        return run.id


def _calendar_payload(title: str, hour: int) -> str:
    return json.dumps(
        {
            "title": title,
            "starts_at": f"2030-01-02T{hour:02d}:00:00+00:00",
        },
        sort_keys=True,
    )


def _final_message(content: str = "事务完成。") -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        reasoning_content=None,
        tool_calls=None,
    )


async def _no_next_message(*_args, **_kwargs):
    return None


def test_registered_tools_are_classified_and_handlers_do_not_commit() -> None:
    restricted_roots = [
        PROJECT_ROOT / "backend" / "app" / "tools",
        PROJECT_ROOT / "backend" / "app" / "services",
        PROJECT_ROOT / "backend" / "app" / "notifications",
    ]
    offenders: list[str] = []
    for root in restricted_roots:
        for source_path in sorted(root.rglob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
                    continue
                call = node.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "commit":
                    offenders.append(f"{source_path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    base_tree = ast.parse(
        (PROJECT_ROOT / "backend" / "app" / "tools" / "base.py").read_text(encoding="utf-8")
    )
    tool_definition = next(
        node
        for node in base_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ToolDefinition"
    )
    declared_fields = {
        node.target.id
        for node in tool_definition.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    unclassified_tools = sorted(
        tool.name for tool in tool_registry.TOOL_MAP.values() if not getattr(tool, "effect_kind", None)
    )

    registry_tree = ast.parse(
        (PROJECT_ROOT / "backend" / "app" / "tools" / "registry.py").read_text(encoding="utf-8")
    )
    executor = next(
        node
        for node in registry_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute_tool"
    )
    executor_consumes_effect_kind = any(
        (isinstance(node, ast.Attribute) and node.attr == "effect_kind")
        or (isinstance(node, ast.Name) and node.id == "effect_kind")
        for node in ast.walk(executor)
    )

    assert (
        "effect_kind" in declared_fields
        and not unclassified_tools
        and executor_consumes_effect_kind
        and not offenders
    ), (
        f"missing effect_kind={('effect_kind' not in declared_fields)}; "
        f"unclassified tools={unclassified_tools}; "
        f"executor consumes classification={executor_consumes_effect_kind}; "
        f"direct commit boundaries={offenders}"
    )


@pytest.mark.asyncio
async def test_concurrent_tool_claim_has_one_winner_and_a_typed_loser(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    handler_calls = 0
    handler_entered = asyncio.Event()
    release_handler = asyncio.Event()

    async def handler(_ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        handler_entered.set()
        await release_handler.wait()
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h0_claim_probe",
        ToolDefinition(
            "h0_claim_probe",
            "Deterministic claim probe.",
            ProbeArgs,
            handler,
            output_model=ProbeOutput,
            idempotent=True,
        ),
    )

    async with sqlite_factory() as first_db, sqlite_factory() as second_db:
        raw = json.dumps({"value": 7})
        ready_count = 0
        both_ready = asyncio.Event()
        start = asyncio.Event()

        async def contender(db: AsyncSession) -> dict:
            nonlocal ready_count
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
            await start.wait()
            return await execute_tool(
                "h0_claim_probe",
                raw,
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="same-provider-call",
                ),
            )

        first_task = asyncio.create_task(contender(first_db))
        second_task = asyncio.create_task(contender(second_db))
        await asyncio.wait_for(both_ready.wait(), timeout=1)
        start.set()
        try:
            await asyncio.wait_for(handler_entered.wait(), timeout=1)
            # Keep the winning handler open past SQLite's busy timeout.  This
            # exercises the observable claim contract without matching SQL or
            # assuming whether the implementation uses INSERT, UPSERT, or CAS.
            await asyncio.sleep(0.15)
        finally:
            release_handler.set()
        results = await asyncio.wait_for(
            asyncio.gather(first_task, second_task, return_exceptions=True),
            timeout=2,
        )

    async with sqlite_factory() as db:
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
    typed_results = [item for item in results if isinstance(item, dict)]
    winners = [item for item in typed_results if item.get("ok") is True]
    losers = [item for item in typed_results if item.get("ok") is False]
    typed_loser = (
        len(losers) == 1
        and losers[0].get("error_code") == "invocation_in_progress"
        and losers[0].get("status") == "running"
        and losers[0].get("reason") == "already_claimed"
    )
    assert (
        len(typed_results) == 2
        and len(winners) == 1
        and typed_loser
        and handler_calls == 1
        and len(invocations) == 1
        and invocations[0].status == "committed"
    ), (
        f"results={results!r}; handler_calls={handler_calls}; "
        f"invocations={[(item.status, item.result_payload) for item in invocations]}"
    )


@pytest.mark.asyncio
async def test_same_action_key_with_a_different_request_is_a_conflict(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _create_run(sqlite_factory)
    async with sqlite_factory() as db:
        context = ToolContext(
            db=db,
            owner_id=OWNER_ID,
            run_id=run_id,
            trigger="user_message",
            tool_call_id="stable-calendar-action",
        )
        first = await execute_tool("calendar_create", _calendar_payload("A", 9), context)
        second = await execute_tool("calendar_create", _calendar_payload("B", 10), context)
        _require_harness(first.get("ok") is True, f"baseline calendar write failed: {first!r}")

    async with sqlite_factory() as db:
        events = list((await db.execute(select(CalendarEvent))).scalars())
        operations = list((await db.execute(select(Operation))).scalars())
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
    assert (
        second.get("ok") is False
        and second.get("error_code") == "idempotency_conflict"
        and (len(events), len(operations), len(invocations)) == (1, 1, 1)
    ), (
        f"second={second!r}; calendar={len(events)}, operations={len(operations)}, "
        f"invocations={len(invocations)}"
    )


@pytest.mark.asyncio
async def test_commit_failure_never_exposes_domain_data_with_a_running_invocation(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    boundary_seen = False
    crash_captured = False
    action_title = "split commit"
    async with sqlite_factory() as db:
        original_commit = db.commit

        async def fail_domain_and_result_boundary() -> None:
            nonlocal boundary_seen
            finalizing_invocation = any(
                isinstance(item, ToolInvocation)
                and item.status == "committed"
                and bool(item.result_payload)
                for item in db.identity_map.values()
            )
            if finalizing_invocation:
                # Flush makes both old already-committed facts and a future
                # single-UoW implementation visible to this transaction.  The
                # kill point is therefore identified by the domain/result
                # boundary, not by an implementation-specific commit number.
                await db.flush()
                event_id = await db.scalar(
                    select(CalendarEvent.id).where(CalendarEvent.title == action_title)
                )
                operation_id = await db.scalar(
                    select(Operation.id).where(
                        Operation.run_id == run_id,
                        Operation.tool_name == "calendar.create",
                    )
                )
                if event_id is not None and operation_id is not None:
                    boundary_seen = True
                    raise InjectedProcessCrash(
                        "H2-TXN-004 killed the domain/invocation atomic boundary"
                    )
            await original_commit()

        monkeypatch.setattr(db, "commit", fail_domain_and_result_boundary)
        try:
            await execute_tool(
                "calendar_create",
                _calendar_payload(action_title, 11),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="split-commit-action",
                ),
            )
        except InjectedProcessCrash:
            crash_captured = True
            await db.rollback()

    _require_harness(boundary_seen, "domain/invocation kill point was not reached")
    _require_harness(crash_captured, "injected domain/invocation crash escaped capture")

    async with sqlite_factory() as db:
        events = list((await db.execute(select(CalendarEvent))).scalars())
        operations = list((await db.execute(select(Operation))).scalars())
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
    nothing_committed = (
        not events
        and not operations
        and not any(item.status == "committed" for item in invocations)
    )
    everything_committed = (
        len(events) == len(operations) == len(invocations) == 1
        and invocations[0].status == "committed"
        and bool(invocations[0].result_payload)
    )
    assert nothing_committed or everything_committed, (
        f"calendar={len(events)}, operations={len(operations)}, "
        f"invocations={[(item.status, item.result_payload) for item in invocations]}"
    )


@pytest.mark.asyncio
async def test_external_web_wait_does_not_hold_the_sqlite_writer_lock(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    entered_provider = asyncio.Event()
    release_provider = asyncio.Event()

    class FakeProvider:
        name = "h0-offline-provider"

        async def search(self, _query: str, _limit: int):
            entered_provider.set()
            await release_provider.wait()
            return []

    monkeypatch.setattr(web_tools, "get_search_provider", lambda _name: FakeProvider())
    monkeypatch.setattr(web_tools.settings, "WEB_SEARCH_FALLBACK_PROVIDER", "none")

    async with sqlite_factory() as tool_db:
        tool_task = asyncio.create_task(
            execute_tool(
                "web_search",
                json.dumps({"query": "offline transaction probe", "limit": 1, "save_results": False}),
                ToolContext(
                    db=tool_db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="external-read-action",
                ),
            )
        )
        await asyncio.wait_for(entered_provider.wait(), timeout=1)
        writer_error: BaseException | None = None
        try:
            async with sqlite_factory() as writer_db:
                profile = await writer_db.get(UserProfile, OWNER_ID)
                _require_harness(profile is not None, "transaction fixture lost its user profile")
                profile.agent_style = "writer progressed while provider waited"
                await writer_db.commit()
        except BaseException as exc:  # captured so the provider gate is always released
            writer_error = exc
        finally:
            release_provider.set()
        tool_result = await asyncio.wait_for(tool_task, timeout=1)

    if isinstance(writer_error, HarnessInvariantError):
        raise writer_error
    _require_harness(
        tool_result.get("ok") is True,
        f"offline provider baseline did not complete successfully: {tool_result!r}",
    )
    assert writer_error is None, f"concurrent writer failed during external wait: {writer_error!r}"


@pytest.mark.asyncio
async def test_final_model_wait_does_not_hold_the_sqlite_writer_lock(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    entered_title_model = asyncio.Event()
    release_title_model = asyncio.Event()
    runtime = AgentRuntime()

    async def fake_model(*_args, **_kwargs):
        return _final_message(), None

    async def gated_title(*_args, **_kwargs):
        entered_title_model.set()
        await release_title_model.wait()
        return False

    async def no_compression(*_args, **_kwargs):
        return False

    async def no_child_cancel(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(runtime, "_call_model", fake_model)
    monkeypatch.setattr(runtime, "_start_next_queued_message", _no_next_message)
    monkeypatch.setattr(agent_runtime_module, "generate_session_title", gated_title)
    monkeypatch.setattr(MemoryManager, "compress_session", no_compression)
    monkeypatch.setattr(runtime_subagents, "cancel_children_for_parent", no_child_cancel)

    async with sqlite_factory() as db:
        session = Session(owner_id=OWNER_ID, title="H0 finalization")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id=OWNER_ID,
            session_id=session.id,
            trigger="user_message",
            objective="finalize without a long writer lock",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.commit()

        runtime_task = asyncio.create_task(
            runtime._loop(
                db,
                run,
                [],
                start_step=0,
                pending_calls=[],
                granted=set(),
                session=session,
                run_cards=[],
                context_snapshot_id=None,
            )
        )
        await asyncio.wait_for(entered_title_model.wait(), timeout=1)
        writer_error: BaseException | None = None
        try:
            async with sqlite_factory() as writer_db:
                profile = await writer_db.get(UserProfile, OWNER_ID)
                _require_harness(profile is not None, "transaction fixture lost its user profile")
                profile.agent_style = "writer progressed during title generation"
                await writer_db.commit()
        except BaseException as exc:
            writer_error = exc
        finally:
            release_title_model.set()
        await asyncio.wait_for(runtime_task, timeout=2)
        _require_harness(
            run.status == "completed",
            f"finalization baseline did not complete successfully: status={run.status!r}",
        )

    if isinstance(writer_error, HarnessInvariantError):
        raise writer_error
    assert writer_error is None, f"title model retained the writer lock: {writer_error!r}"


@pytest.mark.asyncio
async def test_parent_finalization_does_not_self_lock_while_cancelling_a_child(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    runtime = AgentRuntime()

    async def fake_model(*_args, **_kwargs):
        return _final_message("父任务完成。"), None

    async def no_title(*_args, **_kwargs):
        return False

    async def no_compression(*_args, **_kwargs):
        return False

    monkeypatch.setattr(runtime, "_call_model", fake_model)
    monkeypatch.setattr(runtime, "_start_next_queued_message", _no_next_message)
    monkeypatch.setattr(agent_runtime_module, "generate_session_title", no_title)
    monkeypatch.setattr(MemoryManager, "compress_session", no_compression)
    monkeypatch.setattr(agent_runtime_module, "AsyncSessionLocal", sqlite_factory)
    monkeypatch.setattr(runtime_subagents, "AsyncSessionLocal", sqlite_factory)

    async with sqlite_factory() as db:
        session = Session(owner_id=OWNER_ID, title="H0 child cancellation")
        db.add(session)
        await db.flush()
        parent = AgentRun(
            owner_id=OWNER_ID,
            session_id=session.id,
            trigger="user_message",
            objective="complete with an active child",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id=OWNER_ID,
            session_id=session.id,
            parent_run_id=parent.id,
            trigger="subagent",
            objective="still active",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(child)
        await db.commit()
        parent_id = parent.id
        child_id = child.id

        await asyncio.wait_for(
            runtime._loop(
                db,
                parent,
                [],
                start_step=0,
                pending_calls=[],
                granted=set(),
                session=session,
                run_cards=[],
                context_snapshot_id=None,
            ),
            timeout=2,
        )
        await db.rollback()

    async with sqlite_factory() as db:
        stored_parent = await db.get(AgentRun, parent_id)
        stored_child = await db.get(AgentRun, child_id)
    _require_harness(stored_parent is not None, "parent fixture row disappeared")
    _require_harness(stored_child is not None, "child fixture row disappeared")
    assert stored_parent.status == "completed" and stored_child.status == "cancelled", (
        f"parent={stored_parent.status!r}; child={stored_child.status!r}"
    )


@pytest.mark.asyncio
async def test_smtp_accept_then_receipt_commit_failure_is_not_replayed(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    delivery_state = {"accepted": False, "failed_receipt": False, "send_count": 0}
    crash_captured = False

    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)

    def accept_email(_self, _reply_token: str, _title: str, _body: str) -> None:
        delivery_state["send_count"] += 1
        delivery_state["accepted"] = True

    monkeypatch.setattr(NotificationService, "_send_email", accept_email)
    raw = json.dumps(
        {
            "title": "H0 SMTP receipt probe",
            "body": "This message is accepted by a fake transport only.",
            "channels": ["email"],
        },
        sort_keys=True,
    )

    async with sqlite_factory() as first_db:
        enqueue_result = await execute_tool(
            "notification_send",
            raw,
            ToolContext(
                db=first_db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id="stable-email-action",
            ),
        )
    _require_harness(
        enqueue_result.get("ok") is True,
        f"SMTP intent was not enqueued: {enqueue_result!r}",
    )
    _require_harness(
        delivery_state["send_count"] == 0,
        "SMTP ran inline before its durable outbox fence",
    )

    def fail_receipt_commit(_sync_session) -> None:
        if delivery_state["accepted"] and not delivery_state["failed_receipt"]:
            delivery_state["failed_receipt"] = True
            raise InjectedProcessCrash(
                "H2-TXN-007 killed receipt persistence after SMTP accept"
            )

    event.listen(AsyncSession.sync_session_class, "before_commit", fail_receipt_commit)
    try:
        try:
            await dispatch_once(session_factory=sqlite_factory)
        except InjectedProcessCrash:
            crash_captured = True
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", fail_receipt_commit)

    _require_harness(delivery_state["failed_receipt"], "SMTP receipt kill point was not reached")
    _require_harness(crash_captured, "injected SMTP receipt crash escaped capture")

    async with sqlite_factory() as db:
        states_after_crash = {
            *(await db.execute(select(Notification.status))).scalars(),
            *(await db.execute(select(ToolInvocation.status))).scalars(),
        }

    # Exercise the real replay path with the same stable action key.  The old
    # implementation forgot the accepted delivery and calls SMTP a second time.
    async with sqlite_factory() as retry_db:
        second_result = await execute_tool(
            "notification_send",
            raw,
            ToolContext(
                db=retry_db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id="stable-email-action",
            ),
        )
    _require_harness(isinstance(second_result, dict), "SMTP replay returned an untyped result")

    async with sqlite_factory() as db:
        notification_states = list((await db.execute(select(Notification.status))).scalars())
        invocation_states = list((await db.execute(select(ToolInvocation.status))).scalars())
    assert (
        delivery_state["send_count"] == 1
        and "needs_reconciliation" in states_after_crash
        and "needs_reconciliation" in {*notification_states, *invocation_states}
    ), (
        f"second={second_result!r}; after_crash={sorted(states_after_crash)}; "
        f"send_count={delivery_state['send_count']}; notifications={notification_states}; "
        f"invocations={invocation_states}"
    )


@pytest.mark.asyncio
async def test_file_write_commit_failure_leaves_no_orphan_side_effect(
    sqlite_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    workspace_root = tmp_path / "isolated-workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace_root)
    target_path = workspace_root / "artifact.txt"
    write_count = 0
    original_publish = outbox_module.publish_workspace_file

    async def counted_publish(
        path: Path,
        content: str,
        *,
        staging_token: str,
    ) -> None:
        nonlocal write_count
        if path == target_path:
            write_count += 1
        await original_publish(path, content, staging_token=staging_token)

    monkeypatch.setattr(outbox_module, "publish_workspace_file", counted_publish)
    crash_captured = False
    commit_kill_point_seen = False
    raw = json.dumps(
        {"path": "artifact.txt", "content": "external side effect", "overwrite": True}
    )

    async with sqlite_factory() as db:
        enqueue_result = await execute_tool(
            "file_write",
            raw,
            ToolContext(
                db=db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id="stable-file-action",
            ),
        )
    _require_harness(
        enqueue_result.get("ok") is True,
        f"file intent was not enqueued: {enqueue_result!r}",
    )
    _require_harness(
        not target_path.exists(),
        "file_write published inline before its durable outbox fence",
    )

    def fail_after_file_side_effect(_sync_session) -> None:
        nonlocal commit_kill_point_seen
        if target_path.exists() and not commit_kill_point_seen:
            commit_kill_point_seen = True
            raise InjectedProcessCrash(
                "H2-TXN-008 killed persistence after file write"
            )

    event.listen(
        AsyncSession.sync_session_class,
        "before_commit",
        fail_after_file_side_effect,
    )
    try:
        try:
            await dispatch_once(session_factory=sqlite_factory)
        except InjectedProcessCrash:
            crash_captured = True
    finally:
        event.remove(
            AsyncSession.sync_session_class,
            "before_commit",
            fail_after_file_side_effect,
        )

    _require_harness(commit_kill_point_seen, "file side-effect kill point was not reached")
    _require_harness(crash_captured, "injected file persistence crash escaped capture")

    async with sqlite_factory() as db:
        operations_after_crash = list((await db.execute(select(Operation))).scalars())
        invocations_after_crash = list((await db.execute(select(ToolInvocation))).scalars())
    file_existed_after_crash = target_path.exists()
    reconciliation_after_crash = any(
        item.status == "needs_reconciliation" for item in invocations_after_crash
    )

    # Retry through the public tool boundary with the identical action key.
    # An uncertain applied write must be reconciled, not blindly written again.
    async with sqlite_factory() as retry_db:
        retry_result = await execute_tool(
            "file_write",
            raw,
            ToolContext(
                db=retry_db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id="stable-file-action",
            ),
        )
    _require_harness(isinstance(retry_result, dict), "file replay returned an untyped result")

    async with sqlite_factory() as db:
        operations = list((await db.execute(select(Operation))).scalars())
        invocations = list((await db.execute(select(ToolInvocation))).scalars())
    assert (
        (not file_existed_after_crash or reconciliation_after_crash)
        and (not file_existed_after_crash or write_count == 1)
    ), (
        f"retry={retry_result!r}; file_after_crash={file_existed_after_crash}; "
        f"writes={write_count}; operations_after_crash={len(operations_after_crash)}; "
        f"invocations_after_crash={[(item.status, item.result_payload) for item in invocations_after_crash]}; "
        f"operations={len(operations)}; "
        f"invocations={[(item.status, item.result_payload) for item in invocations]}"
    )


@pytest.mark.asyncio
async def test_main_child_and_heartbeat_survive_real_sqlite_writer_contention(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    main_run_id = await _create_run(sqlite_factory, status="queued")
    child_run_id = await _create_run(
        sqlite_factory,
        trigger="subagent",
        parent_run_id=main_run_id,
        status="running",
    )
    handler_calls = 0

    async def handler(_ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h0_contention_probe",
        ToolDefinition(
            "h0_contention_probe",
            "Deterministic SQLite contention probe.",
            ProbeArgs,
            handler,
            output_model=ProbeOutput,
            idempotent=True,
        ),
    )
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", sqlite_factory)

    def do_not_start_runtime(_run_id: str, coroutine):
        coroutine.close()
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(scheduler_module, "start_tracked_task", do_not_start_runtime)
    scheduler = ProactiveScheduler()

    async def main_actor():
        async with sqlite_factory() as db:
            return await execute_tool(
                "h0_contention_probe",
                json.dumps({"value": 9}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=main_run_id,
                    trigger="user_message",
                    tool_call_id="contention-action",
                ),
            )

    async def child_actor():
        async with sqlite_factory() as db:
            return await emit_event(db, child_run_id, "tool.completed", "contention recovered")

    async with sqlite_factory() as lock_holder:
        await lock_holder.execute(text("BEGIN IMMEDIATE"))
        await lock_holder.execute(
            text("UPDATE user_profiles SET coach_style = 'held writer' WHERE owner_id = :owner_id"),
            {"owner_id": OWNER_ID},
        )
        main_task = asyncio.create_task(main_actor())
        child_task = asyncio.create_task(child_actor())
        heartbeat_task = asyncio.create_task(
            scheduler.trigger_now(
                "manual_heartbeat",
                objective="H0 heartbeat contention probe",
            )
        )
        try:
            await asyncio.wait(
                {main_task, heartbeat_task},
                timeout=0.4,
                return_when=asyncio.ALL_COMPLETED,
            )
        finally:
            await lock_holder.rollback()

    results: list[Any] = await asyncio.wait_for(
        asyncio.gather(main_task, child_task, heartbeat_task, return_exceptions=True),
        timeout=2,
    )
    async with sqlite_factory() as db:
        invocations = list((await db.execute(
            select(ToolInvocation).where(ToolInvocation.run_id == main_run_id)
        )).scalars())
        child_events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == child_run_id)
        )).scalars())
        heartbeat_runs = list((await db.execute(
            select(AgentRun).where(AgentRun.trigger.in_(["heartbeat", "manual_heartbeat"]))
        )).scalars())

    assert not any(isinstance(item, BaseException) for item in results), f"actor failures: {results!r}"
    assert handler_calls == 1
    assert len(invocations) == 1 and invocations[0].status == "committed"
    assert len(child_events) == 1
    assert len(heartbeat_runs) == 1
