"""H5 real-process interruption tests for Intervention and mail protocols."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.migrations import migrate_sqlite_database
from app.context.memory import MemoryManager
from app.context.provenance import canonical_digest
from app.models import (
    AgentRun,
    ChatMessage,
    Intervention,
    Owner,
    Plan,
    QueuedMessage,
    Session,
)
from app.notifications.service import NotificationService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MARKER = "H5_PROCESS_RECOVERY_SENTINEL"


NOTIFICATION_KILL_PROGRAM = r"""
import asyncio
import json
import os
import signal
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.tools.registry as registry
from app.models import Intervention
from app.tools.base import ToolContext

async def main():
    database, run_id, session_id, plan_id, phase = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    original_commit = registry.commit_uow

    async def kill_at_domain_commit(db):
        count = int(
            await db.scalar(
                select(func.count(Intervention.id)).where(
                    Intervention.body == "H5_PROCESS_RECOVERY_SENTINEL"
                )
            )
            or 0
        )
        if count:
            if phase == "notification_before_commit":
                os.kill(os.getpid(), signal.SIGKILL)
            await original_commit(db)
            os.kill(os.getpid(), signal.SIGKILL)
        await original_commit(db)

    registry.commit_uow = kill_at_domain_commit
    async with factory() as db:
        result = await registry.execute_tool(
            "notification_send",
            json.dumps(
                {
                    "title": "Process recovery reminder",
                    "body": "H5_PROCESS_RECOVERY_SENTINEL",
                    "plan_id": int(plan_id),
                    "channels": ["in_app"],
                }
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="manual_heartbeat",
                plan_id=int(plan_id),
                session_id=session_id,
                tool_call_id="h5-process-notification",
            ),
        )
        raise AssertionError(f"kill boundary was not reached: {result!r}")

asyncio.run(main())
"""


NOTIFICATION_RECOVER_PROGRAM = r"""
import asyncio
import json
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.tools.base import ToolContext
from app.tools.registry import execute_tool

async def main():
    database, run_id, session_id, plan_id = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await execute_tool(
            "notification_send",
            json.dumps(
                {
                    "title": "Process recovery reminder",
                    "body": "H5_PROCESS_RECOVERY_SENTINEL",
                    "plan_id": int(plan_id),
                    "channels": ["in_app"],
                }
            ),
            ToolContext(
                db=db,
                owner_id="local",
                run_id=run_id,
                trigger="manual_heartbeat",
                plan_id=int(plan_id),
                session_id=session_id,
                tool_call_id="h5-process-notification",
            ),
        )
        assert result.get("ok") is True, result
    await engine.dispose()

asyncio.run(main())
"""


QUEUE_KILL_PROGRAM = r"""
import asyncio
import os
import signal
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.agent as agent_api
from app.models import QueuedMessage
from app.schemas import QueuedMessageCreate
from app.services.queue import dispatch_queued_message

async def main():
    database, phase, session_id, plan_id, intervention_id, queue_id = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        if phase.startswith("enqueue_"):
            original_commit = agent_api.commit_uow

            async def kill_at_enqueue_commit(inner_db):
                if phase == "enqueue_before_commit":
                    os.kill(os.getpid(), signal.SIGKILL)
                await original_commit(inner_db)
                os.kill(os.getpid(), signal.SIGKILL)

            agent_api.commit_uow = kill_at_enqueue_commit
            await agent_api.enqueue_message(
                QueuedMessageCreate(
                    objective="H5_PROCESS_RECOVERY_SENTINEL",
                    session_id=session_id,
                    plan_id=int(plan_id),
                    reply_to_intervention_id=intervention_id,
                ),
                db,
            )
        else:
            queued = await db.get(QueuedMessage, queue_id)
            assert queued is not None
            await dispatch_queued_message(
                db,
                queued,
                owner_id="local",
                expected_version=queued.version,
            )
            if phase == "dispatch_before_commit":
                os.kill(os.getpid(), signal.SIGKILL)
            await db.commit()
            os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("kill boundary was not reached")

asyncio.run(main())
"""


QUEUE_RECOVER_PROGRAM = r"""
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.agent import enqueue_message
from app.models import QueuedMessage
from app.schemas import QueuedMessageCreate
from app.services.queue import dispatch_queued_message

async def main():
    database, action, session_id, plan_id, intervention_id, queue_id = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        if action == "enqueue":
            existing = list(
                (
                    await db.execute(
                        select(QueuedMessage).where(
                            QueuedMessage.owner_id == "local",
                            QueuedMessage.session_id == session_id,
                            QueuedMessage.reply_to_intervention_id == intervention_id,
                            QueuedMessage.objective == "H5_PROCESS_RECOVERY_SENTINEL",
                        )
                    )
                ).scalars()
            )
            if not existing:
                await enqueue_message(
                    QueuedMessageCreate(
                        objective="H5_PROCESS_RECOVERY_SENTINEL",
                        session_id=session_id,
                        plan_id=int(plan_id),
                        reply_to_intervention_id=intervention_id,
                    ),
                    db,
                )
        else:
            queued = await db.get(QueuedMessage, queue_id)
            if queued is not None:
                await dispatch_queued_message(
                    db,
                    queued,
                    owner_id="local",
                    expected_version=queued.version,
                )
                await db.commit()
    await engine.dispose()

asyncio.run(main())
"""


MAIL_KILL_PROGRAM = r"""
import asyncio
import os
import signal
import sys
from datetime import timedelta as real_timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.notifications import email as email_module
from app.notifications.email import EmailReplyPoller

async def main():
    database, reply_token = sys.argv[1:]
    settings.ENABLE_EMAIL_REPLY_POLLING = True
    settings.IMAP_HOST = "imap.process.test"
    settings.IMAP_USERNAME = "process@example.invalid"
    settings.IMAP_PASSWORD = "test-only"
    settings.IMAP_FOLDER = "INBOX"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    poller = EmailReplyPoller()
    poller._fetch_unseen = lambda: [{
        "uid": "901",
        "uidvalidity": "77",
        "reply_token": reply_token,
        "subject": "Re: process recovery",
        "body": "H5_PROCESS_RECOVERY_SENTINEL",
    }]
    email_module.timedelta = lambda **_kwargs: real_timedelta(seconds=2)
    poller._mark_seen = lambda _uidvalidity, _uids: os.kill(os.getpid(), signal.SIGKILL)
    async with factory() as db:
        await poller.poll(db, "local")
    raise AssertionError("mail kill boundary was not reached")

asyncio.run(main())
"""


MAIL_RECOVER_PROGRAM = r"""
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.notifications.email import EmailReplyPoller

async def main():
    database, reply_token, ack_marker = sys.argv[1:]
    settings.ENABLE_EMAIL_REPLY_POLLING = True
    settings.IMAP_HOST = "imap.process.test"
    settings.IMAP_USERNAME = "process@example.invalid"
    settings.IMAP_PASSWORD = "test-only"
    settings.IMAP_FOLDER = "INBOX"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    poller = EmailReplyPoller()
    poller._fetch_unseen = lambda: [{
        "uid": "901",
        "uidvalidity": "77",
        "reply_token": reply_token,
        "subject": "Re: process recovery",
        "body": "H5_PROCESS_RECOVERY_SENTINEL",
    }]

    def acknowledge(uidvalidity, uids):
        assert uidvalidity == 77 and uids == ["901"]
        with Path(ack_marker).open("a", encoding="ascii") as handle:
            handle.write("ACK\n")

    poller._mark_seen = acknowledge
    async with factory() as db:
        await poller.poll(db, "local")
    await engine.dispose()

asyncio.run(main())
"""


EDIT_KILL_PROGRAM = r"""
import asyncio
import os
import signal
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.agent as agent_api
from app.schemas import MessageEdit

async def main():
    database, message_id, phase = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    original_commit = agent_api.commit_uow

    async def kill_at_edit_commit(db):
        if phase == "edit_before_commit":
            os.kill(os.getpid(), signal.SIGKILL)
        await original_commit(db)
        os.kill(os.getpid(), signal.SIGKILL)

    agent_api.commit_uow = kill_at_edit_commit
    async with factory() as db:
        await agent_api.edit_user_message(
            int(message_id),
            MessageEdit(content="H5_PROCESS_RECOVERY_SENTINEL"),
            db,
        )
    raise AssertionError("edit kill boundary was not reached")

asyncio.run(main())
"""


EDIT_RECOVER_PROGRAM = r"""
import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.agent as agent_api
from app.schemas import MessageEdit

async def main():
    database, message_id = sys.argv[1:]
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    agent_api._start_runtime = lambda _run_id: None
    async with factory() as db:
        run = await agent_api.edit_user_message(
            int(message_id),
            MessageEdit(content="H5_PROCESS_RECOVERY_SENTINEL"),
            db,
        )
        print(run.id, flush=True)
    await engine.dispose()

asyncio.run(main())
"""


def _environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "backend")}


def _run_child(program: str, *arguments: object, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", program, *(str(argument) for argument in arguments)],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


async def _seed_intervention_database(
    database: Path,
    *,
    email_delivery: bool = False,
    queued_reply: bool = False,
) -> dict[str, str | int]:
    migrate_sqlite_database(
        database,
        backup_root=database.parent / f"{database.stem}-backups",
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(Owner(id="local", display_name="H5 process fixture", timezone="UTC"))
        plan = Plan(owner_id="local", title="H5 process plan", status="active")
        session = Session(owner_id="local", title="H5 process Session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        source_run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            trigger="manual_heartbeat",
            objective="Create the process-recovery Intervention",
            status="completed",
            phase="terminal",
            completed_at=datetime.now(timezone.utc),
        )
        db.add(source_run)
        await db.flush()
        sent = await NotificationService(db).send(
            owner_id="local",
            run_id=source_run.id,
            session_id=session.id,
            trigger="manual_heartbeat",
            title="Process recovery source",
            body="Reply to the durable process-recovery Intervention",
            plan_id=plan.id,
            channels=["email"] if email_delivery else ["in_app"],
        )
        await db.commit()
        queue_id = ""
        if queued_reply:
            queued = QueuedMessage(
                owner_id="local",
                session_id=session.id,
                plan_id=plan.id,
                objective=MARKER,
                reply_to_intervention_id=str(sent["intervention_id"]),
                position=0,
            )
            db.add(queued)
            await db.commit()
            queue_id = queued.id
        identities = {
            "plan_id": plan.id,
            "session_id": session.id,
            "source_run_id": source_run.id,
            "intervention_id": str(sent["intervention_id"]),
            "queue_id": queue_id,
        }
        intervention = await db.get(
            Intervention,
            sent["intervention_id"],
        )
        assert intervention is not None
        identities["reply_token"] = intervention.reply_token
    await engine.dispose()
    return identities


async def _seed_edit_database(database: Path) -> dict[str, str | int]:
    migrate_sqlite_database(
        database,
        backup_root=database.parent / f"{database.stem}-backups",
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(Owner(id="local", display_name="H5 edit fixture", timezone="UTC"))
        plan = Plan(owner_id="local", title="H5 edit plan", status="active")
        session = Session(owner_id="local", title="H5 edit Session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        source_run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            session_id=session.id,
            trigger="user_message",
            objective="Original message",
            status="completed",
            phase="terminal",
            completed_at=datetime.now(timezone.utc),
        )
        db.add(source_run)
        await db.flush()
        source = ChatMessage(
            session_id=session.id,
            run_id=source_run.id,
            role="user",
            content="Original message",
            version=1,
            content_hash=canonical_digest("Original message"),
        )
        downstream = ChatMessage(
            session_id=session.id,
            run_id=source_run.id,
            role="assistant",
            content="Derived answer that must become stale",
            version=1,
            content_hash=canonical_digest("Derived answer that must become stale"),
        )
        db.add_all([source, downstream])
        await db.flush()
        manager = MemoryManager(db)
        memory, reused = await manager.propose(
            "local",
            scope="session",
            scope_id=session.id,
            layer="semantic",
            content="Derived memory that must become stale",
            source_type="message",
            source_id=str(source.id),
        )
        assert reused is False
        memory = await manager.confirm("local", memory.id)
        await db.commit()
        identities = {
            "message_id": source.id,
            "downstream_id": downstream.id,
            "memory_id": memory.id,
            "session_id": session.id,
        }
    await engine.dispose()
    return identities


def _counts(database: Path) -> dict[str, int | str | None]:
    with sqlite3.connect(database) as connection:
        return {
            "interventions": connection.execute(
                "SELECT count(*) FROM interventions WHERE body=?", (MARKER,)
            ).fetchone()[0],
            "messages": connection.execute(
                "SELECT count(*) FROM chat_messages WHERE content=?", (MARKER,)
            ).fetchone()[0],
            "deliveries": connection.execute(
                "SELECT count(*) FROM notifications WHERE body=?", (MARKER,)
            ).fetchone()[0],
            "queue": connection.execute(
                "SELECT count(*) FROM queued_messages WHERE objective=?", (MARKER,)
            ).fetchone()[0],
            "reply_runs": connection.execute(
                "SELECT count(*) FROM agent_runs WHERE objective=?", (MARKER,)
            ).fetchone()[0],
            "reply_messages": connection.execute(
                "SELECT count(*) FROM chat_messages WHERE content=? AND role='user'",
                (MARKER,),
            ).fetchone()[0],
            "mail_runs": connection.execute(
                "SELECT count(*) FROM agent_runs WHERE trigger='email_reply'"
            ).fetchone()[0],
            "mail_jobs": connection.execute(
                "SELECT count(*) FROM inbound_mail_jobs WHERE uidvalidity=77 AND uid=901"
            ).fetchone()[0],
            "mail_ack_state": connection.execute(
                "SELECT ack_state FROM inbound_mail_jobs WHERE uidvalidity=77 AND uid=901"
            ).fetchone(),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "before_counts"),
    (
        ("notification_before_commit", (0, 0, 0)),
        ("notification_after_commit", (1, 1, 1)),
    ),
)
async def test_notification_sigkill_uow_replays_to_one_logical_intervention(
    tmp_path: Path,
    phase: str,
    before_counts: tuple[int, int, int],
) -> None:
    database = tmp_path / f"{phase}.sqlite3"
    identities = await _seed_intervention_database(database)
    process = _run_child(
        NOTIFICATION_KILL_PROGRAM,
        database,
        identities["source_run_id"],
        identities["session_id"],
        identities["plan_id"],
        phase,
    )
    assert process.returncode == -signal.SIGKILL, process.stderr
    observed = _counts(database)
    assert (
        observed["interventions"],
        observed["messages"],
        observed["deliveries"],
    ) == before_counts

    if phase == "notification_before_commit":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE tool_invocations SET claim_expires_at='2000-01-01T00:00:00+00:00' "
                "WHERE tool_call_id='h5-process-notification' AND status='running'"
            )
    recovered = _run_child(
        NOTIFICATION_RECOVER_PROGRAM,
        database,
        identities["source_run_id"],
        identities["session_id"],
        identities["plan_id"],
    )
    assert recovered.returncode == 0, recovered.stderr
    observed = _counts(database)
    assert (
        observed["interventions"],
        observed["messages"],
        observed["deliveries"],
    ) == (1, 1, 1)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM tool_invocations WHERE tool_call_id='h5-process-notification'"
        ).fetchone() == ("committed",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    (
        "enqueue_before_commit",
        "enqueue_after_commit",
        "dispatch_before_commit",
        "dispatch_after_commit",
    ),
)
async def test_reply_queue_sigkill_restart_preserves_exact_intervention_target(
    tmp_path: Path,
    phase: str,
) -> None:
    database = tmp_path / f"{phase}.sqlite3"
    identities = await _seed_intervention_database(
        database,
        queued_reply=phase.startswith("dispatch_"),
    )
    process = _run_child(
        QUEUE_KILL_PROGRAM,
        database,
        phase,
        identities["session_id"],
        identities["plan_id"],
        identities["intervention_id"],
        identities["queue_id"],
    )
    assert process.returncode == -signal.SIGKILL, process.stderr

    recovered = _run_child(
        QUEUE_RECOVER_PROGRAM,
        database,
        "enqueue" if phase.startswith("enqueue_") else "dispatch",
        identities["session_id"],
        identities["plan_id"],
        identities["intervention_id"],
        identities["queue_id"],
    )
    assert recovered.returncode == 0, recovered.stderr
    observed = _counts(database)
    if phase.startswith("enqueue_"):
        assert observed["queue"] == 1
        assert observed["reply_runs"] == 0
    else:
        assert observed["queue"] == 0
        assert observed["reply_runs"] == 1
        assert observed["reply_messages"] == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT reply_to_intervention_id FROM agent_runs WHERE objective=?",
                (MARKER,),
            ).fetchone() == (identities["intervention_id"],)


@pytest.mark.asyncio
async def test_mail_sigkill_after_job_commit_reclaims_ack_without_duplicate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mail-commit-before-store.sqlite3"
    identities = await _seed_intervention_database(database, email_delivery=True)
    process = _run_child(
        MAIL_KILL_PROGRAM,
        database,
        identities["reply_token"],
    )
    assert process.returncode == -signal.SIGKILL, process.stderr
    observed = _counts(database)
    assert observed["mail_jobs"] == 1
    assert observed["mail_ack_state"] == ("claimed",)
    assert observed["mail_runs"] == 1
    assert observed["reply_messages"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state, outcome FROM interventions WHERE id=?",
            (identities["intervention_id"],),
        ).fetchone() == ("replied", "reply_received")

    # The killed worker owned a two-second test lease. Wait for the real
    # wall-clock fence rather than bypassing the production ACK transition.
    time.sleep(2.2)
    ack_marker = tmp_path / "mail-acks.log"
    recovered = _run_child(
        MAIL_RECOVER_PROGRAM,
        database,
        identities["reply_token"],
        ack_marker,
    )
    assert recovered.returncode == 0, recovered.stderr
    observed = _counts(database)
    assert observed["mail_jobs"] == 1
    assert observed["mail_ack_state"] == ("acked",)
    assert observed["mail_runs"] == 1
    assert observed["reply_messages"] == 1
    assert ack_marker.read_text(encoding="ascii").splitlines() == ["ACK"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "before_shape"),
    (
        ("edit_before_commit", (0, "active", "valid", 0)),
        ("edit_after_commit", (1, "superseded", "invalid", 1)),
    ),
)
async def test_message_edit_sigkill_is_atomic_and_rerun_is_reclaimable(
    tmp_path: Path,
    phase: str,
    before_shape: tuple[int, str, str, int],
) -> None:
    from app.runtime.state import claim_run

    database = tmp_path / f"{phase}.sqlite3"
    identities = await _seed_edit_database(database)
    process = _run_child(
        EDIT_KILL_PROGRAM,
        database,
        identities["message_id"],
        phase,
    )
    assert process.returncode == -signal.SIGKILL, process.stderr

    with sqlite3.connect(database) as connection:
        observed_shape = (
            connection.execute(
                "SELECT count(*) FROM chat_message_revisions WHERE message_id=?",
                (identities["message_id"],),
            ).fetchone()[0],
            connection.execute(
                "SELECT validity_state FROM chat_messages WHERE id=?",
                (identities["downstream_id"],),
            ).fetchone()[0],
            connection.execute(
                "SELECT validity_state FROM memories WHERE id=?",
                (identities["memory_id"],),
            ).fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM agent_runs WHERE objective=?",
                (MARKER,),
            ).fetchone()[0],
        )
    assert observed_shape == before_shape

    recovered = _run_child(
        EDIT_RECOVER_PROGRAM,
        database,
        identities["message_id"],
    )
    assert recovered.returncode == 0, recovered.stderr
    recovered_run_id = recovered.stdout.strip()
    assert recovered_run_id
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM chat_message_revisions WHERE message_id=?",
            (identities["message_id"],),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT content, version, run_id FROM chat_messages WHERE id=?",
            (identities["message_id"],),
        ).fetchone() == (MARKER, 2, recovered_run_id)
        assert connection.execute(
            "SELECT validity_state FROM chat_messages WHERE id=?",
            (identities["downstream_id"],),
        ).fetchone() == ("superseded",)
        assert connection.execute(
            "SELECT validity_state FROM memories WHERE id=?",
            (identities["memory_id"],),
        ).fetchone() == ("invalid",)
        assert connection.execute(
            "SELECT count(*) FROM agent_runs WHERE objective=?",
            (MARKER,),
        ).fetchone() == (1,)

    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    lease = await claim_run(factory, recovered_run_id, worker_id="h5-edit-restart")
    await engine.dispose()
    assert lease is not None
