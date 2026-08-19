from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

import app.context.assembler as assembler_module
import app.context.memory as memory_module
from app.context.assembler import ContextAssembler
from app.db.database import AsyncSessionLocal
from app.db.uow import (
    commit as commit_uow,
    flush as flush_uow,
    rollback as rollback_uow,
)
from app.models import AgentRun, Memory, RunEvent
from app.runtime.events import emit_event
from app.tools import ToolContext, execute_tool


def _memory(content: str) -> Memory:
    return Memory(
        owner_id="local",
        scope="global",
        layer="semantic",
        content=content,
        source_type="test",
        status="confirmed",
    )


@pytest.mark.asyncio
async def test_context_projection_rejects_nested_transaction_scope() -> None:
    async with AsyncSessionLocal() as db:
        nested = await db.begin_nested()
        try:
            with pytest.raises(RuntimeError, match="SAVEPOINT"):
                await ContextAssembler(db).build("local", objective="nested projection")
        finally:
            await nested.rollback()


@pytest.mark.asyncio
async def test_context_projection_waits_for_outer_commit_after_savepoint_release(
    tmp_path: Path,
) -> None:
    projection = tmp_path / "context.md"
    async with AsyncSessionLocal() as db:
        db.add(_memory("outer write before nested commit"))
        await flush_uow(db)
        assembler_module._stage_context_projection(db, projection, "durable context\n")

        async with db.begin_nested():
            db.add(_memory("nested write"))
            await flush_uow(db)

        assert not projection.exists()
        assert db.sync_session.info.get("h2_session_write_activity") is True
        assert str(projection) in db.sync_session.info.get("h2_context_projections", {})

        await commit_uow(db)

    assert projection.read_text(encoding="utf-8") == "durable context\n"


@pytest.mark.asyncio
async def test_savepoint_rollback_preserves_outer_write_guard_until_outer_rollback(
    tmp_path: Path,
) -> None:
    projection = tmp_path / "rolled-back-context.md"
    async with AsyncSessionLocal() as db:
        db.add(_memory("outer write before nested rollback"))
        await flush_uow(db)
        assembler_module._stage_context_projection(db, projection, "must not publish\n")

        nested = await db.begin_nested()
        db.add(_memory("rolled back nested write"))
        await flush_uow(db)
        await nested.rollback()

        assert not projection.exists()
        assert db.sync_session.info.get("h2_session_write_activity") is True
        assert str(projection) in db.sync_session.info.get("h2_context_projections", {})

        await rollback_uow(db)
        assert "h2_session_write_activity" not in db.sync_session.info
        assert "h2_context_projections" not in db.sync_session.info

    assert not projection.exists()


@pytest.mark.asyncio
async def test_memory_embedding_wait_does_not_hold_database_or_event_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maintenance acquires event serialization only after provider preflight."""

    provider_entered = threading.Event()
    provider_release = threading.Event()

    class BlockingProvider:
        name = "h2-blocking-provider"

        def embed(self, _text: str) -> list[float]:
            provider_entered.set()
            if not provider_release.wait(timeout=5):
                raise TimeoutError("deterministic provider gate timed out")
            return [0.25, 0.75]

    monkeypatch.setattr(
        memory_module,
        "get_embedding_provider",
        lambda: BlockingProvider(),
    )

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            trigger="manual_heartbeat",
            objective="exercise deferred maintenance boundary",
            status="running",
        )
        memory = _memory("durable embedding boundary fixture")
        db.add_all([run, memory])
        await commit_uow(db)
        run_id = run.id
        memory_id = memory.id

    async def maintain() -> dict:
        async with AsyncSessionLocal() as db:
            return await execute_tool(
                "memory_maintain",
                "{}",
                ToolContext(
                    db=db,
                    owner_id="local",
                    run_id=run_id,
                    trigger="manual_heartbeat",
                    tool_call_id="h2-memory-maintain",
                ),
            )

    maintenance_task = asyncio.create_task(maintain())
    try:
        entered = await asyncio.to_thread(provider_entered.wait, 2)
        assert entered, "embedding provider was never entered"
        async with AsyncSessionLocal() as event_db:
            progress = await asyncio.wait_for(
                emit_event(
                    event_db,
                    run_id,
                    "run.progress",
                    "writer remained available during embedding",
                ),
                timeout=1.5,
            )
        assert progress.sequence == 1
    finally:
        provider_release.set()

    result = await asyncio.wait_for(maintenance_task, timeout=5)
    assert result["ok"] is True
    assert result["completion_event_persisted"] is True

    async with AsyncSessionLocal() as db:
        stored_memory = await db.get(Memory, memory_id)
        events = list(
            (
                await db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                )
            ).scalars()
        )

    assert stored_memory is not None
    assert stored_memory.embedding == [0.25, 0.75]
    assert [(event.sequence, event.event_type) for event in events] == [
        (1, "run.progress"),
        (2, "tool.completed"),
    ]
