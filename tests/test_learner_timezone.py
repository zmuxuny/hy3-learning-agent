from datetime import datetime, timezone

import pytest
from app.core.learner_time import learner_clock
from app.core.time import frozen_utc
from app.db.database import AsyncSessionLocal
from app.models import Owner, UserProfile
from app.notifications.service import NotificationService


@pytest.mark.asyncio
@pytest.mark.parametrize("zone,local_hour,allowed", [
    ("Asia/Shanghai", "15:10", True),
    ("UTC", "07:10", False),
    ("America/Los_Angeles", "00:10", False),
])
async def test_model_clock_and_guard_share_owner_timezone(zone, local_hour, allowed):
    with frozen_utc(datetime(2026, 9, 8, 7, 10, tzinfo=timezone.utc)):
        async with AsyncSessionLocal() as db:
            owner = await db.get(Owner, "local")
            owner.timezone = zone
            profile = await db.get(UserProfile, "local")
            profile.quiet_hours = {"start": "23:00", "end": "08:00"}
            profile.daily_notification_limit = 3
            await db.commit()
            facts = await learner_clock(db, "local")
            assert facts["timezone"] == zone
            assert f"T{local_hour}:" in facts["now_local"]
            assert "T07:10:" in facts["now_utc"]
            result, reason = await NotificationService(db)._guard("local", "heartbeat", None)
            assert result is allowed, reason


@pytest.mark.asyncio
async def test_local_date_rolls_over_without_changing_utc_instant():
    with frozen_utc(datetime(2026, 9, 8, 23, 30, tzinfo=timezone.utc)):
        async with AsyncSessionLocal() as db:
            facts = await learner_clock(db, "local")
            assert facts["now_utc"].startswith("2026-09-08T23:30")
            assert facts["now_local"].startswith("2026-09-09T07:30")


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["Asia/Shanghai", "America/Los_Angeles"])
async def test_settings_timezone_matches_owner_and_model_clock(zone):
    from app.api.settings import read_settings

    async with AsyncSessionLocal() as db:
        owner = await db.get(Owner, "local")
        owner.timezone = zone
        await db.commit()
        payload = await read_settings(db)
        clock = await learner_clock(db, "local")
        assert payload["timezone"] == clock["timezone"] == zone
