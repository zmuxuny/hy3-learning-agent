from __future__ import annotations

import asyncio
import email
import imaplib
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AgentRun, ChatMessage, LearningEvent, Notification, Session
from app.services.sessions import link_session_plan


class EmailReplyPoller:
    async def poll(self, db: AsyncSession, owner_id: str) -> list[str]:
        if not self.configured:
            return []
        replies = await asyncio.to_thread(self._fetch_unseen)
        run_ids: list[str] = []
        for reply in replies:
            token = reply.get("reply_token", "")
            if not token:
                continue
            notification = (await db.execute(
                select(Notification).where(Notification.owner_id == owner_id, Notification.reply_token == token)
            )).scalars().one_or_none()
            if not notification:
                continue
            session = await db.get(Session, notification.session_id) if notification.session_id else None
            if session and session.archived_at is not None:
                session = None
            if session is None:
                reply_subject = reply["subject"].strip()
                session = Session(
                    owner_id=owner_id,
                    plan_id=notification.plan_id,
                    title=(f"邮件回复 · {reply_subject}" if reply_subject else "邮件回复")[:80],
                    handoff_summary=f"由通知 {notification.id} 的邮件回复建立。",
                )
                db.add(session)
                await db.flush()
                if notification.plan_id is not None:
                    await link_session_plan(
                        db,
                        owner_id=owner_id,
                        session_id=session.id,
                        plan_id=notification.plan_id,
                        relation_type="focused",
                    )
            run = AgentRun(
                owner_id=owner_id,
                plan_id=notification.plan_id,
                session_id=session.id,
                trigger="email_reply",
                objective=(
                    f"学习者回复了通知 {notification.id} 的邮件。"
                    f"主题：{reply['subject']}\n回复内容：\n{reply['body']}\n"
                    "检查当前状态并理解回复，只执行安全且相关的学习动作；"
                    "处理完成后使用 notification_send 的 email 渠道把简洁回复发回邮箱。"
                ),
                model=settings.MODEL_NAME,
            )
            db.add(run)
            await db.flush()
            db.add(ChatMessage(
                session_id=session.id,
                run_id=run.id,
                role="user",
                content=reply["body"],
                message_metadata={"channel": "email", "notification_id": notification.id},
            ))
            db.add(LearningEvent(
                owner_id=owner_id, plan_id=notification.plan_id, run_id=run.id,
                event_type="email.reply.received", summary=f"Email reply to notification {notification.id}",
                payload={"notification_id": notification.id, "subject": reply["subject"]},
            ))
            session.updated_at = datetime.now(timezone.utc)
            run_ids.append(run.id)
        if run_ids:
            await db.commit()
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
                    "reply_token": token.strip(),
                    "subject": subject,
                    "body": body[:20000],
                })
                client.uid("store", uid, "+FLAGS", "(\\Seen)")
            return replies
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
