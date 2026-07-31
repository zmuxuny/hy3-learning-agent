from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RunEvent


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
    return event
