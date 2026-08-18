from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.agent import enqueue_message
from app.context import assembler as context_assembler_module
from app.context.assembler import ContextAssembler
from app.core.config import settings
from app.db.database import Base
from app.models import (
    AgentRun,
    Notification,
    Owner,
    Plan,
    ReviewSchedule,
    Session,
    UserProfile,
)
from app.notifications import email as email_module
from app.notifications import service as notification_service_module
from app.notifications.conversation import open_notification_in_conversation
from app.notifications.email import EmailReplyPoller
from app.notifications.service import NotificationService
from app.runtime import scheduler as scheduler_module
from app.runtime.scheduler import ProactiveScheduler
from app.schemas import QueuedMessageCreate
from app.tools.base import ToolContext
from app.tools.registry import execute_tool


def _require_precondition(condition: bool, message: str) -> None:
    """Keep fixture/setup regressions out of strict-xfail accounting."""

    if not condition:
        raise RuntimeError(f"H0 intervention test precondition failed: {message}")


@pytest_asyncio.fixture
async def isolated_db(tmp_path: Path):
    """Give every regression an independent disposable SQLite database."""

    database_path = tmp_path / "h0-intervention.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as db:
        db.add(Owner(id="local", display_name="H0 learner", timezone="Asia/Shanghai"))
        db.add(
            UserProfile(
                owner_id="local",
                quiet_hours={"start": "00:00", "end": "00:00"},
                daily_notification_limit=10,
            )
        )
        await db.commit()
        yield db, session_factory
    await engine.dispose()


async def _due_review_plan(db: AsyncSession) -> Plan:
    now = datetime.now(timezone.utc)
    plan = Plan(
        owner_id="local",
        title="H0 proactive plan",
        goal="Exercise proactive recovery semantics",
        status="active",
        created_at=now - timedelta(days=10),
    )
    db.add(plan)
    await db.flush()
    db.add(
        ReviewSchedule(
            owner_id="local",
            plan_id=plan.id,
            due_at=now - timedelta(minutes=5),
            status="scheduled",
        )
    )
    await db.commit()
    return plan


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-INT-001: active-run reminder replies are stored as ordinary queue/steer "
        "messages and lose their stable Intervention reply target"
    ),
)
async def test_active_run_intervention_reply_is_queued_with_target(isolated_db):
    db, _ = isolated_db
    plan = Plan(owner_id="local", title="Reply target", goal="Keep the reminder identity")
    session = Session(owner_id="local", plan_id=None, title="Reminder thread")
    db.add_all([plan, session])
    await db.flush()
    session.plan_id = plan.id
    active_run = AgentRun(
        owner_id="local",
        session_id=session.id,
        plan_id=plan.id,
        trigger="user_message",
        objective="Long-running turn",
        status="running",
    )
    delivery = Notification(
        owner_id="local",
        session_id=session.id,
        plan_id=plan.id,
        channel="in_app",
        title="Reminder target",
        body="Reply to this exact intervention.",
        status="sent",
        sent_at=datetime.now(timezone.utc),
    )
    db.add_all([active_run, delivery])
    await db.commit()
    await db.refresh(delivery)

    stable_intervention_id = "11111111-1111-4111-8111-111111111111"
    payload = QueuedMessageCreate.model_validate(
        {
            "objective": "This answer belongs to the reminder.",
            "session_id": session.id,
            "plan_id": plan.id,
            "reply_to_intervention_id": stable_intervention_id,
        }
    )
    queued = await enqueue_message(payload, db)

    assert "reply_to_notification_id" not in queued.message_metadata
    assert queued.message_metadata.get("reply_to_intervention_id") == stable_intervention_id


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-INT-002: plan Context projects delivery rows instead of one authoritative "
        "Intervention"
    ),
)
async def test_plan_context_counts_one_intervention_across_three_deliveries(
    isolated_db,
    monkeypatch,
    tmp_path: Path,
):
    db, _ = isolated_db
    monkeypatch.setattr(context_assembler_module, "PROJECT_ROOT", tmp_path)
    plan = Plan(owner_id="local", title="Delivery projection", goal="One logical reminder")
    run = AgentRun(
        owner_id="local",
        plan_id=None,
        trigger="heartbeat",
        objective="Decide whether to remind",
        status="completed",
    )
    db.add_all([plan, run])
    await db.flush()
    run.plan_id = plan.id
    marker = "H0-ONE-INTERVENTION-THREE-DELIVERIES"
    sent_at = datetime.now(timezone.utc)
    db.add_all(
        [
            Notification(
                owner_id="local",
                run_id=run.id,
                plan_id=plan.id,
                channel=channel,
                title="One reminder",
                body=marker,
                status="sent",
                sent_at=sent_at,
            )
            for channel in ("in_app", "email", "browser")
        ]
    )
    await db.commit()

    snapshot = await ContextAssembler(db).build(
        "local",
        plan_id=plan.id,
        session_id=None,
        run_id=None,
        objective="Inspect the prior intervention",
    )

    assert snapshot.markdown.count(marker) == 1


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-INT-003: delivery grouping and canonical-message lookup use mutable "
        "run/title/body heuristics"
    ),
)
async def test_identical_in_app_reminders_are_not_heuristically_merged(isolated_db):
    db, _ = isolated_db
    session = Session(owner_id="local", title="Canonical reminder thread")
    run = AgentRun(
        owner_id="local",
        session_id=session.id,
        trigger="heartbeat",
        objective="Two intentional reminder calls",
        status="completed",
    )
    db.add_all([session, run])
    await db.flush()
    first = Notification(
        owner_id="local",
        run_id=run.id,
        session_id=session.id,
        channel="in_app",
        title="Same visible text",
        body="The payload is intentionally identical.",
        status="sent",
    )
    second = Notification(
        owner_id="local",
        run_id=run.id,
        session_id=session.id,
        channel="in_app",
        title="Same visible text",
        body="The payload is intentionally identical.",
        status="sent",
    )
    db.add_all([first, second])
    await db.commit()

    _, first_message, _ = await open_notification_in_conversation(db, first)
    assert second.read_at is None

    _, second_message, _ = await open_notification_in_conversation(db, second)
    assert second_message.id != first_message.id


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-MAIL-001: an archived-plan email continuation cannot persist an explicit "
        "read-only answer or failure receipt"
    ),
)
async def test_archived_plan_email_reply_can_emit_read_only_receipt(isolated_db):
    db, _ = isolated_db
    plan = Plan(
        owner_id="local",
        title="Archived history",
        goal="Preserve historical replies",
        status="archived",
        archived_from_status="active",
    )
    session = Session(owner_id="local", title="Historical email thread")
    db.add_all([plan, session])
    await db.flush()
    session.plan_id = plan.id
    run = AgentRun(
        owner_id="local",
        session_id=session.id,
        plan_id=plan.id,
        trigger="email_reply",
        objective="Answer a historical email without mutating the plan",
        status="running",
    )
    db.add(run)
    await db.commit()

    result = await NotificationService(db).send(
        owner_id="local",
        run_id=run.id,
        session_id=session.id,
        trigger="email_reply",
        title="Historical reply receipt",
        body="The plan is archived; this continuation is read-only.",
        plan_id=plan.id,
        channels=["in_app"],
    )

    assert result["blocked"] is False
    assert result["notifications"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-MAIL-002: RFC822 fetch may set Seen before a durable reply job exists, "
        "including for unrelated unread mail"
    ),
)
def test_imap_fetch_uses_body_peek_without_seen_side_effect(monkeypatch):
    token = "00000000-0000-4000-8000-000000000001"
    message = EmailMessage()
    message["Subject"] = "Re: Learning reminder"
    message["X-Learning-Agent-Reply-Token"] = token
    message.set_content("A deterministic offline reply.")
    raw_message = message.as_bytes()

    class FakeIMAP:
        instance = None

        def __init__(self, _host, _port, timeout):
            _require_precondition(timeout == 20, "the fake IMAP client received an unexpected timeout")
            self.fetch_specs: list[str] = []
            self.seen_during_fetch = False
            FakeIMAP.instance = self

        def login(self, _username, _password):
            return "OK", [b""]

        def select(self, _folder):
            return "OK", [b"1"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"101"]
            if command == "fetch":
                spec = str(args[1])
                self.fetch_specs.append(spec)
                if "BODY.PEEK[]" not in spec:
                    self.seen_during_fetch = True
                return "OK", [(b"101 (RFC822)", raw_message)]
            raise RuntimeError(f"Fake IMAP received an unexpected UID command: {command}")

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(email_module.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(email_module.settings, "IMAP_HOST", "imap.invalid")
    monkeypatch.setattr(email_module.settings, "IMAP_PORT", 993)
    monkeypatch.setattr(email_module.settings, "IMAP_USERNAME", "offline@example.invalid")
    monkeypatch.setattr(email_module.settings, "IMAP_PASSWORD", "test-only")
    monkeypatch.setattr(email_module.settings, "IMAP_FOLDER", "INBOX")

    replies = EmailReplyPoller()._fetch_unseen()

    _require_precondition(
        [reply["uid"] for reply in replies] == ["101"],
        "the deterministic reply was not parsed before checking PEEK semantics",
    )
    assert FakeIMAP.instance.fetch_specs == ["(BODY.PEEK[])"]
    assert FakeIMAP.instance.seen_during_fetch is False


@pytest.mark.asyncio
async def test_imap_seen_only_after_reply_job_commit(isolated_db, monkeypatch):
    db, _ = isolated_db
    session = Session(owner_id="local", title="Commit ordering")
    db.add(session)
    await db.flush()
    notification = Notification(
        owner_id="local",
        session_id=session.id,
        channel="email",
        title="Commit before Seen",
        body="Persist the reply first.",
        status="sent",
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)

    poller = EmailReplyPoller()
    monkeypatch.setattr(EmailReplyPoller, "configured", property(lambda _self: True))
    monkeypatch.setattr(
        poller,
        "_fetch_unseen",
        lambda: [
            {
                "uid": "202",
                "reply_token": notification.reply_token,
                "subject": "Re: Commit before Seen",
                "body": "Please preserve this reply.",
            }
        ],
    )
    marked_seen: list[str] = []
    monkeypatch.setattr(poller, "_mark_seen", lambda uids: marked_seen.extend(uids))

    async def fail_commit():
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(db, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="injected commit failure"):
        await poller.poll(db, "local")

    assert marked_seen == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-PRO-001: failed heartbeat Runs consume the same full candidate cooldown "
        "as a successful terminal ProactiveDecision"
    ),
)
async def test_failed_heartbeat_does_not_consume_success_cooldown(isolated_db, monkeypatch):
    db, session_factory = isolated_db
    plan = await _due_review_plan(db)
    db.add(
        AgentRun(
            owner_id="local",
            plan_id=plan.id,
            trigger="heartbeat",
            objective="Model failed before deciding",
            status="failed",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
            completed_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db.commit()
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(settings, "AGENT_CANDIDATE_COOLDOWN_MINUTES", 7 * 24 * 60)

    candidate = await ProactiveScheduler()._next_candidate()

    assert candidate is not None
    assert candidate["plan_id"] == plan.id
    assert candidate["reason"] == "due_review"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-PRO-002: quiet-hours rejection neither records the next reachable time nor "
        "avoids the full success cooldown"
    ),
)
async def test_quiet_hours_rejection_does_not_consume_success_cooldown(
    isolated_db,
    monkeypatch,
):
    db, session_factory = isolated_db
    plan = await _due_review_plan(db)
    run = AgentRun(
        owner_id="local",
        plan_id=plan.id,
        trigger="heartbeat",
        objective="Attempt during quiet hours",
        status="running",
    )
    db.add(run)
    profile = await db.get(UserProfile, "local")
    profile.quiet_hours = {"start": "23:00", "end": "08:00"}
    await db.commit()

    fixed_local = datetime(2026, 8, 18, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_local.replace(tzinfo=None) if tz is None else fixed_local.astimezone(tz)

    monkeypatch.setattr(notification_service_module, "datetime", FrozenDateTime)
    blocked = await NotificationService(db).send(
        owner_id="local",
        run_id=run.id,
        session_id=None,
        trigger="heartbeat",
        title="Quiet-hours attempt",
        body="This must be deferred, not treated as a successful intervention.",
        plan_id=plan.id,
        channels=["in_app"],
    )
    _require_precondition(
        blocked.get("blocked") is True and blocked.get("reason") == "quiet hours",
        "the frozen clock and quiet-hours Guard did not produce a quiet-hours rejection",
    )

    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    profile.quiet_hours = {"start": "00:00", "end": "00:00"}
    await db.commit()
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(settings, "AGENT_CANDIDATE_COOLDOWN_MINUTES", 180)

    candidate = await ProactiveScheduler()._next_candidate()

    assert candidate is not None
    assert candidate["plan_id"] == plan.id
    assert blocked.get("next_eligible_at") == "2026-08-19T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H5-PRO-003: a deterministic notification Guard rejection is represented only "
        "by the existence of a heartbeat Run and therefore consumes success cooldown"
    ),
)
async def test_guard_rejection_does_not_consume_success_cooldown(isolated_db, monkeypatch):
    db, session_factory = isolated_db
    plan = await _due_review_plan(db)
    run = AgentRun(
        owner_id="local",
        plan_id=plan.id,
        trigger="heartbeat",
        objective="Attempt beyond the daily limit",
        status="running",
    )
    db.add(run)
    profile = await db.get(UserProfile, "local")
    profile.daily_notification_limit = 0
    await db.commit()

    tool_result = await execute_tool(
        "notification_send",
        json.dumps(
            {
                "title": "Guarded attempt",
                "body": "The deterministic Guard must not record success.",
                "plan_id": plan.id,
                "channels": ["in_app"],
            }
        ),
        ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            trigger="heartbeat",
            plan_id=plan.id,
            tool_call_id="h5-pro-003",
        ),
    )
    _require_precondition(
        tool_result.get("ok") is True,
        "the deterministic notification tool call did not return a domain result",
    )
    _require_precondition(
        (tool_result.get("data") or {}).get("blocked") is True,
        "the daily-limit Guard did not reject the deterministic notification attempt",
    )

    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    profile.daily_notification_limit = 10
    await db.commit()
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(settings, "AGENT_CANDIDATE_COOLDOWN_MINUTES", 180)

    candidate = await ProactiveScheduler()._next_candidate()

    assert candidate is not None
    assert candidate["plan_id"] == plan.id
