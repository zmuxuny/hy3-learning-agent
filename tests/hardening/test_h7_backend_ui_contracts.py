from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

from app.db.database import AsyncSessionLocal, get_db
from app.main import app
from app.models import Plan, Session, Stage, Task
from app.notifications.service import NotificationService
from app.services.competencies import create_competency, link_competency
from app.services.evidence import append_observation


@pytest.mark.asyncio
async def test_notification_open_returns_authoritative_intervention_identity() -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(
            owner_id="local",
            title="H7 intervention target",
            goal="Keep the reminder reply attached after a reload",
            status="active",
        )
        session = Session(owner_id="local", title="H7 intervention session")
        db.add_all([plan, session])
        await db.flush()
        session.plan_id = plan.id
        sent = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=session.id,
            trigger="manual_heartbeat",
            title="Durable reminder",
            body="Reply to this exact intervention.",
            plan_id=plan.id,
            channels=["in_app", "email"],
        )
        await db.commit()

        notification_id = sent["notifications"][0]["id"]
        intervention_id = sent["intervention_id"]

        async def override_database():
            yield db

        previous = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = override_database
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1",
            ) as client:
                response = await client.post(
                    f"/api/v1/notifications/{notification_id}/open"
                )
                listed_response = await client.get("/api/v1/notifications")
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = previous

        assert response.status_code == 200
        payload = response.json()
        assert payload["intervention_id"] == intervention_id
        assert payload["session_id"] == session.id
        assert payload["message_id"] == sent["canonical_message_id"]
        assert listed_response.status_code == 200
        listed = listed_response.json()
        assert len(listed) == 1
        assert listed[0]["intervention_id"] == intervention_id


@pytest.mark.asyncio
async def test_plan_learning_map_exposes_task_relations_and_evidence_sources() -> None:
    async with AsyncSessionLocal() as db:
        plan = Plan(
            owner_id="local",
            title="Explainable learning map",
            goal="Explain what a task trains and proves",
            status="active",
        )
        stage = Stage(title="Foundation", position=0)
        task = Task(title="Explain spaced retrieval", position=0)
        stage.tasks.append(task)
        plan.stages.append(stage)
        db.add(plan)
        await db.flush()

        competency, _ = await create_competency(
            db,
            "local",
            key="spaced-retrieval",
            title="Spaced retrieval",
            competency_type="concept",
            scope="plan",
            plan_id=plan.id,
            focused_plan_id=plan.id,
        )
        await link_competency(
            db,
            "local",
            competency_id=competency.id,
            task_id=task.id,
            relation="teaches",
            target_stage="practicing",
            focused_plan_id=plan.id,
        )
        await link_competency(
            db,
            "local",
            competency_id=competency.id,
            task_id=task.id,
            relation="assesses",
            target_stage="demonstrated",
            focused_plan_id=plan.id,
        )
        observation, _ = await append_observation(
            db,
            owner_id="local",
            source_type="self_report",
            source_id="h7-learning-map",
            outcome="completed",
            idempotency_key="h7-learning-map",
            plan_id=plan.id,
            task_id=task.id,
            normalized_score=0.8,
            payload={"feedback": "The learner connected spacing to retrieval strength."},
        )
        await db.commit()

        async def override_database():
            yield db

        previous = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = override_database
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1",
            ) as client:
                graph_response = await client.get(
                    f"/api/v1/plans/{plan.id}/competencies"
                )
                evidence_response = await client.get(
                    f"/api/v1/plans/{plan.id}/evidence-observations"
                )
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = previous

        assert graph_response.status_code == 200
        graph = graph_response.json()
        assert graph["competencies"] == [
            {
                "id": competency.id,
                "key": "spaced-retrieval",
                "title": "Spaced retrieval",
                "description": "",
                "type": "concept",
                "scope": "plan",
                "plan_id": plan.id,
                "status": "active",
                "version": 1,
            }
        ]
        assert {
            (item["task_id"], item["competency_id"], item["relation"])
            for item in graph["task_links"]
        } == {
            (task.id, competency.id, "teaches"),
            (task.id, competency.id, "assesses"),
        }

        assert evidence_response.status_code == 200
        observations = evidence_response.json()["observations"]
        assert [item["id"] for item in observations] == [observation.id]
        assert observations[0]["source_type"] == "self_report"
        assert observations[0]["task_id"] == task.id
        assert observations[0]["competency_refs"] == [
            {
                "competency_id": competency.id,
                "competency_key": "spaced-retrieval",
                "association_kind": "task_assesses",
                "task_competency_link_id_snapshot": next(
                    item["id"]
                    for item in graph["task_links"]
                    if item["relation"] == "assesses"
                ),
            }
        ]
