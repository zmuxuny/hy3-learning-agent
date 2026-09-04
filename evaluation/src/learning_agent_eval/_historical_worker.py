"""Private worker retained only for frozen E1/E2 regression fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from .canonical import canonical_json, canonical_json_bytes, sha256_digest
from .delivery import RecordingDeliverySink
from .deltas import DeltaConstructionError, build_state_delta
from .exporter import ExportError, build_decision_episode_v2
from .isolation import EvaluationIsolationError, IsolationGuard
from .models import RuntimeMiniFixture
from .normalizers import normalize_rfc3339
from .privacy import privacy_issues
from .recorder import EvaluationModelRecorder
from .resources import EvaluationSnapshotProvider
from .scripted_model import ScriptedModelClient
from .snapshots import (
    SnapshotCollectionError,
    audit_projection,
    collect_state_snapshot,
    identity_registry,
    normalize_reference_fields,
)
from .validator import validate_episode


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
                description="Fully synthetic E1 fixture plan.",
                goal="Exercise one isolated production Runtime decision.",
                current_level="synthetic baseline",
                weekly_minutes=int(fixture["seed"].get("weekly_minutes", 180)),
                expected_outcome="A deterministic public engineering artifact.",
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
                    status="submitted",
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
                        {"key": "weekly_minutes", "value": "240", "source": "user"}
                    ],
                    open_questions=[],
                    readiness="ready",
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
    replay = await outbox.dispatch_action(
        action_key=first_action_key,
        session_factory=database.AsyncSessionLocal,
        delivery_adapter=sink,
        wait_for_active_seconds=0,
    )
    if not replay.get("replayed") or len(sink.attempts) != 1:
        raise WorkerFailure(
            "outbox_replay_failed", "delivery", "outbox replay was not idempotent"
        )
    return replay


def _assert_public_artifacts(episode: dict[str, Any], capture: dict[str, Any]) -> None:
    issues = [
        *privacy_issues(episode, file="episode"),
        *privacy_issues(capture, file="capture"),
    ]
    if issues:
        raise WorkerFailure(
            "privacy_rejected", "publish", "public artifact privacy check failed"
        )
    encoded = canonical_json({"episode": episode, "capture": capture})
    if "E1_PRIVATE_REASONING_SENTINEL_DO_NOT_EXPORT" in encoded:
        raise WorkerFailure(
            "private_sentinel", "publish", "private model sentinel reached an artifact"
        )
    episode_issues = validate_episode(episode, source=f"{episode['episode_id']}.json")
    if episode_issues:
        raise WorkerFailure(
            "episode_invalid",
            "validate",
            "runtime Episode failed validation",
        )


async def _execute(request: dict[str, Any], guard: IsolationGuard) -> dict[str, Any]:
    fixture_document = _load_json(Path(request["fixture_path"]))
    fixture = RuntimeMiniFixture.model_validate(fixture_document).model_dump(
        mode="json"
    )
    if fixture["fixture_sha256"] != sha256_digest(
        {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    ):
        raise WorkerFailure("fixture_digest", "load", "fixture digest mismatch")
    snapshot = EvaluationSnapshotProvider(request["resource_path"])
    if snapshot.version != fixture["resource_snapshot_version"]:
        raise WorkerFailure(
            "resource_version", "load", "fixture resource snapshot mismatch"
        )

    app_time = import_module("app.core.time")
    app_identity = import_module("app.core.identity")
    app_search = import_module("app.search")
    app_config = import_module("app.core.config")
    database = import_module("app.db.database")
    agent_module = import_module("app.runtime.agent")
    model_clients = import_module("app.runtime.model_clients")

    frozen = datetime.fromisoformat(fixture["frozen_time"].replace("Z", "+00:00"))
    frozen_timestamp = normalize_rfc3339(fixture["frozen_time"])
    with ExitStack() as stack:
        stack.enter_context(app_time.frozen_utc(frozen))
        stack.enter_context(
            app_identity.deterministic_entity_ids(fixture["episode_id"])
        )
        stack.enter_context(app_search.use_snapshot_provider(snapshot))
        app_config.prepare_runtime_directories(app_config.settings.RUNTIME_STATE_ROOT)
        await database.create_schema(state_root=app_config.settings.RUNTIME_STATE_ROOT)
        await _seed_fixture(fixture, app_time.utc_now())
        identities = identity_registry(fixture)
        before_capture = await collect_state_snapshot(
            database.AsyncSessionLocal,
            fixture,
            registry=identities,
            captured_at=frozen_timestamp,
            resource_version=snapshot.version,
            resource_digest=snapshot.digest,
            phase="before",
        )

        if request["model_mode"] == "stub":
            client = ScriptedModelClient(fixture["scripted_turns"])
        else:
            openai = import_module("openai")
            client = openai.AsyncOpenAI(
                api_key=app_config.settings.OPENAI_API_KEY,
                base_url=app_config.settings.OPENAI_API_BASE,
            )
        recorder = EvaluationModelRecorder(
            client,
            invocation_mode=request["model_mode"],
            metadata_provider=model_clients.current_model_call_metadata,
        )
        stack.enter_context(model_clients.use_model_client_factory(lambda: recorder))
        runtime = agent_module.AgentRuntime()
        await runtime.run(fixture["run_id"])
        if request["model_mode"] == "stub" and client.remaining_turns != 0:
            raise WorkerFailure(
                "script_not_consumed",
                "runtime",
                "Runtime did not consume the scripted turns",
            )

        sink = RecordingDeliverySink()
        replay = await _drain_recording_sink(sink)
        after_capture = await collect_state_snapshot(
            database.AsyncSessionLocal,
            fixture,
            registry=identities,
            captured_at=frozen_timestamp,
            resource_version=snapshot.version,
            resource_digest=snapshot.digest,
            phase="after",
        )
        projection = audit_projection(after_capture.document)
        if projection["run"]["status"] not in {"completed", "waiting_approval"}:
            raise WorkerFailure(
                "runtime_not_exportable",
                "runtime",
                "production Runtime did not reach an exportable stable state",
            )
        database_digest = sha256_digest(projection)
        delta = build_state_delta(before_capture.document, after_capture.document)
        if delta["capture_status"] != "complete":
            raise DeltaConstructionError(
                delta["error_codes"][0] if delta["error_codes"] else "delta.incomplete"
            )
        # Oracle facts are intentionally unavailable until production Runtime,
        # delivery, both Snapshots, and the actual State Delta are complete.
        oracle = _load_json(Path(request["oracle_path"]))
        observation_text = canonical_json(recorder.records)
        observed_pending_delivery = "pending_delivery" in observation_text
        capture_records = [
            {
                **{
                    key: value
                    for key, value in record.items()
                    if key not in {"visible_messages", "visible_tool_schemas"}
                },
                "visible_messages_omitted": True,
                "visible_tool_schemas_omitted": True,
            }
            for record in recorder.records
        ]
        public_model_records = normalize_reference_fields(capture_records, identities)
        isolation_evidence = {
            **guard.evidence(),
            "temporary_database": True,
            "database_projection_recomputable": sha256_digest(projection)
            == database_digest,
            "snapshot_provider_calls": dict(snapshot.calls),
            "recording_sink_attempts": len(sink.attempts),
            "outbox_replay_confirmed": bool(
                replay is not None and replay.get("replayed")
            ),
            "agent_observed_pending_delivery": observed_pending_delivery,
            "agent_observed_emulated_receipt": "evaluation_sink" in observation_text,
        }
        episode_isolation_evidence = {
            key: value
            for key, value in isolation_evidence.items()
            if key != "database_projection_recomputable"
        }
        episode_isolation_evidence.update(
            {"published_sqlite_files": 0, "routing_material_exported": False}
        )
        episode = build_decision_episode_v2(
            fixture=fixture,
            oracle=oracle,
            model_records=[
                record
                for record in recorder.records
                if record["decision_relevant"]
                and record["response_status"] == "completed"
            ],
            state_before=before_capture.document,
            state_after=after_capture.document,
            state_delta=delta,
            resource_version=snapshot.version,
            resource_digest=snapshot.digest,
            git_commit=request["git_commit"],
            invocation_mode=request["model_mode"],
            model_name=app_config.settings.MODEL_NAME,
            model_temperature=app_config.settings.MODEL_TEMPERATURE,
            model_reasoning_effort=app_config.settings.MODEL_REASONING_EFFORT,
            isolation_evidence=episode_isolation_evidence,
        )
        if projection["outbox_actions"] and not observed_pending_delivery:
            raise WorkerFailure(
                "pending_delivery_missing",
                "capture",
                "production notification observation was not recorded",
            )
        if isolation_evidence["agent_observed_emulated_receipt"]:
            raise WorkerFailure(
                "receipt_visible_to_model",
                "capture",
                "emulated receipt entered model input",
            )
        capture = {
            "schema_version": "e1-capture-artifact-v1",
            "episode_id": fixture["episode_id"],
            "invocation_mode": request["model_mode"],
            "model_records": public_model_records,
            "database_projection": projection,
            "database_projection_sha256": database_digest,
            "delivery_attempts": sink.attempts,
            "isolation_evidence": isolation_evidence,
            "capture_sha256": "0" * 64,
        }
        capture["capture_sha256"] = sha256_digest(
            {key: value for key, value in capture.items() if key != "capture_sha256"}
        )
        _assert_public_artifacts(episode, capture)

    await database.engine.dispose()
    output = Path(request["output_path"])
    if output.exists():
        raise WorkerFailure("output_exists", "publish", "worker output already exists")
    candidate = output.with_name(f".{output.name}.candidate")
    candidate.mkdir(parents=False, exist_ok=False)
    (candidate / "episode.json").write_bytes(canonical_json_bytes(episode))
    (candidate / "capture.json").write_bytes(canonical_json_bytes(capture))
    os.replace(candidate, output)
    return {
        "status": "ok",
        "episode_id": fixture["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "capture_sha256": capture["capture_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m learning_agent_eval._historical_worker"
    )
    parser.add_argument("--request", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    episode_id = "unknown-episode"
    stage = "bootstrap"
    try:
        request = _load_json(Path(arguments.request))
        episode_id = str(request.get("episode_id") or episode_id)
        project_root = Path(request["project_root"]).resolve()
        worker_root = Path(request["worker_root"]).resolve()
        guard = IsolationGuard(
            project_root=project_root,
            worker_root=worker_root,
            model_mode=request["model_mode"],
            model_base_url=os.environ.get("OPENAI_API_BASE", ""),
        )
        guard.install()
        result = asyncio.run(_execute(request, guard))
        print(canonical_json(result))
        return 0
    except WorkerFailure as exc:
        code, stage, message = exc.code, exc.stage, exc.public_message
    except SnapshotCollectionError as exc:
        code, stage, message = (
            exc.code,
            "snapshot",
            "production Snapshot collection failed",
        )
    except DeltaConstructionError as exc:
        code, stage, message = exc.code, "delta", "State Delta construction failed"
    except ExportError as exc:
        code, stage, message = exc.code, "export", "DecisionEpisode v2 export failed"
    except KeyError:
        code, stage, message = (
            "e2_internal_key_error",
            "validate",
            "runtime artifact validation failed",
        )
    except TypeError:
        code, stage, message = (
            "e2_internal_type_error",
            "validate",
            "runtime artifact validation failed",
        )
    except (EvaluationIsolationError, ValueError, OSError, RuntimeError):
        code, message = "e1_worker_failed", "isolated evaluation worker failed"
    print(
        canonical_json(
            {
                "status": "error",
                "error_code": code,
                "stage": stage,
                "episode_id": episode_id,
                "message": message,
            }
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
