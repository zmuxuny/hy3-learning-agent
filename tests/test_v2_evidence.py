import json

import pytest
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Artifact, EvidenceObservation, LearningEvent, TaskSubmission
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.services.evidence import (
    append_observation,
    artifact_ref,
    audit_observations,
    backfill_legacy_observations,
    build_evidence_state,
    create_artifact,
    list_observations,
    observation_dict,
)
from app.tools import ToolContext, execute_tool


def _plan(title: str) -> PlanCreate:
    return PlanCreate(
        title=title,
        goal="建立可验证的 Python 基础",
        current_level="零基础",
        expected_outcome="能够解释并提交一个小练习",
        stages=[StageCreate(title="第一阶段", tasks=[TaskCreate(
            title="完成并提交练习",
            is_core=True,
            evidence_required=True,
        )])],
    )


@pytest.mark.asyncio
async def test_evidence_is_idempotent_and_projection_is_deterministic():
    async with AsyncSessionLocal() as db:
        first, created = await append_observation(
            db,
            owner_id="local",
            source_type="manual",
            source_id="attempt-1",
            outcome="submitted",
            idempotency_key="evidence-test:attempt-1",
            plan_id=None,
            task_id=None,
            payload={"note": "一次自述"},
        )
        replay, replay_created = await append_observation(
            db,
            owner_id="local",
            source_type="manual",
            source_id="attempt-1",
            outcome="submitted",
            idempotency_key="evidence-test:attempt-1",
            plan_id=None,
            task_id=None,
            payload={"note": "一次自述"},
        )
        with pytest.raises(ValueError, match="Evidence idempotency conflict"):
            await append_observation(
                db,
                owner_id="local",
                source_type="manual",
                source_id="attempt-1",
                outcome="passed",
                idempotency_key="evidence-test:attempt-1",
                plan_id=None,
                task_id=None,
                payload={"note": "重试不应覆盖"},
            )
        await db.commit()

        assert created is True
        assert replay_created is False
        assert replay.id == first.id
        assert replay.outcome == "submitted"

        records = list((await db.execute(
            select(EvidenceObservation).where(EvidenceObservation.owner_id == "local")
        )).scalars())
        projection = build_evidence_state(records)
        assert projection["observation_count"] == 1
        assert projection["digest"] == build_evidence_state(records)["digest"]
        assert projection["by_task"] == []


@pytest.mark.asyncio
async def test_learning_loop_dual_writes_evidence_and_study_state():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", _plan("Evidence loop"))
        task = plan.stages[0].tasks[0]
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="完成提交")
        db.add(run)
        await db.commit()
        ctx = ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            trigger="user_message",
            plan_id=plan.id,
            session_id=None,
        )

        submitted = await execute_tool(
            "submission_create",
            json.dumps({"task_id": task.id, "content": "print('hello')", "submission_type": "code"}),
            ctx,
        )
        assert submitted["ok"] is True
        submission_id = submitted["data"]["submission_id"]

        checked = await execute_tool(
            "submission_check",
            json.dumps({
                "submission_id": submission_id,
                "score": 86,
                "feedback": "能运行并能解释关键步骤",
                "checks": [{"name": "运行结果", "passed": True}],
            }),
            ctx,
        )
        assert checked["ok"] is True
        assert checked["data"]["task_status"] == "completed"

        observations = await list_observations(
            db,
            "local",
            plan_id=plan.id,
            limit=None,
        )
        serialized = [observation_dict(item) for item in observations]
        # submission_create records the attempt; submission_check records one
        # verdict. Task completion is only a business projection and must not
        # add a third, independently weighted success observation.
        assert len(serialized) == 2
        assert sorted(item["outcome"] for item in serialized) == ["accepted", "submitted"]
        assert sum(item["counts_as_success"] for item in serialized) == 1
        artifacts = list((await db.execute(
            select(Artifact).where(Artifact.plan_id == plan.id)
        )).scalars())
        assert {item.artifact_type for item in artifacts} == {"submission"}
        artifact_ids = {item.id for item in artifacts}
        assert all(
            ref["artifact_id"] in artifact_ids
            for item in serialized
            for ref in item["artifact_refs"]
        )

        state = await execute_tool("study_state_get", json.dumps({"plan_id": plan.id}), ctx)
        assert state["ok"] is True
        evidence_state = state["data"]["evidence_state"]
        assert evidence_state["observation_count"] == 2
        assert evidence_state["by_task"][0]["success_count"] == 1
        assert evidence_state["by_task"][0]["task_id"] == task.id
        assert evidence_state["by_task"][0]["evidence_stage"] == "demonstrated"
        assert evidence_state["by_task"][0]["best_score"] == pytest.approx(0.86)
        assert any("不等同于掌握度概率" in caveat for caveat in evidence_state["caveats"])


@pytest.mark.asyncio
async def test_evidence_projection_isolated_by_plan_and_supersession_is_append_only():
    async with AsyncSessionLocal() as db:
        plan_a = await plan_service.create_plan(db, "local", _plan("Plan A"))
        plan_b = await plan_service.create_plan(db, "local", _plan("Plan B"))
        task_a = plan_a.stages[0].tasks[0]
        first, _ = await append_observation(
            db,
            owner_id="local",
            source_type="self_report",
            source_id="a-1",
            outcome="submitted",
            idempotency_key="evidence-test:a-1",
            plan_id=plan_a.id,
            task_id=task_a.id,
        )
        second, _ = await append_observation(
            db,
            owner_id="local",
            source_type="manual_assessment",
            source_id="a-2",
            outcome="verified",
            idempotency_key="evidence-test:a-2",
            plan_id=plan_a.id,
            task_id=task_a.id,
            normalized_score=0.9,
            evaluator={"type": "human", "id": "mentor-a"},
            fact_kind="amendment",
            target_observation_id=first.id,
            reason_code="MANUAL_REASSESSMENT",
        )
        await append_observation(
            db,
            owner_id="local",
            source_type="quiz",
            source_id="b-1",
            outcome="passed",
            idempotency_key="evidence-test:b-1",
            plan_id=plan_b.id,
            task_id=plan_b.stages[0].tasks[0].id,
            normalized_score=1.0,
        )
        await db.commit()

        records_a = list((await db.execute(
            select(EvidenceObservation).where(
                EvidenceObservation.owner_id == "local",
                EvidenceObservation.plan_id == plan_a.id,
            )
        )).scalars())
        projection = build_evidence_state(records_a)
        assert projection["observation_count"] == 1
        assert projection["by_task"][0]["evidence_ids"] == [second.id]
        assert projection["by_task"][0]["evidence_stage"] == "demonstrated"


@pytest.mark.asyncio
async def test_v1_backfill_is_conservative_and_repeatable():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", _plan("Legacy evidence"))
        task = plan.stages[0].tasks[0]
        submission = TaskSubmission(
            owner_id="local",
            plan_id=plan.id,
            task_id=task.id,
            submission_type="text",
            content="legacy answer",
            status="accepted",
            score=75,
            feedback="good",
        )
        db.add(submission)
        await db.flush()
        db.add(LearningEvent(
            owner_id="local",
            plan_id=plan.id,
            task_id=task.id,
            event_type="task.updated",
            payload={
                "after": {"status": "completed"},
                "evidence": [{"kind": "manual", "value": "legacy"}],
            },
        ))
        await db.commit()

        first = await backfill_legacy_observations(db, "local", plan_id=plan.id)
        second = await backfill_legacy_observations(db, "local", plan_id=plan.id)
        assert first == {"created": 3, "skipped": 0}
        assert second == {"created": 0, "skipped": 3}

        records = list((await db.execute(
            select(EvidenceObservation).where(EvidenceObservation.plan_id == plan.id)
        )).scalars())
        assert {record.source_type for record in records} == {"submission", "task_completion"}
        assert all(record.payload.get("backfilled") is True for record in records)


@pytest.mark.asyncio
async def test_artifact_audit_validates_source_existence_and_hash():
    async with AsyncSessionLocal() as db:
        artifact, _ = await create_artifact(
            db,
            owner_id="local",
            artifact_type="submission",
            source_uri="submission:fixture",
            idempotency_key="fixture:artifact",
            content="answer",
            metadata={"submission_type": "text"},
        )
        observation, _ = await append_observation(
            db,
            owner_id="local",
            source_type="submission",
            source_id="fixture",
            outcome="submitted",
            idempotency_key="fixture:observation",
            artifact_refs=[artifact_ref(artifact, kind="submission")],
        )
        with pytest.raises(ValueError, match="content hash mismatch"):
            await append_observation(
                db,
                owner_id="local",
                source_type="submission",
                source_id="fixture-mismatch",
                outcome="submitted",
                idempotency_key="fixture:observation-mismatch",
                artifact_refs=[
                    {
                        **artifact_ref(artifact, kind="submission"),
                        "content_hash": "f" * 64,
                    }
                ],
            )
        await db.commit()
        records = await list_observations(db, "local", limit=None)
        assert [item.id for item in records] == [observation.id]
        report = audit_observations(records, [artifact])
        assert report["ok"] is True
        missing = audit_observations(records, [])
        assert any("missing artifact" in error for error in missing["errors"])
