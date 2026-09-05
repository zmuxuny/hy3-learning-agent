"""Product-side invariants required by evaluation, using disposable test DBs."""

import json

import pytest
from app.api.operations import undo_operation
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Plan, Stage
from app.notifications.service import NotificationService
from app.tools.base import ToolContext
from app.tools.registry import execute_tool
from learning_agent_eval.delivery import RecordingDeliverySink
from learning_agent_eval.runtime_fixture import _drain_recording_sink
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_stage_undo_restores_sibling_order():
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Ordering", status="active")
        db.add(plan)
        await db.flush()
        db.add_all(
            [Stage(plan_id=plan.id, title=f"Old {i}", position=i) for i in range(3)]
        )
        run = AgentRun(
            owner_id="local",
            plan_id=plan.id,
            trigger="user_message",
            objective="Add a stage",
        )
        db.add(run)
        await db.commit()
        ctx = ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            plan_id=plan.id,
            trigger="user_message",
            tool_call_id="audit-create-stage",
        )
        result = await execute_tool(
            "stage_create",
            json.dumps({"plan_id": plan.id, "title": "Inserted", "position": 1}),
            ctx,
        )
        assert result["ok"], result
        operation_id = result["data"]["operation_id"]
        await db.commit()
        await undo_operation(operation_id, db)
        stages = list(
            (
                await db.execute(
                    select(Stage)
                    .where(Stage.plan_id == plan.id)
                    .order_by(Stage.position)
                )
            ).scalars()
        )
        assert [(stage.title, stage.position) for stage in stages] == [
            (f"Old {i}", i) for i in range(3)
        ]


@pytest.mark.asyncio
async def test_drain_distinct_outbox_intents_and_verify_no_delivery_on_replay(
    monkeypatch,
):
    for key, value in {
        "SMTP_HOST": "smtp.invalid",
        "SMTP_USERNAME": "synthetic",
        "SMTP_PASSWORD": "synthetic",
        "SMTP_FROM": "sender@example.test",
        "SMTP_TO": "recipient@example.test",
    }.items():
        monkeypatch.setattr(settings, key, value)
    async with AsyncSessionLocal() as db:
        for index in range(2):
            await NotificationService(db).send(
                owner_id="local",
                run_id=None,
                session_id=None,
                trigger="user_message",
                title=f"Intent {index}",
                body="Public text",
                plan_id=None,
                channels=["email"],
            )
        await db.commit()
    sink = RecordingDeliverySink()
    replay = await _drain_recording_sink(sink)
    assert replay["replayed"]
    assert len(sink.attempts) == 2
    assert sink.delivery_calls == 2
