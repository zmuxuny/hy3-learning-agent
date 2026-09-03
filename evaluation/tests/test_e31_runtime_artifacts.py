from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.e31_runtime import (
    E31RuntimeArtifactError,
    build_model_calls_v3,
    build_runtime_failure,
    build_runtime_manifest_v2,
    build_stub_provider_attestation,
)
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    model_visible_context_digest,
    runtime_failure_digest,
)
from learning_agent_eval.models import RuntimeFailureV1, RuntimeRunManifestV2
from learning_agent_eval.recorder import EvaluationModelRecorder
from learning_agent_eval.scripted_model import ScriptedModelClient

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
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        parent_run_id=parent_run_id,
        parent_call_id=None,
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

    calls = build_model_calls_v3(recorder.records)
    context = calls[0]["visible_context"]
    assert context["context_sha256"] == model_visible_context_digest(context)
    assert calls[0]["assistant_text"] == "public answer"


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
            },
            {
                "case_id": "case-failure",
                "case_spec_sha256": SHA,
                "track": "assessment",
                "terminal_kind": "failure",
                "artifact_id": failure["failure_id"],
                "artifact_sha256": failure["failure_sha256"],
            },
        ],
        git_commit=GIT,
        dependency_lock_version="runtime-lock-v1",
        dependency_lock_sha256=SHA,
    )
    RuntimeRunManifestV2.model_validate(manifest)
    assert manifest["selected_case_ids"] == ["case-failure", "case-success"]
    assert manifest["manifest_sha256"] == artifact_manifest_digest(manifest)


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
