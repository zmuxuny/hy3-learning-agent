from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.uow import flush as flush_uow
from app.models import AgentRun, ChatMessage, Notification, Plan, Session
from app.services.sessions import link_session_plan


def notification_thread_key(*, run_id: str | None, plan_id: int | None, title: str, body: str) -> str:
    raw = f"{run_id or '-'}\0{plan_id or '-'}\0{title.strip()}\0{body.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


async def resolve_notification_session(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str | None,
    plan_id: int | None,
    source_run_id: str | None,
) -> Session:
    """Return the continuous conversation that owns a proactive notification.

    A supplied active Session always wins. Background reminders otherwise reuse the
    most recent active Session with the same plan focus, so repeated heartbeats do
    not create one conversation per reminder.
    """
    if session_id:
        supplied = await db.get(Session, session_id)
        if supplied and supplied.owner_id == owner_id and supplied.archived_at is None:
            return supplied

    query = select(Session).where(
        Session.owner_id == owner_id,
        Session.archived_at.is_(None),
    )
    query = query.where(Session.plan_id == plan_id) if plan_id is not None else query.where(Session.plan_id.is_(None))
    session = (await db.execute(query.order_by(Session.updated_at.desc()).limit(1))).scalars().one_or_none()
    if session:
        return session

    plan = await db.get(Plan, plan_id) if plan_id is not None else None
    title = f"{plan.title} · 学习跟进" if plan else "学习跟进"
    session = Session(
        owner_id=owner_id,
        plan_id=plan_id,
        title=title[:80],
        summary="Agent 主动提醒与学习者后续回复所在的连续对话。",
    )
    db.add(session)
    await flush_uow(db)
    if plan_id is not None:
        await link_session_plan(
            db,
            owner_id=owner_id,
            session_id=session.id,
            plan_id=plan_id,
            relation_type="focused",
            source_run_id=source_run_id,
        )
    return session


async def materialize_notification_message(
    db: AsyncSession,
    *,
    session: Session,
    notification: Notification,
    notification_ids: list[int] | None = None,
) -> ChatMessage:
    """Project one notification delivery group into the canonical conversation."""
    thread_key = notification_thread_key(
        run_id=notification.run_id,
        plan_id=notification.plan_id,
        title=notification.title,
        body=notification.body,
    )
    messages = list((await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )).scalars())
    for message in messages:
        metadata = message.message_metadata or {}
        if metadata.get("notification_thread_key") == thread_key:
            return message
        if notification.id in (metadata.get("notification_ids") or []):
            return message

    # A notification emitted during a user turn already has a visible final answer.
    # Reuse it as the reply target instead of duplicating another assistant message.
    source_run = await db.get(AgentRun, notification.run_id) if notification.run_id else None
    if source_run and source_run.trigger == "user_message":
        existing = next((message for message in messages if message.run_id == source_run.id), None)
        if existing:
            existing.message_metadata = {
                **(existing.message_metadata or {}),
                "notification_thread_key": thread_key,
                "notification_id": notification.id,
                "notification_ids": notification_ids or [notification.id],
            }
            return existing

    message = ChatMessage(
        session_id=session.id,
        run_id=notification.run_id,
        role="assistant",
        content=notification.body,
        message_metadata={
            "ui_kind": "proactive_notification",
            "notification_id": notification.id,
            "notification_ids": notification_ids or [notification.id],
            "notification_thread_key": thread_key,
            "notification_title": notification.title,
            "plan_id": notification.plan_id,
            "channel": notification.channel,
        },
    )
    db.add(message)
    session.updated_at = datetime.now(timezone.utc)
    await flush_uow(db)
    return message


async def open_notification_in_conversation(
    db: AsyncSession,
    notification: Notification,
) -> tuple[Session, ChatMessage, Notification]:
    """Repair old rows if needed and return the exact conversation/message target."""
    session = await resolve_notification_session(
        db,
        owner_id=notification.owner_id,
        session_id=notification.session_id,
        plan_id=notification.plan_id,
        source_run_id=notification.run_id,
    )
    group_query = select(Notification).where(
        Notification.owner_id == notification.owner_id,
        Notification.run_id == notification.run_id,
        Notification.plan_id == notification.plan_id,
        Notification.title == notification.title,
        Notification.body == notification.body,
    )
    group = list((await db.execute(group_query.order_by(Notification.id))).scalars())
    if not group:
        group = [notification]
    opened_at = datetime.now(timezone.utc)
    for sibling in group:
        sibling.session_id = session.id
        sibling.read_at = sibling.read_at or opened_at
    primary = next((item for item in group if item.channel == "in_app"), notification)
    message = await materialize_notification_message(
        db,
        session=session,
        notification=primary,
        notification_ids=[item.id for item in group],
    )
    return session, message, primary
