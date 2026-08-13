import asyncio
from datetime import datetime, time, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Notification, Plan, UserProfile
from app.notifications.conversation import materialize_notification_message, resolve_notification_session
from app.notifications.diagnostics import smtp_connection
from app.notifications.push import push_service


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
            return {"blocked": True, "reason": reason, "session_id": None, "notifications": []}
        if plan_id is not None:
            plan = await self.db.get(Plan, plan_id)
            if (
                not plan
                or plan.owner_id != owner_id
                or plan.status == "archived"
                or (trigger not in {"user_message", "email_reply"} and plan.status != "active")
            ):
                return {
                    "blocked": True,
                    "reason": "plan no longer active",
                    "session_id": None,
                    "notifications": [],
                }

        session = await resolve_notification_session(
            self.db,
            owner_id=owner_id,
            session_id=session_id,
            plan_id=plan_id,
            source_run_id=run_id,
        )

        created: list[dict] = []
        created_models: list[Notification] = []
        requested = list(dict.fromkeys(["in_app", *channels]))
        for channel in requested:
            notification = Notification(
                owner_id=owner_id,
                run_id=run_id,
                session_id=session.id,
                plan_id=plan_id,
                channel=channel,
                title=title,
                body=body,
                status="queued",
            )
            self.db.add(notification)
            await self.db.flush()
            created_models.append(notification)

            if channel == "in_app":
                notification.status = "sent"
                notification.sent_at = datetime.now(timezone.utc)
            elif channel == "browser":
                delivered = await push_service.send(
                    self.db,
                    owner_id,
                    title,
                    body,
                    {
                        "notification_id": notification.id,
                        "url": f"/?notification={created_models[0].id}",
                    },
                )
                # `sent` means the open page should display the notification;
                # `pushed` means the Service Worker already delivered it.
                notification.status = "pushed" if delivered else "sent"
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
        if created_models and trigger not in {"user_message", "email_reply"}:
            primary = next((item for item in created_models if item.channel == "in_app"), created_models[0])
            await materialize_notification_message(
                self.db,
                session=session,
                notification=primary,
                notification_ids=[item.id for item in created_models],
            )
        await self.db.commit()
        return {"blocked": False, "session_id": session.id, "notifications": created}

    async def _guard(self, owner_id: str, trigger: str, plan_id: int | None) -> tuple[bool, str]:
        if trigger in {"user_message", "email_reply", "manual_heartbeat"}:
            return True, "user initiated"

        profile = await self.db.get(UserProfile, owner_id)
        timezone_name = settings.DEFAULT_TIMEZONE
        now_local = datetime.now(ZoneInfo(timezone_name))
        quiet_hours = profile.quiet_hours if profile else {"start": "23:00", "end": "08:00"}
        if _within_quiet_hours(now_local.time(), quiet_hours):
            return False, "quiet hours"

        local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_midnight = local_midnight.astimezone(timezone.utc)
        # One intervention can have both in-app and email delivery rows. Count
        # that intervention once rather than consuming the daily limit twice.
        logical_delivery = func.coalesce(
            Notification.run_id + "\x00" + Notification.title + "\x00" + Notification.body,
            cast(Notification.id, String),
        )
        result = await self.db.execute(
            select(func.count(func.distinct(logical_delivery))).where(
                Notification.owner_id == owner_id,
                Notification.channel.in_(["in_app", "email"]),
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
            Notification.channel.in_(["in_app", "email"]),
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
