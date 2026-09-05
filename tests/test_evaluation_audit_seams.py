"""Product-side invariants required by evaluation, using disposable test DBs."""

import json

import pytest
from app.api.operations import undo_operation
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Operation, Plan, Stage, Task
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
async def test_task_patch_records_parent_effects_and_undo_restores_learning_state():
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="Task patch attribution", status="active", version=1)
        db.add(plan)
        await db.flush()
        stage = Stage(plan_id=plan.id, title="Stage", position=0, status="pending")
        db.add(stage)
        await db.flush()
        task = Task(stage_id=stage.id, title="Task", position=0, status="pending")
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="Start task")
        db.add_all([task, run])
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, plan_id=plan.id,
                          trigger="user_message", tool_call_id="audit-task-start")
        result = await execute_tool("task_patch", json.dumps({"task_id": task.id,
                                    "changes": {"status": "active"}, "reason": "User started"}), ctx)
        assert result["ok"], result
        operation = await db.get(Operation, result["data"]["operation_id"])
        assert operation.forward_patch["affected"] == {"plan_id": plan.id}
        assert operation.inverse_patch["plan"] == {"version": 1, "progress": 0.0}
        assert operation.forward_patch["plan"] == {"version": 2, "progress": 0.0}
        assert operation.forward_patch["entities"] == [{"stage_id": stage.id, "changes": {"status": "active"}}]
        await db.commit()
        await undo_operation(operation.id, db)
        assert task.status == stage.status == "pending"
        assert plan.version == 3


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
