import asyncio
from datetime import datetime, time, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Notification, UserProfile
from app.notifications.diagnostics import smtp_connection


class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def send(
        self,
        *,
        owner_id: str,
        run_id: str,
        session_id: str | None,
        trigger: str,
        title: str,
        body: str,
        plan_id: int | None,
        channels: list[str],
    ) -> dict:
        allowed, reason = await self._guard(owner_id, trigger, plan_id)
        if not allowed:
            return {"blocked": True, "reason": reason, "notifications": []}

        created: list[dict] = []
        requested = list(dict.fromkeys(["in_app", *channels]))
        for channel in requested:
            notification = Notification(
                owner_id=owner_id,
                run_id=run_id,
                session_id=session_id,
                plan_id=plan_id,
                channel=channel,
                title=title,
                body=body,
                status="queued",
            )
            self.db.add(notification)
            await self.db.flush()

            if channel in {"in_app", "browser"}:
                notification.status = "sent"
                notification.sent_at = datetime.now(timezone.utc)
            elif channel == "email":
                if not self._email_configured():
                    notification.status = "skipped"
                else:
                    try:
                        await asyncio.to_thread(self._send_email, notification.reply_token, title, body)
                        notification.status = "sent"
                        notification.sent_at = datetime.now(timezone.utc)
                    except Exception as exc:
                        notification.status = "failed"
                        body_preview = str(exc)[:200]
                        created.append({"id": notification.id, "channel": channel, "status": "failed", "error": body_preview})
                        continue
            else:
                notification.status = "skipped"
            created.append({"id": notification.id, "channel": channel, "status": notification.status})
        await self.db.commit()
        return {"blocked": False, "notifications": created}

    async def _guard(self, owner_id: str, trigger: str, plan_id: int | None) -> tuple[bool, str]:
        if trigger in {"user_message", "manual_heartbeat"}:
            return True, "user initiated"

        profile = await self.db.get(UserProfile, owner_id)
        timezone_name = settings.DEFAULT_TIMEZONE
        now_local = datetime.now(ZoneInfo(timezone_name))
        quiet_hours = profile.quiet_hours if profile else {"start": "23:00", "end": "08:00"}
        if _within_quiet_hours(now_local.time(), quiet_hours):
            return False, "quiet hours"

        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_midnight = local_midnight.astimezone(timezone.utc)
        result = await self.db.execute(
            select(func.count(Notification.id)).where(
                Notification.owner_id == owner_id,
                Notification.channel == "in_app",
                Notification.sent_at >= utc_midnight,
            )
        )
        daily_limit = profile.daily_notification_limit if profile else settings.AGENT_DAILY_NOTIFICATION_LIMIT
        if int(result.scalar_one()) >= daily_limit:
            return False, "daily notification limit"

        cooldown_start = datetime.now(timezone.utc).timestamp() - settings.AGENT_NOTIFICATION_COOLDOWN_MINUTES * 60
        cooldown_dt = datetime.fromtimestamp(cooldown_start, tz=timezone.utc)
        cooldown_query = select(Notification.id).where(
            Notification.owner_id == owner_id,
            Notification.channel == "in_app",
            Notification.sent_at >= cooldown_dt,
        )
        if plan_id is not None:
            cooldown_query = cooldown_query.where(Notification.plan_id == plan_id)
        if (await self.db.execute(cooldown_query.limit(1))).scalar_one_or_none() is not None:
            return False, "notification cooldown"
        return True, "allowed"

    def _email_configured(self) -> bool:
        return bool(settings.SMTP_HOST and settings.SMTP_USERNAME and settings.SMTP_PASSWORD and settings.SMTP_TO)

    def _send_email(self, reply_token: str, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"[Learning Agent][LA:{reply_token}] {title}"
        message["From"] = settings.SMTP_FROM or settings.SMTP_USERNAME
        message["To"] = settings.SMTP_TO
        message["Reply-To"] = settings.SMTP_FROM or settings.SMTP_USERNAME
        message["X-Learning-Agent-Reply-Token"] = reply_token
        message.set_content(f"{body}\n\n直接回复此邮件即可继续与 Learning Agent 沟通。\nReply token: {reply_token}")

        with smtp_connection() as server:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(message)


def _within_quiet_hours(current: time, quiet_hours: dict) -> bool:
    try:
        start = time.fromisoformat(str(quiet_hours.get("start", "23:00")))
        end = time.fromisoformat(str(quiet_hours.get("end", "08:00")))
    except ValueError:
        return False
    if start <= end:
        return start <= current < end
    return current >= start or current < end
