"""Isolated active worker for Evaluation Protocol Release 1.0."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import ExitStack
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from .action_protocol import evaluation_system_prompt
from .canonical import canonical_json, canonical_json_bytes, sha256_digest
from .case_specs import (
    build_judge_reference,
    build_judge_reference_v2,
    legacy_runtime_projection,
    legacy_runtime_projection_v2,
    validate_case_spec,
    validate_case_spec_v2,
)
from .delivery import RecordingDeliverySink
from .deltas import DeltaConstructionError, build_state_delta
from .e31_runtime import (
    build_model_calls_v3,
    build_model_calls_v4,
    build_real_provider_attestation,
    build_runtime_failure,
    build_runtime_failure_v2,
    build_stub_provider_attestation,
    canonicalize_concurrent_model_records,
)
from .exporter import ExportError
from .exporter_v3 import ExportV3Error, build_decision_episode_v3
from .exporter_v4 import ExportV4Error, build_decision_episode_v4
from .isolation import EvaluationIsolationError, IsolationGuard
from .normalizers import NormalizationError, normalize_json, normalize_rfc3339
from .privacy import privacy_issues
from .recorder import EvaluationModelRecorder
from .resources import EvaluationSnapshotProvider
from .runtime_fixture import (
    WorkerFailure,
    _drain_recording_sink,
    _load_json,
    _seed_fixture,
)
from .runtime_metadata import (
    AGENT_RUNTIME_CONFIG_SHA256,
    ENDPOINT_POLICY_SHA256,
    ENDPOINT_POLICY_VERSION,
)
from .scripted_model import ScriptedModelClient
from .snapshots import (
    FIELD_ALLOWLISTS,
    SnapshotCollectionError,
    audit_projection,
    collect_state_snapshot,
    identity_registry,
    normalize_reference_fields,
)
from .source_bundles import git_source_bundle_sha256
from .validator import validate_episode


def _publish(output: Path, files: dict[str, dict[str, Any]]) -> None:
    if output.exists():
        raise WorkerFailure("output_exists", "publish", "worker output already exists")
    candidate = output.with_name(f".{output.name}.candidate")
    candidate.mkdir(parents=False, exist_ok=False)
    try:
        for name, document in sorted(files.items()):
            (candidate / name).write_bytes(canonical_json_bytes(document))
        os.replace(candidate, output)
    except BaseException:
        if candidate.is_dir():
            for child in candidate.iterdir():
                child.unlink()
            candidate.rmdir()
        raise


def _provider_attestation(
    request: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    configured_model: str,
    frozen_time: str,
) -> dict[str, Any]:
    if request["model_mode"] == "stub":
        return build_stub_provider_attestation(
            records,
            scope="agent_runtime",
            configured_model=configured_model,
            frozen_time=frozen_time,
            git_commit=request["git_commit"],
            dependency_lock_version=request["dependency_lock_version"],
            dependency_lock_sha256=request["dependency_lock_sha256"],
            endpoint_policy_version=ENDPOINT_POLICY_VERSION,
            endpoint_policy_sha256=ENDPOINT_POLICY_SHA256,
            worktree_clean=bool(request["worktree_clean"]),
        )
    return build_real_provider_attestation(
        records,
        scope="agent_runtime",
        configuration_sha256=AGENT_RUNTIME_CONFIG_SHA256,
        git_commit=request["git_commit"],
        worktree_clean=bool(request["worktree_clean"]),
        dependency_lock_verified=bool(request["dependency_lock_verified"]),
    )


def _failure_class(stage: str) -> str:
    if stage == "isolation":
        return "isolation_violation"
    if stage == "provider":
        return "provider_error"
    if stage in {"load", "seed"}:
        return "fixture_error"
    return "framework_error"


def _failure_document(
    *,
    request: dict[str, Any],
    case: dict[str, Any],
    stage: str,
    reason_code: str,
    public_summary: str,
    records: list[dict[str, Any]] | None = None,
    isolation_evidence: dict[str, Any] | None = None,
    configured_model: str = "e31-scripted-model",
) -> dict[str, Any]:
    frozen_time = normalize_rfc3339(case["runtime_setup"]["frozen_time"])
    public_records = records or []
    attestation = _provider_attestation(
        request,
        public_records,
        configured_model=configured_model,
        frozen_time=frozen_time,
    )
    active = request.get("contract_version") == "v4"
    builder = build_runtime_failure_v2 if active else build_runtime_failure
    call_builder = build_model_calls_v4 if active else build_model_calls_v3
    return builder(
        case_id=case["case_id"],
        case_spec_sha256=case["case_spec_sha256"],
        stage=stage,
        failure_class=_failure_class(stage),
        reason_code=reason_code,
        public_summary=public_summary,
        model_calls=call_builder(public_records),
        provider_attestation=attestation,
        isolation_evidence=isolation_evidence,
        started_at=frozen_time,
        failed_at=frozen_time,
        **(
            {
                "evaluation_protocol_release_sha256": request[
                    "evaluation_protocol_release_sha256"
                ],
                "benchmark_release_id": request["benchmark_release_id"],
                "benchmark_release_sha256": request["benchmark_release_sha256"],
                "runtime_run_id": request["runtime_run_id"],
                "runtime_source_bundle_version": request[
                    "runtime_source_bundle_version"
                ],
                "runtime_source_bundle_sha256": request[
                    "runtime_source_bundle_sha256"
                ],
            }
            if active
            else {}
        ),
    )


def _assert_episode_public(episode: dict[str, Any], reference: dict[str, Any]) -> None:
    version = str(episode["schema_version"])
    if privacy_issues({"episode": episode, "reference": reference}, file=version):
        raise WorkerFailure(
            "privacy_rejected", "publish", "public evaluation artifacts failed privacy checks"
        )
    validation_issues = validate_episode(
        episode, source=f"{episode['episode_id']}.json"
    )
    if validation_issues:
        raise WorkerFailure(
            "episode_invalid",
            "validate",
            f"{version} failed validation ({validation_issues[0].code})",
        )


def _public_model_records(
    records: list[dict[str, Any]], identities: Any, *, active: bool, failure: bool = False
) -> list[dict[str, Any]]:
    """Normalize actual references without interpreting tool JSON Schema keys."""

    projected: list[dict[str, Any]] = []
    def run_ref(raw: str) -> str:
        try:
            return identities.resolve("agent_run", raw)
        except NormalizationError:
            if failure:
                return raw  # Failure records may precede a complete Snapshot registry.
            raise
    for source in records:
        record = normalize_json(source)
        record["run_id"] = run_ref(source["run_id"])
        parent_run_id = source.get("parent_run_id")
        record["parent_run_id"] = (
            run_ref(parent_run_id)
            if parent_run_id is not None
            else None
        )
        record["function_calls"] = [
            {
                **call,
                "canonical_arguments": call["canonical_arguments"] if active else normalize_reference_fields(
                    call["canonical_arguments"], identities
                ),
            }
            for call in record["function_calls"]
        ]
        projected.append(record)
    return canonicalize_concurrent_model_records(projected) if active else projected


async def _execute_inner(
    request: dict[str, Any], guard: IsolationGuard, progress: dict[str, Any]
) -> dict[str, Any]:
    active = request.get("contract_version") == "v4"
    case_validator = validate_case_spec_v2 if active else validate_case_spec
    reference_builder = build_judge_reference_v2 if active else build_judge_reference
    fixture_builder = legacy_runtime_projection_v2 if active else legacy_runtime_projection
    episode_builder = build_decision_episode_v4 if active else build_decision_episode_v3
    case = case_validator(_load_json(Path(request["case_path"])))
    progress["case"] = case
    runtime_setup = case["runtime_setup"]
    if runtime_setup["invocation_mode"] != request["model_mode"]:
        raise WorkerFailure(
            "case_mode_mismatch",
            "load",
            "CaseSpec invocation mode differs from the selected model mode",
        )
    reference = reference_builder(case)
    fixture = fixture_builder(case)
    snapshot = EvaluationSnapshotProvider(request["resource_path"])
    if active:
        fixture["resource_catalog"] = snapshot.public_catalog()
    progress["snapshot"] = snapshot
    if snapshot.version != runtime_setup["resource_snapshot_version"]:
        raise WorkerFailure(
            "resource_version", "load", "CaseSpec resource Snapshot mismatch"
        )
    frozen_time = normalize_rfc3339(runtime_setup["frozen_time"])

    injected = runtime_setup["seed"].get("evaluation_injected_failure")
    if injected is not None:
        if injected != "provider_error":
            raise WorkerFailure(
                "injected_failure_invalid",
                "load",
                "engineering failure injection is unsupported",
            )
        failure = _failure_document(
            request=request,
            case=case,
            stage="provider",
            reason_code="provider.injected_unavailable",
            public_summary="The isolated engineering provider did not complete.",
        )
        _publish(Path(request["output_path"]), {"failure.json": failure})
        return {
            "status": "failure",
            "case_id": case["case_id"],
            "track": case["track"],
            "artifact_id": failure["failure_id"],
            "artifact_sha256": failure["failure_sha256"],
        }

    app_time = import_module("app.core.time")
    app_identity = import_module("app.core.identity")
    app_search = import_module("app.search")
    app_config = import_module("app.core.config")
    database = import_module("app.db.database")
    agent_module = import_module("app.runtime.agent")
    model_clients = import_module("app.runtime.model_clients")

    frozen = datetime.fromisoformat(runtime_setup["frozen_time"].replace("Z", "+00:00"))
    with ExitStack() as stack:
        stack.enter_context(app_time.frozen_utc(frozen))
        stack.enter_context(
            app_identity.deterministic_entity_ids(fixture["episode_id"])
        )
        stack.enter_context(app_search.use_snapshot_provider(snapshot))
        if active:
            agent_module.SYSTEM_PROMPT = evaluation_system_prompt(
                agent_module.SYSTEM_PROMPT
            )
        app_config.prepare_runtime_directories(app_config.settings.RUNTIME_STATE_ROOT)
        await database.create_schema(state_root=app_config.settings.RUNTIME_STATE_ROOT)
        await _seed_fixture(fixture, app_time.utc_now())
        identities = identity_registry(
            fixture, identity_bindings=case["identity_bindings"]
        )
        before = await collect_state_snapshot(
            database.AsyncSessionLocal,
            fixture,
            registry=identities,
            captured_at=frozen_time,
            resource_version=snapshot.version,
            resource_digest=snapshot.digest,
            phase="before",
        )
        if active:
            actual = {entity["logical_id"]: entity for entity in before.document["logical_entities"]}
            for declared in runtime_setup["state_before"]["logical_entities"]:
                if declared["entity_type"] in {"goal", "constraint", "resource"}:
                    continue  # These are declared public facts, not seeded ORM rows.
                captured = actual.get(declared["logical_id"])
                allowed = set(FIELD_ALLOWLISTS.get(declared["entity_type"], ()))
                if declared["entity_type"] not in {"learner"} and set(declared["data"]) - allowed - {"content_kind"}:
                    raise WorkerFailure("seed.unknown_state_field", "seed", "persistent entity declares an unsupported state field")
                if captured is None or any(
                    captured["data"].get(key) != value
                    for key, value in declared["data"].items() if key in FIELD_ALLOWLISTS.get(declared["entity_type"], ())
                ):
                    raise WorkerFailure("seed.state_mismatch", "seed", "declared pre-state differs from the executable seed")
        if request["model_mode"] == "stub":
            client = ScriptedModelClient(runtime_setup["scripted_turns"])
        else:
            openai = import_module("openai")
            client = openai.AsyncOpenAI(
                api_key=app_config.settings.OPENAI_API_KEY,
                base_url=app_config.settings.OPENAI_API_BASE,
                max_retries=0,
            )
        pilot = active and case["dataset_role"] == "protocol_pilot" and request["model_mode"] == "real"
        from .model_budget import OUTPUT_LIMIT, ModelBudget

        budget = None
        if active and request["model_mode"] == "real":
            if not request.get("budget_ledger"):
                raise WorkerFailure("budget.required", "prepare", "real worker requires a prepaid budget ledger")
            budget = ModelBudget(request["budget_ledger"])
        recorder = EvaluationModelRecorder(
            client,
            invocation_mode=request["model_mode"],
            metadata_provider=model_clients.current_model_call_metadata,
            max_calls=24 if pilot else None,
            max_output_tokens=OUTPUT_LIMIT if budget is not None else None,
            budget=budget,
            budget_scope=case["case_id"],
        )
        progress.update(recorder=recorder, identities=identities, configured_model=app_config.settings.MODEL_NAME)
        stack.enter_context(model_clients.use_model_client_factory(lambda: recorder))
        runtime = agent_module.AgentRuntime()
        await runtime.run(fixture["run_id"])
        if request["model_mode"] == "stub" and client.remaining_turns != 0:
            raise WorkerFailure(
                "script_not_consumed",
                "runtime",
                "Runtime did not consume all fixed scripted turns",
            )

        sink = RecordingDeliverySink()
        progress["sink"] = sink
        replay = await _drain_recording_sink(sink)
        after = await collect_state_snapshot(
            database.AsyncSessionLocal,
            fixture,
            registry=identities,
            captured_at=frozen_time,
            resource_version=snapshot.version,
            resource_digest=snapshot.digest,
            phase="after",
        )
        projection = audit_projection(after.document)
        observation_text = canonical_json(recorder.records)
        public_records = _public_model_records(
            recorder.records, identities, active=active
        )
        isolation = {
            **guard.evidence(),
            "temporary_database": True,
            "snapshot_provider_calls": dict(snapshot.calls),
            "recording_sink_attempts": len(sink.attempts),
            "outbox_replay_confirmed": bool(replay and replay.get("replayed")),
            "agent_observed_pending_delivery": "pending_delivery" in observation_text,
            "agent_observed_emulated_receipt": "evaluation_sink" in observation_text,
            "published_sqlite_files": 0,
            "routing_material_exported": False,
        }
        progress["isolation"] = isolation
        root_runs = [
            item
            for item in after.document["logical_entities"]
            if item["entity_type"] == "agent_run"
            and item["data"].get("parent_run_ref") is None
        ]
        if len(root_runs) != 1:
            raise WorkerFailure(
                "runtime_not_exportable",
                "runtime",
                "production Runtime did not reach an exportable stable state",
            )
        root_status = root_runs[0]["data"]["status"]
        if root_status not in {"completed", "waiting_approval"}:
            provider_failed = any(
                record.get("response_status") == "provider_error"
                for record in public_records
            )
            failure = _failure_document(
                request=request,
                case=case,
                stage="provider" if provider_failed else "runtime",
                reason_code=(
                    "provider.runtime_call_failed"
                    if provider_failed
                    else "runtime.non_exportable_terminal"
                ),
                public_summary=(
                    "The isolated model provider did not complete."
                    if provider_failed
                    else "The isolated Runtime ended without a scoreable Episode."
                ),
                records=public_records,
                isolation_evidence=isolation,
                configured_model=app_config.settings.MODEL_NAME,
            )
            await database.engine.dispose()
            _publish(Path(request["output_path"]), {"failure.json": failure})
            return {
                "status": "failure",
                "case_id": case["case_id"],
                "track": case["track"],
                "artifact_id": failure["failure_id"],
                "artifact_sha256": failure["failure_sha256"],
            }
        delta = build_state_delta(before.document, after.document)
        if delta["capture_status"] != "complete":
            raise DeltaConstructionError(
                delta["error_codes"][0] if delta["error_codes"] else "delta.incomplete"
            )
        attestation = _provider_attestation(
            request,
            public_records,
            configured_model=app_config.settings.MODEL_NAME,
            frozen_time=frozen_time,
        )
        episode = episode_builder(
            case_spec=case,
            judge_reference=reference,
            model_records=public_records,
            state_before=before.document,
            state_after=after.document,
            state_delta=delta,
            resource_version=snapshot.version,
            resource_digest=snapshot.digest,
            git_commit=request["git_commit"],
            invocation_mode=request["model_mode"],
            model_name=app_config.settings.MODEL_NAME,
            model_temperature=app_config.settings.MODEL_TEMPERATURE,
            model_reasoning_effort=app_config.settings.MODEL_REASONING_EFFORT,
            isolation_evidence=isolation,
            provider_attestation=attestation,
            dependency_lock_version=request["dependency_lock_version"],
            dependency_lock_sha256=request["dependency_lock_sha256"],
            **(
                {
                    "evaluation_protocol_release_sha256": request[
                        "evaluation_protocol_release_sha256"
                    ],
                    "benchmark_release_id": request["benchmark_release_id"],
                    "benchmark_release_sha256": request[
                        "benchmark_release_sha256"
                    ],
                    "runtime_run_id": request["runtime_run_id"],
                    "runtime_source_bundle_version": request[
                        "runtime_source_bundle_version"
                    ],
                    "runtime_source_bundle_sha256": request[
                        "runtime_source_bundle_sha256"
                    ],
                }
                if active
                else {}
            ),
        )
        if (
            projection["outbox_actions"]
            and not isolation["agent_observed_pending_delivery"]
        ):
            raise WorkerFailure(
                "pending_delivery_missing",
                "capture",
                "durable delivery state was absent from model observation",
            )
        if isolation["agent_observed_emulated_receipt"]:
            raise WorkerFailure(
                "receipt_visible_to_model",
                "capture",
                "emulated delivery receipt entered model input",
            )
        if sha256_digest(projection) != sha256_digest(audit_projection(after.document)):
            raise WorkerFailure(
                "database_projection_unstable",
                "capture",
                "database projection was not deterministic",
            )
        _assert_episode_public(episode, reference)

    await database.engine.dispose()
    _publish(
        Path(request["output_path"]),
        {"episode.json": episode, "judge-reference.json": reference},
    )
    return {
        "status": "episode",
        "case_id": case["case_id"],
        "track": case["track"],
        "artifact_id": episode["episode_id"],
        "artifact_sha256": episode["provenance"]["episode_sha256"],
        "reference_sha256": reference["reference_sha256"],
    }


async def _execute(request: dict[str, Any], guard: IsolationGuard) -> dict[str, Any]:
    progress: dict[str, Any] = {}
    try:
        return await _execute_inner(request, guard, progress)
    except (WorkerFailure, SnapshotCollectionError, DeltaConstructionError,
            ExportError, ExportV3Error, ExportV4Error, EvaluationIsolationError,
            KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        case = progress.get("case")
        recorder = progress.get("recorder")
        # Failures before Case validation retain the control-plane error path.
        if case is None or recorder is None:
            raise
        stage = getattr(exc, "stage", "runtime")
        if isinstance(exc, DeltaConstructionError):
            stage = "delta"
        elif isinstance(exc, SnapshotCollectionError):
            stage = "snapshot"
        elif isinstance(exc, (ExportError, ExportV3Error, ExportV4Error)):
            stage = "export"
        elif isinstance(exc, EvaluationIsolationError):
            stage = "isolation"
        records = _public_model_records(
            recorder.records, progress["identities"],
            active=request.get("contract_version") == "v4",
            failure=True,
        )
        sink = progress.get("sink")
        isolation = progress.get("isolation") or {
            **guard.evidence(), "temporary_database": True,
            "snapshot_provider_calls": dict(progress["snapshot"].calls),
            "recording_sink_attempts": len(sink.attempts) if sink else 0,
            "outbox_replay_confirmed": False,
            "agent_observed_pending_delivery": False,
            "agent_observed_emulated_receipt": False,
            "published_sqlite_files": 0, "routing_material_exported": False,
        }
        failure = _failure_document(
            request=request, case=case, stage=stage,
            reason_code=getattr(exc, "code", "worker.framework_error"),
            public_summary=(exc.public_message if isinstance(exc, WorkerFailure)
                            else "Isolated execution failed; completed public calls are retained."),
            records=records, isolation_evidence=isolation,
            configured_model=progress["configured_model"],
        )
        await import_module("app.db.database").engine.dispose()
        _publish(Path(request["output_path"]), {"failure.json": failure})
        return {"status": "failure", "case_id": case["case_id"], "track": case["track"],
                "artifact_id": failure["failure_id"], "artifact_sha256": failure["failure_sha256"]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m learning_agent_eval.active_worker")
    parser.add_argument("--request", required=True)
    return parser


def _main(argv: list[str] | None = None, *, contract_version: str = "v4") -> int:
    arguments = _parser().parse_args(argv)
    case_id = "unknown-case"
    stage = "bootstrap"
    try:
        request = _load_json(Path(arguments.request))
        if request.get("contract_version") != contract_version:
            raise WorkerFailure("historical_execution_disabled", "preflight", "worker contract is not active")
        if contract_version == "v4":
            git_source_bundle_sha256(request["git_commit"], "runtime")
        if request["model_mode"] == "real":
            # The pinned SDK lazily probes platform metadata (including a
            # possible local uname subprocess). Resolve it during bootstrap,
            # before the product/Provider phase prohibits all subprocesses.
            import_module("openai._base_client").get_platform()
        case_id = str(request.get("case_id") or case_id)
        guard = IsolationGuard(
            project_root=Path(request["project_root"]).resolve(),
            worker_root=Path(request["worker_root"]).resolve(),
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
        code, stage, message = exc.code, "snapshot", "Snapshot collection failed"
    except DeltaConstructionError as exc:
        code, stage, message = exc.code, "delta", "State Delta construction failed"
    except (ExportError, ExportV3Error, ExportV4Error) as exc:
        code, stage, message = exc.code, "export", "DecisionEpisode export failed"
    except EvaluationIsolationError:
        code, stage, message = (
            "isolation.violation",
            "isolation",
            "evaluation isolation rejected an operation",
        )
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        code, message = "worker.framework_error", "isolated evaluation worker failed"
    print(
        canonical_json(
            {
                "status": "error",
                "error_code": code,
                "stage": stage,
                "case_id": case_id,
                "message": message,
            }
        ),
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    return _main(argv, contract_version="v4")


if __name__ == "__main__":
    raise SystemExit(main())
