"""H2 completion tests for transaction ownership and recoverable effects.

The older H0 baselines prove the first visible symptom of each defect.  This
module specifies the missing protocol surface: the complete commit allowlist,
canonical request identity, the full domain/event atomic set, representative
external waits, and both sides of durable external-effect reconciliation.

Every injected process death inherits directly from :class:`BaseException` so
production ``except Exception`` blocks cannot turn a broken kill point into a
domain result.  Harness precondition failures use the same rule, keeping a
broken fixture distinguishable from an actual protocol assertion.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.runtime.agent as agent_runtime_module
import app.runtime.scheduler as scheduler_module
import app.runtime.subagents as runtime_subagents
import app.outbox as outbox_module
import app.context.memory as memory_context_module
import app.tools.registry as tool_registry
import app.tools.subagents as subagent_tools
import app.tools.web as web_tools
import app.tools.workspace as workspace_tools
from app.db.migrations import migrate_sqlite_database
from app.db.uow import DatabaseBusyError, commit as commit_uow, rollback as rollback_uow
from app.models import (
    AgentRun,
    Artifact,
    ChatMessage,
    EvidenceObservation,
    LearningEvent,
    Memory,
    Notification,
    Operation,
    OutboxAction,
    OutboxReceipt,
    Owner,
    Plan,
    RunEvent,
    Session,
    Stage,
    Task,
    TaskSubmission,
    ToolInvocation,
    UserProfile,
)
from app.notifications.service import NotificationService
from app.runtime.agent import AgentRuntime
from app.runtime.checkpoints import make_checkpoint
from app.runtime.events import emit_event
from app.runtime.scheduler import ProactiveScheduler
from app.services.evidence import append_observation, create_artifact
from app.tools import ToolContext, execute_tool
from app.tools.base import ToolDefinition


OWNER_ID = "local"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALID_EFFECT_KINDS = {
    "pure_read",
    "database_write",
    "external_read",
    "external_write",
}
ALLOWED_TRANSACTION_CALLS = {
    # H1's synchronous file migration/maintenance internals own sqlite3
    # connections.  They are intentionally separate from application UoWs.
    ("backend/app/db/migrations.py", "_writer_guard", "rollback"),
    ("backend/app/db/migrations.py", "_publication_guard", "rollback"),
    ("backend/app/db/migrations.py", "_reserve_absent_database_path", "rollback"),
    ("backend/app/db/migrations.py", "_snapshot_database", "commit"),
    ("backend/app/db/migrations.py", "_canonicalize_live_journal", "rollback"),
    ("backend/app/db/maintenance.py", "_recover_sqlite_crash_journals_locked", "rollback"),
    ("backend/app/db/maintenance.py", "_writer_guards", "rollback"),
    ("backend/app/db/maintenance.py", "_snapshot_database", "commit"),
    # Every application AsyncSession boundary is physically centralized here.
    ("backend/app/db/uow.py", "_safe_rollback", "rollback"),
    ("backend/app/db/uow.py", "rollback", "rollback"),
    ("backend/app/db/uow.py", "commit", "commit"),
    ("backend/app/db/uow.py", "flush", "flush"),
}
ALLOWED_FILE_FLUSH_CALLS = {
    # File-object durability flushes are not AsyncSession transaction ownership.
    # Keep them exact so a new db.flush() in the same module cannot hide here.
    ("backend/app/api/workspace.py", "_atomic_publish_upload", "stream"),
    ("backend/app/context/assembler.py", "_atomic_write_text", "stream"),
    ("backend/app/core/envfile.py", "_atomic_replace", "stream"),
    ("backend/app/db/maintenance.py", "_write_exclusive", "handle"),
    ("backend/app/outbox.py", "_atomic_replace_text", "stream"),
    ("scripts/rebuild-evidence.py", "_write_json", "stream"),
}
ALLOWED_UOW_ALIAS_CALLS = {
    # HTTP endpoints own one request-level Unit of Work.
    *{
        ("backend/app/api/agent.py", name, "commit")
        for name in {
            "create_run",
            "rename_session",
            "handoff_session",
            "submit_planning_answers",
            "decide_plan_proposal",
            "edit_user_message",
            "cancel_run",
            "enqueue_message",
            "update_queued_message",
            "delete_queued_message",
            "send_queued_message",
        }
    },
    *{
        ("backend/app/api/agent.py", name, "rollback")
        for name in {
            "create_run",
            "handoff_session",
            "submit_planning_answers",
            "edit_user_message",
        }
    },
    *{
        ("backend/app/api/memories.py", name, "commit")
        for name in {
            "create_memory_proposal",
            "confirm_memory",
            "delete_memory",
            "restore_memory",
            "create_snapshot",
        }
    },
    *{
        ("backend/app/api/notifications.py", name, "commit")
        for name in {
            "create_push_subscription",
            "delete_push_subscription",
            "archive_read_notifications",
            "mark_notification_read",
            "open_notification",
            "set_notification_archived",
        }
    },
    ("backend/app/api/operations.py", "undo_operation", "commit"),
    ("backend/app/api/operations.py", "undo_operation", "rollback"),
    ("backend/app/api/operations.py", "redo_operation", "commit"),
    ("backend/app/api/operations.py", "redo_operation", "rollback"),
    *{
        ("backend/app/api/plans.py", name, "commit")
        for name in {"create_plan", "set_plan_archived", "update_task"}
    },
    *{
        ("backend/app/api/profile.py", name, "commit")
        for name in {"read_profile", "update_profile"}
    },
    *{
        ("backend/app/api/settings.py", name, "commit")
        for name in {
            "update_followup_behavior",
            "update_proactive_pause",
            "update_notification_policy",
            "test_email_configuration",
        }
    },
    ("backend/app/api/system.py", "dashboard", "commit"),
    # Runtime/lifecycle coordinators deliberately cross several short UoWs.
    ("backend/app/context/assembler.py", "build", "commit"),
    ("backend/app/context/memory.py", "retrieve_with_scores", "commit"),
    ("backend/app/context/memory.py", "maintain", "commit"),
    ("backend/app/context/memory.py", "compress_session", "commit"),
    ("backend/app/main.py", "ensure_local_owner", "commit"),
    ("backend/app/main.py", "verify_database_writable", "rollback"),
    ("backend/app/main.py", "reconcile_interrupted_runs", "commit"),
    # IMAP acknowledgement is deliberately split into durable continuation,
    # fenced ACK claim, external Seen mutation, and terminal ACK receipt.
    *{
        ("backend/app/notifications/email.py", name, "commit")
        for name in {
            "poll",
            "_claim_ack_jobs",
            "_complete_ack_claims",
            "_fail_ack_claims",
        }
    },
    *{
        ("backend/app/runtime/agent.py", name, "commit")
        for name in {
            "_initialize_run",
            "_refresh_stateless_context",
            "_loop",
            "_after_terminal",
            "_ensure_session",
            "_call_model",
        }
    },
    ("backend/app/runtime/agent.py", "run", "rollback"),
    ("backend/app/runtime/agent.py", "_fail", "rollback"),
    ("backend/app/runtime/events.py", "emit_event", "commit"),
    ("backend/app/runtime/events.py", "emit_event", "rollback"),
    *{
        ("backend/app/runtime/state.py", name, method)
        for name, method in {
            ("persist_checkpoint", "rollback"),
            ("persist_checkpoint", "commit"),
            ("pause_for_approval", "rollback"),
            ("pause_for_approval", "commit"),
            ("decide_approval", "commit"),
            ("record_steer", "commit"),
        }
    },
    ("backend/app/runtime/subagents.py", "execute_durable_child", "rollback"),
    ("scripts/h3-runtime-recovery-demo.py", "_seed", "commit"),
    # These are protocol coordinators, not ordinary effect handlers: each
    # releases/finishes a short DB phase before or after external work.
    *{
        ("backend/app/tools/planning.py", name, method)
        for name, method in {
            ("persist_children", "commit"),
        }
    },
    *{
        ("backend/app/tools/registry.py", name, method)
        for name, method in {
            ("_release_claim_read", "commit"),
            ("_persist_invocation_failure", "rollback"),
            ("_persist_invocation_failure", "commit"),
            ("_transition_owned_invocation", "rollback"),
            ("_mark_retry_pending", "rollback"),
            ("_durable_claim_loss_result", "rollback"),
            ("_append_atomic_completion", "commit"),
            ("execute_tool", "rollback"),
            ("execute_tool", "commit"),
        }
    },
    *{
        ("backend/app/tools/subagents.py", name, method)
        for name, method in {
            ("persist_new_child", "commit"),
            ("release_replay_read", "commit"),
            ("subagent_cancel", "commit"),
        }
    },
    ("scripts/rebuild-evidence.py", "_run_uncoordinated", "commit"),
}


class ProbeArgs(BaseModel):
    value: int = 1


class ProbeOutput(BaseModel):
    value: int


class InjectedProcessCrash(BaseException):
    """A deterministic process-death boundary production must not absorb."""


class HarnessInvariantError(BaseException):
    """A broken fixture or probe, distinct from a protocol assertion."""


def _require_harness(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessInvariantError(message)


@pytest_asyncio.fixture
async def sqlite_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Migrate an offline per-test database, then enable WAL lock probes.

    This intentionally consumes the production migration output instead of
    calling ``Base.metadata.create_all``.  A missing H2 table, column, index,
    migration-history row, or legacy conversion must therefore fail the same
    way it would for a real upgraded installation.
    """

    database_path = tmp_path / "h2-transaction-protocol.sqlite3"
    report = await asyncio.to_thread(
        migrate_sqlite_database,
        database_path,
        backup_root=tmp_path / "migration-backups",
        application_version="h2-transaction-test",
        database_identity=database_path.name,
    )
    _require_harness(
        report.database_path == str(database_path),
        f"migration prepared an unexpected database: {report!r}",
    )
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_guards(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=0")
        cursor.close()

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.commit()
        async with factory() as db:
            db.add(Owner(id=OWNER_ID, display_name="H2 transaction fixture"))
            db.add(UserProfile(owner_id=OWNER_ID))
            await db.commit()
        yield factory
    finally:
        await engine.dispose()


async def _create_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    trigger: str = "user_message",
    status: str = "queued",
    session_id: str | None = None,
    parent_run_id: str | None = None,
) -> str:
    async with factory() as db:
        run = AgentRun(
            owner_id=OWNER_ID,
            trigger=trigger,
            status=status,
            session_id=session_id,
            parent_run_id=parent_run_id,
            objective=f"H2 protocol probe: {trigger}",
            started_at=datetime.now(timezone.utc) if status == "running" else None,
        )
        db.add(run)
        await db.commit()
        return run.id


def _tool_definition(
    name: str,
    handler: Callable[[ToolContext, ProbeArgs], Awaitable[dict[str, int]]],
) -> ToolDefinition:
    """Build a probe tool across the pre-H2 and post-H2 dataclass shapes."""

    kwargs: dict[str, Any] = {
        "output_model": ProbeOutput,
        "idempotent": True,
    }
    if "effect_kind" in {item.name for item in fields(ToolDefinition)}:
        exemplar = getattr(tool_registry.TOOL_MAP["calendar_create"], "effect_kind", None)
        if isinstance(exemplar, Enum):
            enum_type = type(exemplar)
            kwargs["effect_kind"] = next(
                item for item in enum_type if str(item.value) == "database_write"
            )
        else:
            kwargs["effect_kind"] = "database_write"
    return ToolDefinition(name, "Deterministic H2 protocol probe.", ProbeArgs, handler, **kwargs)


def _effect_value(value: Any) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    return str(value) if value is not None else None


def _pending_objects(sync_session) -> list[Any]:
    return (
        list(sync_session.new)
        + list(sync_session.dirty)
        + list(sync_session.identity_map.values())
    )


async def _durable_statuses(
    factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str | None = None,
) -> set[str]:
    """Read every mapped/protocol status without assuming an outbox table name."""

    statuses: set[str] = set()
    async with factory() as db:
        table_names = list(
            (
                await db.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )
            ).scalars()
        )
        for table_name in table_names:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
                raise HarnessInvariantError(f"unsafe fixture table name: {table_name!r}")
            columns = list(
                (
                    await db.execute(text(f'PRAGMA table_info("{table_name}")'))
                ).mappings()
            )
            column_names = {column["name"] for column in columns}
            if "status" not in column_names or (
                run_id is not None and "run_id" not in column_names
            ):
                continue
            statement = f'SELECT status FROM "{table_name}" WHERE status IS NOT NULL'
            parameters: dict[str, Any] = {}
            if run_id is not None:
                statement += " AND run_id = :run_id"
                parameters["run_id"] = run_id
            values = (await db.execute(text(statement), parameters)).scalars()
            statuses.update(str(value) for value in values)
    return statuses


async def _outbox_row_count(factory: async_sessionmaker[AsyncSession]) -> int:
    count = 0
    async with factory() as db:
        table_names = list(
            (
                await db.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND lower(name) LIKE '%outbox%'"
                    )
                )
            ).scalars()
        )
        for table_name in table_names:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
                raise HarnessInvariantError(f"unsafe outbox table name: {table_name!r}")
            count += int(
                await db.scalar(text(f'SELECT count(*) FROM "{table_name}"')) or 0
            )
    return count


async def _writer_is_available(factory: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with factory() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            await db.rollback()
        return True
    except OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            return False
        raise HarnessInvariantError(f"unexpected writer probe error: {exc!r}") from exc


async def _probe_external_wait(
    factory: async_sessionmaker[AsyncSession],
    operation: Awaitable[Any],
    entered: asyncio.Event,
    release: Callable[[], None],
) -> tuple[bool, Any]:
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        writer_available = await _writer_is_available(factory)
    finally:
        release()
    try:
        result = await asyncio.wait_for(task, timeout=3)
    except BaseException as exc:
        raise HarnessInvariantError(f"external-wait fixture failed: {exc!r}") from exc
    return writer_available, result


def _outbox_dispatcher() -> tuple[ModuleType, Callable[..., Awaitable[Any]]] | None:
    candidates = (
        "app.outbox",
        "app.runtime.outbox",
        "app.services.outbox",
        "app.notifications.outbox",
    )
    names = ("dispatch_once", "dispatch_pending_once", "deliver_once")
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise HarnessInvariantError(
                    f"outbox module {module_name} has a broken import: {exc!r}"
                ) from exc
            continue
        for name in names:
            dispatcher = getattr(module, name, None)
            if callable(dispatcher):
                return module, dispatcher
    return None


async def _dispatch_one(
    dispatcher_entry: tuple[ModuleType, Callable[..., Awaitable[Any]]],
    factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> Any:
    module, dispatcher = dispatcher_entry
    monkeypatch.setattr(module, "AsyncSessionLocal", factory, raising=False)
    parameters = inspect.signature(dispatcher).parameters
    if "session_factory" in parameters:
        return await dispatcher(session_factory=factory)
    if "factory" in parameters:
        return await dispatcher(factory=factory)
    if parameters:
        raise HarnessInvariantError(
            "outbox dispatcher needs a testable session_factory parameter"
        )
    return await dispatcher()


class _TransactionBoundaryVisitor(ast.NodeVisitor):
    """Record enclosing functions for raw session commit/rollback/flush calls."""

    def __init__(
        self,
        relative: str,
        transaction_aliases: dict[str, str],
    ) -> None:
        self.relative = relative
        self.transaction_aliases = transaction_aliases
        self.function_stack: list[str] = []
        self.calls: list[tuple[str, str, str, int]] = []
        self.file_flushes: list[tuple[str, str, str, int]] = []
        self.commit_flags: list[tuple[str, str, int]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if any(argument.arg == "commit" for argument in arguments):
            self.commit_flags.append((self.relative, node.name, node.lineno))
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        method: str | None = None
        owner = self.function_stack[-1] if self.function_stack else "<module>"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback", "flush"}
        ):
            method = node.func.attr
            if method == "flush":
                receiver = ast.unparse(node.func.value)
                file_flush = (self.relative, owner, receiver)
                if file_flush in ALLOWED_FILE_FLUSH_CALLS:
                    self.file_flushes.append((*file_flush, node.lineno))
                    self.generic_visit(node)
                    return
        elif isinstance(node.func, ast.Name):
            method = self.transaction_aliases.get(node.func.id)
        if method is not None:
            self.calls.append((self.relative, owner, method, node.lineno))
        self.generic_visit(node)


def test_full_production_commit_allowlist_and_effect_coordinator_architecture() -> None:
    transaction_offenders: list[str] = []
    commit_flags: list[str] = []
    outbox_dispatchers: list[str] = []
    observed_transaction_calls: set[tuple[str, str, str]] = set()
    observed_file_flushes: set[tuple[str, str, str]] = set()

    production_sources = [
        *(PROJECT_ROOT / "backend" / "app").rglob("*.py"),
        *(PROJECT_ROOT / "scripts").rglob("*.py"),
    ]
    for source_path in sorted(production_sources):
        relative = source_path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        transaction_aliases = {
            alias.asname or alias.name: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.uow"
            for alias in node.names
            if alias.name in {"commit", "rollback"}
        }
        visitor = _TransactionBoundaryVisitor(relative, transaction_aliases)
        visitor.visit(tree)
        observed_file_flushes.update(
            (call_path, owner, receiver)
            for call_path, owner, receiver, _lineno in visitor.file_flushes
        )
        for call_path, owner, method, lineno in visitor.calls:
            boundary = (call_path, owner, method)
            observed_transaction_calls.add(boundary)
            if (
                boundary not in ALLOWED_TRANSACTION_CALLS
                and boundary not in ALLOWED_UOW_ALIAS_CALLS
            ):
                transaction_offenders.append(
                    f"{call_path}:{lineno}:{owner}:{method}"
                )
        commit_flags.extend(
            f"{flag_path}:{lineno}:{owner}"
            for flag_path, owner, lineno in visitor.commit_flags
        )
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "outbox" in Path(relative).name and node.name in {
                    "dispatch_once",
                    "dispatch_pending_once",
                    "deliver_once",
                }:
                    outbox_dispatchers.append(f"{relative}:{node.name}")

    declared_fields = {item.name for item in fields(ToolDefinition)}
    classifications = {
        tool.name: _effect_value(getattr(tool, "effect_kind", None))
        for tool in tool_registry.TOOL_MAP.values()
    }
    invalid_classifications = {
        name: value for name, value in classifications.items() if value not in VALID_EFFECT_KINDS
    }
    registry_tree = ast.parse(
        (PROJECT_ROOT / "backend" / "app" / "tools" / "registry.py").read_text(
            encoding="utf-8"
        )
    )
    executor = next(
        node
        for node in registry_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "execute_tool"
    )
    consumes_effect_kind = any(
        (isinstance(node, ast.Attribute) and node.attr == "effect_kind")
        or (isinstance(node, ast.Name) and node.id == "effect_kind")
        for node in ast.walk(executor)
    )
    stale_allowlist = (
        ALLOWED_TRANSACTION_CALLS | ALLOWED_UOW_ALIAS_CALLS
    ) - observed_transaction_calls
    stale_file_flush_allowlist = ALLOWED_FILE_FLUSH_CALLS - observed_file_flushes

    assert (
        not transaction_offenders
        and not commit_flags
        and not stale_allowlist
        and not stale_file_flush_allowlist
        and "effect_kind" in declared_fields
        and not invalid_classifications
        and consumes_effect_kind
        and outbox_dispatchers
    ), (
        f"transaction calls outside exact allowlist={transaction_offenders}; "
        f"commit flags={commit_flags}; "
        f"stale allowlist entries={sorted(stale_allowlist)}; "
        f"stale file flush allowlist={sorted(stale_file_flush_allowlist)}; "
        f"effect field={'effect_kind' in declared_fields}; "
        f"invalid effects={invalid_classifications}; "
        f"executor consumes effect={consumes_effect_kind}; outbox dispatchers={outbox_dispatchers}"
    )


@pytest.mark.asyncio
async def test_canonical_equivalent_validated_json_replays_one_action(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    handler_calls = 0

    async def handler(_ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_canonical_probe",
        _tool_definition("h2_canonical_probe", handler),
    )
    async with sqlite_factory() as db:
        context = ToolContext(
            db=db,
            owner_id=OWNER_ID,
            run_id=run_id,
            trigger="user_message",
            tool_call_id="stable-canonical-action",
        )
        first = await execute_tool("h2_canonical_probe", "{}", context)
        replay = await execute_tool(
            "h2_canonical_probe", json.dumps({"value": 1}), context
        )

    _require_harness(first.get("ok") is True, f"canonical baseline failed: {first!r}")
    _require_harness(replay.get("ok") is True, f"canonical replay failed: {replay!r}")
    async with sqlite_factory() as db:
        invocations = list(
            (
                await db.execute(
                    select(ToolInvocation).where(ToolInvocation.run_id == run_id)
                )
            ).scalars()
        )
    digest = (
        getattr(invocations[0], "request_digest", None) if len(invocations) == 1 else None
    )
    assert (
        handler_calls == 1
        and replay.get("replayed") is True
        and len(invocations) == 1
        and isinstance(digest, str)
        and len(digest) == 64
    ), (
        f"handler_calls={handler_calls}; replay={replay!r}; "
        f"invocations={len(invocations)}; request_digest={digest!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_state",
    ["pending", "flushed"],
    ids=["pending-unflushed", "already-flushed"],
)
async def test_dirty_caller_unit_of_work_is_rejected_without_rollback_or_claim(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
    caller_state: str,
) -> None:
    """Claim setup must never silently discard caller-owned domain work."""

    run_id = await _create_run(sqlite_factory, status="running")
    handler_calls = 0

    async def handler(_ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_dirty_caller_probe",
        _tool_definition("h2_dirty_caller_probe", handler),
    )
    async with sqlite_factory() as db:
        caller_operation = Operation(
            owner_id=OWNER_ID,
            run_id=run_id,
            tool_name="caller.owned",
            entity_type="caller_probe",
            entity_id=caller_state,
            forward_patch={"state": caller_state},
            inverse_patch={},
        )
        db.add(caller_operation)
        if caller_state == "flushed":
            await db.flush()

        result = await execute_tool(
            "h2_dirty_caller_probe",
            json.dumps({"value": 29}),
            ToolContext(
                db=db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id=f"h2-dirty-caller-{caller_state}",
            ),
        )
        assert (
            result.get("ok") is False
            and result.get("error_code") == "caller_unit_of_work_not_clean"
            and result.get("status") == "not_claimed"
            and result.get("retryable") is False
            and handler_calls == 0
            and (
                caller_operation in db.new
                if caller_state == "pending"
                else caller_operation in db.identity_map.values()
            )
        ), (
            f"state={caller_state}; result={result!r}; calls={handler_calls}; "
            f"new={list(db.new)!r}"
        )

        async with sqlite_factory() as observer:
            before_commit_operations = int(
                await observer.scalar(
                    select(func.count(Operation.id)).where(Operation.run_id == run_id)
                )
                or 0
            )
            before_commit_invocations = int(
                await observer.scalar(
                    select(func.count(ToolInvocation.id)).where(
                        ToolInvocation.run_id == run_id
                    )
                )
                or 0
            )
        assert (before_commit_operations, before_commit_invocations) == (0, 0)
        await commit_uow(db)

    async with sqlite_factory() as db:
        operation_count = int(
            await db.scalar(
                select(func.count(Operation.id)).where(Operation.run_id == run_id)
            )
            or 0
        )
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
    assert (operation_count, invocation_count, handler_calls) == (1, 0, 0)


@pytest.mark.asyncio
async def test_nested_caller_scope_is_rejected_before_claim_or_handler(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """A coordinator must never commit a caller's surrounding SAVEPOINT/root UoW."""

    run_id = await _create_run(sqlite_factory, status="running")
    handler_calls = 0

    async def handler(_ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_nested_caller_probe",
        _tool_definition("h2_nested_caller_probe", handler),
    )
    async with sqlite_factory() as db:
        nested = await db.begin_nested()
        try:
            result = await execute_tool(
                "h2_nested_caller_probe",
                json.dumps({"value": 31}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-nested-caller",
                ),
            )
            assert (
                result.get("ok") is False
                and result.get("error_code") == "caller_unit_of_work_not_clean"
                and result.get("status") == "not_claimed"
                and handler_calls == 0
                and db.in_nested_transaction()
            ), f"result={result!r}; calls={handler_calls}"
        finally:
            await nested.rollback()

    async with sqlite_factory() as db:
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
    assert invocation_count == 0


@pytest.mark.asyncio
async def test_clean_preselected_snapshot_releases_before_claim_and_observes_durable_owner(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """A read snapshot is releasable and does not hide the short claim row."""

    run_id = await _create_run(sqlite_factory, status="running")

    async def handler(ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        ctx.db.add(
            Operation(
                owner_id=ctx.owner_id,
                run_id=ctx.run_id,
                invocation_id=ctx.invocation_id,
                tool_name="h2.preselected",
                entity_type="snapshot_probe",
                entity_id=str(args.value),
                forward_patch={"value": args.value},
                inverse_patch={},
            )
        )
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_preselected_probe",
        _tool_definition("h2_preselected_probe", handler),
    )
    async with sqlite_factory() as db:
        preselected = await db.get(AgentRun, run_id)
        _require_harness(preselected is not None, "preselected run disappeared")
        result = await execute_tool(
            "h2_preselected_probe",
            json.dumps({"value": 31}),
            ToolContext(
                db=db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id="h2-preselected-snapshot",
            ),
        )
        retained_snapshot = (preselected.id, preselected.status)

    async with sqlite_factory() as db:
        invocation = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id)
            )
        ).scalar_one()
        operation = (
            await db.execute(select(Operation).where(Operation.run_id == run_id))
        ).scalar_one()
    assert (
        result.get("ok") is True
        and result.get("data") == {"value": 31}
        and retained_snapshot == (run_id, "running")
        and invocation.status == "committed"
        and operation.invocation_id == invocation.id
    ), (
        f"result={result!r}; snapshot={retained_snapshot!r}; "
        f"invocation={(invocation.id, invocation.status)!r}; "
        f"operation={(operation.id, operation.invocation_id)!r}"
    )


@pytest.mark.asyncio
async def test_claim_commit_crash_is_reclaimed_once_after_lease_expiry(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """A post-claim crash permits exactly one later executor to reclaim."""

    run_id = await _create_run(sqlite_factory, status="running")
    clock = SimpleNamespace(now=datetime(2035, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(tool_registry, "utc_now", lambda: clock.now)
    handler_calls = 0

    async def handler(ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        ctx.db.add(
            Operation(
                owner_id=ctx.owner_id,
                run_id=ctx.run_id,
                invocation_id=ctx.invocation_id,
                tool_name="h2.lease.reclaim",
                entity_type="lease_probe",
                entity_id=str(args.value),
                forward_patch={"value": args.value},
                inverse_patch={},
            )
        )
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_lease_reclaim_probe",
        _tool_definition("h2_lease_reclaim_probe", handler),
    )
    original_claim = tool_registry._claim_invocation
    crash_armed = True

    async def crash_after_durable_claim(*args, **kwargs):
        nonlocal crash_armed
        invocation, observed = await original_claim(*args, **kwargs)
        if crash_armed and invocation is not None:
            crash_armed = False
            raise InjectedProcessCrash("killed after claim commit and before handler")
        return invocation, observed

    monkeypatch.setattr(tool_registry, "_claim_invocation", crash_after_durable_claim)
    async with sqlite_factory() as db:
        with pytest.raises(InjectedProcessCrash):
            await execute_tool(
                "h2_lease_reclaim_probe",
                json.dumps({"value": 23}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-expired-lease",
                ),
            )

    async with sqlite_factory() as db:
        abandoned = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id)
            )
        ).scalar_one()
        abandoned_token = abandoned.claim_token
        assert (
            handler_calls == 0
            and abandoned.status == "running"
            and abandoned.attempt == 1
            and abandoned_token
            and abandoned.claim_expires_at == clock.now + timedelta(minutes=5)
        )

    monkeypatch.setattr(tool_registry, "_claim_invocation", original_claim)
    clock.now += timedelta(minutes=6)
    original_short_transaction = tool_registry.run_short_transaction
    reclaim_arrivals = 0
    both_contenders_observed = asyncio.Event()
    release_reclaim = asyncio.Event()

    async def synchronize_reclaim(factory, operation, **kwargs):
        nonlocal reclaim_arrivals
        if getattr(operation, "__name__", "") == "reclaim":
            reclaim_arrivals += 1
            if reclaim_arrivals == 2:
                both_contenders_observed.set()
            await asyncio.wait_for(release_reclaim.wait(), timeout=2)
        return await original_short_transaction(factory, operation, **kwargs)

    monkeypatch.setattr(
        tool_registry,
        "run_short_transaction",
        synchronize_reclaim,
    )

    async def contender() -> dict[str, Any]:
        async with sqlite_factory() as db:
            return await execute_tool(
                "h2_lease_reclaim_probe",
                json.dumps({"value": 23}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-expired-lease",
                ),
            )

    first_task = asyncio.create_task(contender())
    second_task = asyncio.create_task(contender())
    await asyncio.wait_for(both_contenders_observed.wait(), timeout=2)
    release_reclaim.set()
    first, second = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=3,
    )
    results = [first, second]
    winners = [item for item in results if item.get("ok") is True]
    losers = [item for item in results if item.get("ok") is False]

    replay = await contender()
    async with sqlite_factory() as db:
        invocation = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id)
            )
        ).scalar_one()
        operations = list(
            (
                await db.execute(select(Operation).where(Operation.run_id == run_id))
            ).scalars()
        )
        completion_events = list(
            (
                await db.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "tool.completed",
                    )
                )
            ).scalars()
        )

    assert (
        reclaim_arrivals == 2
        and len(winners) == 1
        and len(losers) == 1
        and losers[0].get("error_code") == "invocation_in_progress"
        and losers[0].get("reason") == "already_claimed"
        and losers[0].get("retryable") is True
        and handler_calls == 1
        and replay.get("ok") is True
        and replay.get("replayed") is True
        and invocation.status == "committed"
        and invocation.attempt == 2
        and invocation.claim_token is None
        and len(operations) == 1
        and operations[0].invocation_id == invocation.id
        and len(completion_events) == 1
    ), (
        f"results={results!r}; replay={replay!r}; handler_calls={handler_calls}; "
        f"invocation={(invocation.status, invocation.attempt, invocation.version)!r}; "
        f"operations={[(item.id, item.entity_id) for item in operations]!r}; "
        f"events={[(item.sequence, item.event_type) for item in completion_events]!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_outcome",
    ["success", "failure"],
    ids=["stale-success-finalization", "stale-failure-persistence"],
)
async def test_reclaimed_holder_cannot_overwrite_new_claim_or_domain(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
    stale_outcome: str,
) -> None:
    """A returned or failed stale holder loses every write at its token fence."""

    run_id = await _create_run(sqlite_factory, status="running")
    clock = SimpleNamespace(now=datetime(2036, 2, 3, tzinfo=timezone.utc))
    monkeypatch.setattr(tool_registry, "utc_now", lambda: clock.now)
    stale_entered = asyncio.Event()
    release_stale = asyncio.Event()
    winner_entered = asyncio.Event()
    release_winner = asyncio.Event()
    handler_calls = 0
    stale_token: str | None = None
    winner_token: str | None = None

    async def handler(ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls, stale_token, winner_token
        handler_calls += 1
        if handler_calls == 1:
            stale_token = ctx.claim_token
            stale_entered.set()
            await release_stale.wait()
            if stale_outcome == "failure":
                raise RuntimeError("stale executor failed after its lease was reclaimed")
            ctx.db.add(
                Operation(
                    owner_id=ctx.owner_id,
                    run_id=ctx.run_id,
                    invocation_id=ctx.invocation_id,
                    tool_name="h2.lease.stale",
                    entity_type="lease_probe",
                    entity_id="stale",
                    forward_patch={"value": 1},
                    inverse_patch={},
                )
            )
            return {"value": 1}

        winner_token = ctx.claim_token
        winner_entered.set()
        await release_winner.wait()
        ctx.db.add(
            Operation(
                owner_id=ctx.owner_id,
                run_id=ctx.run_id,
                invocation_id=ctx.invocation_id,
                tool_name="h2.lease.winner",
                entity_type="lease_probe",
                entity_id="winner",
                forward_patch={"value": args.value},
                inverse_patch={},
            )
        )
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_stale_holder_probe",
        _tool_definition("h2_stale_holder_probe", handler),
    )

    async def execute(value: int) -> dict[str, Any]:
        async with sqlite_factory() as db:
            return await execute_tool(
                "h2_stale_holder_probe",
                json.dumps({"value": value}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-stale-holder",
                ),
            )

    stale_task = asyncio.create_task(execute(2))
    await asyncio.wait_for(stale_entered.wait(), timeout=2)
    _require_harness(bool(stale_token), "first holder did not receive a claim token")
    clock.now += timedelta(minutes=6)

    original_claim = tool_registry._claim_invocation
    reclaimed = asyncio.Event()

    async def observe_reclaim(*args, **kwargs):
        invocation, observed = await original_claim(*args, **kwargs)
        if invocation is not None and invocation.claim_token != stale_token:
            reclaimed.set()
        return invocation, observed

    monkeypatch.setattr(tool_registry, "_claim_invocation", observe_reclaim)
    winner_task = asyncio.create_task(execute(2))
    await asyncio.wait_for(reclaimed.wait(), timeout=2)

    async with sqlite_factory() as db:
        reclaimed_row = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id)
            )
        ).scalar_one()
        reclaimed_token = reclaimed_row.claim_token
        assert (
            reclaimed_row.status == "running"
            and reclaimed_row.attempt == 2
            and reclaimed_token
            and reclaimed_token != stale_token
        )

    release_stale.set()
    stale_result = await asyncio.wait_for(stale_task, timeout=2)
    await asyncio.wait_for(winner_entered.wait(), timeout=2)
    _require_harness(
        winner_token == reclaimed_token,
        "winner handler did not inherit the durable reclaimed token",
    )

    async with sqlite_factory() as db:
        fenced_row = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id)
            )
        ).scalar_one()
        staged_operation_count = int(
            await db.scalar(
                select(func.count(Operation.id)).where(Operation.run_id == run_id)
            )
            or 0
        )
        staged_event_count = int(
            await db.scalar(
                select(func.count(RunEvent.id)).where(RunEvent.run_id == run_id)
            )
            or 0
        )
    assert (
        stale_result.get("ok") is False
        and stale_result.get("error_code") == "invocation_claim_lost"
        and stale_result.get("retryable") is True
        and fenced_row.status == "running"
        and fenced_row.claim_token == reclaimed_token
        and fenced_row.attempt == 2
        and staged_operation_count == 0
        and staged_event_count == 0
    ), (
        f"stale_result={stale_result!r}; "
        f"fenced={(fenced_row.status, fenced_row.claim_token, fenced_row.attempt)!r}; "
        f"operations={staged_operation_count}; events={staged_event_count}"
    )

    release_winner.set()
    winner_result = await asyncio.wait_for(winner_task, timeout=2)
    async with sqlite_factory() as db:
        invocation = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id)
            )
        ).scalar_one()
        operations = list(
            (
                await db.execute(select(Operation).where(Operation.run_id == run_id))
            ).scalars()
        )
        events = list(
            (
                await db.execute(
                    select(RunEvent).where(RunEvent.run_id == run_id)
                )
            ).scalars()
        )

    assert (
        winner_result.get("ok") is True
        and winner_result.get("data") == {"value": 2}
        and handler_calls == 2
        and invocation.status == "committed"
        and invocation.result_payload == {"value": 2}
        and invocation.claim_token is None
        and invocation.attempt == 2
        and [item.entity_id for item in operations] == ["winner"]
        and operations[0].invocation_id == invocation.id
        and len(events) == 1
        and events[0].event_type == "tool.completed"
        and events[0].payload["result"]["data"] == {"value": 2}
    ), (
        f"winner={winner_result!r}; handler_calls={handler_calls}; "
        f"invocation={(invocation.status, invocation.result_payload)!r}; "
        f"operations={[(item.entity_id, item.invocation_id) for item in operations]!r}; "
        f"events={[(item.event_type, item.payload) for item in events]!r}"
    )


@pytest.mark.asyncio
async def test_artifact_request_digest_replays_equal_and_conflicts_on_change(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    conflict: ValueError | None = None
    async with sqlite_factory() as db:
        first, created = await create_artifact(
            db,
            owner_id=OWNER_ID,
            artifact_type="text",
            source_uri="memory://h2-domain-digest",
            idempotency_key="h2-domain-digest:artifact",
            content="original",
            metadata={"kind": "answer", "rank": 1},
        )
        await commit_uow(db)
    async with sqlite_factory() as db:
        replay, replay_created = await create_artifact(
            db,
            owner_id=OWNER_ID,
            artifact_type="text",
            source_uri="memory://h2-domain-digest",
            idempotency_key="h2-domain-digest:artifact",
            content="original",
            metadata={"rank": 1, "kind": "answer"},
        )
        try:
            await create_artifact(
                db,
                owner_id=OWNER_ID,
                artifact_type="text",
                source_uri="memory://h2-domain-digest",
                idempotency_key="h2-domain-digest:artifact",
                content="different",
                metadata={"kind": "answer", "rank": 1},
            )
        except ValueError as exc:
            conflict = exc
    async with sqlite_factory() as db:
        rows = list((await db.execute(select(Artifact))).scalars())

    _require_harness(created is True, "Artifact fixture was not created")
    _require_harness(replay_created is False, "equal Artifact request was not replayed")
    _require_harness(replay.id == first.id, "equal Artifact request changed identity")
    digest = getattr(rows[0], "request_digest", None) if rows else None
    assert (
        conflict is not None
        and "idempotency" in str(conflict).lower()
        and "conflict" in str(conflict).lower()
        and len(rows) == 1
        and rows[0].content_hash == first.content_hash
        and isinstance(digest, str)
        and len(digest) == 64
    ), (
        f"conflict={conflict!r}; rows={len(rows)}; "
        f"content_hash={rows[0].content_hash if rows else None!r}; digest={digest!r}"
    )


@pytest.mark.asyncio
async def test_evidence_request_digest_replays_equal_and_conflicts_on_change(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    conflict: ValueError | None = None
    occurred_at = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc)
    async with sqlite_factory() as db:
        first, created = await append_observation(
            db,
            owner_id=OWNER_ID,
            source_type="quiz",
            source_id="attempt:1",
            outcome="passed",
            idempotency_key="h2-domain-digest:evidence",
            payload={"answer": "A", "rank": 1},
            occurred_at=occurred_at,
        )
        await commit_uow(db)
    async with sqlite_factory() as db:
        replay, replay_created = await append_observation(
            db,
            owner_id=OWNER_ID,
            source_type="quiz",
            source_id="attempt:1",
            outcome="passed",
            idempotency_key="h2-domain-digest:evidence",
            payload={"rank": 1, "answer": "A"},
            occurred_at=occurred_at,
        )
        try:
            await append_observation(
                db,
                owner_id=OWNER_ID,
                source_type="quiz",
                source_id="attempt:1",
                outcome="failed",
                idempotency_key="h2-domain-digest:evidence",
                payload={"answer": "different", "rank": 1},
                occurred_at=occurred_at,
            )
        except ValueError as exc:
            conflict = exc
    async with sqlite_factory() as db:
        rows = list((await db.execute(select(EvidenceObservation))).scalars())

    _require_harness(created is True, "Evidence fixture was not created")
    _require_harness(replay_created is False, "equal Evidence request was not replayed")
    _require_harness(replay.id == first.id, "equal Evidence request changed identity")
    digest = getattr(rows[0], "request_digest", None) if rows else None
    assert (
        conflict is not None
        and "idempotency" in str(conflict).lower()
        and "conflict" in str(conflict).lower()
        and len(rows) == 1
        and (rows[0].outcome, rows[0].payload)
        == ("passed", {"answer": "A", "rank": 1})
        and isinstance(digest, str)
        and len(digest) == 64
    ), (
        f"conflict={conflict!r}; rows={len(rows)}; "
        f"outcome={rows[0].outcome if rows else None!r}; "
        f"payload={rows[0].payload if rows else None!r}; digest={digest!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("domain_kind", ["artifact", "evidence"])
@pytest.mark.parametrize("request_kind", ["exact", "conflict"])
async def test_domain_idempotency_concurrent_requests_converge_without_raw_db_errors(
    sqlite_factory: async_sessionmaker[AsyncSession],
    domain_kind: str,
    request_kind: str,
) -> None:
    """Concurrent domain keys replay exactly or report a semantic conflict."""

    call_arrivals = 0
    both_ready_to_call = asyncio.Event()
    release_calls = asyncio.Event()
    occurred_at = datetime(2037, 4, 5, 6, 7, tzinfo=timezone.utc)

    async def worker(variant: str) -> dict[str, Any]:
        nonlocal call_arrivals
        async with sqlite_factory() as db:
            # Start both logical requests together. The domain service acquires
            # a physical SQLite write transaction before reading the stable key;
            # the loser therefore waits, observes the committed winner, and
            # returns an exact replay or a semantic conflict without leaking a
            # raw unique/busy error.
            await db.execute(text("PRAGMA busy_timeout=2000"))
            await commit_uow(db)
            call_arrivals += 1
            if call_arrivals == 2:
                both_ready_to_call.set()
            await asyncio.wait_for(release_calls.wait(), timeout=2)
            try:
                if domain_kind == "artifact":
                    row, created = await create_artifact(
                        db,
                        owner_id=OWNER_ID,
                        artifact_type="text",
                        source_uri="memory://h2-concurrent-artifact",
                        idempotency_key="h2-concurrent-domain-key",
                        title=f"title-{variant}",
                        content=f"content-{variant}",
                        metadata={"variant": variant},
                    )
                else:
                    row, created = await append_observation(
                        db,
                        owner_id=OWNER_ID,
                        source_type="quiz",
                        source_id="h2-concurrent-evidence",
                        outcome="passed" if variant == "same" else variant,
                        idempotency_key="h2-concurrent-domain-key",
                        payload={"variant": variant},
                        occurred_at=occurred_at,
                    )
                await commit_uow(db)
                return {
                    "kind": "ok",
                    "created": created,
                    "row_id": row.id,
                    "variant": variant,
                }
            except ValueError as exc:
                await rollback_uow(db)
                return {
                    "kind": "conflict",
                    "message": str(exc),
                    "variant": variant,
                }
            except BaseException as exc:
                await rollback_uow(db)
                return {
                    "kind": "unexpected",
                    "error_type": type(exc).__name__,
                    "variant": variant,
                }

    variants = (
        ("same", "same")
        if request_kind == "exact"
        else ("passed", "failed")
    )
    tasks = [asyncio.create_task(worker(variant)) for variant in variants]
    try:
        await asyncio.wait_for(both_ready_to_call.wait(), timeout=2)
    finally:
        release_calls.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4)

    async with sqlite_factory() as db:
        if domain_kind == "artifact":
            rows = list((await db.execute(select(Artifact))).scalars())
        else:
            rows = list((await db.execute(select(EvidenceObservation))).scalars())

    successful = [item for item in results if item["kind"] == "ok"]
    conflicts = [item for item in results if item["kind"] == "conflict"]
    unexpected = [item for item in results if item["kind"] == "unexpected"]
    assert not unexpected, f"raw database/concurrency error escaped: {unexpected!r}"
    assert len(rows) == 1, f"{domain_kind} rows={len(rows)}; results={results!r}"

    if request_kind == "exact":
        assert (
            len(successful) == 2
            and not conflicts
            and sorted(item["created"] for item in successful) == [False, True]
            and len({item["row_id"] for item in successful}) == 1
            and successful[0]["row_id"] == rows[0].id
        ), f"exact concurrent replay did not converge: {results!r}"
    else:
        assert (
            len(successful) == 1
            and successful[0]["created"] is True
            and len(conflicts) == 1
            and "idempotency" in conflicts[0]["message"].lower()
            and "conflict" in conflicts[0]["message"].lower()
        ), f"concurrent semantic conflict was not typed: {results!r}"
        winner = successful[0]["variant"]
        if domain_kind == "artifact":
            assert rows[0].title == f"title-{winner}"
            assert rows[0].artifact_metadata == {"variant": winner}
        else:
            assert rows[0].outcome == winner
            assert rows[0].payload == {"variant": winner}


@pytest.mark.asyncio
async def test_domain_idempotency_savepoints_remain_inside_the_caller_uow(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Releasing an idempotency savepoint must not publish before outer commit."""

    async with sqlite_factory() as db:
        artifact, artifact_created = await create_artifact(
            db,
            owner_id=OWNER_ID,
            artifact_type="text",
            source_uri="memory://h2-outer-rollback",
            idempotency_key="h2-outer-rollback:artifact",
            content="not committed",
        )
        observation, observation_created = await append_observation(
            db,
            owner_id=OWNER_ID,
            source_type="manual",
            source_id="h2-outer-rollback",
            outcome="submitted",
            idempotency_key="h2-outer-rollback:evidence",
            artifact_refs=[{"artifact_id": artifact.id}],
            occurred_at=datetime(2039, 6, 7, 8, 9, tzinfo=timezone.utc),
        )
        assert artifact_created and observation_created and observation.id is not None
        await rollback_uow(db)

    async with sqlite_factory() as db:
        artifact_count = int(await db.scalar(select(func.count(Artifact.id))) or 0)
        observation_count = int(
            await db.scalar(select(func.count(EvidenceObservation.id))) or 0
        )
    assert (artifact_count, observation_count) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain_kind", ["artifact", "evidence"])
async def test_domain_idempotency_writer_lock_returns_typed_busy(
    sqlite_factory: async_sessionmaker[AsyncSession],
    domain_kind: str,
) -> None:
    """A lock before the stable-key read is retryable, never a raw SQLite error."""

    async with sqlite_factory() as locker:
        await locker.execute(text("BEGIN IMMEDIATE"))
        async with sqlite_factory() as contender:
            await contender.execute(text("PRAGMA busy_timeout=0"))
            await commit_uow(contender)
            with pytest.raises(DatabaseBusyError) as captured:
                if domain_kind == "artifact":
                    await create_artifact(
                        contender,
                        owner_id=OWNER_ID,
                        artifact_type="text",
                        source_uri="memory://h2-domain-busy",
                        idempotency_key="h2-domain-busy:artifact",
                        content="blocked",
                    )
                else:
                    await append_observation(
                        contender,
                        owner_id=OWNER_ID,
                        source_type="manual",
                        source_id="h2-domain-busy",
                        outcome="submitted",
                        idempotency_key="h2-domain-busy:evidence",
                        occurred_at=datetime(2039, 6, 7, 8, 9, tzinfo=timezone.utc),
                    )
            assert captured.value.retryable is True
        await rollback_uow(locker)

    async with sqlite_factory() as db:
        artifact_count = int(await db.scalar(select(func.count(Artifact.id))) or 0)
        observation_count = int(
            await db.scalar(select(func.count(EvidenceObservation.id))) or 0
        )
    assert (artifact_count, observation_count) == (0, 0)


@pytest.mark.asyncio
async def test_bounded_duplicate_database_storm_commits_one_invocation_operation_and_evidence(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Eight identical callers converge on one complete database-write UoW."""

    run_id = await _create_run(sqlite_factory, status="running")
    handler_entered = asyncio.Event()
    release_handler = asyncio.Event()
    handler_calls = 0
    occurred_at = datetime(2038, 5, 6, 7, 8, tzinfo=timezone.utc)

    async def handler(ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        handler_entered.set()
        await release_handler.wait()
        ctx.db.add(
            Operation(
                owner_id=ctx.owner_id,
                run_id=ctx.run_id,
                invocation_id=ctx.invocation_id,
                tool_name="h2.duplicate.storm",
                entity_type="storm_probe",
                entity_id=str(args.value),
                forward_patch={"value": args.value},
                inverse_patch={},
            )
        )
        await append_observation(
            ctx.db,
            owner_id=ctx.owner_id,
            source_type="tool",
            source_id="h2-duplicate-storm",
            outcome="completed",
            idempotency_key="h2-duplicate-storm:evidence",
            run_id=ctx.run_id,
            payload={"value": args.value},
            occurred_at=occurred_at,
        )
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_duplicate_database_probe",
        _tool_definition("h2_duplicate_database_probe", handler),
    )

    async def caller() -> dict[str, Any]:
        async with sqlite_factory() as db:
            return await execute_tool(
                "h2_duplicate_database_probe",
                json.dumps({"value": 37}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-bounded-database-storm",
                ),
            )

    tasks = [asyncio.create_task(caller()) for _ in range(8)]
    await asyncio.wait_for(handler_entered.wait(), timeout=2)
    deadline = asyncio.get_running_loop().time() + 3
    while sum(task.done() for task in tasks) < 7 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    _require_harness(
        sum(task.done() for task in tasks) == 7,
        "duplicate claim contenders did not return while the winner was gated",
    )
    release_handler.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    replay = await caller()

    async with sqlite_factory() as db:
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
        operation_count = int(
            await db.scalar(
                select(func.count(Operation.id)).where(Operation.run_id == run_id)
            )
            or 0
        )
        evidence_count = int(
            await db.scalar(
                select(func.count(EvidenceObservation.id)).where(
                    EvidenceObservation.run_id == run_id
                )
            )
            or 0
        )
        completion_count = int(
            await db.scalar(
                select(func.count(RunEvent.id)).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "tool.completed",
                )
            )
            or 0
        )

    successes = [result for result in results if result.get("ok") is True]
    losers = [result for result in results if result.get("ok") is False]
    assert (
        len(successes) == 1
        and len(losers) == 7
        and all(
            result.get("error_code") in {"invocation_in_progress", "database_busy"}
            and result.get("retryable") is True
            for result in losers
        )
        and handler_calls == 1
        and replay.get("ok") is True
        and replay.get("replayed") is True
        and (invocation_count, operation_count, evidence_count, completion_count)
        == (1, 1, 1, 1)
    ), (
        f"results={results!r}; replay={replay!r}; calls={handler_calls}; "
        f"counts={(invocation_count, operation_count, evidence_count, completion_count)!r}"
    )


@pytest.mark.asyncio
async def test_database_write_killpoint_is_atomic_across_domain_evidence_and_events(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    async with sqlite_factory() as db:
        session = Session(owner_id=OWNER_ID, title="H2 atomic set")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id=OWNER_ID,
            session_id=session.id,
            trigger="user_message",
            objective="check one submitted task atomically",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        plan = Plan(owner_id=OWNER_ID, title="Atomic plan", status="active")
        db.add_all([run, plan])
        await db.flush()
        stage = Stage(plan_id=plan.id, title="Atomic stage", position=0)
        db.add(stage)
        await db.flush()
        task = Task(
            stage_id=stage.id,
            title="Atomic task",
            status="active",
            position=0,
            evidence_required=True,
        )
        db.add(task)
        await db.flush()
        submission = TaskSubmission(
            owner_id=OWNER_ID,
            plan_id=plan.id,
            task_id=task.id,
            run_id=run.id,
            submission_type="text",
            content="deterministic answer",
            status="submitted",
        )
        db.add(submission)
        await db.flush()
        run.checkpoint_schema_version = 1
        run.checkpoint = make_checkpoint(
            kind="agent",
            phase="tool_ready",
            step=0,
            messages=[],
            current_tool_call={
                "id": "atomic-submission-call",
                "name": "submission_check",
                "arguments": json.dumps(
                    {
                        "submission_id": submission.id,
                        "score": 91,
                        "feedback": "accepted by deterministic fixture",
                        "checks": [{"name": "offline", "passed": True}],
                    },
                    sort_keys=True,
                ),
            },
        )
        await db.commit()
        run_id = run.id
        session_id = session.id
        submission_id = submission.id

    monkeypatch.setattr(agent_runtime_module, "AsyncSessionLocal", sqlite_factory)
    runtime = AgentRuntime()
    killpoint_seen = False

    def kill_atomic_commit(sync_session) -> None:
        nonlocal killpoint_seen
        finalizing = any(
            isinstance(item, ToolInvocation)
            and item.run_id == run_id
            and item.status == "committed"
            for item in _pending_objects(sync_session)
        )
        if finalizing:
            killpoint_seen = True
            raise InjectedProcessCrash("killed the database-write atomic commit")

    event.listen(AsyncSession.sync_session_class, "before_commit", kill_atomic_commit)
    crash_captured = False
    try:
        async with sqlite_factory() as runtime_db:
            stored_run = await runtime_db.get(AgentRun, run_id)
            stored_session = await runtime_db.get(Session, session_id)
            _require_harness(stored_run is not None, "atomic fixture lost its run")
            _require_harness(stored_session is not None, "atomic fixture lost its session")
            try:
                await runtime.run(run_id, resume=True)
            except InjectedProcessCrash:
                crash_captured = True
                await runtime_db.rollback()
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", kill_atomic_commit)

    _require_harness(killpoint_seen, "database-write atomic killpoint was not reached")
    _require_harness(crash_captured, "database-write process death was swallowed")

    async with sqlite_factory() as db:
        stored_submission = await db.get(TaskSubmission, submission_id)
        _require_harness(stored_submission is not None, "submission fixture disappeared")
        artifact_count = int(
            await db.scalar(
                select(func.count(Artifact.id)).where(
                    Artifact.idempotency_key == f"submission:{submission_id}:artifact"
                )
            )
            or 0
        )
        evidence_count = int(
            await db.scalar(
                select(func.count(EvidenceObservation.id)).where(
                    EvidenceObservation.idempotency_key
                    == f"submission:{submission_id}:checked"
                )
            )
            or 0
        )
        learning_count = int(
            await db.scalar(
                select(func.count(LearningEvent.id)).where(
                    LearningEvent.run_id == run_id,
                    LearningEvent.event_type == "submission.checked",
                )
            )
            or 0
        )
        operation_count = int(
            await db.scalar(
                select(func.count(Operation.id)).where(
                    Operation.run_id == run_id,
                    Operation.tool_name == "submission.check",
                )
            )
            or 0
        )
        completed_event_count = int(
            await db.scalar(
                select(func.count(RunEvent.id)).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "tool.completed",
                )
            )
            or 0
        )
        invocations = list(
            (
                await db.execute(
                    select(ToolInvocation).where(ToolInvocation.run_id == run_id)
                )
            ).scalars()
        )

    domain_counts = (
        artifact_count,
        evidence_count,
        learning_count,
        operation_count,
    )
    nothing_committed = (
        stored_submission.status == "submitted"
        and domain_counts == (0, 0, 0, 0)
        and completed_event_count == 0
        and not any(item.status == "committed" for item in invocations)
    )
    everything_committed = (
        stored_submission.status == "accepted"
        and domain_counts == (1, 1, 1, 1)
        and completed_event_count == 1
        and len(invocations) == 1
        and invocations[0].status == "committed"
    )
    assert nothing_committed or everything_committed, (
        f"submission={stored_submission.status!r}; domain={domain_counts}; "
        f"tool.completed={completed_event_count}; "
        f"invocations={[(item.status, item.result_payload) for item in invocations]}"
    )


@pytest.mark.asyncio
async def test_database_write_and_independent_event_share_sequence_boundary(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory, status="running")
    handler_entered = asyncio.Event()
    release_handler = asyncio.Event()

    async def handler(ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        ctx.db.add(
            Operation(
                owner_id=ctx.owner_id,
                run_id=ctx.run_id,
                invocation_id=ctx.invocation_id,
                tool_name="h2.sequence.probe",
                entity_type="probe",
                entity_id=str(args.value),
                forward_patch={"value": args.value},
                inverse_patch={},
            )
        )
        # Deliberately own SQLite's writer while the independent append starts.
        # Registry must already hold the shared event serialization boundary.
        await ctx.db.flush()
        handler_entered.set()
        await release_handler.wait()
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_sequence_probe",
        _tool_definition("h2_sequence_probe", handler),
    )

    async def run_tool() -> dict[str, Any]:
        async with sqlite_factory() as db:
            return await execute_tool(
                "h2_sequence_probe",
                json.dumps({"value": 17}),
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-sequence-tool",
                ),
            )

    async def append_independent_event() -> RunEvent:
        async with sqlite_factory() as db:
            return await emit_event(
                db,
                run_id,
                "run.progress",
                "independent overlapping append",
            )

    tool_task = asyncio.create_task(run_tool())
    await asyncio.wait_for(handler_entered.wait(), timeout=2)
    event_task = asyncio.create_task(append_independent_event())
    try:
        # This exceeds the complete .025/.05/.1/.2 SQLite busy budget.  Without
        # the shared pre-writer lock, emit_event finishes with DatabaseBusyError.
        await asyncio.sleep(0.5)
        event_waited_at_boundary = not event_task.done()
    finally:
        release_handler.set()
    tool_result, independent_event = await asyncio.wait_for(
        asyncio.gather(tool_task, event_task, return_exceptions=True),
        timeout=3,
    )

    async with sqlite_factory() as db:
        invocations = list(
            (
                await db.execute(
                    select(ToolInvocation).where(ToolInvocation.run_id == run_id)
                )
            ).scalars()
        )
        operations = list(
            (
                await db.execute(
                    select(Operation).where(Operation.run_id == run_id)
                )
            ).scalars()
        )
        events = list(
            (
                await db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                )
            ).scalars()
        )

    assert (
        event_waited_at_boundary
        and isinstance(tool_result, dict)
        and tool_result.get("ok") is True
        and isinstance(independent_event, RunEvent)
        and len(invocations) == 1
        and invocations[0].status == "committed"
        and len(operations) == 1
        and operations[0].invocation_id == invocations[0].id
        and [item.sequence for item in events] == [1, 2]
        and [item.event_type for item in events] == ["tool.completed", "run.progress"]
    ), (
        f"event_waited={event_waited_at_boundary}; tool={tool_result!r}; "
        f"independent_event={independent_event!r}; "
        f"invocations={[(item.id, item.status) for item in invocations]}; "
        f"operations={[(item.id, item.invocation_id) for item in operations]}; "
        f"events={[(item.sequence, item.event_type) for item in events]}"
    )


@pytest.mark.asyncio
async def test_memory_embedding_wait_precedes_event_write_boundary(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """A mixed-effect tool must not reserve RunEvent order across provider I/O."""

    run_id = await _create_run(sqlite_factory, status="running")
    async with sqlite_factory() as db:
        manager = memory_context_module.MemoryManager(db)
        memory, reused = await manager.propose(
            OWNER_ID,
            scope="global",
            scope_id=None,
            layer="semantic",
            content="offline embedding boundary probe",
            source_type="user",
        )
        _require_harness(not reused, "embedding fixture unexpectedly reused a Memory")
        memory = await manager.confirm(OWNER_ID, memory.id)
        await commit_uow(db)
        memory_id = memory.id

    provider_entered = threading.Event()
    release_provider = threading.Event()
    provider_calls = 0

    class GatedProvider:
        name = "h2_gated_provider"
        dimension = 64

        def embed(self, _content: str) -> list[float]:
            nonlocal provider_calls
            provider_calls += 1
            provider_entered.set()
            if not release_provider.wait(timeout=5):
                raise HarnessInvariantError("embedding provider gate was not released")
            return [0.0] * self.dimension

    monkeypatch.setattr(
        memory_context_module,
        "get_embedding_provider",
        lambda: GatedProvider(),
    )

    async def run_maintenance() -> dict[str, Any]:
        async with sqlite_factory() as db:
            return await execute_tool(
                "memory_maintain",
                "{}",
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=run_id,
                    trigger="user_message",
                    tool_call_id="h2-memory-provider-boundary",
                ),
            )

    maintenance_task = asyncio.create_task(run_maintenance())
    await asyncio.wait_for(asyncio.to_thread(provider_entered.wait, 3), timeout=4)
    try:
        async with sqlite_factory() as event_db:
            independent = await asyncio.wait_for(
                emit_event(
                    event_db,
                    run_id,
                    "run.progress",
                    "provider wait does not reserve sequence",
                ),
                timeout=0.5,
            )
    finally:
        release_provider.set()
    maintenance_result = await asyncio.wait_for(maintenance_task, timeout=3)

    async with sqlite_factory() as db:
        refreshed = await db.get(Memory, memory_id)
        events = list(
            (
                await db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                )
            ).scalars()
        )
    assert (
        independent.sequence == 1
        and maintenance_result.get("ok") is True
        and provider_calls == 1
        and refreshed is not None
        and refreshed.embedding == [0.0] * 64
        and refreshed.embedding_provider == "h2_gated_provider"
        and [event.sequence for event in events] == [1, 2]
        and [event.event_type for event in events]
        == ["run.progress", "tool.completed"]
    ), (
        f"independent={independent.sequence}; maintenance={maintenance_result!r}; "
        f"provider_calls={provider_calls}; "
        f"embedding_provider={refreshed.embedding_provider if refreshed else None!r}; "
        f"events={[(event.sequence, event.event_type) for event in events]!r}"
    )


@pytest.mark.asyncio
async def test_http_open_wait_never_holds_sqlite_writer(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    http_entered = asyncio.Event()
    http_release = asyncio.Event()

    async def gated_open(_client, url: str):
        http_entered.set()
        await http_release.wait()
        return (
            SimpleNamespace(
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><title>offline</title><body>probe</body></html>",
                url=url,
            ),
            0,
        )

    monkeypatch.setattr(web_tools, "fetch_with_safe_redirects", gated_open)
    web_run_id = await _create_run(sqlite_factory)
    async with sqlite_factory() as web_db:
        available, web_result = await _probe_external_wait(
            sqlite_factory,
            execute_tool(
                "web_open",
                json.dumps({"url": "https://example.invalid/offline", "max_chars": 500}),
                ToolContext(
                    db=web_db,
                    owner_id=OWNER_ID,
                    run_id=web_run_id,
                    trigger="user_message",
                    tool_call_id="external-open",
                ),
            ),
            http_entered,
            http_release.set,
        )
    _require_harness(web_result.get("ok") is True, f"offline web_open failed: {web_result!r}")
    assert available, "HTTP/open retained SQLite's writer while awaiting the provider"


@pytest.mark.asyncio
async def test_subprocess_wait_never_holds_sqlite_writer(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    # The subprocess body is replaced by a thread gate; no command is spawned.
    subprocess_entered = asyncio.Event()
    subprocess_release = threading.Event()
    event_loop = asyncio.get_running_loop()

    def gated_subprocess(_args) -> dict[str, Any]:
        event_loop.call_soon_threadsafe(subprocess_entered.set)
        if not subprocess_release.wait(timeout=3):
            raise HarnessInvariantError("subprocess release gate timed out")
        return {"exit_code": 0, "stdout": "offline", "stderr": "", "truncated": False}

    monkeypatch.setattr(workspace_tools, "_run_code", gated_subprocess)
    subprocess_run_id = await _create_run(sqlite_factory)
    async with sqlite_factory() as subprocess_db:
        available, subprocess_result = await _probe_external_wait(
            sqlite_factory,
            execute_tool(
                "code_execute",
                json.dumps({"language": "python", "code": "pass", "timeout_seconds": 1}),
                ToolContext(
                    db=subprocess_db,
                    owner_id=OWNER_ID,
                    run_id=subprocess_run_id,
                    trigger="user_message",
                    tool_call_id="external-subprocess",
                ),
            ),
            subprocess_entered,
            subprocess_release.set,
        )
    _require_harness(
        subprocess_result.get("ok") is True,
        f"offline code_execute failed: {subprocess_result!r}",
    )
    assert available, "subprocess wait retained SQLite's writer"


@pytest.mark.asyncio
async def test_subagent_join_wait_never_holds_sqlite_writer(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    # A child wait returns a synthetic terminal record and never starts an Agent.
    parent_run_id = await _create_run(sqlite_factory, status="running")
    child_run_id = await _create_run(
        sqlite_factory,
        trigger="subagent",
        status="running",
        parent_run_id=parent_run_id,
    )
    subagent_entered = asyncio.Event()
    subagent_release = asyncio.Event()

    async def gated_child(child_id: str, _timeout: float):
        _require_harness(child_id == child_run_id, "sub-agent fixture joined the wrong child")
        subagent_entered.set()
        await subagent_release.wait()
        return SimpleNamespace(id=child_id, status="completed", output="offline report")

    monkeypatch.setattr(subagent_tools, "wait_for_child", gated_child)
    async with sqlite_factory() as subagent_db:
        available, subagent_result = await _probe_external_wait(
            sqlite_factory,
            execute_tool(
                "subagent_join",
                json.dumps({"run_id": child_run_id, "timeout_seconds": 1}),
                ToolContext(
                    db=subagent_db,
                    owner_id=OWNER_ID,
                    run_id=parent_run_id,
                    trigger="user_message",
                    tool_call_id="external-subagent",
                ),
            ),
            subagent_entered,
            subagent_release.set,
        )
    _require_harness(
        subagent_result.get("ok") is True,
        f"offline subagent_join failed: {subagent_result!r}",
    )
    assert available, "sub-agent join retained SQLite's writer"


@pytest.mark.asyncio
async def test_session_compression_wait_never_holds_sqlite_writer(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    compression_entered = asyncio.Event()
    compression_release = asyncio.Event()

    class FakeCompletions:
        async def create(self, **_kwargs):
            compression_entered.set()
            await compression_release.wait()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="offline summary"))]
            )

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    async def final_model(*_args, **_kwargs):
        return SimpleNamespace(
            content="offline final answer", reasoning_content=None, tool_calls=None
        ), None

    async def no_title(*_args, **_kwargs):
        return False

    async def no_child_cancel(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(runtime, "_call_model", final_model)
    monkeypatch.setattr(agent_runtime_module, "AsyncSessionLocal", sqlite_factory)
    monkeypatch.setattr(agent_runtime_module, "generate_session_title", no_title)
    monkeypatch.setattr(runtime_subagents, "cancel_children_for_parent", no_child_cancel)
    monkeypatch.setattr(agent_runtime_module.settings, "AGENT_RECENT_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(
        agent_runtime_module.settings, "AGENT_SESSION_COMPRESSION_THRESHOLD", 1
    )

    async with sqlite_factory() as compression_db:
        session = Session(owner_id=OWNER_ID, title="H2 compression wait")
        compression_db.add(session)
        await compression_db.flush()
        run = AgentRun(
            owner_id=OWNER_ID,
            session_id=session.id,
            trigger="user_message",
            objective="compress offline",
            status="queued",
        )
        compression_db.add(run)
        await compression_db.flush()
        compression_db.add(
            ChatMessage(
                session_id=session.id,
                run_id=run.id,
                role="user",
                content="old message that must be summarized",
            )
        )
        await compression_db.commit()
        available, _ = await _probe_external_wait(
            sqlite_factory,
            runtime.run(run.id),
            compression_entered,
            compression_release.set,
        )
        await compression_db.refresh(run)
        _require_harness(
            run.status == "completed",
            f"compression fixture did not finalize: {run.status!r}",
        )
    assert available, "session compression retained SQLite's writer"


def _notification_payload(label: str) -> str:
    return json.dumps(
        {
            "title": f"H2 outbox {label}",
            "body": "Synthetic transport only; no external service is contacted.",
            "channels": ["email"],
        },
        sort_keys=True,
    )


def _is_outbox_enqueue(sync_session) -> bool:
    for item in _pending_objects(sync_session):
        table_name = getattr(getattr(item, "__table__", None), "name", "")
        if isinstance(item, Notification) or "outbox" in table_name.lower():
            return True
    return False


@pytest.mark.asyncio
async def test_outbox_kill_before_enqueue_commit_has_no_external_delivery(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    send_count = 0
    killpoint_seen = False

    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)

    def fake_email(_self, _reply_token: str, _title: str, _body: str) -> None:
        nonlocal send_count
        send_count += 1

    monkeypatch.setattr(NotificationService, "_send_email", fake_email)

    def kill_enqueue(sync_session) -> None:
        nonlocal killpoint_seen
        if _is_outbox_enqueue(sync_session):
            killpoint_seen = True
            raise InjectedProcessCrash("killed before the first durable outbox enqueue")

    event.listen(AsyncSession.sync_session_class, "before_commit", kill_enqueue)
    crash_captured = False
    observed_result: dict[str, Any] | None = None
    try:
        async with sqlite_factory() as db:
            try:
                observed_result = await execute_tool(
                    "notification_send",
                    _notification_payload("pre-enqueue"),
                    ToolContext(
                        db=db,
                        owner_id=OWNER_ID,
                        run_id=run_id,
                        trigger="user_message",
                        tool_call_id="outbox-pre-enqueue",
                    ),
                )
            except InjectedProcessCrash:
                crash_captured = True
                await db.rollback()
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", kill_enqueue)

    _require_harness(
        killpoint_seen,
        f"outbox enqueue killpoint was not reached; result={observed_result!r}",
    )
    _require_harness(crash_captured, "outbox enqueue process death was swallowed")
    async with sqlite_factory() as db:
        notification_channels = list(
            (
                await db.execute(
                    select(Notification.channel).where(Notification.run_id == run_id)
                )
            ).scalars()
        )
        notification_count = len(notification_channels)
    outbox_count = await _outbox_row_count(sqlite_factory)
    assert (send_count, notification_count, outbox_count) == (0, 0, 0), (
        f"send_count={send_count}; notifications={notification_count}; "
        f"outbox_rows={outbox_count}"
    )


@pytest.mark.asyncio
async def test_outbox_accept_then_receipt_kill_requires_reconciliation_not_resend(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    run_id = await _create_run(sqlite_factory)
    state = {"accepted": False, "send_count": 0}
    raw = _notification_payload("receipt")
    context_kwargs = {
        "owner_id": OWNER_ID,
        "run_id": run_id,
        "trigger": "user_message",
        "tool_call_id": "outbox-receipt",
    }
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)

    def fake_email(_self, _reply_token: str, _title: str, _body: str) -> None:
        state["send_count"] += 1
        state["accepted"] = True

    monkeypatch.setattr(NotificationService, "_send_email", fake_email)
    dispatcher_entry = _outbox_dispatcher()
    assert dispatcher_entry is not None, (
        "H2-TXN-007 requires an independently callable outbox dispatcher"
    )

    async with sqlite_factory() as enqueue_db:
        enqueue_result = await execute_tool(
            "notification_send",
            raw,
            ToolContext(db=enqueue_db, **context_kwargs),
        )
    _require_harness(
        enqueue_result.get("ok") is True,
        f"outbox enqueue baseline failed: {enqueue_result!r}",
    )
    _require_harness(
        state["send_count"] == 0,
        "notification tool delivered inline despite an outbox dispatcher",
    )

    killpoint_seen = False

    def kill_receipt(_sync_session) -> None:
        nonlocal killpoint_seen
        if state["accepted"]:
            killpoint_seen = True
            raise InjectedProcessCrash("killed after transport acceptance before receipt commit")

    event.listen(AsyncSession.sync_session_class, "before_commit", kill_receipt)
    crash_captured = False
    try:
        try:
            await _dispatch_one(dispatcher_entry, sqlite_factory, monkeypatch)
        except InjectedProcessCrash:
            crash_captured = True
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", kill_receipt)

    _require_harness(state["accepted"], "synthetic transport was never accepted")
    _require_harness(killpoint_seen, "outbox receipt killpoint was not reached")
    _require_harness(crash_captured, "outbox receipt process death was swallowed")
    states_after_crash = await _durable_statuses(sqlite_factory, run_id=run_id)

    async with sqlite_factory() as retry_db:
        retry_result = await execute_tool(
            "notification_send",
            raw,
            ToolContext(db=retry_db, **context_kwargs),
        )
    _require_harness(isinstance(retry_result, dict), "outbox retry returned no typed result")
    states_after_retry = await _durable_statuses(sqlite_factory, run_id=run_id)
    typed_reconciliation = (
        retry_result.get("error_code") == "needs_reconciliation"
        or retry_result.get("status") == "needs_reconciliation"
        or (retry_result.get("data") or {}).get("status") == "needs_reconciliation"
        or retry_result.get("reconciled") is True
    )
    assert (
        state["send_count"] == 1
        and bool(states_after_crash.intersection({"delivering", "needs_reconciliation"}))
        and "needs_reconciliation" in states_after_retry
        and typed_reconciliation
    ), (
        f"retry={retry_result!r}; send_count={state['send_count']}; "
        f"after_crash={sorted(states_after_crash)}; after_retry={sorted(states_after_retry)}"
    )


@pytest.mark.asyncio
async def test_receipt_sqlite_lock_preserves_uncertain_fence_and_exact_retry_does_not_resend(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """A real writer lock after SMTP acceptance must not become a blind retry."""

    run_id = await _create_run(sqlite_factory)
    raw = _notification_payload("receipt-lock")
    context_kwargs = {
        "owner_id": OWNER_ID,
        "run_id": run_id,
        "trigger": "user_message",
        "tool_call_id": "outbox-receipt-lock",
    }
    send_entered = threading.Event()
    release_send = threading.Event()
    send_count = 0
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)

    def gated_email(_self, _reply_token: str, _title: str, _body: str) -> None:
        nonlocal send_count
        send_count += 1
        send_entered.set()
        if not release_send.wait(timeout=5):
            raise HarnessInvariantError("SMTP acceptance gate was not released")

    monkeypatch.setattr(NotificationService, "_send_email", gated_email)
    async with sqlite_factory() as enqueue_db:
        enqueue_result = await execute_tool(
            "notification_send",
            raw,
            ToolContext(db=enqueue_db, **context_kwargs),
        )
    _require_harness(
        enqueue_result.get("status") == "pending_delivery",
        f"SMTP intent did not become pending delivery: {enqueue_result!r}",
    )
    action_key = str(enqueue_result["data"]["outbox_action_keys"][0])

    dispatch_task = asyncio.create_task(
        outbox_module.dispatch_action(
            action_key=action_key,
            session_factory=sqlite_factory,
            wait_for_active_seconds=0,
        )
    )
    await asyncio.wait_for(asyncio.to_thread(send_entered.wait, 3), timeout=4)

    async with sqlite_factory() as lock_db:
        await lock_db.execute(text("BEGIN IMMEDIATE"))
        try:
            release_send.set()
            dispatch_result = await asyncio.wait_for(dispatch_task, timeout=3)

            async with sqlite_factory() as inspect_db:
                action = (
                    await inspect_db.execute(
                        select(OutboxAction).where(
                            OutboxAction.action_key == action_key
                        )
                    )
                ).scalar_one()
                invocation = await inspect_db.get(ToolInvocation, action.invocation_id)
                notification = await inspect_db.get(Notification, action.notification_id)
                receipt_count = int(
                    await inspect_db.scalar(
                        select(func.count(OutboxReceipt.id)).where(
                            OutboxReceipt.outbox_action_id == action.id
                        )
                    )
                    or 0
                )
            assert (
                dispatch_result.get("status") == "needs_reconciliation"
                and dispatch_result.get("error_code") == "needs_reconciliation"
                and dispatch_result.get("uncertain_outcome") is True
                and send_count == 1
                and action.status == "delivering"
                and action.attempt == 1
                and receipt_count == 0
                and invocation is not None
                and invocation.status == "needs_reconciliation"
                and notification is not None
                and notification.status == "needs_reconciliation"
            ), (
                f"dispatch={dispatch_result!r}; send_count={send_count}; "
                f"action={(action.status, action.attempt)!r}; receipts={receipt_count}; "
                f"invocation={invocation.status if invocation else None!r}; "
                f"notification={notification.status if notification else None!r}"
            )
        finally:
            release_send.set()
            await rollback_uow(lock_db)

    recovered = await outbox_module.recover_interrupted_deliveries(
        session_factory=sqlite_factory
    )
    async with sqlite_factory() as retry_db:
        retry_result = await execute_tool(
            "notification_send",
            raw,
            ToolContext(db=retry_db, **context_kwargs),
        )
    async with sqlite_factory() as inspect_db:
        action = (
            await inspect_db.execute(
                select(OutboxAction).where(OutboxAction.action_key == action_key)
            )
        ).scalar_one()
        receipt_count = int(
            await inspect_db.scalar(
                select(func.count(OutboxReceipt.id)).where(
                    OutboxReceipt.outbox_action_id == action.id
                )
            )
            or 0
        )

    assert (
        recovered.get("fenced_external") == 1
        and action.status == "needs_reconciliation"
        and retry_result.get("ok") is False
        and retry_result.get("error_code") == "needs_reconciliation"
        and retry_result.get("retryable") is False
        and send_count == 1
        and receipt_count == 0
    ), (
        f"recovered={recovered!r}; retry={retry_result!r}; "
        f"action={action.status!r}; send_count={send_count}; receipts={receipt_count}"
    )


@pytest.mark.asyncio
async def test_two_dispatchers_claim_one_outbox_action_and_deliver_once(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    dispatcher_entry = _outbox_dispatcher()
    assert dispatcher_entry is not None, "H2 requires an independent outbox dispatcher"
    run_id = await _create_run(sqlite_factory)
    send_count = 0
    send_entered = asyncio.Event()
    send_release = threading.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)

    def gated_email(_self, _reply_token: str, _title: str, _body: str) -> None:
        nonlocal send_count
        send_count += 1
        loop.call_soon_threadsafe(send_entered.set)
        if not send_release.wait(timeout=3):
            raise HarnessInvariantError("dispatcher transport release gate timed out")

    monkeypatch.setattr(NotificationService, "_send_email", gated_email)
    async with sqlite_factory() as enqueue_db:
        enqueue_result = await execute_tool(
            "notification_send",
            _notification_payload("double-dispatch"),
            ToolContext(
                db=enqueue_db,
                owner_id=OWNER_ID,
                run_id=run_id,
                trigger="user_message",
                tool_call_id="outbox-double-dispatch",
            ),
        )
    _require_harness(
        enqueue_result.get("ok") is True,
        f"double-dispatch intent was not enqueued: {enqueue_result!r}",
    )

    first_task = asyncio.create_task(
        _dispatch_one(dispatcher_entry, sqlite_factory, monkeypatch)
    )
    try:
        await asyncio.wait_for(send_entered.wait(), timeout=2)
        second_result = await asyncio.wait_for(
            _dispatch_one(dispatcher_entry, sqlite_factory, monkeypatch),
            timeout=2,
        )
    finally:
        send_release.set()
    first_result = await asyncio.wait_for(first_task, timeout=3)

    async with sqlite_factory() as db:
        actions = list(
            (
                await db.execute(
                    select(OutboxAction).where(OutboxAction.run_id == run_id)
                )
            ).scalars()
        )
        receipts = list(
            (
                await db.execute(
                    select(OutboxReceipt).join(
                        OutboxAction,
                        OutboxReceipt.outbox_action_id == OutboxAction.id,
                    ).where(OutboxAction.run_id == run_id)
                )
            ).scalars()
        )
    dispatcher_states = {
        first_result.get("status") if isinstance(first_result, dict) else None,
        second_result.get("status") if isinstance(second_result, dict) else None,
    }
    assert (
        send_count == 1
        and len(actions) == 1
        and actions[0].status == "delivered"
        and actions[0].attempt == 1
        and len(receipts) == 1
        and dispatcher_states == {"delivered", "idle"}
    ), (
        f"send_count={send_count}; actions="
        f"{[(item.status, item.attempt) for item in actions]}; "
        f"receipts={len(receipts)}; first={first_result!r}; second={second_result!r}"
    )


@pytest.mark.asyncio
async def test_bounded_duplicate_notification_storm_delivers_one_receipt_and_transport(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Eight callers and dispatchers preserve one notification side effect."""

    run_id = await _create_run(sqlite_factory, status="running")
    raw = _notification_payload("bounded-storm")
    context_kwargs = {
        "owner_id": OWNER_ID,
        "run_id": run_id,
        "trigger": "user_message",
        "tool_call_id": "h2-bounded-notification-storm",
    }
    send_count = 0
    send_entered = asyncio.Event()
    release_send = threading.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(NotificationService, "_email_configured", lambda _self: True)

    def gated_email(_self, _reply_token: str, _title: str, _body: str) -> None:
        nonlocal send_count
        send_count += 1
        loop.call_soon_threadsafe(send_entered.set)
        if not release_send.wait(timeout=5):
            raise HarnessInvariantError("notification storm transport gate timed out")

    monkeypatch.setattr(NotificationService, "_send_email", gated_email)

    async def caller() -> dict[str, Any]:
        async with sqlite_factory() as db:
            return await execute_tool(
                "notification_send",
                raw,
                ToolContext(db=db, **context_kwargs),
            )

    caller_results = await asyncio.wait_for(
        asyncio.gather(*(caller() for _ in range(8))),
        timeout=5,
    )
    async with sqlite_factory() as db:
        action = (
            await db.execute(
                select(OutboxAction).where(OutboxAction.run_id == run_id)
            )
        ).scalar_one()
        action_key = action.action_key

    async def dispatcher() -> dict[str, Any]:
        try:
            return await outbox_module.dispatch_action(
                action_key=action_key,
                session_factory=sqlite_factory,
                wait_for_active_seconds=2,
            )
        except DatabaseBusyError as exc:
            # Internal workers surface the typed exception to their owning
            # coordinator; HTTP/runtime adapters use this same stable payload.
            return exc.as_result()

    dispatch_tasks = [asyncio.create_task(dispatcher()) for _ in range(8)]
    try:
        await asyncio.wait_for(send_entered.wait(), timeout=2)
        # Give every targeted waiter one event-loop turn to observe the active
        # durable claim before the synthetic transport returns.
        await asyncio.sleep(0.05)
    finally:
        release_send.set()
    dispatch_results = await asyncio.wait_for(
        asyncio.gather(*dispatch_tasks),
        timeout=4,
    )
    converged_dispatch_results = []
    for result in dispatch_results:
        if result.get("error_code") == "database_busy":
            result = await outbox_module.dispatch_action(
                action_key=action_key,
                session_factory=sqlite_factory,
                wait_for_active_seconds=0,
            )
        converged_dispatch_results.append(result)
    replay = await caller()

    async with sqlite_factory() as db:
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
        notification_channels = list(
            (
                await db.execute(
                    select(Notification.channel).where(Notification.run_id == run_id)
                )
            ).scalars()
        )
        notification_count = len(notification_channels)
        action_count = int(
            await db.scalar(
                select(func.count(OutboxAction.id)).where(OutboxAction.run_id == run_id)
            )
            or 0
        )
        receipt_count = int(
            await db.scalar(
                select(func.count(OutboxReceipt.id)).join(
                    OutboxAction,
                    OutboxReceipt.outbox_action_id == OutboxAction.id,
                ).where(OutboxAction.run_id == run_id)
            )
            or 0
        )

    caller_errors = [item for item in caller_results if item.get("ok") is False]
    assert (
        any(item.get("ok") is True for item in caller_results)
        and all(
            item.get("error_code") in {"invocation_in_progress", "database_busy"}
            and item.get("retryable") is True
            for item in caller_errors
        )
        and all(
            item.get("status") == "delivered"
            or (
                item.get("error_code") == "database_busy"
                and item.get("retryable") is True
            )
            for item in dispatch_results
        )
        and all(
            item.get("status") == "delivered"
            for item in converged_dispatch_results
        )
        and all(
            item.get("data") == converged_dispatch_results[0].get("data")
            for item in converged_dispatch_results
        )
        and replay.get("ok") is True
        and replay.get("replayed") is True
        and replay.get("status") == "committed"
        and send_count == 1
        and (invocation_count, notification_count, action_count, receipt_count)
        == (1, 2, 1, 1)
        and sorted(notification_channels) == ["email", "in_app"]
    ), (
        f"callers={caller_results!r}; dispatchers={dispatch_results!r}; "
        f"converged={converged_dispatch_results!r}; "
        f"replay={replay!r}; send_count={send_count}; "
        f"counts={(invocation_count, notification_count, action_count, receipt_count)!r}; "
        f"channels={notification_channels!r}"
    )


def _is_file_intent(sync_session, run_id: str) -> bool:
    # The handler flushes the action so it can link Operation/Invocation before
    # the owning UoW commits.  A flushed row has left ``session.new`` but still
    # belongs to the uncommitted identity map and must trigger this killpoint.
    for item in _pending_objects(sync_session):
        table_name = getattr(getattr(item, "__table__", None), "name", "").lower()
        item_run_id = getattr(item, "run_id", run_id)
        if item_run_id == run_id and (
            isinstance(item, Operation)
            or "outbox" in table_name
            or "file_effect" in table_name
            or "file_intent" in table_name
        ):
            return True
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["new", "overwrite"],
    ids=["new-target", "overwrite-target"],
)
async def test_file_new_and_overwrite_kill_before_intent_leave_targets_unchanged(
    sqlite_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,
    mode: str,
) -> None:
    workspace_root = tmp_path / f"file-pre-intent-{mode}"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace_root)
    target = workspace_root / "artifact.txt"
    original = None if mode == "new" else "original bytes"
    if original is not None:
        target.write_text(original, encoding="utf-8")
    run_id = await _create_run(sqlite_factory)
    killpoint_seen = False

    def kill_intent(sync_session) -> None:
        nonlocal killpoint_seen
        if _is_file_intent(sync_session, run_id):
            killpoint_seen = True
            raise InjectedProcessCrash(f"killed {mode} before durable file intent")

    event.listen(AsyncSession.sync_session_class, "before_commit", kill_intent)
    crash_captured = False
    try:
        async with sqlite_factory() as db:
            try:
                await execute_tool(
                    "file_write",
                    json.dumps(
                        {
                            "path": "artifact.txt",
                            "content": "desired bytes",
                            "overwrite": True,
                        },
                        sort_keys=True,
                    ),
                    ToolContext(
                        db=db,
                        owner_id=OWNER_ID,
                        run_id=run_id,
                        trigger="user_message",
                        tool_call_id=f"file-pre-intent-{mode}",
                    ),
                )
            except InjectedProcessCrash:
                crash_captured = True
                await db.rollback()
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", kill_intent)

    _require_harness(killpoint_seen, f"{mode} file-intent killpoint was not reached")
    _require_harness(crash_captured, f"{mode} file-intent process death was swallowed")
    actual = target.read_text(encoding="utf-8") if target.exists() else None
    residue = sorted(
        str(path.relative_to(workspace_root)) for path in workspace_root.rglob("*")
    )
    expected_residue = ["artifact.txt"] if mode == "overwrite" else []
    assert actual == original and residue == expected_residue, (
        f"mode={mode}; actual={actual!r}; expected={original!r}; "
        f"residue={residue!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["new", "overwrite"],
    ids=["new-target", "overwrite-target"],
)
async def test_file_new_and_overwrite_publish_kill_reconcile_exact_retry(
    sqlite_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,
    mode: str,
) -> None:
    dispatcher_entry = _outbox_dispatcher()
    assert dispatcher_entry is not None, (
        "H2-TXN-008 requires filesystem effects to use the outbox dispatcher"
    )
    workspace_root = tmp_path / f"file-post-publish-{mode}"
    workspace_root.mkdir()
    monkeypatch.setattr(workspace_tools, "WORKSPACE_ROOT", workspace_root)
    target = workspace_root / "artifact.txt"
    original = None if mode == "new" else "original bytes"
    if original is not None:
        target.write_text(original, encoding="utf-8")
    desired = f"desired bytes for {mode}"
    run_id = await _create_run(sqlite_factory)
    raw = json.dumps(
        {"path": "artifact.txt", "content": desired, "overwrite": True},
        sort_keys=True,
    )
    context_kwargs = {
        "owner_id": OWNER_ID,
        "run_id": run_id,
        "trigger": "user_message",
        "tool_call_id": f"file-post-publish-{mode}",
    }
    async with sqlite_factory() as enqueue_db:
        enqueue_result = await execute_tool(
            "file_write", raw, ToolContext(db=enqueue_db, **context_kwargs)
        )
    _require_harness(
        enqueue_result.get("ok") is True,
        f"{mode} file intent was not enqueued: {enqueue_result!r}",
    )
    untouched = target.read_text(encoding="utf-8") if target.exists() else None
    _require_harness(
        untouched == original,
        f"{mode} file was published inline before dispatcher claim: {untouched!r}",
    )

    killpoint_seen = False

    def kill_receipt(_sync_session) -> None:
        nonlocal killpoint_seen
        if target.exists() and target.read_text(encoding="utf-8") == desired:
            killpoint_seen = True
            raise InjectedProcessCrash(f"killed {mode} file receipt commit")

    event.listen(AsyncSession.sync_session_class, "before_commit", kill_receipt)
    crash_captured = False
    try:
        try:
            await _dispatch_one(dispatcher_entry, sqlite_factory, monkeypatch)
        except InjectedProcessCrash:
            crash_captured = True
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", kill_receipt)

    _require_harness(killpoint_seen, f"{mode} file-receipt killpoint was not reached")
    _require_harness(crash_captured, f"{mode} file-receipt process death was swallowed")
    states_after_crash = await _durable_statuses(sqlite_factory, run_id=run_id)
    content_after_crash = target.read_text(encoding="utf-8") if target.exists() else None

    async with sqlite_factory() as retry_db:
        retry_result = await execute_tool(
            "file_write", raw, ToolContext(db=retry_db, **context_kwargs)
        )
    _require_harness(isinstance(retry_result, dict), f"{mode} retry was not typed")
    async with sqlite_factory() as db:
        operation_count = int(
            await db.scalar(
                select(func.count(Operation.id)).where(Operation.run_id == run_id)
            )
            or 0
        )
        invocation_count = int(
            await db.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.run_id == run_id
                )
            )
            or 0
        )
    recovery_marker = (
        retry_result.get("replayed") is True
        or retry_result.get("reconciled") is True
        or retry_result.get("error_code") == "needs_reconciliation"
        or retry_result.get("status") == "needs_reconciliation"
        or (retry_result.get("data") or {}).get("status")
        in {"reconciled", "needs_reconciliation"}
    )
    final_content = target.read_text(encoding="utf-8") if target.exists() else None
    assert (
        content_after_crash == desired
        and final_content == desired
        and bool(states_after_crash.intersection({"delivering", "needs_reconciliation"}))
        and recovery_marker
        and operation_count == 1
        and invocation_count == 1
    ), (
        f"mode={mode}; content_after_crash={content_after_crash!r}; "
        f"final_content={final_content!r}; states={sorted(states_after_crash)}; "
        f"retry={retry_result!r}; operations={operation_count}; "
        f"invocations={invocation_count}"
    )


def _typed_busy_state(result: Any) -> bool:
    if isinstance(result, OperationalError):
        return False
    if isinstance(result, dict):
        code = result.get("error_code") or result.get("code")
        retryable = result.get("retryable")
        state = result.get("state") or result.get("status")
        retry_after = result.get("retry_after_ms")
    elif isinstance(result, BaseException):
        code = getattr(result, "error_code", None) or getattr(result, "code", None)
        retryable = getattr(result, "retryable", None)
        state = getattr(result, "state", None) or getattr(result, "status", None)
        retry_after = getattr(result, "retry_after_ms", None)
    else:
        return False
    return (
        code in {"database_busy", "database_locked", "writer_budget_exhausted"}
        and retryable is True
        and state in {"queued", "retry_pending", "database_busy", "database_locked"}
        and isinstance(retry_after, int)
        and retry_after >= 0
    )


@pytest.mark.asyncio
async def test_main_tool_lock_budget_is_typed_then_exact_retry_commits_once(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    main_run_id = await _create_run(sqlite_factory, status="queued")
    handler_calls = 0

    async def handler(_ctx: ToolContext, args: ProbeArgs) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": args.value}

    monkeypatch.setitem(
        tool_registry.TOOL_MAP,
        "h2_lock_budget_probe",
        _tool_definition("h2_lock_budget_probe", handler),
    )
    raw = json.dumps({"value": 9})

    async def main_actor() -> Any:
        async with sqlite_factory() as db:
            return await execute_tool(
                "h2_lock_budget_probe",
                raw,
                ToolContext(
                    db=db,
                    owner_id=OWNER_ID,
                    run_id=main_run_id,
                    trigger="user_message",
                    tool_call_id="lock-budget-main",
                ),
            )

    async with sqlite_factory() as lock_holder:
        await lock_holder.execute(text("BEGIN IMMEDIATE"))
        try:
            locked_result = await asyncio.wait_for(main_actor(), timeout=2)
        except asyncio.TimeoutError as exc:
            raise HarnessInvariantError(
                "main tool exceeded its bounded lock deadline"
            ) from exc
        except BaseException as exc:
            locked_result = exc
        finally:
            await lock_holder.rollback()

    async with sqlite_factory() as db:
        durable_main = await db.get(AgentRun, main_run_id)
    _require_harness(durable_main is not None, "lock fixture lost its main Run")
    durable_pre_retry = durable_main.status == "queued" and handler_calls == 0

    main_retry = await main_actor()
    _require_harness(isinstance(main_retry, dict), "main retry returned no typed result")

    async with sqlite_factory() as db:
        invocations = list(
            (
                await db.execute(
                    select(ToolInvocation).where(ToolInvocation.run_id == main_run_id)
                )
            ).scalars()
        )

    assert (
        durable_pre_retry
        and _typed_busy_state(locked_result)
        and main_retry.get("ok") is True
        and handler_calls == 1
        and len(invocations) == 1
        and invocations[0].status == "committed"
    ), (
        f"locked_result={locked_result!r}; durable_pre_retry={durable_pre_retry}; "
        f"main_retry={main_retry!r}; handler_calls={handler_calls}; "
        f"invocations={[(item.status, item.result_payload) for item in invocations]}"
    )


@pytest.mark.asyncio
async def test_child_event_lock_budget_is_typed_then_exact_retry_commits_once(
    sqlite_factory: async_sessionmaker[AsyncSession],
) -> None:
    parent_run_id = await _create_run(sqlite_factory, status="running")
    child_run_id = await _create_run(
        sqlite_factory,
        trigger="subagent",
        status="running",
        parent_run_id=parent_run_id,
    )

    async def child_actor() -> RunEvent:
        async with sqlite_factory() as db:
            return await emit_event(
                db,
                child_run_id,
                "tool.completed",
                "lock-budget child event",
            )

    async with sqlite_factory() as lock_holder:
        await lock_holder.execute(text("BEGIN IMMEDIATE"))
        try:
            locked_result: Any = await asyncio.wait_for(child_actor(), timeout=2)
        except asyncio.TimeoutError as exc:
            raise HarnessInvariantError(
                "child event exceeded its bounded lock deadline"
            ) from exc
        except BaseException as exc:
            locked_result = exc
        finally:
            await lock_holder.rollback()

    async with sqlite_factory() as db:
        before_retry = list(
            (
                await db.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == child_run_id,
                        RunEvent.event_type == "tool.completed",
                    )
                )
            ).scalars()
        )
    child_retry = await child_actor()
    _require_harness(isinstance(child_retry, RunEvent), "child retry did not persist an event")
    async with sqlite_factory() as db:
        after_retry = list(
            (
                await db.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == child_run_id,
                        RunEvent.event_type == "tool.completed",
                    )
                )
            ).scalars()
        )
    assert _typed_busy_state(locked_result) and not before_retry and len(after_retry) == 1, (
        f"locked_result={locked_result!r}; before_retry={len(before_retry)}; "
        f"after_retry={len(after_retry)}"
    )


@pytest.mark.asyncio
async def test_heartbeat_lock_budget_is_typed_then_exact_retry_commits_once(
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", sqlite_factory)

    def do_not_start_runtime(_run_id: str, coroutine):
        coroutine.close()
        task = asyncio.get_running_loop().create_future()
        task.set_result(None)
        return task

    monkeypatch.setattr(scheduler_module, "start_tracked_task", do_not_start_runtime)
    scheduler = ProactiveScheduler()
    objective = "H2 deterministic lock-budget heartbeat"

    async def heartbeat_actor() -> AgentRun:
        return await scheduler.trigger_now(
            "manual_heartbeat",
            objective=objective,
        )

    async with sqlite_factory() as lock_holder:
        await lock_holder.execute(text("BEGIN IMMEDIATE"))
        try:
            locked_result: Any = await asyncio.wait_for(heartbeat_actor(), timeout=2)
        except asyncio.TimeoutError as exc:
            raise HarnessInvariantError(
                "heartbeat exceeded its bounded lock deadline"
            ) from exc
        except BaseException as exc:
            locked_result = exc
        finally:
            await lock_holder.rollback()

    async with sqlite_factory() as db:
        before_retry = list(
            (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.trigger == "manual_heartbeat",
                        AgentRun.objective == objective,
                    )
                )
            ).scalars()
        )
    heartbeat_retry = await heartbeat_actor()
    _require_harness(
        isinstance(heartbeat_retry, AgentRun),
        "heartbeat retry did not persist a Run",
    )
    async with sqlite_factory() as db:
        after_retry = list(
            (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.trigger == "manual_heartbeat",
                        AgentRun.objective == objective,
                    )
                )
            ).scalars()
        )
    assert _typed_busy_state(locked_result) and not before_retry and len(after_retry) == 1, (
        f"locked_result={locked_result!r}; before_retry={len(before_retry)}; "
        f"after_retry={len(after_retry)}"
    )
