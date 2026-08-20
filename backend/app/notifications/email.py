from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.uow import (
    commit as commit_uow,
    ensure_sqlite_write_transaction,
    flush as flush_uow,
)
from app.models import (
    AgentRun,
    ChatMessage,
    InboundMailJob,
    Intervention,
    LearningEvent,
    Notification,
    Plan,
    QueuedMessage,
)
from app.notifications.conversation import (
    open_notification_in_conversation,
)
from app.outbox import enqueue_action
from app.runtime.interventions import accept_intervention_reply
from app.runtime.state import RunStateError, ensure_root_scope_available


class MailboxIdentityChangedError(RuntimeError):
    """The mailbox was rebuilt between PEEK and the Seen acknowledgement."""


@dataclass(frozen=True)
class _AckClaim:
    job_id: str
    token: str
    uidvalidity: int
    uid: int


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
        current_uidvalidity = getattr(self, "_last_uidvalidity", None)
        if replies or current_uidvalidity is not None:
            # IMAP is complete before this short serialized queue/root-run UoW.
            await ensure_sqlite_write_transaction(db)
        run_ids: list[str] = []
        acknowledged_jobs: list[InboundMailJob] = []
        mailbox_key = f"{settings.IMAP_USERNAME}:{settings.IMAP_FOLDER}"[:160]
        if current_uidvalidity is not None:
            acknowledged_jobs.extend(
                list(
                    (
                        await db.execute(
                            select(InboundMailJob).where(
                                InboundMailJob.owner_id == owner_id,
                                InboundMailJob.mailbox_key == mailbox_key,
                                InboundMailJob.uidvalidity == int(current_uidvalidity),
                                InboundMailJob.ack_state != "acked",
                            )
                        )
                    ).scalars()
                )
            )
        for reply in replies:
            mailbox_uid = str(reply.get("uid") or "").strip()
            try:
                uid = int(mailbox_uid)
                uidvalidity = int(str(reply.get("uidvalidity") or ""))
            except (TypeError, ValueError):
                continue
            if uid < 1 or uidvalidity < 1:
                continue
            email_uid = f"{mailbox_key}:{uid}"
            token = reply.get("reply_token", "")
            if not token:
                continue

            intervention = (
                await db.execute(
                    select(Intervention).where(
                        Intervention.owner_id == owner_id,
                        Intervention.reply_token == token,
                    )
                )
            ).scalars().one_or_none()
            notification = None
            if intervention is not None:
                notification = (
                    await db.execute(
                        select(Notification)
                        .where(Notification.intervention_id == intervention.id)
                        .order_by(
                            (Notification.channel == "email").desc(),
                            Notification.id,
                        )
                        .limit(1)
                    )
                ).scalars().one_or_none()
            else:
                notification = (
                    await db.execute(
                        select(Notification).where(
                            Notification.owner_id == owner_id,
                            Notification.reply_token == token,
                        )
                    )
                ).scalars().one_or_none()
            if notification is None:
                continue

            if intervention is None:
                session, canonical_message, _ = await open_notification_in_conversation(
                    db, notification
                )
                intervention = Intervention(
                    owner_id=owner_id,
                    source_run_id=notification.run_id,
                    source_invocation_id=notification.invocation_id,
                    plan_id=notification.plan_id,
                    session_id=session.id,
                    canonical_message_id=canonical_message.id,
                    reply_token=notification.reply_token,
                    title=notification.title,
                    body=notification.body,
                    content_digest=_content_digest(
                        notification.title, notification.body
                    ),
                    reason_code="legacy_email_reply",
                    state="active",
                )
                db.add(intervention)
                await flush_uow(db)
                notification.intervention_id = intervention.id
                notification.legacy_unlinked = False
                await flush_uow(db)
            else:
                session, _, _ = await open_notification_in_conversation(db, notification)

            existing_job = (
                await db.execute(
                    select(InboundMailJob).where(
                        InboundMailJob.owner_id == owner_id,
                        InboundMailJob.mailbox_key == mailbox_key,
                        InboundMailJob.uidvalidity == uidvalidity,
                        InboundMailJob.uid == uid,
                    )
                )
            ).scalars().one_or_none()
            if existing_job is not None:
                if existing_job.ack_state != "acked":
                    acknowledged_jobs.append(existing_job)
                continue

            plan = await db.get(Plan, intervention.plan_id) if intervention.plan_id else None
            archived_plan = plan is not None and plan.status == "archived"
            payload_digest = hashlib.sha256(
                json.dumps(
                    {
                        "body": str(reply.get("body") or ""),
                        "mailbox_key": mailbox_key,
                        "reply_token": token,
                        "subject": str(reply.get("subject") or ""),
                        "uid": uid,
                        "uidvalidity": uidvalidity,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            job = InboundMailJob(
                owner_id=owner_id,
                intervention_id=intervention.id,
                source_notification_id=notification.id,
                session_id=session.id,
                plan_id=intervention.plan_id,
                mailbox_key=mailbox_key,
                uidvalidity=uidvalidity,
                uid=uid,
                subject=str(reply.get("subject") or ""),
                body=str(reply.get("body") or "")[:20000],
                payload_digest=payload_digest,
                state="readonly_receipt" if archived_plan else "queued",
                execution_mode="read_only" if archived_plan else "normal",
                outcome="failure_receipt" if archived_plan else "",
                ack_state="pending",
                last_error_code="archived_plan_read_only" if archived_plan else "",
            )
            db.add(job)
            await flush_uow(db)

            if archived_plan:
                receipt_body = (
                    "已收到你对归档计划的邮件回复。该计划当前为只读历史，"
                    "因此本次不会修改任务、证据或计划状态；如需继续执行，请先恢复计划。"
                )
                receipt_message = ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content=receipt_body,
                    version=1,
                    content_hash=hashlib.sha256(
                        receipt_body.encode("utf-8")
                    ).hexdigest(),
                    reply_to_intervention_id=intervention.id,
                    message_key=f"inbound-mail:{job.id}:readonly-receipt",
                    message_metadata={
                        "ui_kind": "email_read_only_receipt",
                        "intervention_id": intervention.id,
                        "inbound_mail_job_id": job.id,
                    },
                )
                db.add(receipt_message)
                await flush_uow(db)
                job.chat_message_id = receipt_message.id
                session.updated_at = datetime.now(timezone.utc)
                from app.notifications.service import NotificationService

                receipt_payload = {
                    "reply_token": intervention.reply_token,
                    "title": f"Re: {notification.title}",
                    "body": receipt_body,
                    "route_digest": NotificationService._smtp_route_digest(),
                }
                await enqueue_action(
                    db,
                    owner_id=owner_id,
                    run_id=notification.run_id,
                    notification_id=notification.id,
                    action_key=f"inbound-mail:{job.id}:readonly-receipt:smtp",
                    request_digest=hashlib.sha256(
                        json.dumps(
                            receipt_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    destination="smtp",
                    payload=receipt_payload,
                )
                await accept_intervention_reply(db, intervention)
                intervention.state = "resolved"
                intervention.outcome = "archived_read_only_receipt"
                intervention.resolved_at = datetime.now(timezone.utc)
                acknowledged_jobs.append(job)
                continue

            objective = (
                f"学习者回复了通知 {notification.id} 的邮件。"
                f"主题：{reply['subject']}\n回复内容：\n{reply['body']}\n"
                "检查当前状态并理解回复，只执行安全且相关的学习动作；"
                "处理完成后使用 notification_send 的 email 渠道把简洁回复发回邮箱。"
            )
            message_metadata = {
                "channel": "email",
                "intervention_id": intervention.id,
                "notification_id": notification.id,
                "reply_to_intervention_id": intervention.id,
                "email_uid": email_uid,
            }
            scope_busy = False
            try:
                await ensure_root_scope_available(
                    db,
                    owner_id=owner_id,
                    plan_id=notification.plan_id,
                    session_id=session.id,
                )
            except RunStateError:
                scope_busy = True
            if scope_busy:
                max_position = await db.scalar(
                    select(func.coalesce(func.max(QueuedMessage.position), -1)).where(
                        QueuedMessage.owner_id == owner_id,
                        QueuedMessage.session_id == session.id,
                    )
                )
                queued = QueuedMessage(
                    owner_id=owner_id,
                    plan_id=intervention.plan_id,
                    session_id=session.id,
                    trigger="email_reply",
                    objective=objective,
                    user_content=reply["body"],
                    message_metadata=message_metadata,
                    position=int(max_position if max_position is not None else -1) + 1,
                    execution_mode=job.execution_mode,
                    reply_to_intervention_id=intervention.id,
                )
                db.add(queued)
                await flush_uow(db)
                job.queued_message_id = queued.id
                job.state = "queued"
                job.outcome = (
                    "read_only_queued_for_scope"
                    if archived_plan
                    else "queued_for_scope"
                )
                await accept_intervention_reply(db, intervention)
                acknowledged_jobs.append(job)
                continue
            run = AgentRun(
                owner_id=owner_id,
                plan_id=intervention.plan_id,
                session_id=session.id,
                trigger="email_reply",
                objective=objective,
                model=settings.MODEL_NAME,
                execution_mode=job.execution_mode,
                reply_to_intervention_id=intervention.id,
            )
            db.add(run)
            await flush_uow(db)
            reply_message = ChatMessage(
                session_id=session.id,
                run_id=run.id,
                message_key=f"run:{run.id}:input",
                role="user",
                content=reply["body"],
                version=1,
                content_hash=hashlib.sha256(reply["body"].encode("utf-8")).hexdigest(),
                message_metadata=message_metadata,
                reply_to_intervention_id=intervention.id,
            )
            db.add(reply_message)
            db.add(LearningEvent(
                owner_id=owner_id, plan_id=intervention.plan_id, run_id=run.id,
                event_type="email.reply.received", summary=f"Email reply to notification {notification.id}",
                payload={
                    "intervention_id": intervention.id,
                    "notification_id": notification.id,
                    "subject": reply["subject"],
                },
            ))
            session.updated_at = datetime.now(timezone.utc)
            await accept_intervention_reply(db, intervention)
            await flush_uow(db)
            job.run_id = run.id
            job.chat_message_id = reply_message.id
            job.state = "run_started"
            job.outcome = "read_only_continuation" if archived_plan else "run_started"
            run_ids.append(run.id)
            acknowledged_jobs.append(job)
        acknowledged_jobs = list({job.id: job for job in acknowledged_jobs}.values())
        if acknowledged_jobs and db.in_transaction():
            # The unique durable job and its continuation are committed before
            # the only mailbox mutation. ACK ownership is claimed in a second
            # short CAS transaction below.
            await commit_uow(db)
        claims = await self._claim_ack_jobs(db, acknowledged_jobs)
        for uidvalidity, grouped_claims in _group_ack_claims(claims).items():
            try:
                await asyncio.to_thread(
                    self._mark_seen,
                    uidvalidity,
                    [str(claim.uid) for claim in grouped_claims],
                )
            except Exception as exc:
                error_code = (
                    "imap_uidvalidity_changed"
                    if isinstance(exc, MailboxIdentityChangedError)
                    else "imap_seen_failed"
                )
                await self._fail_ack_claims(db, grouped_claims, error_code)
                raise
            await self._complete_ack_claims(db, grouped_claims)
        if not claims and db.in_transaction():
            await commit_uow(db)
        return run_ids

    async def _claim_ack_jobs(
        self,
        db: AsyncSession,
        jobs: list[InboundMailJob],
    ) -> list[_AckClaim]:
        if not jobs:
            return []
        await ensure_sqlite_write_transaction(db)
        claim_now = datetime.now(timezone.utc)
        claims: list[_AckClaim] = []
        for job in jobs:
            token = str(uuid4())
            result = await db.execute(
                update(InboundMailJob)
                .where(
                    InboundMailJob.id == job.id,
                    or_(
                        InboundMailJob.ack_state.in_(["pending", "failed"]),
                        and_(
                            InboundMailJob.ack_state == "claimed",
                            InboundMailJob.ack_claim_expires_at <= claim_now,
                        ),
                    ),
                )
                .values(
                    ack_state="claimed",
                    ack_attempt=InboundMailJob.ack_attempt + 1,
                    ack_claim_token=token,
                    ack_claim_expires_at=claim_now + timedelta(minutes=5),
                    acked_at=None,
                )
            )
            if result.rowcount == 1:
                claims.append(
                    _AckClaim(
                        job_id=job.id,
                        token=token,
                        uidvalidity=job.uidvalidity,
                        uid=job.uid,
                    )
                )
        await commit_uow(db)
        return claims

    async def _complete_ack_claims(
        self,
        db: AsyncSession,
        claims: list[_AckClaim],
    ) -> None:
        await ensure_sqlite_write_transaction(db)
        acked_at = datetime.now(timezone.utc)
        for claim in claims:
            await db.execute(
                update(InboundMailJob)
                .where(
                    InboundMailJob.id == claim.job_id,
                    InboundMailJob.ack_state == "claimed",
                    InboundMailJob.ack_claim_token == claim.token,
                )
                .values(
                    ack_state="acked",
                    ack_claim_token=None,
                    ack_claim_expires_at=None,
                    acked_at=acked_at,
                    last_error_code="",
                )
            )
        await commit_uow(db)

    async def _fail_ack_claims(
        self,
        db: AsyncSession,
        claims: list[_AckClaim],
        error_code: str,
    ) -> None:
        await ensure_sqlite_write_transaction(db)
        for claim in claims:
            await db.execute(
                update(InboundMailJob)
                .where(
                    InboundMailJob.id == claim.job_id,
                    InboundMailJob.ack_state == "claimed",
                    InboundMailJob.ack_claim_token == claim.token,
                )
                .values(
                    ack_state="failed",
                    ack_claim_token=None,
                    ack_claim_expires_at=None,
                    acked_at=None,
                    last_error_code=error_code,
                )
            )
        await commit_uow(db)

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
            uidvalidity = _uidvalidity(client)
            self._last_uidvalidity = uidvalidity
            if uidvalidity is None:
                return []
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                return []
            replies: list[dict[str, str]] = []
            for uid in data[0].split()[-20:]:
                status, message_data = client.uid("fetch", uid, "(BODY.PEEK[])")
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
                    "uidvalidity": str(uidvalidity),
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

    def _mark_seen(self, expected_uidvalidity: int, uids: list[str]) -> None:
        client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT, timeout=20)
        try:
            client.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
            client.select(settings.IMAP_FOLDER)
            actual_uidvalidity = _uidvalidity(client)
            if actual_uidvalidity is None or actual_uidvalidity != expected_uidvalidity:
                raise MailboxIdentityChangedError(
                    "mailbox UIDVALIDITY is unavailable or changed before Seen acknowledgement"
                )
            for uid in uids:
                status, _ = client.uid(
                    "store", uid.encode("ascii"), "+FLAGS", "(\\Seen)"
                )
                if status != "OK":
                    raise RuntimeError("IMAP STORE did not acknowledge Seen")
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


def _uidvalidity(client) -> int | None:
    response = getattr(client, "response", None)
    if response is None:
        return None
    try:
        status, values = response("UIDVALIDITY")
    except Exception:
        return None
    if str(status).upper() not in {"OK", "UIDVALIDITY"} or not values:
        return None
    raw = values[-1]
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    match = re.search(r"\d+", str(raw))
    if match is None:
        return None
    value = int(match.group(0))
    return value if value >= 1 else None


def _content_digest(title: str, body: str) -> str:
    canonical = json.dumps(
        {"body": body, "title": title},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _group_ack_claims(claims: list[_AckClaim]) -> dict[int, list[_AckClaim]]:
    grouped: dict[int, list[_AckClaim]] = {}
    for claim in claims:
        grouped.setdefault(claim.uidvalidity, []).append(claim)
    return grouped
