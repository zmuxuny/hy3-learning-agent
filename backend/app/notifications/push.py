from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import PushSubscription


class PushService:
    """Optional Web Push delivery via VAPID. Without keys it safely no-ops."""

    @property
    def configured(self) -> bool:
        return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY and settings.VAPID_SUBJECT)

    async def subscribe(self, db: AsyncSession, owner_id: str, endpoint: str, keys: dict[str, str]) -> PushSubscription:
        existing = (await db.execute(
            select(PushSubscription).where(PushSubscription.owner_id == owner_id, PushSubscription.endpoint == endpoint)
        )).scalars().one_or_none()
        if existing is None:
            existing = PushSubscription(owner_id=owner_id, endpoint=endpoint, keys=keys)
            db.add(existing)
        else:
            existing.keys = keys
            existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        return existing

    async def unsubscribe(self, db: AsyncSession, owner_id: str, endpoint: str) -> bool:
        result = await db.execute(
            delete(PushSubscription).where(
                PushSubscription.owner_id == owner_id,
                PushSubscription.endpoint == endpoint,
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    async def send(self, db: AsyncSession, owner_id: str, title: str, body: str, data: dict[str, Any] | None = None) -> list[PushSubscription]:
        if not self.configured:
            return []
        subscriptions = list((await db.execute(
            select(PushSubscription).where(PushSubscription.owner_id == owner_id)
        )).scalars())
        if not subscriptions:
            return []
        payload = json.dumps({"title": title, "body": body, **(data or {})}, ensure_ascii=False)
        delivered: list[PushSubscription] = []
        for subscription in subscriptions:
            try:
                await asyncio.to_thread(self._send_one, subscription, payload)
                delivered.append(subscription)
            except Exception as exc:
                if getattr(exc, "response", None) is not None and exc.response.status_code in {404, 410}:
                    await db.delete(subscription)
        if delivered:
            await db.commit()
        return delivered

    def _send_one(self, subscription: PushSubscription, payload: str) -> None:
        from pywebpush import WebPusher

        subscription_info = {
            "endpoint": subscription.endpoint,
            "keys": subscription.keys,
        }
        claims = {
            "sub": settings.VAPID_SUBJECT,
            "aud": subscription.endpoint,
        }
        pusher = WebPusher(subscription_info, data=payload, vapid_private_key=settings.VAPID_PRIVATE_KEY, vapid_claims=claims)
        pusher.send()


push_service = PushService()
