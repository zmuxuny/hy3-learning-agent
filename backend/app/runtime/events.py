import asyncio
from weakref import WeakKeyDictionary
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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
) -> RunEvent:
    # Runtime, steering and cancellation can all emit against the same Run at
    # once.  The sequence is scoped to a Run, so serialize its max+1 write in
    # this single-process personal server instead of letting a harmless race
    # fail the whole Run with a unique-constraint error.
    # A few call sites intentionally use emit_event() as the commit boundary
    # for an already-mutated ORM object. Such a session may already own the
    # SQLite writer lock, so it must not wait behind a clean event-only writer.
    # Child/runtime event sessions are clean and take the cross-run lock.
    has_pending_orm_writes = bool(db.new or db.dirty or db.deleted)
    lock = (
        _sqlite_event_write_lock()
        if settings.DATABASE_URL.startswith("sqlite") and not has_pending_orm_writes
        else _run_event_lock(run_id)
    )
    async with lock:
        event = None
        for attempt in range(4):
            try:
                result = await db.execute(
                    select(func.coalesce(func.max(RunEvent.sequence), 0)).where(RunEvent.run_id == run_id)
                )
                event = RunEvent(
                    run_id=run_id,
                    sequence=int(result.scalar_one()) + 1,
                    event_type=event_type,
                    summary=summary,
                    payload=payload or {},
                )
                db.add(event)
                await db.commit()
                await db.refresh(event)
                break
            except OperationalError as exc:
                await db.rollback()
                transient = any(marker in str(exc).lower() for marker in ("locked", "busy"))
                if not transient or attempt == 3:
                    raise
                await asyncio.sleep(0.05 * (2 ** attempt))
        if event is None:  # pragma: no cover - defensive; loop either succeeds or raises
            raise RuntimeError("Run event was not persisted")
    publish_stream_event(run_id, {
        "sequence": event.sequence,
        "type": event.event_type,
        "summary": event.summary,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    })
    return event
