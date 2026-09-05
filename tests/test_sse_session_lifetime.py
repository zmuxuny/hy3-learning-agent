"""SSE disconnects must not strand a SQLite connection in a cancelled scope."""

import json

import anyio
import pytest
from app.api import agent as agent_api
from app.db.database import AsyncSessionLocal, engine
from app.models import AgentRun, RunEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def seed_run():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="SSE lifetime probe")
        db.add(run)
        await db.flush()
        db.add_all([
            RunEvent(run_id=run.id, sequence=1, event_type="run.started", summary="started"),
            RunEvent(run_id=run.id, sequence=2, event_type="assistant.status", summary="working"),
        ])
        await db.commit()
        return run.id


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [True, False])
async def test_sse_returns_connection_before_any_client_yield(exists):
    run_id = await seed_run() if exists else "missing-run"
    response = await agent_api.stream_run_events(run_id)
    iterator = response.body_iterator
    try:
        first = await anext(iterator)
        # This is the suspended point where the HTTP client can disconnect or
        # remain slow indefinitely. The database must already be released.
        assert engine.sync_engine.pool.checkedout() == 0
        if exists:
            second = await anext(iterator)
            payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in [first, second]]
            assert [payload["sequence"] for payload in payloads] == [1, 2]
            assert engine.sync_engine.pool.checkedout() == 0
        else:
            assert first == 'event: error\ndata: {"error": "Run not found"}\n\n'
    finally:
        await iterator.aclose()
    assert engine.sync_engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_anyio_disconnect_during_poll_finishes_close_and_unsubscribes(monkeypatch):
    run_id = await seed_run()
    read_finished = anyio.Event()
    release_read = anyio.Event()
    consumer_finished = anyio.Event()
    sessions = []
    scopes = []
    unsubscribed = []

    class ObservedSession(AsyncSession):
        close_finished = False

        async def execute(self, *args, **kwargs):
            result = await super().execute(*args, **kwargs)
            # The actual SQLite connection is checked out. Hold the poll here
            # until the HTTP request's enclosing cancellation scope is cancelled.
            read_finished.set()
            await release_read.wait()
            return result

        async def close(self):
            # A level-cancelled AnyIO scope interrupts this await repeatedly
            # unless the DB lifetime is shielded, unlike one asyncio cancel().
            await anyio.lowlevel.checkpoint()
            await super().close()
            self.close_finished = True

    factory = async_sessionmaker(engine, class_=ObservedSession, expire_on_commit=False)

    def session_factory():
        session = factory()
        sessions.append(session)
        return session

    original_unsubscribe = agent_api.unsubscribe_stream

    def unsubscribe(run_id, queue):
        original_unsubscribe(run_id, queue)
        unsubscribed.append(run_id)

    monkeypatch.setattr(agent_api, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(agent_api, "unsubscribe_stream", unsubscribe)
    response = await agent_api.stream_run_events(run_id)
    iterator = response.body_iterator

    async def consume():
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    async for _frame in iterator:
                        await anyio.lowlevel.checkpoint()
                finally:
                    # Match an ASGI consumer closing its body iterator after
                    # cancellation; this must not be needed to release the DB.
                    with anyio.CancelScope(shield=True):
                        await iterator.aclose()
        finally:
            consumer_finished.set()

    try:
        with anyio.fail_after(3):
            async with anyio.create_task_group() as group:
                group.start_soon(consume)
                await read_finished.wait()
                assert engine.sync_engine.pool.checkedout() == 1
                scopes[0].cancel()
                release_read.set()
                await consumer_finished.wait()
        assert sessions and all(session.close_finished for session in sessions)
        assert engine.sync_engine.pool.checkedout() == 0
        assert unsubscribed == [run_id]
    finally:
        # Also clean up when this counterexample is run against the old code,
        # so a regression failure does not itself leak a test connection.
        with anyio.CancelScope(shield=True):
            await iterator.aclose()
            for session in sessions:
                await session.close()
