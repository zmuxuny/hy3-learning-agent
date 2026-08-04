import asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RunEvent


_subscribers: dict[str, set[asyncio.Queue]] = {}


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
    result = await db.execute(select(func.coalesce(func.max(RunEvent.sequence), 0)).where(RunEvent.run_id == run_id))
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
    publish_stream_event(run_id, {
        "sequence": event.sequence,
        "type": event.event_type,
        "summary": event.summary,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    })
    return event
