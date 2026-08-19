from __future__ import annotations

import asyncio
import email
import imaplib
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.uow import commit as commit_uow, flush as flush_uow
from app.models import AgentRun, ChatMessage, LearningEvent, Notification, QueuedMessage
from app.notifications.conversation import open_notification_in_conversation


class EmailReplyPoller:
    async def poll(self, db: AsyncSession, owner_id: str) -> list[str]:
        if not self.configured:
            return []
        if db.in_nested_transaction():
            raise RuntimeError("email polling cannot coordinate inside a SAVEPOINT")
        if db.new or db.dirty or db.deleted:
            raise RuntimeError(
                "email polling requires a clean session before an IMAP wait"
            )
        if db.in_transaction():
            await commit_uow(db)
        replies = await asyncio.to_thread(self._fetch_unseen)
        run_ids: list[str] = []
        handled_reply = False
        acknowledged_uids: list[str] = []
        for reply in replies:
            mailbox_uid = str(reply.get("uid") or "")
            email_uid = (
                f"{settings.IMAP_USERNAME}:{settings.IMAP_FOLDER}:{mailbox_uid}"
                if mailbox_uid else ""
            )
            if email_uid:
                existing = (await db.execute(
                    select(ChatMessage.id).where(
                        ChatMessage.message_metadata["email_uid"].as_string() == email_uid
                    ).limit(1)
                )).scalar_one_or_none()
                existing_queued = (await db.execute(
                    select(QueuedMessage.id).where(
                        QueuedMessage.message_metadata["email_uid"].as_string() == email_uid
                    ).limit(1)
                )).scalar_one_or_none()
                if existing or existing_queued:
                    acknowledged_uids.append(mailbox_uid)
                    continue
            token = reply.get("reply_token", "")
            if not token:
                continue
            notification = (await db.execute(
                select(Notification).where(Notification.owner_id == owner_id, Notification.reply_token == token)
            )).scalars().one_or_none()
            if not notification:
                continue
            handled_reply = True
            session, _, _ = await open_notification_in_conversation(db, notification)
            objective = (
                f"学习者回复了通知 {notification.id} 的邮件。"
                f"主题：{reply['subject']}\n回复内容：\n{reply['body']}\n"
                "检查当前状态并理解回复，只执行安全且相关的学习动作；"
                "处理完成后使用 notification_send 的 email 渠道把简洁回复发回邮箱。"
            )
            message_metadata = {
                "channel": "email",
                "notification_id": notification.id,
                "reply_to_notification_id": notification.id,
                **({"email_uid": email_uid} if email_uid else {}),
            }
            active_run = (await db.execute(
                select(AgentRun.id).where(
                    AgentRun.owner_id == owner_id,
                    AgentRun.session_id == session.id,
                    AgentRun.parent_run_id.is_(None),
                    AgentRun.status.in_(["queued", "running", "waiting_approval"]),
                ).limit(1)
            )).scalar_one_or_none()
            if active_run:
                max_position = await db.scalar(
                    select(func.coalesce(func.max(QueuedMessage.position), -1)).where(
                        QueuedMessage.owner_id == owner_id,
                        QueuedMessage.session_id == session.id,
                    )
                )
                db.add(QueuedMessage(
                    owner_id=owner_id,
                    plan_id=notification.plan_id,
                    session_id=session.id,
                    trigger="email_reply",
                    objective=objective,
                    user_content=reply["body"],
                    message_metadata=message_metadata,
                    position=int(max_position if max_position is not None else -1) + 1,
                ))
                if mailbox_uid:
                    acknowledged_uids.append(mailbox_uid)
                continue
            run = AgentRun(
                owner_id=owner_id,
                plan_id=notification.plan_id,
                session_id=session.id,
                trigger="email_reply",
                objective=objective,
                model=settings.MODEL_NAME,
            )
            db.add(run)
            await flush_uow(db)
            db.add(ChatMessage(
                session_id=session.id,
                run_id=run.id,
                role="user",
                content=reply["body"],
                message_metadata=message_metadata,
            ))
            db.add(LearningEvent(
                owner_id=owner_id, plan_id=notification.plan_id, run_id=run.id,
                event_type="email.reply.received", summary=f"Email reply to notification {notification.id}",
                payload={"notification_id": notification.id, "subject": reply["subject"]},
            ))
            session.updated_at = datetime.now(timezone.utc)
            run_ids.append(run.id)
            if mailbox_uid:
                acknowledged_uids.append(mailbox_uid)
        if handled_reply or (acknowledged_uids and db.in_transaction()):
            # Persist the reply/queue/run before the explicit IMAP Seen ACK.
            # The durable email_uid guard makes a later poll idempotent.  H5
            # separately upgrades the fetch itself from RFC822 to BODY.PEEK.
            # Exact duplicate probes may be read-only, but their snapshot must
            # also end before the external Seen mutation.
            await commit_uow(db)
        if acknowledged_uids:
            await asyncio.to_thread(self._mark_seen, acknowledged_uids)
        return run_ids

    @property
    def configured(self) -> bool:
        return bool(
            settings.ENABLE_EMAIL_REPLY_POLLING
            and settings.IMAP_HOST
            and settings.IMAP_USERNAME
            and settings.IMAP_PASSWORD
        )

    def _fetch_unseen(self) -> list[dict[str, str]]:
        client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=20)
        try:
            client.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
            client.select(settings.IMAP_FOLDER)
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                return []
            replies: list[dict[str, str]] = []
            for uid in data[0].split()[-20:]:
                status, message_data = client.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not message_data or not isinstance(message_data[0], tuple):
                    continue
                message = email.message_from_bytes(message_data[0][1])
                subject = str(make_header(decode_header(message.get("Subject", ""))))
                body = _plain_body(message)
                token = message.get("X-Learning-Agent-Reply-Token", "")
                if not token:
                    match = re.search(r"(?:LA:|Reply token:\s*)([0-9a-f-]{36})", f"{subject}\n{body}", re.IGNORECASE)
                    token = match.group(1) if match else ""
                if not token:
                    continue
                replies.append({
                    "uid": uid.decode("ascii", errors="ignore"),
                    "reply_token": token.strip(),
                    "subject": subject,
                    "body": body[:20000],
                })
            return replies
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _mark_seen(self, uids: list[str]) -> None:
        client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=20)
        try:
            client.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
            client.select(settings.IMAP_FOLDER)
            for uid in uids:
                client.uid("store", uid.encode("ascii"), "+FLAGS", "(\\Seen)")
        finally:
            try:
                client.logout()
            except Exception:
                pass


def _plain_body(message) -> str:
    if message.is_multipart():
        parts = []
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                charset = part.get_content_charset() or "utf-8"
                parts.append(part.get_payload(decode=True).decode(charset, errors="replace"))
        return "\n".join(parts).strip()
    charset = message.get_content_charset() or "utf-8"
    payload = message.get_payload(decode=True)
    return payload.decode(charset, errors="replace").strip() if payload else ""
