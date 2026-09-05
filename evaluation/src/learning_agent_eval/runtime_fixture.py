"""Shared isolated fixture construction and emulated delivery; no execution entrypoint."""

from __future__ import annotations

import json
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from .delivery import RecordingDeliverySink


class WorkerFailure(RuntimeError):
    def __init__(self, code: str, stage: str, message: str):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.public_message = message


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkerFailure(
            "invalid_document", "load", "worker input must be an object"
        )
    return value


async def _seed_fixture(fixture: dict[str, Any], frozen: datetime) -> None:
    models = import_module("app.models")
    database = import_module("app.db.database")
    proactive = import_module("app.runtime.proactive")
    settings = import_module("app.core.config").settings
    owner_id = fixture["owner_id"]
    kind = fixture["seed_kind"]
    async with database.AsyncSessionLocal() as db:
        owner = models.Owner(
            id=owner_id,
            display_name=f"Synthetic {kind} learner",
            timezone=fixture["timezone"],
        )
        db.add(owner)
        await db.flush()
        profile = models.UserProfile(
            owner_id=owner_id,
            quiet_hours={
                "start": fixture["seed"].get("quiet_start", "23:00"),
                "end": fixture["seed"].get("quiet_end", "08:00"),
            },
            preferences={
                "notification_cooldown_minutes": int(
                    fixture["seed"].get("notification_cooldown_minutes", 180)
                )
            },
            daily_notification_limit=int(
                fixture["seed"].get("daily_notification_limit", 3)
            ),
        )
        db.add(profile)
        await db.flush()
        plan = None
        task = None
        if kind in {"intervention", "assessment", "revision"}:
            plan = models.Plan(
                owner_id=owner_id,
                title=fixture["seed"]["plan_title"],
                description=fixture["seed"].get("plan_description", "Fully synthetic E1 fixture plan."),
                goal=fixture["seed"].get("goal", "Exercise one isolated production Runtime decision."),
                current_level=fixture["seed"].get("current_level", "synthetic baseline"),
                weekly_minutes=int(fixture["seed"].get("weekly_minutes", 180)),
                expected_outcome=fixture["seed"].get("expected_outcome", "A deterministic public engineering artifact."),
                status="active",
                version=int(fixture["seed"].get("plan_version", 1)),
            )
            db.add(plan)
            await db.flush()
        if kind == "assessment":
            stage = models.Stage(
                plan_id=plan.id,
                title="Synthetic assessment stage",
                description="Public synthetic data only.",
                objectives=["Assess declared evidence"],
                position=0,
                status="active",
            )
            db.add(stage)
            await db.flush()
            task = models.Task(
                stage_id=stage.id,
                title=fixture["seed"]["task_title"],
                description="Require a measured baseline.",
                status="pending",
                is_core=True,
                evidence_required=True,
                estimated_minutes=45,
                position=0,
            )
            db.add(task)
            await db.flush()
            db.add(
                models.TaskSubmission(
                    owner_id=owner_id,
                    plan_id=plan.id,
                    task_id=task.id,
                    submission_type="text",
                    content=fixture["seed"].get(
                        "submission_content",
                        "Synthetic conclusion without a measured baseline.",
                    ),
                    artifacts=[],
                    status=fixture["seed"].get("submission_status", "submitted"),
                )
            )
        for stage_data in fixture["seed"].get("additional_stages", []):
            extra_stage = models.Stage(
                plan_id=plan.id,
                title=stage_data["title"],
                description="Public synthetic multi-entity binding fixture.",
                objectives=["Keep semantic identities distinct"],
                position=int(stage_data["position"]),
                status="pending",
            )
            db.add(extra_stage)
            await db.flush()
            for task_data in stage_data.get("tasks", []):
                db.add(
                    models.Task(
                        stage_id=extra_stage.id,
                        title=task_data["title"],
                        description="Synthetic distractor task.",
                        status="pending",
                        is_core=False,
                        evidence_required=False,
                        estimated_minutes=15,
                        position=int(task_data["position"]),
                    )
                )
        session = None
        if fixture["session_id"] is not None:
            session = models.Session(
                id=fixture["session_id"],
                owner_id=owner_id,
                plan_id=plan.id if plan is not None else None,
                title=f"E1 {kind} fixture session",
                summary="Public synthetic evaluation session.",
            )
            db.add(session)
            await db.flush()
        runtime_trigger = {
            "planning": "user_message",
            "intervention": "heartbeat",
            "assessment": "submission",
            "revision": "constraint_change",
        }[kind]
        run = models.AgentRun(
            id=fixture["run_id"],
            owner_id=owner_id,
            session_id=session.id if session is not None else None,
            plan_id=plan.id if plan is not None else None,
            trigger=runtime_trigger,
            objective=fixture["trigger"]["objective"],
            status="queued",
            phase="not_started",
            model=settings.MODEL_NAME,
        )
        if kind == "intervention":
            proactive.capture_proactive_candidate(
                run,
                {
                    "candidate_key": "candidate:e1:i:001",
                    "candidate_kind": "study_reminder",
                    "candidate_payload": {"reason": "synthetic_due_window"},
                },
                detected_at=frozen,
            )
        db.add(run)
        await db.flush()
        if kind == "planning":
            db.add(
                models.PlanningIntake(
                    session_id=session.id,
                    owner_id=owner_id,
                    source_run_id=run.id,
                    goal=fixture["trigger"]["objective"],
                    confirmed_facts=[
                        {"key": "weekly_minutes", "value": str(fixture["seed"].get("weekly_minutes", 240)), "source": "user"}
                    ],
                    open_questions=[],
                    readiness=fixture["seed"].get("planning_readiness", "ready"),
                    readiness_confidence=1.0,
                    rationale="All synthetic constraints are explicit.",
                )
            )
        await db.commit()


async def _drain_recording_sink(sink: RecordingDeliverySink) -> dict[str, Any] | None:
    database = import_module("app.db.database")
    outbox = import_module("app.outbox")
    first_action_key = None
    for _ in range(20):
        result = await outbox.dispatch_once(
            session_factory=database.AsyncSessionLocal,
            delivery_adapter=sink,
        )
        if result["status"] == "idle":
            break
        if result["status"] != "delivered":
            raise WorkerFailure(
                "outbox_not_delivered",
                "delivery",
                "recording delivery did not reach a terminal delivered state",
            )
        first_action_key = first_action_key or result["action_key"]
    else:
        raise WorkerFailure(
            "outbox_not_idle", "delivery", "outbox drain exceeded its bound"
        )
    if first_action_key is None:
        return None
    attempts_before = list(sink.attempts)
    calls_before = sink.delivery_calls
    replay = await outbox.dispatch_action(
        action_key=first_action_key,
        session_factory=database.AsyncSessionLocal,
        delivery_adapter=sink,
        wait_for_active_seconds=0,
    )
    if (not replay.get("replayed") or sink.attempts != attempts_before
            or sink.delivery_calls != calls_before):
        raise WorkerFailure(
            "outbox_replay_failed", "delivery", "outbox replay was not idempotent"
        )
    return replay
