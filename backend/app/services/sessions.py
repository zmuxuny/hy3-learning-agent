from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage, Session, SessionPlanLink


async def link_session_plan(
    db: AsyncSession,
    *,
    owner_id: str,
    session_id: str,
    plan_id: int,
    relation_type: str,
    source_run_id: str | None = None,
) -> SessionPlanLink:
    link = (await db.execute(
        select(SessionPlanLink).where(
            SessionPlanLink.session_id == session_id,
            SessionPlanLink.plan_id == plan_id,
            SessionPlanLink.relation_type == relation_type,
        )
    )).scalars().one_or_none()
    if link is None:
        link = SessionPlanLink(
            owner_id=owner_id,
            session_id=session_id,
            plan_id=plan_id,
            relation_type=relation_type,
            source_run_id=source_run_id,
        )
        db.add(link)
        await db.flush()
    elif source_run_id and link.source_run_id is None:
        link.source_run_id = source_run_id
    return link


async def build_handoff_summary(db: AsyncSession, session: Session) -> str:
    messages = list((await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )).scalars())
    messages = [
        message for message in messages
        if not message.message_metadata.get("superseded_by_edit")
    ][:6]
    excerpts = [
        f"{message.role}: {' '.join(message.content.split())[:360]}"
        for message in reversed(messages)
    ]
    parts = [
        f"由对话《{session.title}》转入。",
        session.summary.strip(),
        "最近上下文：\n" + "\n".join(excerpts) if excerpts else "",
    ]
    return "\n".join(part for part in parts if part)[:5000]
