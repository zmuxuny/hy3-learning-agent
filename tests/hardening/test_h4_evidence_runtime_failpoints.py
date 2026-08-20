"""Runtime-level H4 Evidence recovery checks on the disposable pytest DB."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.operations import redo_operation, undo_operation
from app.db.database import AsyncSessionLocal
from app.models import (
    Achievement,
    ActivityDay,
    AgentRun,
    Operation,
    OperationEvidenceLink,
    Plan,
    Stage,
    Task,
    UserProfile,
)
from app.schemas.evidence import EvidenceObservationOutput
from app.services.evidence import (
    append_observation,
    artifact_ref,
    audit_observations,
    create_artifact,
    list_observations,
    observation_dict,
)
from app.tools import ToolContext, execute_tool


async def _context(db, title: str) -> tuple[ToolContext, Task]:
    task = Task(title=f"{title} task", evidence_required=True, is_core=True)
    plan = Plan(
        owner_id="local",
        title=title,
        goal="verify exact Evidence recovery",
        stages=[Stage(title="stage", tasks=[task])],
    )
    db.add(plan)
    await db.flush()
    run = AgentRun(
        owner_id="local",
        plan_id=plan.id,
        trigger="user_message",
        objective=title,
    )
    db.add(run)
    await db.commit()
    return (
        ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            trigger="user_message",
            plan_id=plan.id,
        ),
        task,
    )


async def _gamification_state(db) -> dict:
    profile = await db.get(UserProfile, "local")
    days = list((await db.execute(select(ActivityDay).order_by(ActivityDay.id))).scalars())
    achievements = list(
        (await db.execute(select(Achievement).order_by(Achievement.id))).scalars()
    )
    return {
        "profile": (profile.xp, profile.level, profile.streak_days),
        "days": [
            (item.id, item.date, item.xp, item.completed_tasks, item.passed_quizzes)
            for item in days
        ],
        "achievements": [
            (item.id, item.key, item.title, item.description, item.badge_kind)
            for item in achievements
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["submission", "quiz"])
async def test_redo_restores_complete_gamification_state_and_is_idempotent(case: str):
    async with AsyncSessionLocal() as db:
        ctx, task = await _context(db, f"H4 {case} recovery")
        before = await _gamification_state(db)
        if case == "submission":
            created = await execute_tool(
                "submission_create",
                json.dumps({"task_id": task.id, "submission_type": "code", "content": "print(1)"}),
                ctx,
            )
            assert created["ok"] is True
            changed = await execute_tool(
                "submission_check",
                json.dumps(
                    {
                        "submission_id": created["data"]["submission_id"],
                        "score": 90,
                        "feedback": "passed",
                        "checks": [{"name": "run", "passed": True}],
                    }
                ),
                ctx,
            )
        else:
            created = await execute_tool(
                "quiz_create",
                json.dumps(
                    {
                        "plan_id": ctx.plan_id,
                        "task_id": task.id,
                        "prompt": "Explain the loop",
                        "rubric": {"threshold": 70},
                    }
                ),
                ctx,
            )
            assert created["ok"] is True
            changed = await execute_tool(
                "quiz_grade",
                json.dumps(
                    {
                        "quiz_id": created["data"]["quiz_id"],
                        "answer": "The loop schedules ready work.",
                        "score": 90,
                        "feedback": "passed",
                        "evidence": [{"criterion": "schedule", "passed": True}],
                    }
                ),
                ctx,
            )
        assert changed["ok"] is True
        operation_id = changed["data"]["operation_id"]
        committed = await _gamification_state(db)

        undone = await undo_operation(operation_id, db)
        assert undone.status == "undone"
        assert await _gamification_state(db) == before

        redone = await redo_operation(operation_id, db)
        assert redone.status == "committed"
        assert await _gamification_state(db) == committed
        replay = await redo_operation(operation_id, db)
        assert replay.status == "committed"
        assert await _gamification_state(db) == committed

        await undo_operation(operation_id, db)
        assert await _gamification_state(db) == before
        links = list(
            (
                await db.execute(
                    select(OperationEvidenceLink)
                    .where(OperationEvidenceLink.operation_id == operation_id)
                    .order_by(OperationEvidenceLink.generation, OperationEvidenceLink.id)
                )
            ).scalars()
        )
        assert [(item.generation, item.role) for item in links] == [
            (0, "produced"),
            (0, "invalidation"),
            (1, "produced"),
            (1, "invalidation"),
        ]


@pytest.mark.asyncio
async def test_legacy_ambiguous_redo_fails_closed_without_changing_operation():
    async with AsyncSessionLocal() as db:
        operation = Operation(
            owner_id="local",
            tool_name="legacy.task",
            entity_type="task",
            entity_id="999",
            forward_patch={"changes": {"status": "completed"}},
            inverse_patch={"changes": {"status": "pending"}},
            status="undone",
        )
        db.add(operation)
        await db.commit()
        operation_id = operation.id

        with pytest.raises(HTTPException) as failure:
            await redo_operation(operation_id, db)
        assert failure.value.status_code == 409
        current = await db.get(Operation, operation_id)
        assert current is not None and current.status == "undone"


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["activity_day", "achievement"])
async def test_redo_identity_reuse_returns_409_without_partial_generation(collision: str):
    async with AsyncSessionLocal() as db:
        ctx, task = await _context(db, f"H4 {collision} collision")
        created = await execute_tool(
            "submission_create",
            json.dumps({"task_id": task.id, "submission_type": "code", "content": "print(2)"}),
            ctx,
        )
        changed = await execute_tool(
            "submission_check",
            json.dumps(
                {
                    "submission_id": created["data"]["submission_id"],
                    "score": 95,
                    "feedback": "passed",
                    "checks": [{"name": "run", "passed": True}],
                }
            ),
            ctx,
        )
        operation_id = changed["data"]["operation_id"]
        committed_day = await db.scalar(select(ActivityDay).order_by(ActivityDay.id))
        committed_achievement = await db.scalar(select(Achievement).order_by(Achievement.id))
        assert committed_day is not None and committed_achievement is not None
        day_date = committed_day.date
        achievement_key = committed_achievement.key
        await undo_operation(operation_id, db)

        if collision == "activity_day":
            db.add(ActivityDay(owner_id="local", date="1999-01-01"))
            await db.flush()
            db.add(ActivityDay(owner_id="local", date=day_date))
        else:
            db.add(
                Achievement(
                    owner_id="local",
                    key=achievement_key,
                    title="conflicting later achievement",
                    description="not the original semantic fact",
                )
            )
        await db.commit()

        with pytest.raises(HTTPException) as failure:
            await redo_operation(operation_id, db)
        assert failure.value.status_code == 409
        current = await db.get(Operation, operation_id)
        assert current is not None and current.status == "undone"
        links = list(
            (
                await db.execute(
                    select(OperationEvidenceLink).where(
                        OperationEvidenceLink.operation_id == operation_id
                    )
                )
            ).scalars()
        )
        assert {(item.generation, item.role) for item in links} == {
            (0, "produced"),
            (0, "invalidation"),
        }


@pytest.mark.asyncio
async def test_artifact_audit_verifies_bytes_envelope_and_public_contract():
    async with AsyncSessionLocal() as db:
        ctx, task = await _context(db, "H4 Artifact audit")
        artifact, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="task_evidence",
            source_uri="memory://h4/audit",
            idempotency_key="h4:audit:artifact",
            content=b"verified bytes",
            metadata={"kind": "repository", "revision": 1},
            plan_id=ctx.plan_id,
            task_id=task.id,
            run_id=ctx.run_id,
        )
        await append_observation(
            db,
            owner_id="local",
            source_type="task_evidence",
            source_id="h4:audit:observation",
            outcome="verified",
            idempotency_key="h4:audit:observation",
            run_id=ctx.run_id,
            plan_id=ctx.plan_id,
            task_id=task.id,
            evaluator={"type": "deterministic", "custom": "preserved"},
            payload={"evidence": [{"kind": "repository", "verified": True}]},
            artifact_refs=[artifact_ref(artifact)],
        )
        await db.flush()
        observations = await list_observations(
            db,
            "local",
            plan_id=ctx.plan_id,
            limit=None,
        )

        assert audit_observations(observations, [artifact])["ok"] is True
        EvidenceObservationOutput.model_validate(observation_dict(observations[0]))

        artifact.task_id = task.id + 1
        cross_task = audit_observations(observations, [artifact])
        assert "EVIDENCE_ARTIFACT_CROSS_TASK" in cross_task["error_codes"]
        artifact.task_id = task.id

        original = artifact.snapshot_bytes
        artifact.snapshot_bytes = b"tampered bytes"
        tampered = audit_observations(observations, [artifact])
        assert "EVIDENCE_ARTIFACT_SNAPSHOT_HASH_MISMATCH" in tampered["error_codes"]

        artifact.snapshot_bytes = original
        artifact.storage_state = "external_reference"
        unavailable = audit_observations(observations, [artifact])
        assert "EVIDENCE_ARTIFACT_NOT_DURABLE" in unavailable["error_codes"]
