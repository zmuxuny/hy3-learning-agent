import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.time import canonical_utc
from app.db.uow import (
    DEFAULT_RETRY_DELAYS,
    DatabaseBusyError,
    commit as commit_uow,
    flush as flush_uow,
    rollback as rollback_uow,
)
from app.models import RunEvent


_subscribers: dict[str, set[asyncio.Queue]] = {}
_event_locks: WeakKeyDictionary = WeakKeyDictionary()
# SQLite permits only one writer at a time. Sub-agents write events to
# different run_ids concurrently, so per-run locks alone do not protect the
# single database writer. Keep the wider lock SQLite-only; server databases can
# continue writing unrelated runs concurrently.
_sqlite_event_write_locks: WeakKeyDictionary = WeakKeyDictionary()


def _sqlite_event_write_lock() -> asyncio.Lock:
    """Return the single-writer lock owned by the current event loop.

    The production server has one loop. Tests and embedded callers may create
    several loops over the process lifetime; asyncio locks cannot cross them.
    """
    loop = asyncio.get_running_loop()
    lock = _sqlite_event_write_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _sqlite_event_write_locks[loop] = lock
    return lock


def _run_event_lock(run_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _event_locks.get(loop)
    if locks is None:
        locks = {}
        _event_locks[loop] = locks
    return locks.setdefault(run_id, asyncio.Lock())


def _event_serialization_lock(run_id: str) -> asyncio.Lock:
    return (
        _sqlite_event_write_lock()
        if settings.DATABASE_URL.startswith("sqlite")
        else _run_event_lock(run_id)
    )


@asynccontextmanager
async def serialize_event_write(run_id: str) -> AsyncIterator[None]:
    """Serialize sequence allocation before any participant takes a writer.

    Database-write tool coordination acquires this context before invoking its
    flush-only handler and holds it through domain + completion-event commit.
    Ordinary event append uses the same order, preventing max(sequence)+1
    races without ever waiting for this lock while already owning SQLite's
    writer.
    """

    async with _event_serialization_lock(run_id):
        yield


def publish_stream_event(run_id: str, payload: dict) -> None:
    """Push a realtime event to this process's SSE subscribers without persisting it."""
    queues = _subscribers.get(run_id)
    if not queues:
        return
    for queue in list(queues):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


async def subscribe_stream(run_id: str) -> asyncio.Queue:
    """Create a realtime event queue for one run. Callers must unsubscribe when done."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    _subscribers.setdefault(run_id, set()).add(queue)
    return queue


def unsubscribe_stream(run_id: str, queue: asyncio.Queue) -> None:
    queues = _subscribers.get(run_id)
    if queues:
        queues.discard(queue)
        if not queues:
            _subscribers.pop(run_id, None)


async def emit_event(
    db: AsyncSession,
    run_id: str,
    event_type: str,
    summary: str = "",
    payload: dict | None = None,
    *,
    event_key: str | None = None,
) -> RunEvent:
    # Runtime, steering and cancellation can all emit against the same Run at
    # once.  The sequence is scoped to a Run, so serialize its max+1 write in
    # this single-process personal server instead of letting a harmless race
    # fail the whole Run with a unique-constraint error.
    # Event append is a replay-safe short transaction.  It must not silently
    # become the commit boundary for unrelated caller mutations: a busy retry
    # rolls the failed append back before rebuilding only the RunEvent row.
    if db.new or db.dirty or db.deleted:
        raise RuntimeError(
            "emit_event requires a clean session; commit the owning Unit of Work first"
        )
    async with serialize_event_write(run_id):
        event = None
        retry_delays = (*DEFAULT_RETRY_DELAYS, None)
        last_busy: DatabaseBusyError | None = None
        for attempt, retry_delay in enumerate(retry_delays, start=1):
            try:
                if event_key is not None:
                    existing = (await db.execute(
                        select(RunEvent).where(
                            RunEvent.run_id == run_id,
                            RunEvent.event_key == event_key,
                        )
                    )).scalars().one_or_none()
                    if existing is not None:
                        await commit_uow(db)
                        event = existing
                        break
                result = await db.execute(
                    select(func.coalesce(func.max(RunEvent.sequence), 0)).where(RunEvent.run_id == run_id)
                )
                event = RunEvent(
                    run_id=run_id,
                    sequence=int(result.scalar_one()) + 1,
                    event_type=event_type,
                    event_key=event_key,
                    summary=summary,
                    payload=payload or {},
                )
                db.add(event)
                await commit_uow(db)
                await db.refresh(event)
                break
            except DatabaseBusyError as exc:
                last_busy = exc
                if retry_delay is None:
                    raise DatabaseBusyError(
                        retry_after_ms=int(DEFAULT_RETRY_DELAYS[-1] * 1000),
                        attempts=attempt,
                    ) from exc
                await asyncio.sleep(retry_delay)
            except BaseException:
                await rollback_uow(db)
                raise
        if event is None:  # pragma: no cover - defensive; loop either succeeds or raises
            raise RuntimeError("Run event was not persisted") from last_busy
    publish_stream_event(run_id, {
        "sequence": event.sequence,
        "type": event.event_type,
        "summary": event.summary,
        "payload": event.payload,
        "created_at": canonical_utc(event.created_at),
    })
    return event


async def stage_event(
    db: AsyncSession,
    run_id: str,
    event_type: str,
    summary: str = "",
    payload: dict | None = None,
    *,
    event_key: str | None = None,
) -> RunEvent:
    """Stage one event inside the caller's already-serialized Unit of Work.

    H3 terminal transitions need Run state, final messages and cross-run child
    projections to commit atomically.  Unlike :func:`emit_event`, this helper
    never commits and therefore must only be used while the caller owns the
    SQLite writer (or an equivalent database transaction lock).
    """

    if event_key is not None:
        existing = (await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_key == event_key,
            )
        )).scalars().one_or_none()
        if existing is not None:
            return existing
    next_sequence = int(
        await db.scalar(
            select(func.coalesce(func.max(RunEvent.sequence), 0)).where(
                RunEvent.run_id == run_id
            )
        )
        or 0
    ) + 1
    event = RunEvent(
        run_id=run_id,
        sequence=next_sequence,
        event_type=event_type,
        event_key=event_key,
        summary=summary,
        payload=payload or {},
    )
    db.add(event)
    await flush_uow(db)
    return event
