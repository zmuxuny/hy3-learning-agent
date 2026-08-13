from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, ChatMessage, Plan, QueuedMessage, Session
from app.runtime.session_titles import initial_session_title


ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_approval")


async def dispatch_queued_message(
    db: AsyncSession,
    message: QueuedMessage,
    *,
    owner_id: str,
) -> AgentRun:
    """Turn one durable queue item into the next durable Session turn."""
    if message.owner_id != owner_id:
        raise ValueError("Queued message not found")

    session = await db.get(Session, message.session_id) if message.session_id else None
    if message.session_id and (not session or session.owner_id != owner_id):
        raise ValueError("Queued Session no longer exists")
    if session and session.archived_at is not None:
        raise ValueError("Restore the Session before sending this message")
    if session and session.plan_id != message.plan_id:
        raise ValueError("Queued message no longer matches the Session focus")

    if message.plan_id is not None:
        plan = await db.get(Plan, message.plan_id)
        if not plan or plan.owner_id != owner_id or plan.status == "archived":
            raise ValueError("Queued plan is unavailable or archived")

    if session:
        active_run = (await db.execute(
            select(AgentRun.id).where(
                AgentRun.owner_id == owner_id,
                AgentRun.session_id == session.id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            ).limit(1)
        )).scalar_one_or_none()
        if active_run:
            raise RuntimeError("This Session already has an active run")
    else:
        session = Session(
            owner_id=owner_id,
            plan_id=message.plan_id,
            title=initial_session_title(message.user_content or message.objective),
        )
        db.add(session)
        await db.flush()

    run = AgentRun(
        owner_id=owner_id,
        session_id=session.id,
        plan_id=message.plan_id,
        trigger=message.trigger,
        objective=message.objective,
    )
    db.add(run)
    await db.flush()
    db.add(ChatMessage(
        session_id=session.id,
        run_id=run.id,
        role="user",
        content=message.user_content or message.objective,
        message_metadata=message.message_metadata or {},
    ))
    session.updated_at = datetime.now(timezone.utc)
    deleted_position = message.position
    deleted_session_id = message.session_id
    await db.delete(message)
    await db.execute(
        QueuedMessage.__table__.update()
        .where(
            QueuedMessage.owner_id == owner_id,
            (
                QueuedMessage.session_id == deleted_session_id
                if deleted_session_id is not None
                else QueuedMessage.session_id.is_(None)
            ),
            QueuedMessage.position > deleted_position,
        )
        .values(position=QueuedMessage.position - 1)
    )
    await db.commit()
    await db.refresh(run)
    return run


async def dispatch_next_queued_message(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str,
) -> AgentRun | None:
    message = (await db.execute(
        select(QueuedMessage).where(
            QueuedMessage.owner_id == owner_id,
            QueuedMessage.session_id == session_id,
        ).order_by(QueuedMessage.position, QueuedMessage.created_at).limit(1)
    )).scalars().one_or_none()
    if message is None:
        return None
    try:
        return await dispatch_queued_message(db, message, owner_id=owner_id)
    except (RuntimeError, ValueError):
        await db.rollback()
        return None
