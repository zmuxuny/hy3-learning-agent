from __future__ import annotations

import asyncio
from email.message import EmailMessage
import hashlib
import threading

import pytest
from sqlalchemy import func, select

import app.models as model_module
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    ChatMessage,
    EvidenceObservation,
    Notification,
    OutboxAction,
    Plan,
    Session,
    Stage,
    Task,
)
from app.notifications import email as email_module
from app.notifications.email import EmailReplyPoller, MailboxIdentityChangedError


def _require_setup(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"H5 mail setup failed: {message}")


def _raw_mail(*, subject: str, body: str, reply_token: str | None) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    if reply_token is not None:
        message["X-Learning-Agent-Reply-Token"] = reply_token
    message.set_content(body)
    return message.as_bytes()


def _configure_imap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_EMAIL_REPLY_POLLING", True)
    monkeypatch.setattr(settings, "IMAP_HOST", "imap.test.invalid")
    monkeypatch.setattr(settings, "IMAP_PORT", 993)
    monkeypatch.setattr(settings, "IMAP_USERNAME", "h5@example.invalid")
    monkeypatch.setattr(settings, "IMAP_PASSWORD", "test-only-password")
    monkeypatch.setattr(settings, "IMAP_FOLDER", "INBOX")


@pytest.mark.asyncio
async def test_imap_peek_preserves_unrelated_and_tokenless_unread_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_imap(monkeypatch)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Stateful IMAP protocol")
        db.add(session)
        await db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            channel="email",
            title="Relevant reminder",
            body="Reply to the relevant reminder only",
            status="sent",
        )
        db.add(notification)
        await db.commit()
        relevant_token = notification.reply_token

    mailbox = {
        "101": _raw_mail(
            subject="Re: Relevant reminder",
            body="This reply belongs to the Learning Agent.",
            reply_token=relevant_token,
        ),
        "102": _raw_mail(
            subject="Re: Another system",
            body="This token has no matching local notification.",
            reply_token="22222222-2222-4222-8222-222222222222",
        ),
        "103": _raw_mail(
            subject="Ordinary unread mail",
            body="No Learning Agent reply token is present.",
            reply_token=None,
        ),
    }

    class StatefulIMAP:
        seen: set[str] = set()
        fetch_specs: dict[str, str] = {}

        def __init__(self, _host, _port, timeout):
            _require_setup(timeout == 20, "unexpected IMAP timeout")

        def login(self, _username, _password):
            return "OK", [b""]

        def select(self, _folder):
            return "OK", [str(len(mailbox)).encode()]

        def response(self, name):
            _require_setup(name == "UIDVALIDITY", "unexpected IMAP response query")
            return "OK", [b"1"]

        def uid(self, command, *args):
            if command == "search":
                unseen = [uid for uid in mailbox if uid not in self.seen]
                return "OK", [" ".join(unseen).encode("ascii")]
            uid = args[0].decode("ascii") if isinstance(args[0], bytes) else str(args[0])
            if command == "fetch":
                spec = str(args[1])
                self.fetch_specs[uid] = spec
                if "BODY.PEEK[]" not in spec:
                    self.seen.add(uid)
                return "OK", [(f"{uid} (RFC822)".encode("ascii"), mailbox[uid])]
            if command == "store":
                self.seen.add(uid)
                return "OK", [b""]
            raise RuntimeError(f"unexpected fake IMAP command: {command}")

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(email_module.imaplib, "IMAP4_SSL", StatefulIMAP)
    async with AsyncSessionLocal() as db:
        run_ids = await EmailReplyPoller().poll(db, "local")

    _require_setup(len(run_ids) == 1, "relevant reply did not create exactly one Run")
    failures: list[str] = []
    if set(StatefulIMAP.fetch_specs) != set(mailbox):
        failures.append("not_every_unread_uid_was_examined")
    if any("BODY.PEEK[]" not in spec for spec in StatefulIMAP.fetch_specs.values()):
        failures.append("fetch_was_not_non_mutating_body_peek")
    if StatefulIMAP.seen != {"101"}:
        failures.append(f"unrelated_or_tokenless_mail_marked_seen:{sorted(StatefulIMAP.seen)}")

    assert failures == []


@pytest.mark.asyncio
async def test_legacy_email_notification_gets_its_own_canonical_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_imap(monkeypatch)
    final_text = "The source Run's ordinary final answer is not the reminder."
    notification_body = "This exact legacy notification is the reply target."
    async with AsyncSessionLocal() as setup_db:
        session = Session(owner_id="local", title="Legacy email canonical identity")
        setup_db.add(session)
        await setup_db.flush()
        source_run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="Produce a final answer and a different legacy reminder",
            status="completed",
            phase="terminal",
        )
        setup_db.add(source_run)
        await setup_db.flush()
        setup_db.add(
            ChatMessage(
                session_id=session.id,
                run_id=source_run.id,
                role="assistant",
                content=final_text,
                version=1,
                content_hash=hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            )
        )
        notification = Notification(
            owner_id="local",
            run_id=source_run.id,
            session_id=session.id,
            channel="email",
            title="Legacy reminder",
            body=notification_body,
            status="sent",
        )
        setup_db.add(notification)
        await setup_db.commit()
        token = notification.reply_token
        notification_id = notification.id

    poller = EmailReplyPoller()
    monkeypatch.setattr(
        poller,
        "_fetch_unseen",
        lambda: [
            {
                "uid": "104",
                "uidvalidity": "2",
                "reply_token": token,
                "subject": "Re: Legacy reminder",
                "body": "Reply to the legacy reminder, not the Run final answer.",
            }
        ],
    )
    acknowledged: list[str] = []
    monkeypatch.setattr(
        poller,
        "_mark_seen",
        lambda _uidvalidity, uids: acknowledged.extend(uids),
    )
    async with AsyncSessionLocal() as db:
        run_ids = await poller.poll(db, "local")

    async with AsyncSessionLocal() as check_db:
        stored_notification = await check_db.get(Notification, notification_id)
        _require_setup(stored_notification is not None, "legacy Notification disappeared")
        intervention_model = getattr(model_module, "Intervention", None)
        _require_setup(intervention_model is not None, "Intervention model is absent")
        intervention = await check_db.get(
            intervention_model,
            stored_notification.intervention_id,
        )
        _require_setup(intervention is not None, "legacy delivery was not repaired")
        canonical = await check_db.get(ChatMessage, intervention.canonical_message_id)

    assert len(run_ids) == 1
    assert acknowledged == ["104"]
    assert canonical is not None
    assert canonical.content == notification_body
    assert canonical.content != final_text
    assert canonical.id == intervention.canonical_message_id
    assert intervention.state == "replied"
    assert intervention.outcome == "reply_received"


@pytest.mark.asyncio
async def test_two_pollers_create_one_durable_inbound_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_imap(monkeypatch)
    async with AsyncSessionLocal() as setup_db:
        session = Session(owner_id="local", title="Concurrent inbound reply")
        setup_db.add(session)
        await setup_db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            channel="email",
            title="Concurrent reminder",
            body="Deduplicate this reply",
            status="sent",
        )
        setup_db.add(notification)
        await setup_db.commit()
        reply_token = notification.reply_token

    reply = {
        "uid": "701",
        "uidvalidity": "55",
        "reply_token": reply_token,
        "subject": "Re: Concurrent reminder",
        "body": "One inbound reply, observed twice.",
    }
    pollers = [EmailReplyPoller(), EmailReplyPoller()]
    acknowledgements: list[tuple[str, ...]] = []
    entered_store = threading.Event()
    release_store = threading.Event()
    for poller in pollers:
        monkeypatch.setattr(poller, "_fetch_unseen", lambda reply=reply: [dict(reply)])

    def first_mark_seen(_uidvalidity, uids):
        entered_store.set()
        _require_setup(
            release_store.wait(timeout=5),
            "concurrent ACK fixture did not release first poller",
        )
        acknowledgements.append(tuple(uids))

    monkeypatch.setattr(pollers[0], "_mark_seen", first_mark_seen)
    monkeypatch.setattr(
        pollers[1],
        "_mark_seen",
        lambda _uidvalidity, uids: acknowledgements.append(tuple(uids)),
    )

    async with AsyncSessionLocal() as first_db, AsyncSessionLocal() as second_db:
        first_task = asyncio.create_task(pollers[0].poll(first_db, "local"))
        _require_setup(
            await asyncio.to_thread(entered_store.wait, 5),
            "first poller did not reach commit-to-STORE boundary",
        )
        try:
            await pollers[1].poll(second_db, "local")
        finally:
            release_store.set()
        await first_task

    async with AsyncSessionLocal() as check_db:
        run_count = int(
            await check_db.scalar(
                select(func.count(AgentRun.id)).where(AgentRun.trigger == "email_reply")
            )
            or 0
        )
        message_count = int(
            await check_db.scalar(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.message_metadata["email_uid"].as_string()
                    == "h5@example.invalid:INBOX:701"
                )
            )
            or 0
        )
        inbound_model = getattr(model_module, "InboundMailJob", None)
        inbound_jobs = []
        if inbound_model is not None:
            inbound_jobs = list((await check_db.execute(select(inbound_model))).scalars())

    failures: list[str] = []
    if run_count != 1:
        failures.append(f"duplicate_email_reply_runs:{run_count}")
    if message_count != 1:
        failures.append(f"duplicate_email_reply_messages:{message_count}")
    if acknowledgements != [("701",)]:
        failures.append(f"duplicate_or_missing_seen_store:{acknowledgements}")
    if inbound_model is None:
        failures.append("inbound_mail_job_model_missing")
    elif len(inbound_jobs) != 1:
        failures.append(f"durable_inbound_job_count:{len(inbound_jobs)}")
    else:
        job = inbound_jobs[0]
        if str(getattr(job, "uid", "")) != "701":
            failures.append("durable_job_lost_uid")
        if str(getattr(job, "uidvalidity", "")) != "55":
            failures.append("durable_job_lost_uidvalidity")

    assert failures == []


@pytest.mark.asyncio
async def test_restart_after_reply_commit_resumes_seen_ack_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_imap(monkeypatch)
    async with AsyncSessionLocal() as setup_db:
        session = Session(owner_id="local", title="Commit then ACK restart")
        setup_db.add(session)
        await setup_db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            channel="email",
            title="Crash boundary",
            body="Commit before Seen",
            status="sent",
        )
        setup_db.add(notification)
        await setup_db.commit()
        token = notification.reply_token

    reply = {
        "uid": "801",
        "uidvalidity": "88",
        "reply_token": token,
        "subject": "Re: Crash boundary",
        "body": "Persist me before acknowledging the mailbox.",
    }

    class InjectedProcessKill(RuntimeError):
        pass

    first = EmailReplyPoller()
    monkeypatch.setattr(first, "_fetch_unseen", lambda: [dict(reply)])

    def kill_before_seen(_uidvalidity, _uids):
        raise InjectedProcessKill("after durable commit, before IMAP Seen")

    monkeypatch.setattr(first, "_mark_seen", kill_before_seen)
    async with AsyncSessionLocal() as first_db:
        with pytest.raises(InjectedProcessKill, match="before IMAP Seen"):
            await first.poll(first_db, "local")

    acknowledged: list[str] = []
    recovered = EmailReplyPoller()
    monkeypatch.setattr(recovered, "_fetch_unseen", lambda: [dict(reply)])
    monkeypatch.setattr(
        recovered,
        "_mark_seen",
        lambda _uidvalidity, uids: acknowledged.extend(uids),
    )
    async with AsyncSessionLocal() as recovered_db:
        await recovered.poll(recovered_db, "local")

    async with AsyncSessionLocal() as check_db:
        run_count = int(
            await check_db.scalar(
                select(func.count(AgentRun.id)).where(AgentRun.trigger == "email_reply")
            )
            or 0
        )
        inbound_model = getattr(model_module, "InboundMailJob", None)
        jobs = [] if inbound_model is None else list(
            (await check_db.execute(select(inbound_model))).scalars()
        )

    failures: list[str] = []
    if run_count != 1:
        failures.append(f"restart_duplicated_reply_run:{run_count}")
    if acknowledged != ["801"]:
        failures.append(f"restart_did_not_finish_exact_ack:{acknowledged}")
    if inbound_model is None:
        failures.append("inbound_mail_job_model_missing")
    elif len(jobs) != 1:
        failures.append(f"restart_job_count:{len(jobs)}")
    elif getattr(jobs[0], "ack_state", None) not in {"acked", "acknowledged", "seen"}:
        failures.append("durable_job_ack_state_not_terminal")

    assert failures == []


@pytest.mark.asyncio
async def test_seen_ack_refuses_reused_uid_after_uidvalidity_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_imap(monkeypatch)
    async with AsyncSessionLocal() as setup_db:
        session = Session(owner_id="local", title="UIDVALIDITY fence")
        setup_db.add(session)
        await setup_db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            channel="email",
            title="Mailbox identity",
            body="Do not ACK a reused UID",
            status="sent",
        )
        setup_db.add(notification)
        await setup_db.commit()
        token = notification.reply_token

    reply = {
        "uid": "850",
        "uidvalidity": "55",
        "reply_token": token,
        "subject": "Re: Mailbox identity",
        "body": "The mailbox will be rebuilt before ACK.",
    }
    stored: list[str] = []

    class RebuiltMailbox:
        def __init__(self, _host, _port, timeout):
            _require_setup(timeout == 20, "unexpected IMAP timeout")

        def login(self, _username, _password):
            return "OK", [b""]

        def select(self, _folder):
            return "OK", [b"1"]

        def response(self, name):
            _require_setup(name == "UIDVALIDITY", "unexpected IMAP response query")
            return "OK", [b"56"]

        def uid(self, command, *args):
            if command == "store":
                stored.append(str(args[0]))
                return "OK", [b""]
            raise RuntimeError(f"unexpected command: {command}")

        def logout(self):
            return "BYE", [b""]

    poller = EmailReplyPoller()
    monkeypatch.setattr(poller, "_fetch_unseen", lambda: [dict(reply)])
    monkeypatch.setattr(email_module.imaplib, "IMAP4_SSL", RebuiltMailbox)
    async with AsyncSessionLocal() as db:
        with pytest.raises(MailboxIdentityChangedError, match="UIDVALIDITY"):
            await poller.poll(db, "local")

    async with AsyncSessionLocal() as check_db:
        inbound_model = getattr(model_module, "InboundMailJob")
        job = (await check_db.execute(select(inbound_model))).scalars().one()
    assert stored == []
    assert job.ack_state == "failed"
    assert job.last_error_code == "imap_uidvalidity_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uidvalidity_response",
    [("NO", []), ("OK", [b"not-a-number"])],
)
async def test_unprovable_uidvalidity_does_not_create_or_ack_job(
    monkeypatch: pytest.MonkeyPatch,
    uidvalidity_response,
) -> None:
    _configure_imap(monkeypatch)
    commands: list[str] = []

    class UnprovableMailbox:
        def __init__(self, _host, _port, timeout):
            _require_setup(timeout == 20, "unexpected IMAP timeout")

        def login(self, _username, _password):
            return "OK", [b""]

        def select(self, _folder):
            return "OK", [b"1"]

        def response(self, name):
            _require_setup(name == "UIDVALIDITY", "unexpected IMAP response query")
            return uidvalidity_response

        def uid(self, command, *_args):
            commands.append(command)
            return "OK", [b"1101"]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(email_module.imaplib, "IMAP4_SSL", UnprovableMailbox)
    async with AsyncSessionLocal() as db:
        assert await EmailReplyPoller().poll(db, "local") == []
        inbound_model = getattr(model_module, "InboundMailJob")
        count = int(await db.scalar(select(func.count(inbound_model.id))) or 0)
    assert count == 0
    assert "store" not in commands


@pytest.mark.asyncio
async def test_non_ok_seen_store_is_durable_failed_not_acked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_imap(monkeypatch)
    async with AsyncSessionLocal() as setup_db:
        session = Session(owner_id="local", title="STORE failure fence")
        setup_db.add(session)
        await setup_db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            channel="email",
            title="STORE failure",
            body="Do not claim a failed STORE succeeded",
            status="sent",
        )
        setup_db.add(notification)
        await setup_db.commit()
        token = notification.reply_token

    reply = {
        "uid": "860",
        "uidvalidity": "66",
        "reply_token": token,
        "subject": "Re: STORE failure",
        "body": "Persist failure state.",
    }

    class RejectingStoreMailbox:
        def __init__(self, _host, _port, timeout):
            _require_setup(timeout == 20, "unexpected IMAP timeout")

        def login(self, _username, _password):
            return "OK", [b""]

        def select(self, _folder):
            return "OK", [b"1"]

        def response(self, _name):
            return "OK", [b"66"]

        def uid(self, command, *_args):
            _require_setup(command == "store", "unexpected IMAP command")
            return "NO", [b"provider rejected STORE"]

        def logout(self):
            return "BYE", [b""]

    poller = EmailReplyPoller()
    monkeypatch.setattr(poller, "_fetch_unseen", lambda: [dict(reply)])
    monkeypatch.setattr(email_module.imaplib, "IMAP4_SSL", RejectingStoreMailbox)
    async with AsyncSessionLocal() as db:
        with pytest.raises(RuntimeError, match="STORE"):
            await poller.poll(db, "local")

    async with AsyncSessionLocal() as check_db:
        inbound_model = getattr(model_module, "InboundMailJob")
        job = (await check_db.execute(select(inbound_model))).scalars().one()
    assert job.ack_state == "failed"
    assert job.acked_at is None
    assert job.last_error_code == "imap_seen_failed"


@pytest.mark.asyncio
async def test_archived_plan_reply_is_read_only_or_has_explicit_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_imap(monkeypatch)
    async with AsyncSessionLocal() as setup_db:
        plan = Plan(
            owner_id="local",
            title="Archived mail history",
            goal="Historical state must not change",
            status="archived",
            archived_from_status="active",
        )
        stage = Stage(plan=plan, title="Frozen stage", status="completed")
        task = Task(stage=stage, title="Frozen task", status="completed")
        session = Session(owner_id="local", title="Archived reply thread")
        setup_db.add_all([plan, stage, task, session])
        await setup_db.flush()
        session.plan_id = plan.id
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            channel="email",
            title="Archived reminder",
            body="This is historical",
            status="sent",
        )
        setup_db.add(notification)
        await setup_db.commit()
        plan_id = plan.id
        task_id = task.id
        session_id = session.id
        notification_id = notification.id
        token = notification.reply_token
        frozen_plan = (plan.status, plan.goal, plan.updated_at)
        frozen_task = (task.status, task.title, task.completed_at)

    reply = {
        "uid": "901",
        "uidvalidity": "99",
        "reply_token": token,
        "subject": "Re: Archived reminder",
        "body": "Explain the old result without changing the archived plan.",
    }
    poller = EmailReplyPoller()
    monkeypatch.setattr(poller, "_fetch_unseen", lambda: [dict(reply)])
    acknowledged: list[str] = []
    monkeypatch.setattr(
        poller,
        "_mark_seen",
        lambda _uidvalidity, uids: acknowledged.extend(uids),
    )
    async with AsyncSessionLocal() as poll_db:
        await poller.poll(poll_db, "local")

    async with AsyncSessionLocal() as check_db:
        plan = await check_db.get(Plan, plan_id)
        task = await check_db.get(Task, task_id)
        runs = list(
            (
                await check_db.execute(
                    select(AgentRun).where(
                        AgentRun.plan_id == plan_id,
                        AgentRun.trigger == "email_reply",
                    )
                )
            ).scalars()
        )
        evidence_count = int(
            await check_db.scalar(
                select(func.count(EvidenceObservation.id)).where(
                    EvidenceObservation.plan_id == plan_id
                )
            )
            or 0
        )
        inbound_model = getattr(model_module, "InboundMailJob", None)
        jobs = [] if inbound_model is None else list(
            (await check_db.execute(select(inbound_model))).scalars()
        )
        receipt_messages = list(
            (
                await check_db.execute(
                    select(ChatMessage).where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.role == "assistant",
                        ChatMessage.reply_to_intervention_id.is_not(None),
                    )
                )
            ).scalars()
        )
        receipt_outbox = list(
            (
                await check_db.execute(
                    select(OutboxAction).where(
                        OutboxAction.destination == "smtp",
                        OutboxAction.notification_id == notification_id,
                    )
                )
            ).scalars()
        )

    _require_setup(plan is not None and task is not None, "archived scope disappeared")
    _require_setup((plan.status, plan.goal, plan.updated_at) == frozen_plan, "Plan fixture mutated")
    _require_setup((task.status, task.title, task.completed_at) == frozen_task, "Task fixture mutated")
    _require_setup(evidence_count == 0, "mail ingress unexpectedly wrote Evidence")

    read_only_run = any(
        getattr(run, "execution_mode", None) == "read_only"
        or bool(getattr(run, "read_only", False))
        or (getattr(run, "checkpoint", None) or {}).get("capability_mode") == "read_only"
        for run in runs
    )
    explicit_receipt = any(
        getattr(job, "outcome", None)
        in {"read_only_answer", "rejected_archived_plan", "failure_receipt"}
        for job in jobs
    ) and bool(receipt_messages) and bool(receipt_outbox)
    failures: list[str] = []
    if not read_only_run and not explicit_receipt:
        failures.append("archived_reply_has_no_read_only_continuation_or_failure_receipt")
    if inbound_model is None:
        failures.append("archived_reply_has_no_durable_inbound_job")
    if acknowledged != ["901"]:
        failures.append(f"archived_reply_ack_not_exact:{acknowledged}")
    if runs and any(getattr(run, "reply_to_intervention_id", None) is None for run in runs):
        failures.append("archived_continuation_lost_intervention_target")

    assert failures == []
