from __future__ import annotations

import hashlib

from app.core.time import utc_now
from app.db.uow import flush as flush_uow
from app.models import ChatMessage, Intervention, Notification, Plan, Session
from app.services.sessions import link_session_plan
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


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
    """Project a delivery into its canonical conversation message.

    New rows are keyed exclusively by ``Intervention.canonical_message_id``.
    Legacy unlinked rows are repaired by their immutable Notification id; mutable
    title/body/run fields are deliberately not used as identity.
    """
    if notification.intervention_id is not None:
        intervention = await db.get(Intervention, notification.intervention_id)
        if intervention is None or intervention.owner_id != notification.owner_id:
            raise ValueError("notification intervention is missing or outside owner scope")
        return await materialize_intervention_message(
            db,
            session=session,
            intervention=intervention,
            notification_ids=notification_ids,
        )

    legacy_key = f"legacy-notification:{notification.id}"
    messages = list((await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )).scalars())
    for message in messages:
        metadata = message.message_metadata or {}
        if notification.id in (metadata.get("notification_ids") or []):
            return message
        if metadata.get("legacy_notification_key") == legacy_key:
            return message

    message = ChatMessage(
        session_id=session.id,
        run_id=notification.run_id,
        role="assistant",
        content=notification.body,
        version=1,
        content_hash=hashlib.sha256(notification.body.encode("utf-8")).hexdigest(),
        message_metadata={
            "ui_kind": "proactive_notification",
            "notification_id": notification.id,
            "notification_ids": notification_ids or [notification.id],
            "legacy_notification_key": legacy_key,
            "notification_title": notification.title,
            "plan_id": notification.plan_id,
            "channel": notification.channel,
        },
    )
    db.add(message)
    session.updated_at = utc_now()
    await flush_uow(db)
    return message


async def materialize_intervention_message(
    db: AsyncSession,
    *,
    session: Session,
    intervention: Intervention,
    notification_ids: list[int] | None = None,
) -> ChatMessage:
    """Materialize exactly one canonical message for an Intervention."""
    if intervention.owner_id != session.owner_id or intervention.session_id != session.id:
        raise ValueError("intervention and conversation session scope do not match")

    if intervention.canonical_message_id is not None:
        message = await db.get(ChatMessage, intervention.canonical_message_id)
        if message is None or message.session_id != session.id:
            raise ValueError("intervention canonical message is missing or outside session scope")
        if notification_ids:
            existing_ids = list((message.message_metadata or {}).get("notification_ids") or [])
            message.message_metadata = {
                **(message.message_metadata or {}),
                "intervention_id": intervention.id,
                "notification_ids": list(dict.fromkeys([*existing_ids, *notification_ids])),
            }
        return message

    message = ChatMessage(
        session_id=session.id,
        run_id=intervention.source_run_id,
        role="assistant",
        content=intervention.body,
        version=1,
        content_hash=hashlib.sha256(intervention.body.encode("utf-8")).hexdigest(),
        message_metadata={
            "ui_kind": "proactive_notification",
            "intervention_id": intervention.id,
            "notification_ids": notification_ids or [],
            "notification_title": intervention.title,
            "plan_id": intervention.plan_id,
        },
    )
    db.add(message)
    session.updated_at = utc_now()
    await flush_uow(db)
    intervention.canonical_message_id = message.id
    intervention.state = "active"
    return message


async def open_notification_in_conversation(
    db: AsyncSession,
    notification: Notification,
) -> tuple[Session, ChatMessage, Notification]:
    """Repair old rows if needed and return the exact conversation/message target."""
    intervention = None
    if notification.intervention_id is not None:
        intervention = await db.get(Intervention, notification.intervention_id)
        if intervention is None or intervention.owner_id != notification.owner_id:
            raise ValueError("notification intervention is missing or outside owner scope")
        group = list(
            (
                await db.execute(
                    select(Notification)
                    .where(Notification.intervention_id == intervention.id)
                    .order_by(Notification.id)
                )
            ).scalars()
        )
        session_id = intervention.session_id
        plan_id = intervention.plan_id
        source_run_id = intervention.source_run_id
    else:
        # There is no sound way to infer a logical group for revision-4 rows.
        # Treat each delivery as a separate legacy intervention rather than
        # merging unrelated reminders whose mutable text happens to match.
        group = [notification]
        session_id = notification.session_id
        plan_id = notification.plan_id
        source_run_id = notification.run_id

    session = await resolve_notification_session(
        db,
        owner_id=notification.owner_id,
        session_id=session_id,
        plan_id=plan_id,
        source_run_id=source_run_id,
    )
    opened_at = utc_now()
    for sibling in group:
        sibling.session_id = session.id
        sibling.read_at = sibling.read_at or opened_at
    if intervention is not None:
        intervention.read_at = intervention.read_at or opened_at
    primary = next((item for item in group if item.channel == "in_app"), notification)
    if intervention is not None:
        message = await materialize_intervention_message(
            db,
            session=session,
            intervention=intervention,
            notification_ids=[item.id for item in group],
        )
    else:
        message = await materialize_notification_message(
            db,
            session=session,
            notification=primary,
            notification_ids=[item.id for item in group],
        )
    return session, message, primary
