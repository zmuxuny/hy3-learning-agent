"""The learner clock shared by model context and notification policy."""

from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.time import canonical_utc, utc_now
from app.models import Owner
from sqlalchemy.ext.asyncio import AsyncSession


async def learner_timezone(db: AsyncSession, owner_id: str) -> str:
    owner = await db.get(Owner, owner_id)
    return owner.timezone if owner else settings.DEFAULT_TIMEZONE


async def learner_clock(db: AsyncSession, owner_id: str) -> dict[str, str]:
    timezone_name = await learner_timezone(db, owner_id)
    now = utc_now()
    return {
        "timezone": timezone_name,
        "now_utc": canonical_utc(now),
        "now_local": now.astimezone(ZoneInfo(timezone_name)).isoformat(),
    }
