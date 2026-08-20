from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.db.uow import ensure_sqlite_write_transaction, flush as flush_uow
from app.models import AgentRun, ChatMessage, Plan, QueuedMessage, Session
from app.runtime.session_titles import initial_session_title
from app.runtime.state import RunStateError, ensure_root_scope_available


class QueueStateError(RuntimeError):
    """A queue mutation lost its optimistic-concurrency precondition."""


def _queue_scope(owner_id: str, session_id: str | None):
    return (
        QueuedMessage.owner_id == owner_id,
        (
            QueuedMessage.session_id == session_id
            if session_id is not None
            else QueuedMessage.session_id.is_(None)
        ),
    )


async def compact_queue_after_removal(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str | None,
    deleted_position: int,
) -> None:
    """Close one queue gap without transiently violating scoped uniqueness."""

    scope = _queue_scope(owner_id, session_id)
    max_position = await db.scalar(select(func.max(QueuedMessage.position)).where(*scope))
    if max_position is None or int(max_position) <= deleted_position:
        return
    offset = int(max_position) + 1
    # SQLite UNIQUE constraints are immediate. Move affected rows above the
    # live range first, then compact them into the durable gap.
    await db.execute(
        update(QueuedMessage)
        .where(*scope, QueuedMessage.position > deleted_position)
        .values(
            position=QueuedMessage.position + offset,
            version=QueuedMessage.version + 1,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session="fetch")
    )
    await db.execute(
        update(QueuedMessage)
        .where(*scope, QueuedMessage.position > deleted_position + offset)
        .values(position=QueuedMessage.position - offset - 1)
        .execution_options(synchronize_session="fetch")
    )
    list((await db.execute(
        select(QueuedMessage)
        .where(*scope)
        .execution_options(populate_existing=True)
    )).scalars())


async def reorder_queue(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str | None,
    ordered: list[QueuedMessage],
) -> None:
    """Replace one scoped ordering through a collision-free two-phase move."""

    if not ordered:
        return
    ids = [item.id for item in ordered]
    offset = max(int(item.position) for item in ordered) + len(ordered) + 1
    now = utc_now()
    scope = _queue_scope(owner_id, session_id)
    await db.execute(
        update(QueuedMessage)
        .where(*scope, QueuedMessage.id.in_(ids))
        .values(
            position=QueuedMessage.position + offset,
            version=QueuedMessage.version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session="fetch")
    )
    for position, message in enumerate(ordered):
        await db.execute(
            update(QueuedMessage)
            .where(*scope, QueuedMessage.id == message.id)
            .values(position=position)
            .execution_options(synchronize_session="fetch")
        )
    list((await db.execute(
        select(QueuedMessage)
        .where(*scope)
        .execution_options(populate_existing=True)
    )).scalars())


async def dispatch_queued_message(
    db: AsyncSession,
    message: QueuedMessage,
    *,
    owner_id: str,
    expected_version: int | None = None,
) -> AgentRun:
    """Turn one durable queue item into the next durable Session turn."""
    await ensure_sqlite_write_transaction(db)
    if message.owner_id != owner_id:
        raise ValueError("Queued message not found")
    if expected_version is not None and message.version != expected_version:
        raise QueueStateError("queued_message_version_conflict")

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

    source_message = None
    if message.source_message_id is not None:
        source_message = await db.get(ChatMessage, message.source_message_id)
        if (
            source_message is None
            or message.session_id is None
            or source_message.session_id != message.session_id
        ):
            raise ValueError("Queued source message no longer exists")

    if session is None:
        session = Session(
            owner_id=owner_id,
            plan_id=message.plan_id,
            title=initial_session_title(message.user_content or message.objective),
        )
        db.add(session)
        await flush_uow(db)

    await ensure_root_scope_available(
        db,
        owner_id=owner_id,
        plan_id=message.plan_id,
        session_id=session.id,
    )

    run = AgentRun(
        owner_id=owner_id,
        session_id=session.id,
        plan_id=message.plan_id,
        trigger=message.trigger,
        objective=message.objective,
    )
    db.add(run)
    await flush_uow(db)
    if source_message is not None:
        source_message.run_id = run.id
        source_message.message_key = f"run:{run.id}:input"
    else:
        db.add(ChatMessage(
            session_id=session.id,
            run_id=run.id,
            message_key=f"run:{run.id}:input",
            role="user",
            content=message.user_content or message.objective,
            message_metadata=message.message_metadata or {},
        ))
    session.updated_at = datetime.now(timezone.utc)
    deleted_position = message.position
    deleted_session_id = message.session_id
    await db.delete(message)
    await flush_uow(db)
    await compact_queue_after_removal(
        db,
        owner_id=owner_id,
        session_id=deleted_session_id,
        deleted_position=deleted_position,
    )
    # The caller owns the Unit of Work and starts the runtime only after that
    # commit succeeds.  Keep queue removal, position compaction, user message,
    # and queued Run creation in the same staged transaction.
    await flush_uow(db)
    return run


async def dispatch_next_queued_message(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str,
) -> AgentRun | None:
    await ensure_sqlite_write_transaction(db)
    message = (await db.execute(
        select(QueuedMessage).where(
            QueuedMessage.owner_id == owner_id,
            QueuedMessage.session_id == session_id,
        ).order_by(QueuedMessage.position, QueuedMessage.created_at).limit(1)
    )).scalars().one_or_none()
    if message is None:
        return None
    try:
        # A malformed legacy queue item must not abort the current Run's
        # terminal commit. The SAVEPOINT guarantees that even a late domain
        # validation error cannot leak a partially staged successor.
        async with db.begin_nested():
            return await dispatch_queued_message(db, message, owner_id=owner_id)
    except (QueueStateError, RunStateError, ValueError):
        return None
