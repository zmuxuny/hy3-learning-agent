from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models import Notification
from app.schemas import NotificationRead


router = APIRouter()


@router.get("", response_model=list[NotificationRead])
async def read_notifications(unread_only: bool = False, db: AsyncSession = Depends(get_db)):
    query = select(Notification).where(Notification.owner_id == settings.DEFAULT_OWNER_ID)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    result = await db.execute(query.order_by(Notification.created_at.desc()).limit(100))
    return list(result.scalars())


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_notification_read(notification_id: int, db: AsyncSession = Depends(get_db)):
    notification = await db.get(Notification, notification_id)
    if not notification or notification.owner_id != settings.DEFAULT_OWNER_ID:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(notification)
    return notification
