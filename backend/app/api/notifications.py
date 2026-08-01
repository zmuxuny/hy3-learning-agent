from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models import Notification
from app.schemas import NotificationArchiveResult, NotificationArchiveUpdate, NotificationRead


router = APIRouter()


@router.get("", response_model=list[NotificationRead])
async def read_notifications(
    unread_only: bool = False,
    archived: bool = False,
    db: AsyncSession = Depends(get_db),
):
    query = select(Notification).where(Notification.owner_id == settings.DEFAULT_OWNER_ID)
    archive_filter = Notification.archived_at.is_not(None) if archived else Notification.archived_at.is_(None)
    query = query.where(archive_filter)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    result = await db.execute(query.order_by(Notification.created_at.desc()).limit(100))
    return list(result.scalars())


@router.post("/archive-read", response_model=NotificationArchiveResult)
async def archive_read_notifications(db: AsyncSession = Depends(get_db)):
    archived_at = datetime.now(timezone.utc)
    result = await db.execute(
        update(Notification)
        .where(
            Notification.owner_id == settings.DEFAULT_OWNER_ID,
            Notification.archived_at.is_(None),
            Notification.read_at.is_not(None),
        )
        .values(archived_at=archived_at)
    )
    await db.commit()
    return {"archived": result.rowcount or 0, "archived_at": archived_at}


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_notification_read(notification_id: int, db: AsyncSession = Depends(get_db)):
    notification = await db.get(Notification, notification_id)
    if not notification or notification.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(notification)
    return notification


@router.patch("/{notification_id}/archive", response_model=NotificationRead)
async def set_notification_archived(
    notification_id: int,
    data: NotificationArchiveUpdate,
    db: AsyncSession = Depends(get_db),
):
    notification = await db.get(Notification, notification_id)
    if not notification or notification.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.archived_at = datetime.now(timezone.utc) if data.archived else None
    await db.commit()
    await db.refresh(notification)
    return notification
