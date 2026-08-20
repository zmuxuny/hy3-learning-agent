from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.api.agent import enqueue_message
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage, Notification, Plan, QueuedMessage, Session
from app.notifications.service import NotificationService
from app.schemas import QueuedMessageCreate
from app.services.queue import dispatch_queued_message


def _require_setup(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"H5 reply queue setup failed: {message}")


def _reply_target(value) -> str | None:
    typed = getattr(value, "reply_to_intervention_id", None)
    metadata = getattr(value, "message_metadata", None) or {}
    metadata_target = metadata.get("reply_to_intervention_id")
    if typed is not None and metadata_target is not None and str(typed) != str(metadata_target):
        return "__conflicting_typed_and_metadata_targets__"
    target = typed if typed is not None else metadata_target
    return str(target) if target is not None else None


@pytest.mark.asyncio
async def test_reply_target_survives_queue_restart_dispatch_without_polluting_next_message() -> None:
    async with AsyncSessionLocal() as setup_db:
        plan = Plan(
            owner_id="local",
            title="Reply target queue plan",
            goal="Preserve one exact Intervention target",
            status="active",
        )
        session = Session(owner_id="local", title="Reply target queue session")
        setup_db.add_all([plan, session])
        await setup_db.flush()
        session.plan_id = plan.id
        active_run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="heartbeat",
            objective="Keep the scope busy while the user replies",
            status="running",
            phase="awaiting_model",
        )
        setup_db.add(active_run)
        await setup_db.commit()

        sent = await NotificationService(setup_db).send(
            owner_id="local",
            run_id=active_run.id,
            session_id=session.id,
            trigger="manual_heartbeat",
            title="Reply to this exact reminder",
            body="H5 queue target sentinel",
            plan_id=plan.id,
            channels=["in_app"],
        )
        await setup_db.commit()
        delivery = (
            await setup_db.execute(
                select(Notification)
                .where(Notification.run_id == active_run.id)
                .order_by(Notification.id.desc())
                .limit(1)
            )
        ).scalars().one()
        intervention_id = (
            sent.get("intervention_id")
            or getattr(delivery, "intervention_id", None)
            or delivery.reply_token
        )
        _require_setup(bool(intervention_id), "fixture has no stable identity placeholder")

        reply_payload = QueuedMessageCreate.model_validate(
            {
                "objective": "This answer belongs only to the reminder.",
                "session_id": session.id,
                "plan_id": plan.id,
                "reply_to_intervention_id": str(intervention_id),
            }
        )
        ordinary_payload = QueuedMessageCreate.model_validate(
            {
                "objective": "This later message is an ordinary turn.",
                "session_id": session.id,
                "plan_id": plan.id,
            }
        )
        reply_item = await enqueue_message(reply_payload, setup_db)
        ordinary_item = await enqueue_message(ordinary_payload, setup_db)
        reply_item_id = reply_item.id
        ordinary_item_id = ordinary_item.id
        active_run_id = active_run.id
        session_id = session.id
        plan_id = plan.id

    # Reopen the database before dispatch: an in-memory/UI-only target cannot pass this boundary.
    async with AsyncSessionLocal() as dispatch_db:
        active_run = await dispatch_db.get(AgentRun, active_run_id)
        _require_setup(active_run is not None, "active Run disappeared before dispatch")
        active_run.status = "completed"
        active_run.phase = "terminal"
        active_run.completed_at = datetime.now(timezone.utc)
        await dispatch_db.commit()

        reply_item = await dispatch_db.get(QueuedMessage, reply_item_id)
        _require_setup(reply_item is not None, "reply queue item did not survive reopen")
        reopened_target = _reply_target(reply_item)
        reply_run = await dispatch_queued_message(
            dispatch_db,
            reply_item,
            owner_id="local",
            expected_version=reply_item.version,
        )
        await dispatch_db.commit()
        reply_run_id = reply_run.id

        reply_run.status = "completed"
        reply_run.phase = "terminal"
        reply_run.completed_at = datetime.now(timezone.utc)
        await dispatch_db.commit()

        ordinary_item = await dispatch_db.get(QueuedMessage, ordinary_item_id)
        _require_setup(ordinary_item is not None, "ordinary queue item disappeared")
        ordinary_run = await dispatch_queued_message(
            dispatch_db,
            ordinary_item,
            owner_id="local",
            expected_version=ordinary_item.version,
        )
        await dispatch_db.commit()
        ordinary_run_id = ordinary_run.id

    async with AsyncSessionLocal() as reopened:
        reply_run = await reopened.get(AgentRun, reply_run_id)
        ordinary_run = await reopened.get(AgentRun, ordinary_run_id)
        reply_message = (
            await reopened.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.run_id == reply_run_id,
                    ChatMessage.role == "user",
                )
            )
        ).scalars().one()
        ordinary_message = (
            await reopened.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.run_id == ordinary_run_id,
                    ChatMessage.role == "user",
                )
            )
        ).scalars().one()

    _require_setup(reply_run is not None and ordinary_run is not None, "successor Run missing")
    failures: list[str] = []
    expected_target = str(intervention_id)
    if sent.get("intervention_id") is None or getattr(delivery, "intervention_id", None) is None:
        failures.append("notification_path_has_no_real_intervention_identity")
    if reopened_target != expected_target:
        failures.append(f"queue_reopen_lost_target:{reopened_target}")
    if _reply_target(reply_run) != expected_target:
        failures.append(f"successor_run_lost_target:{_reply_target(reply_run)}")
    if _reply_target(reply_message) != expected_target:
        failures.append(f"successor_message_lost_target:{_reply_target(reply_message)}")
    if _reply_target(ordinary_run) is not None:
        failures.append("ordinary_successor_run_inherited_stale_target")
    if _reply_target(ordinary_message) is not None:
        failures.append("ordinary_message_inherited_stale_target")
    if reply_run.plan_id != plan_id or ordinary_run.plan_id != plan_id:
        failures.append("queue_dispatch_changed_plan_scope")

    assert failures == []
