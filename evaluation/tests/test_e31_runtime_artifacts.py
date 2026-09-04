from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.e31_runtime import (
    E31RuntimeArtifactError,
    build_model_calls_v3,
    build_real_provider_attestation,
    build_runtime_failure,
    build_runtime_manifest_v2,
    build_stub_provider_attestation,
)
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    model_visible_context_digest,
    provider_attestation_digest,
    runtime_failure_digest,
)
from learning_agent_eval.models import (
    ProviderAttestationV1,
    RuntimeFailureV1,
    RuntimeRunManifestV2,
)
from learning_agent_eval.normalizers import (
    IdentityCandidate,
    NormalizationError,
    StableIdentityRegistry,
)
from learning_agent_eval.recorder import EvaluationModelRecorder
from learning_agent_eval.rubric import JUDGE_CONFIG_SHA256_V2
from learning_agent_eval.runtime_metadata import (
    dependency_environment_reason_codes,
    provider_attribution_reason_codes,
)
from learning_agent_eval.scripted_model import ScriptedModelClient
from learning_agent_eval.snapshots import (
    audit_projection,
    collect_state_snapshot,
    identity_registry,
)

SHA = "0" * 64
GIT = "0" * 40
NOW = "2026-01-02T03:04:05Z"


def _metadata(
    *,
    run_id: str = "run-main",
    parent_run_id: str | None = None,
    purpose: str = "decision",
    relevant: bool = True,
    depth: int = 0,
    parent_call_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        parent_run_id=parent_run_id,
        parent_call_id=parent_call_id,
        call_purpose=purpose,
        decision_relevant=relevant,
        depth=depth,
    )


async def _record(
    recorder: EvaluationModelRecorder,
    *,
    model: str = "e1-scripted-model",
) -> str:
    response = await recorder.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Keep the Good baseline and candidate context exact.",
            },
            {
                "role": "user",
                "content": "Explain the reasoning process as lesson content.",
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "profile_get",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        temperature=0,
        max_tokens=128,
    )
    return str(response.choices[0].message.content)


@pytest.mark.asyncio
async def test_recorder_retains_exact_sanitized_context_and_call_metadata() -> None:
    client = ScriptedModelClient(
        [
            {
                "delivery": "nonstream",
                "assistant_text": "public answer",
                "tool_calls": [],
            }
        ]
    )
    recorder = EvaluationModelRecorder(
        client,
        invocation_mode="stub",
        metadata_provider=lambda: _metadata(),
    )
    assert await _record(recorder) == "public answer"

    record = recorder.records[0]
    assert record["run_id"] == "run-main"
    assert record["call_purpose"] == "decision"
    assert record["decision_relevant"] is True
    assert record["visible_messages"][0]["content"] == (
        "Keep the Good baseline and candidate context exact."
    )
    assert record["visible_tool_schemas"][0]["function"]["name"] == "profile_get"
    assert record["visible_context_digest"] == sha256_digest(
        {
            "messages": record["visible_messages"],
            "tool_schemas": record["visible_tool_schemas"],
        }
    )
    assert "reasoning_content" not in str(record)
    assert "E1_PRIVATE_REASONING_SENTINEL_DO_NOT_EXPORT" not in str(record)
    assert "requested_at" not in record
    assert "responded_at" not in record

    calls = build_model_calls_v3(recorder.records)
    context = calls[0]["visible_context"]
    assert context["context_sha256"] == model_visible_context_digest(context)
    assert calls[0]["assistant_text"] == "public answer"


@pytest.mark.asyncio
async def test_real_recorder_retains_provider_call_times() -> None:
    client = ScriptedModelClient(
        [
            {
                "delivery": "nonstream",
                "assistant_text": "public answer",
                "tool_calls": [],
            }
        ]
    )
    recorder = EvaluationModelRecorder(
        client,
        invocation_mode="real",
        metadata_provider=lambda: _metadata(),
    )
    await _record(recorder, model="hy3")

    record = recorder.records[0]
    assert record["requested_at"].endswith("Z")
    assert record["responded_at"].endswith("Z")
    assert record["requested_at"] <= record["responded_at"]


@pytest.mark.asyncio
async def test_recorder_reserves_unique_start_order_before_concurrent_completion() -> None:
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    class ConcurrentCompletions:
        def __init__(self) -> None:
            self.ordinal = 0

        async def create(self, **_: Any) -> Any:
            index = self.ordinal
            self.ordinal += 1
            started[index].set()
            await release[index].wait()
            message = SimpleNamespace(
                content=f"reply-{index + 1}",
                reasoning_content=None,
                tool_calls=None,
            )
            return SimpleNamespace(
                id=f"response-{index + 1}",
                model="e1-scripted-model",
                choices=[SimpleNamespace(message=message)],
                usage=None,
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=ConcurrentCompletions())
    )
    recorder = EvaluationModelRecorder(
        client,
        invocation_mode="stub",
        metadata_provider=lambda: _metadata(),
    )
    first = asyncio.create_task(_record(recorder))
    await started[0].wait()
    second = asyncio.create_task(_record(recorder))
    await started[1].wait()
    release[1].set()
    assert await second == "reply-2"
    release[0].set()
    assert await first == "reply-1"

    assert [item["call_id"] for item in recorder.records] == [
        "model-call:001",
        "model-call:002",
    ]
    assert [item["ordinal"] for item in recorder.records] == [1, 2]
    assert [item["assistant_text"] for item in recorder.records] == [
        "reply-1",
        "reply-2",
    ]


@pytest.mark.asyncio
async def test_runtime_model_factory_covers_scoped_child_and_auxiliary_calls() -> None:
    from app.runtime.model_clients import (
        create_model_client,
        model_call_scope,
        use_model_client_factory,
    )

    client = ScriptedModelClient(
        [
            {
                "delivery": "nonstream",
                "assistant_text": "child report",
                "tool_calls": [],
            },
            {
                "delivery": "nonstream",
                "assistant_text": "session title",
                "tool_calls": [],
            },
        ]
    )
    current: SimpleNamespace | None = None
    recorder = EvaluationModelRecorder(
        client,
        invocation_mode="stub",
        metadata_provider=lambda: current,
    )
    with use_model_client_factory(lambda: recorder):
        assert create_model_client() is recorder
        current = _metadata(
            run_id="run-child",
            parent_run_id="run-main",
            purpose="subagent_decision",
            relevant=True,
            depth=1,
        )
        with model_call_scope(
            run_id="run-child",
            parent_run_id="run-main",
            parent_call_id="model-call:parent",
            call_purpose="subagent_decision",
            decision_relevant=True,
            depth=1,
        ):
            # The test callback mirrors what the recorder receives from the
            # production ContextVar without importing evaluation in backend.
            from app.runtime.model_clients import current_model_call_metadata

            recorder._metadata_provider = current_model_call_metadata
            await _record(recorder)
        with model_call_scope(
            run_id="run-main",
            parent_run_id=None,
            call_purpose="session_title",
            decision_relevant=False,
            depth=0,
        ):
            await _record(recorder)

    calls = build_model_calls_v3(recorder.records)
    assert [item["call_purpose"] for item in calls] == [
        "subagent_decision",
        "session_title",
    ]
    assert [item["decision_relevant"] for item in calls] == [True, False]
    assert calls[0]["parent_run_id"] == "run-main"
    assert calls[0]["parent_call_id"] == "model-call:parent"


@pytest.mark.asyncio
async def test_provider_failure_is_recorded_without_raw_exception_payload() -> None:
    class FailingCompletions:
        async def create(self, **_: Any) -> Any:
            raise RuntimeError("SECRET provider packet")

    recorder = EvaluationModelRecorder(
        SimpleNamespace(chat=SimpleNamespace(completions=FailingCompletions())),
        invocation_mode="stub",
        metadata_provider=lambda: _metadata(),
    )
    with pytest.raises(RuntimeError, match="SECRET provider packet"):
        await recorder.chat.completions.create(
            model="e1-scripted-model",
            messages=[{"role": "user", "content": "public"}],
            tools=[],
        )
    assert len(recorder.records) == 1
    assert recorder.records[0]["response_status"] == "provider_error"
    assert "SECRET" not in str(recorder.records[0])
    calls = build_model_calls_v3(recorder.records)
    assert calls[0]["status"] == "provider_error"
    assert calls[0]["assistant_text"] is None
    assert calls[0]["response_sha256"] is None


def test_failure_and_manifest_preserve_one_terminal_per_case() -> None:
    records: list[dict[str, Any]] = []
    attestation = build_stub_provider_attestation(
        records,
        scope="agent_runtime",
        configured_model="e1-scripted-model",
        frozen_time=NOW,
        git_commit=GIT,
        dependency_lock_version="runtime-lock-v1",
        dependency_lock_sha256=SHA,
        endpoint_policy_version="hy3-endpoints-v1",
        endpoint_policy_sha256=SHA,
    )
    failure = build_runtime_failure(
        case_id="case-failure",
        case_spec_sha256=SHA,
        stage="runtime",
        failure_class="provider_error",
        reason_code="provider.timeout",
        public_summary="The isolated provider call did not complete.",
        model_calls=[],
        provider_attestation=attestation,
        isolation_evidence=None,
        started_at=NOW,
        failed_at=NOW,
    )
    RuntimeFailureV1.model_validate(failure)
    assert failure["failure_sha256"] == runtime_failure_digest(failure)
    assert "exception" not in str(failure).casefold()

    manifest = build_runtime_manifest_v2(
        dataset_version="decisionbench-engineering-v3",
        invocation_mode="stub",
        terminals=[
            {
                "case_id": "case-success",
                "case_spec_sha256": SHA,
                "track": "planning",
                "terminal_kind": "episode",
                "artifact_id": "episode-success",
                "artifact_sha256": SHA,
                "formal_evaluation_result": False,
            },
            {
                "case_id": "case-failure",
                "case_spec_sha256": SHA,
                "track": "assessment",
                "terminal_kind": "failure",
                "artifact_id": failure["failure_id"],
                "artifact_sha256": failure["failure_sha256"],
                "formal_evaluation_result": False,
            },
        ],
        git_commit=GIT,
        dependency_lock_version="runtime-lock-v1",
        dependency_lock_sha256=SHA,
    )
    RuntimeRunManifestV2.model_validate(manifest)
    assert manifest["selected_case_ids"] == ["case-failure", "case-success"]
    assert manifest["manifest_sha256"] == artifact_manifest_digest(manifest)

    formal = build_runtime_manifest_v2(
        dataset_version="decisionbench-primary-v3",
        invocation_mode="real",
        terminals=[
            {
                "case_id": "case-primary",
                "case_spec_sha256": SHA,
                "track": "planning",
                "terminal_kind": "episode",
                "artifact_id": "episode-primary",
                "artifact_sha256": SHA,
                "formal_evaluation_result": True,
            }
        ],
        git_commit=GIT,
        dependency_lock_version="runtime-lock-v1",
        dependency_lock_sha256=SHA,
    )
    assert formal["formal_evaluation_result"] is True
    assert formal["evaluation_status"] == "formal_model_evaluation"


def test_unscoped_model_call_fails_closed_for_v3() -> None:
    with pytest.raises(E31RuntimeArtifactError, match="trajectory.call_scope_missing"):
        build_model_calls_v3(
            [
                {
                    "ordinal": 1,
                    "run_id": "unscoped-run",
                    "visible_messages": [],
                    "visible_tool_schemas": [],
                    "response_status": "completed",
                    "response_digest": SHA,
                    "decision_relevant": True,
                }
            ]
        )


def test_identity_bindings_follow_semantics_instead_of_row_order() -> None:
    bindings = [
        {
            "entity_type": "task",
            "logical_id": "task-alpha",
            "identity_fields": {"stage_ref": "stage-main", "title": "Alpha"},
        },
        {
            "entity_type": "task",
            "logical_id": "task-beta",
            "identity_fields": {"stage_ref": "stage-main", "title": "Beta"},
        },
    ]
    registry = StableIdentityRegistry(
        episode_id="episode-bindings",
        declarations={"task": ["task-alpha", "task-beta"]},
        identity_bindings=bindings,
    )
    registry.register_many(
        "task",
        [
            IdentityCandidate(22, {"stage_ref": "stage-main", "title": "Beta"}),
            IdentityCandidate(11, {"stage_ref": "stage-main", "title": "Alpha"}),
        ],
    )
    registry.assert_bindings_resolved(["task"])
    assert registry.resolve("task", 11) == "task-alpha"
    assert registry.resolve("task", 22) == "task-beta"

    missing = StableIdentityRegistry(
        episode_id="episode-bindings",
        identity_bindings=bindings,
    )
    missing.register_many(
        "task",
        [IdentityCandidate(33, {"stage_ref": "stage-main", "title": "Gamma"})],
    )
    with pytest.raises(NormalizationError, match="identity.binding_unresolved"):
        missing.assert_bindings_resolved(["task"])


def test_real_provider_attribution_is_recomputed_from_allowlisted_facts() -> None:
    assert dependency_environment_reason_codes() == ()
    attestation = build_real_provider_attestation(
        [
            {
                "call_id": "provider-call-001",
                "request_model": "hy3",
                "response_model": "hy3",
                "provider_request_id": "request-public-001",
                "requested_at": NOW,
                "responded_at": NOW,
                "response_status": "completed",
            }
        ],
        scope="semantic_judge",
        configuration_sha256=JUDGE_CONFIG_SHA256_V2,
        git_commit=GIT,
        worktree_clean=True,
        dependency_lock_verified=True,
    )
    assert attestation["attribution_status"] == "eligible"
    assert attestation["reason_codes"] == []

    for mutate in ("endpoint", "response_model"):
        forged = deepcopy(attestation)
        if mutate == "endpoint":
            forged["endpoint_origin"] = "https://fake-provider.example"
        else:
            forged["calls"][0]["response_model"] = "not-hy3"
        forged["reason_codes"] = list(provider_attribution_reason_codes(forged))
        forged["attribution_status"] = "invalid"
        forged["attestation_sha256"] = provider_attestation_digest(forged)
        validated = ProviderAttestationV1.model_validate(forged)
        assert validated.attribution_status == "invalid"
        assert validated.reason_codes


@pytest.mark.asyncio
async def test_snapshot_captures_root_and_child_agent_run_hierarchy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "app-runtime"
    database_path = runtime_root / "hierarchy.sqlite3"
    runtime_root.mkdir()
    monkeypatch.setenv("EVALUATION_MODE", "1")
    monkeypatch.setenv("RUNTIME_STATE_ROOT", str(runtime_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    monkeypatch.setenv("ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("ENABLE_EMAIL_REPLY_POLLING", "false")

    from app.db.database import Base
    from app.models import AgentRun, Owner, UserProfile
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        db.add(
            Owner(
                id="local", display_name="Synthetic learner", timezone="Asia/Shanghai"
            )
        )
        db.add(UserProfile(owner_id="local"))
        parent = AgentRun(
            id="e31-parent-run",
            owner_id="local",
            trigger="user_message",
            objective="Coordinate a synthetic decision.",
            status="completed",
            phase="terminal",
        )
        child = AgentRun(
            id="e31-child-run",
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="Collect synthetic supporting evidence.",
            status="completed",
            phase="terminal",
        )
        db.add_all([parent, child])
        await db.commit()

    fixture = {
        "episode_id": "episode-child-hierarchy",
        "owner_id": "local",
        "run_id": parent.id,
        "state_before": {
            "logical_entities": [
                {
                    "entity_type": "learner",
                    "logical_id": "learner-child-hierarchy",
                    "data": {"fixture": "public"},
                }
            ],
            "context": {
                "public_summary": "Synthetic parent and child runtime context.",
                "source_refs": ["learner-child-hierarchy"],
                "context_sha256": SHA,
            },
        },
    }
    registry = identity_registry(
        fixture,
        identity_bindings=[
            {
                "entity_type": "learner",
                "logical_id": "learner-child-hierarchy",
                "identity_fields": {
                    "timezone": "Asia/Shanghai",
                },
            }
        ],
    )
    snapshot = await collect_state_snapshot(
        session_factory,
        fixture,
        registry=registry,
        captured_at=NOW,
        resource_version="resource-snapshot-v1",
        resource_digest=SHA,
        phase="before",
    )
    runs = [
        item
        for item in snapshot.document["logical_entities"]
        if item["entity_type"] == "agent_run"
    ]
    assert len(runs) == 2
    root = next(item for item in runs if item["data"]["parent_run_ref"] is None)
    nested = next(item for item in runs if item is not root)
    assert nested["data"]["parent_run_ref"] == root["logical_id"]
    assert audit_projection(snapshot.document)["run"]["run_ref"] == root["logical_id"]
    await engine.dispose()
