"""Submission verdicts must agree with their recorded mandatory checks."""

import json

import pytest
from app.api.operations import undo_operation
from app.db.database import AsyncSessionLocal
from app.models import (
    Achievement,
    ActivityDay,
    AgentRun,
    EvidenceObservation,
    Operation,
    TaskSubmission,
    UserProfile,
)
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool
from sqlalchemy import func, select


async def _submission(db):
    plan = await plan_service.create_plan(db, "local", PlanCreate(
        title="Directory statistics",
        goal="Produce correct directory statistics",
        current_level="Basic Python",
        expected_outcome="Correct results for ordinary and empty directories",
        stages=[StageCreate(title="Implementation", tasks=[TaskCreate(
            title="Submit directory statistics",
            is_core=True,
            evidence_required=True,
        )])],
    ))
    task = plan.stages[0].tasks[0]
    run = AgentRun(
        owner_id="local", plan_id=plan.id, trigger="user_message",
        objective="Check the submitted directory statistics",
    )
    db.add(run)
    await db.commit()
    ctx = ToolContext(
        db=db, owner_id="local", run_id=run.id, trigger="user_message",
        plan_id=plan.id,
    )
    created = await execute_tool("submission_create", json.dumps({
        "task_id": task.id,
        "submission_type": "text",
        "content": "ordinary directory: 3 files; empty directory: 1 file",
    }), ctx)
    assert created["ok"] is True
    return plan, task, ctx, created["data"]["submission_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [True, None], ids=["explicit", "legacy-default"])
async def test_high_score_cannot_override_a_required_failure(required):
    async with AsyncSessionLocal() as db:
        plan, task, ctx, submission_id = await _submission(db)
        profile = await db.get(UserProfile, "local")
        before_xp = profile.xp
        before_activity = await db.scalar(select(func.count(ActivityDay.id)))
        before_achievements = await db.scalar(select(func.count(Achievement.id)))
        checks = [{
            "name": "empty directory",
            "passed": False,
            "evidence": "Expected 0 files; observed 1 file",
        }]
        if required is not None:
            checks[0]["required"] = required
        result = await execute_tool("submission_check", json.dumps({
            "submission_id": submission_id,
            "score": 95,
            "feedback": "Retry an empty directory; the expected file count is 0.",
            "checks": checks,
        }), ctx)

        assert result["ok"] is True
        assert result["data"]["status"] == "revision_required"
        assert result["data"]["task_status"] == "active"
        assert result["data"]["score"] == 95
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 0
        assert refreshed.stages[0].tasks[0].completed_at is None
        await db.refresh(profile)
        assert profile.xp == before_xp
        assert await db.scalar(select(func.count(ActivityDay.id))) == before_activity
        assert await db.scalar(select(func.count(Achievement.id))) == before_achievements
        submission = await db.get(TaskSubmission, submission_id)
        assert submission.status == "revision_required"
        observation = (await db.execute(select(EvidenceObservation).where(
            EvidenceObservation.task_id == task.id,
            EvidenceObservation.source_id == f"{submission_id}:check",
        ))).scalars().one()
        assert observation.outcome == "needs_revision"
        assert observation.is_correct is False
        assert observation.counts_as_success is False
        assert observation.rubric_snapshot["checks"] == checks


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "checks", "accepted"), [
    pytest.param(95, [{"name": "style", "passed": False, "required": False}], True, id="optional-failure"),
    pytest.param(69, [{"name": "empty", "passed": True}], False, id="low-score"),
    pytest.param(70, [{"name": "empty", "passed": True}], True, id="threshold-equality"),
    pytest.param(95, [], True, id="empty-checks"),
    pytest.param(95, None, True, id="omitted-checks"),
    pytest.param(95, [{"criterion": "empty", "passed": True, "actual": 0, "expected": 0}], True, id="legacy-metadata"),
    pytest.param(95, [{"name": "empty"}], True, id="missing-result-compatible"),
    pytest.param(95, [{"name": "empty", "passed": None}], True, id="unknown-result-compatible"),
    pytest.param(95, [{"name": "empty", "passed": 0}], True, id="no-false-coercion"),
    pytest.param(95, [{"name": "empty", "passed": False, "required": 0}], False, id="no-optional-coercion"),
    pytest.param(95, [
        {"name": "style", "passed": False, "required": False},
        {"name": "empty", "passed": False},
    ], False, id="optional-does-not-hide-required"),
])
async def test_verdict_threshold_and_check_compatibility(score, checks, accepted):
    async with AsyncSessionLocal() as db:
        plan, _, ctx, submission_id = await _submission(db)
        arguments = {
            "submission_id": submission_id,
            "score": score,
            "feedback": "Observed checks are recorded with the score.",
        }
        if checks is not None:
            arguments["checks"] = checks
        result = await execute_tool("submission_check", json.dumps(arguments), ctx)
        assert result["ok"] is True
        assert result["data"]["status"] == ("accepted" if accepted else "revision_required")
        assert result["data"]["task_status"] == ("completed" if accepted else "active")
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == (1 if accepted else 0)


@pytest.mark.asyncio
async def test_failed_check_replay_and_undo_preserve_evidence_history():
    async with AsyncSessionLocal() as db:
        plan, task, ctx, submission_id = await _submission(db)
        arguments = json.dumps({
            "submission_id": submission_id,
            "score": 95,
            "feedback": "Retry empty directory; expected 0 files.",
            "checks": [{"name": "empty", "passed": False}],
        })
        first = await execute_tool("submission_check", arguments, ctx)
        replay = await execute_tool("submission_check", arguments, ctx)
        assert first["ok"] is True and replay["ok"] is True
        assert replay["data"]["operation_id"] == first["data"]["operation_id"]
        assert first["data"]["status"] == "revision_required"
        assert await db.scalar(select(func.count(Operation.id)).where(
            Operation.tool_name == "submission.check",
        )) == 1
        assert await db.scalar(select(func.count(EvidenceObservation.id)).where(
            EvidenceObservation.task_id == task.id,
        )) == 2

        operation = await undo_operation(first["data"]["operation_id"], db)
        assert operation.status == "undone"
        submission = await db.get(TaskSubmission, submission_id)
        assert submission.status == "submitted"
        assert submission.score is None
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 0
        assert refreshed.stages[0].tasks[0].status == "pending"
        observations = list((await db.execute(select(EvidenceObservation).where(
            EvidenceObservation.task_id == task.id,
        ))).scalars())
        assert len(observations) == 3
        assert sum(item.outcome == "needs_revision" for item in observations) == 1
        assert sum(item.fact_kind == "invalidation" for item in observations) == 1


@pytest.mark.asyncio
async def test_revised_submission_passes_without_erasing_failed_attempt():
    async with AsyncSessionLocal() as db:
        plan, task, ctx, first_id = await _submission(db)
        ctx.tool_call_id = "check-first-attempt"
        first = await execute_tool("submission_check", json.dumps({
            "submission_id": first_id,
            "score": 95,
            "feedback": "Retry empty directory; expected 0 files.",
            "checks": [{"name": "empty", "passed": False}],
        }), ctx)
        assert first["data"]["status"] == "revision_required"

        ctx.tool_call_id = "submit-revised-attempt"
        revised = await execute_tool("submission_create", json.dumps({
            "task_id": task.id,
            "content": "ordinary directory: 3 files; empty directory: 0 files",
        }), ctx)
        assert revised["ok"] is True
        revised_id = revised["data"]["submission_id"]
        ctx.tool_call_id = "check-revised-attempt"
        accepted = await execute_tool("submission_check", json.dumps({
            "submission_id": revised_id,
            "score": 95,
            "feedback": "Both directory results meet the requirements.",
            "checks": [{"name": "empty", "passed": True}],
        }), ctx)
        assert accepted["ok"] is True
        assert accepted["data"]["status"] == "accepted"
        assert revised_id != first_id
        original = await db.get(TaskSubmission, first_id)
        assert original.status == "revision_required"
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 1
        observations = list((await db.execute(select(EvidenceObservation).where(
            EvidenceObservation.task_id == task.id,
        ))).scalars())
        assert len(observations) == 4
        assert sum(item.outcome == "needs_revision" for item in observations) == 1
        assert sum(item.counts_as_success for item in observations) == 1


@pytest.mark.asyncio
async def test_failed_later_submission_does_not_demote_or_reward_completed_task():
    async with AsyncSessionLocal() as db:
        plan, task, ctx, first_id = await _submission(db)
        ctx.tool_call_id = "accept-original-attempt"
        first = await execute_tool("submission_check", json.dumps({
            "submission_id": first_id,
            "score": 95,
            "feedback": "Observed checks passed.",
            "checks": [{"name": "empty", "passed": True}],
        }), ctx)
        assert first["data"]["status"] == "accepted"
        profile = await db.get(UserProfile, "local")
        before_xp = profile.xp
        before_completed_at = task.completed_at
        before_achievements = await db.scalar(select(func.count(Achievement.id)))
        before_activity = await db.scalar(select(func.sum(ActivityDay.completed_tasks)))

        ctx.tool_call_id = "submit-later-attempt"
        later = await execute_tool("submission_create", json.dumps({
            "task_id": task.id,
            "content": "empty directory: 1 file",
        }), ctx)
        assert later["ok"] is True
        ctx.tool_call_id = "check-later-attempt"
        rejected = await execute_tool("submission_check", json.dumps({
            "submission_id": later["data"]["submission_id"],
            "score": 95,
            "feedback": "This later attempt failed; expected 0 files.",
            "checks": [{"name": "empty", "passed": False}],
        }), ctx)
        assert rejected["ok"] is True
        assert rejected["data"]["status"] == "revision_required"
        assert rejected["data"]["task_status"] == "completed"
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 1
        assert refreshed.stages[0].tasks[0].completed_at == before_completed_at
        await db.refresh(profile)
        assert profile.xp == before_xp
        assert await db.scalar(select(func.count(Achievement.id))) == before_achievements
        assert await db.scalar(select(func.sum(ActivityDay.completed_tasks))) == before_activity
