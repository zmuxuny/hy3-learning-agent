import hashlib
import json
from datetime import datetime, time, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.uow import flush as flush_uow
from app.models import Notification, Plan, PushSubscription, UserProfile
from app.outbox import enqueue_action
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
        run_id: str | None,
        session_id: str | None,
        trigger: str,
        title: str,
        body: str,
        plan_id: int | None,
        channels: list[str],
        invocation_id: int | None = None,
        action_key: str | None = None,
        request_digest: str | None = None,
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
        queued_actions: list[str] = []
        requested = list(dict.fromkeys(["in_app", *channels]))
        for channel in requested:
            notification = Notification(
                owner_id=owner_id,
                run_id=run_id,
                invocation_id=invocation_id,
                session_id=session.id,
                plan_id=plan_id,
                channel=channel,
                title=title,
                body=body,
                status="queued",
            )
            self.db.add(notification)
            await flush_uow(self.db)
            created_models.append(notification)

            if channel == "in_app":
                notification.status = "sent"
                notification.sent_at = datetime.now(timezone.utc)
            elif channel == "browser":
                subscriptions = list(
                    (
                        await self.db.execute(
                            select(PushSubscription).where(
                                PushSubscription.owner_id == owner_id
                            )
                        )
                    ).scalars()
                )
                if not push_service.configured or not subscriptions:
                    notification.status = "skipped"
                else:
                    base_key, digest = self._delivery_identity(
                        notification,
                        action_key=action_key,
                        request_digest=request_digest,
                    )
                    for subscription in subscriptions:
                        outbox = await enqueue_action(
                            self.db,
                            owner_id=owner_id,
                            run_id=run_id,
                            invocation_id=invocation_id,
                            notification_id=notification.id,
                            action_key=f"{base_key}:webpush:{subscription.id}",
                            request_digest=digest,
                            destination="web_push",
                            payload={
                                "subscription_id": subscription.id,
                                "endpoint": subscription.endpoint,
                                "keys": dict(subscription.keys or {}),
                                "title": title,
                                "body": body,
                                "data": {
                                    "notification_id": notification.id,
                                    "url": f"/?notification={created_models[0].id}",
                                },
                            },
                        )
                        queued_actions.append(outbox.action_key)
            elif channel == "email":
                if not self._email_configured():
                    notification.status = "skipped"
                else:
                    base_key, digest = self._delivery_identity(
                        notification,
                        action_key=action_key,
                        request_digest=request_digest,
                    )
                    outbox = await enqueue_action(
                        self.db,
                        owner_id=owner_id,
                        run_id=run_id,
                        invocation_id=invocation_id,
                        notification_id=notification.id,
                        action_key=f"{base_key}:smtp",
                        request_digest=digest,
                        destination="smtp",
                        payload={
                            "reply_token": notification.reply_token,
                            "title": title,
                            "body": body,
                            "route_digest": self._smtp_route_digest(),
                        },
                    )
                    queued_actions.append(outbox.action_key)
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
        await flush_uow(self.db)
        return {
            "blocked": False,
            "session_id": session.id,
            "notifications": created,
            "outbox_action_keys": queued_actions,
            **({"_invocation_status": "pending_delivery"} if queued_actions else {}),
        }

    @staticmethod
    def _delivery_identity(
        notification: Notification,
        *,
        action_key: str | None,
        request_digest: str | None,
    ) -> tuple[str, str]:
        base_key = action_key or f"notification:{notification.id}"
        if request_digest:
            return base_key, request_digest
        canonical = json.dumps(
            {
                "owner_id": notification.owner_id,
                "run_id": notification.run_id,
                "channel": notification.channel,
                "title": notification.title,
                "body": notification.body,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return base_key, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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

        cooldown_minutes = settings.AGENT_NOTIFICATION_COOLDOWN_MINUTES
        if profile is not None:
            configured_cooldown = (profile.preferences or {}).get(
                "notification_cooldown_minutes"
            )
            if (
                isinstance(configured_cooldown, int)
                and not isinstance(configured_cooldown, bool)
                and 0 <= configured_cooldown <= 1440
            ):
                cooldown_minutes = configured_cooldown
        cooldown_start = datetime.now(timezone.utc).timestamp() - cooldown_minutes * 60
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

    @staticmethod
    def _smtp_route_digest() -> str:
        """Fingerprint non-secret routing identity without persisting credentials."""

        canonical = json.dumps(
            {
                "host": settings.SMTP_HOST,
                "port": settings.SMTP_PORT,
                "username": settings.SMTP_USERNAME,
                "from": settings.SMTP_FROM,
                "to": settings.SMTP_TO,
                "use_tls": settings.SMTP_USE_TLS,
                "use_ssl": settings.SMTP_USE_SSL,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _send_email(self, reply_token: str, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"[Learning Agent][LA:{reply_token}] {title}"
        message["From"] = settings.SMTP_FROM or settings.SMTP_USERNAME
        message["To"] = settings.SMTP_TO
        message["Reply-To"] = settings.SMTP_FROM or settings.SMTP_USERNAME
        # Stable diagnostic identity for provider support/reconciliation.  It
        # does not claim that SMTP providers deduplicate repeated submissions.
        message["Message-ID"] = f"<{reply_token}@learning-agent.local>"
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
