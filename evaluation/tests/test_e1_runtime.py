from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from historical_execution import run_agent_v2 as run_agent
from learning_agent_eval import oracle_envelope_digest, sha256_digest
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.isolation import (
    EvaluationIsolationError,
    IsolationGuard,
    worker_environment,
)
from learning_agent_eval.privacy import privacy_issues
from learning_agent_eval.recorder import (
    EvaluationModelRecorder,
    public_projection,
)
from learning_agent_eval.resources import EvaluationSnapshotProvider
from learning_agent_eval.runner import RunAgentError, RunAgentSummary
from learning_agent_eval.scripted_model import ScriptedModelClient
from learning_agent_eval.validator import validate_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"
MANIFEST = DATASET / "manifests" / "e1-mini-stub.json"


@pytest.fixture(scope="session")
def e1_batch(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, RunAgentSummary]:
    root = tmp_path_factory.mktemp("e1-batch")
    output = root / "output"
    summary = run_agent(dataset=DATASET, manifest=MANIFEST, output=output)
    return output, summary


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_output_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_four_runtime_mini_tracks_validate_with_e0(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, summary = e1_batch
    report = validate_dataset(output)

    assert report.ok, [issue.render() for issue in report.issues]
    assert report.stats.episodes == 4
    assert dict(report.stats.by_track) == {
        "planning": 1,
        "intervention": 1,
        "assessment": 1,
        "revision": 1,
    }
    assert set(summary.episode_ids) == {
        "P-E1-MINI-001",
        "I-E1-MINI-001",
        "A-E1-MINI-001",
        "R-E1-MINI-001",
    }


def test_runtime_exports_declare_the_engineering_boundary(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    for path in sorted((output / "episodes").glob("*.json")):
        episode = _load(path)
        assert episode["observable_trace"]["capture_mode"] == "runtime_recording"
        assert episode["provenance"] == {
            **episode["provenance"],
            "source_type": "runtime_export",
            "construction_method": "runtime_recorded",
            "runtime_executed": True,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
        }
        assert episode["environment"]["model"]["invocation_mode"] == "stub"
        assert episode["environment"]["model"] == {
            "provider": "none",
            "name": "e1-scripted-model",
            "invocation_mode": "stub",
            "temperature": 0,
            "reasoning_effort": "none",
            "max_tokens": 4096,
        }
        assert episode["environment"]["runtime"]["database_mode"] == "temporary_fixture"
        assert episode["environment"]["isolation"] == {
            "production_database_access": False,
            "network_access": "disabled",
            "notification_mode": "fake_outbox",
        }


def test_oracles_are_not_model_visible(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    for oracle_path in sorted((DATASET / "oracles" / "mini").glob("*.json")):
        oracle = _load(oracle_path)
        capture = _load(output / "captures" / oracle_path.name)
        visible_model_record = json.dumps(
            capture["model_records"], ensure_ascii=False, sort_keys=True
        )
        for requirement in oracle["must_satisfy"]:
            assert requirement["statement"] not in visible_model_record


def test_each_episode_used_a_distinct_destroyed_worker_root(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    _, summary = e1_batch
    assert len(summary.worker_roots) == 4
    assert len(set(summary.worker_roots)) == 4
    assert all(not Path(path).exists() for path in summary.worker_roots)
    database_paths = {
        Path(path) / "data" / "learning_companion.db"
        for path in summary.worker_roots
    }
    assert len(database_paths) == 4
    assert all(not path.exists() for path in database_paths)


def test_fixture_database_projection_is_recomputable_and_no_sqlite_is_published(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    assert list(output.rglob("*.db")) == []
    assert list(output.rglob("*.sqlite*")) == []
    assert list(output.rglob("*-wal")) == []
    assert list(output.rglob("*-shm")) == []
    for path in sorted((output / "captures").glob("*.json")):
        capture = _load(path)
        assert capture["database_projection_sha256"] == sha256_digest(
            capture["database_projection"]
        )
        assert capture["isolation_evidence"]["database_projection_recomputable"] is True
        assert capture["isolation_evidence"]["database_inside_worker_root"] is True


def test_worker_isolation_evidence_has_zero_external_calls(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    zero_fields = {
        "network_calls",
        "smtp_calls",
        "smtp_ssl_calls",
        "web_push_calls",
        "imap_calls",
        "imap_ssl_calls",
        "prohibited_file_access",
        "outside_sqlite_access",
        "subprocess_calls",
    }
    for path in sorted((output / "captures").glob("*.json")):
        evidence = _load(path)["isolation_evidence"]
        assert {field: evidence[field] for field in zero_fields} == {
            field: 0 for field in zero_fields
        }
        assert evidence["env_file_read"] is False
        assert evidence["repository_runtime_data_access"] is False
        assert evidence["background_services_started"] is False
        assert evidence["snapshot_provider_calls"] == {
            "search": 0,
            "open": 0,
            "validate": 0,
        }
        assert evidence["agent_observed_pending_delivery"] is (
            path.stem == "I-E1-MINI-001"
        )
        assert evidence["outbox_replay_confirmed"] is (
            path.stem == "I-E1-MINI-001"
        )


def test_fake_outbox_is_real_protocol_plus_recording_sink(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    capture = _load(output / "captures" / "I-E1-MINI-001.json")
    database = capture["database_projection"]

    assert [item["channel"] for item in database["notifications"]] == ["in_app", "email"]
    assert [item["status"] for item in database["notifications"]] == ["sent", "sent"]
    assert database["outbox_actions"][0]["destination"] == "smtp"
    assert database["outbox_actions"][0]["status"] == "delivered"
    assert database["outbox_actions"][0]["attempt"] == 1
    receipt = database["outbox_receipts"][0]
    assert receipt["status"] == "accepted"
    assert receipt["response"] == {
        "transport": "evaluation_sink",
        "emulated_destination": "smtp",
        "external_side_effect": False,
        "sink_version": "recording-delivery-sink-v1",
    }
    assert database["tool_invocations"][0]["status"] == "committed"
    assert capture["isolation_evidence"]["recording_sink_attempts"] == 1
    assert capture["isolation_evidence"]["outbox_replay_confirmed"] is True
    assert capture["isolation_evidence"]["agent_observed_pending_delivery"] is True
    assert capture["isolation_evidence"]["agent_observed_emulated_receipt"] is False


def test_frozen_time_reaches_events_notifications_and_receipts(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    capture = _load(output / "captures" / "I-E1-MINI-001.json")
    database = capture["database_projection"]
    expected = "2026-08-31T01:15:00.000000Z"

    assert database["run"]["started_at"] == expected
    assert database["run"]["completed_at"] == expected
    assert {item["created_at"] for item in database["run_events"]} == {expected}
    assert {item["sent_at"] for item in database["notifications"]} == {expected}
    assert database["outbox_receipts"][0]["accepted_at"] == expected
    assert database["proactive_decisions"][0]["decided_at"] == expected


def test_artifacts_contain_no_private_reasoning_or_routing_material(
    e1_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e1_batch
    encoded = b"\n".join(_all_output_bytes(output).values()).decode("utf-8")
    prohibited = (
        "reasoning_content",
        "private_reasoning",
        "chain_of_thought",
        "chain-of-thought",
        "chain of thought",
        "thinking_content",
        "thought process",
        "scratchpad",
        "思维链",
        "推理过程",
        "内部推理",
        "E1_PRIVATE_REASONING_SENTINEL_DO_NOT_EXPORT",
        "evaluation-stub-placeholder",
        "recipient@example.test",
        '"reply_token"',
        '"endpoint"',
        '"keys"',
    )
    lowered = encoded.casefold()
    # `reasoning_effort` is an intentional E0 manifest field. The standalone
    # private-work term and all private-content field names remain prohibited.
    assert re.search(r"(?<![a-z0-9_])reasoning(?![a-z0-9_])", lowered) is None
    assert all(value.casefold() not in lowered for value in prohibited)


def test_all_four_fixtures_are_byte_identical_across_runs(
    e1_batch: tuple[Path, RunAgentSummary],
    tmp_path: Path,
) -> None:
    first, _ = e1_batch
    second = tmp_path / "second"
    run_agent(dataset=DATASET, manifest=MANIFEST, output=second)

    assert _all_output_bytes(first) == _all_output_bytes(second)


def test_episode_and_track_filters_are_supported(tmp_path: Path) -> None:
    by_track = tmp_path / "track"
    summary = run_agent(
        dataset=DATASET,
        manifest=MANIFEST,
        output=by_track,
        track="revision",
    )
    assert summary.episode_ids == ("R-E1-MINI-001",)
    assert [path.name for path in (by_track / "episodes").glob("*.json")] == [
        "R-E1-MINI-001.json"
    ]
    by_episode = tmp_path / "episode"
    summary = run_agent(
        dataset=DATASET,
        manifest=MANIFEST,
        output=by_episode,
        episode_ids={"A-E1-MINI-001"},
    )
    assert summary.episode_ids == ("A-E1-MINI-001",)


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "marker.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(RunAgentError) as error:
        run_agent(dataset=DATASET, manifest=MANIFEST, output=output)

    assert error.value.code == "output_exists"
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_mid_run_validation_failure_publishes_no_partial_output(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    oracle_path = dataset / "oracles" / "mini" / "P-E1-MINI-001.json"
    oracle = _load(oracle_path)
    oracle["allowed_action_classes"] = ["REQUEST_USER_INPUT"]
    oracle["expected_effects"][0]["value"] = "REQUEST_USER_INPUT"
    oracle["envelope_sha256"] = oracle_envelope_digest(oracle)
    oracle_path.write_text(json.dumps(oracle, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "must-not-exist"

    errors = []
    for _ in range(2):
        with pytest.raises(RunAgentError) as error:
            run_agent(
                dataset=dataset,
                manifest=dataset / "manifests" / "e1-mini-stub.json",
                output=output,
                episode_ids={"P-E1-MINI-001"},
            )
        errors.append(error.value.as_dict())
        assert not output.exists()
        assert list(tmp_path.glob(".must-not-exist.e1-stage-*")) == []
    assert errors[0] == errors[1]
    assert errors[0]["error_code"] == "episode_invalid"


def test_real_model_requires_two_explicit_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RunAgentError) as error:
        run_agent(
            dataset=DATASET,
            manifest=MANIFEST,
            output=tmp_path / "real",
            model_mode="real",
            allow_real_model=False,
        )
    assert error.value.code == "real_model_not_allowed"
    assert not (tmp_path / "real").exists()


def test_real_model_provider_url_must_be_credential_free_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-caller-key")
    monkeypatch.setenv("OPENAI_API_BASE", "http://model.example.test/v1")
    with pytest.raises(EvaluationIsolationError, match="credential-free HTTPS"):
        worker_environment(
            project_root=PROJECT_ROOT,
            worker_root=tmp_path,
            model_mode="real",
            allow_real_model=True,
        )


@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("https://fake-provider.example/v1", "hy3"),
        ("https://tokenhub.tencentmaas.com/v1", "not-hy3"),
    ],
)
def test_real_model_requires_allowlisted_hy3_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    model: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-caller-key")
    monkeypatch.setenv("OPENAI_API_BASE", base_url)
    monkeypatch.setenv("MODEL_NAME", model)
    with pytest.raises(EvaluationIsolationError, match="Hy3 allowlist"):
        worker_environment(
            project_root=PROJECT_ROOT,
            worker_root=tmp_path,
            model_mode="real",
            allow_real_model=True,
        )


def test_isolation_guard_rejects_synthetic_env_and_database_canaries(
    tmp_path: Path,
) -> None:
    synthetic_project = tmp_path / "synthetic-project"
    worker_root = tmp_path / "worker"
    synthetic_project.mkdir()
    worker_root.mkdir()
    env_canary = synthetic_project / ".env"
    database_canary = synthetic_project / "data" / "learning_companion.db"
    database_canary.parent.mkdir()
    env_canary.write_bytes(b"SYNTHETIC_ENV_CANARY")
    database_canary.write_bytes(b"SYNTHETIC_DATABASE_CANARY")
    before = {
        env_canary: sha256_digest(env_canary.read_text(encoding="utf-8")),
        database_canary: sha256_digest(database_canary.read_text(encoding="utf-8")),
    }
    guard = IsolationGuard(
        project_root=synthetic_project,
        worker_root=worker_root,
        model_mode="stub",
        model_base_url="https://model.example.invalid/v1",
    )

    with pytest.raises(EvaluationIsolationError, match="runtime data"):
        guard._audit("open", (str(env_canary), "r", 0))
    with pytest.raises(EvaluationIsolationError, match="outside worker"):
        guard._audit("sqlite3.connect", (str(database_canary),))

    assert before == {
        env_canary: sha256_digest(env_canary.read_text(encoding="utf-8")),
        database_canary: sha256_digest(database_canary.read_text(encoding="utf-8")),
    }
    assert guard.counters["prohibited_file_access"] == 1
    assert guard.counters["outside_sqlite_access"] == 1


def test_isolation_guard_resolves_relative_repository_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_project = tmp_path / "synthetic-project"
    worker_root = tmp_path / "worker"
    (synthetic_project / "data").mkdir(parents=True)
    worker_root.mkdir()
    guard = IsolationGuard(
        project_root=synthetic_project,
        worker_root=worker_root,
        model_mode="stub",
        model_base_url="https://model.example.invalid/v1",
    )
    monkeypatch.chdir(synthetic_project)

    with pytest.raises(EvaluationIsolationError, match="runtime data"):
        guard._audit("open", (".env", "r", 0))
    with pytest.raises(EvaluationIsolationError, match="runtime data"):
        guard._audit("open", ("data/learning_companion.db", "r", 0))
    guard._audit("open", (str(worker_root / "public.json"), "w", 0))

    assert guard.counters["prohibited_file_access"] == 2


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"note": "Bearer abcdefghijklmnopqrstuvwxyz"}, "privacy.credential_value"),
        ({"note": "eyJabcdefgh.abcdefgh.abcdefgh"}, "privacy.credential_value"),
        ({"note": "person@nonreserved.example"}, "privacy.real_email"),
        ({"note": "13812345678"}, "privacy.personal_identifier_value"),
        ({"government_id": "synthetic"}, "privacy.personal_identifier_field"),
        ({"chain-of-thought": "private"}, "privacy.private_reasoning"),
        ({"internal-analysis": "private"}, "privacy.private_reasoning"),
    ],
)
def test_e1_publication_reuses_strict_e0_privacy(
    payload: dict[str, str],
    code: str,
) -> None:
    assert code in {issue.code for issue in privacy_issues(payload)}


@pytest.mark.parametrize(
    "business_text",
    ["reasoning", "chain of thought", "思维链", "推理过程"],
)
def test_privacy_does_not_treat_normal_business_vocabulary_as_private_work(
    business_text: str,
) -> None:
    assert privacy_issues({"note": business_text}) == []


def test_historical_runtime_cli_is_disabled_before_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    code = cli_main(
        [
            "run-agent",
            "--dataset",
            str(DATASET),
            "--manifest",
            str(MANIFEST),
            "--output",
            str(output),
            "--episode-id",
            "P-E1-MISSING",
        ]
    )
    error = json.loads(capsys.readouterr().err)
    assert code == 1
    assert error["status"] == "error"
    assert error["error_code"] == "legacy_execution_disabled"
    assert error["stage"] == "preflight"
    assert error["entrypoint"] == "run-agent"
    assert not output.exists()


@pytest.mark.asyncio
async def test_recorder_preserves_stream_chunks_and_nonstream_response_identity() -> None:
    turns = [
        {
            "delivery": "stream",
            "assistant_text": "public stream",
            "tool_calls": [
                {"call_id": "call:test:1", "name": "profile_get", "arguments": {}}
            ],
        },
        {"delivery": "nonstream", "assistant_text": "public final", "tool_calls": []},
    ]
    client = ScriptedModelClient(turns)
    recorder = EvaluationModelRecorder(client, invocation_mode="stub")
    request = {
        "messages": [
            {"role": "system", "content": "public system"},
            {"role": "assistant", "content": "visible", "reasoning_content": "private"},
        ],
        "tools": [{"type": "function", "function": {"name": "profile_get"}}],
        "stream": True,
    }
    stream = await recorder.chat.completions.create(**request)
    source_chunks = list(stream._source.chunks)
    received = [chunk async for chunk in stream]
    assert all(actual is expected for actual, expected in zip(received, source_chunks, strict=True))

    class FixedCompletions:
        def __init__(self) -> None:
            self.response = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="same object", tool_calls=None))]
            )

        async def create(self, **_: Any) -> Any:
            return self.response

    fixed = FixedCompletions()
    wrapped = EvaluationModelRecorder(
        SimpleNamespace(chat=SimpleNamespace(completions=fixed)),
        invocation_mode="stub",
    )
    response = await wrapped.chat.completions.create(messages=[], tools=[])
    assert response is fixed.response
    assert recorder.records[0]["visible_messages"][0] == {
        "role": "system",
        "content": "public system",
    }
    assert recorder.records[0]["system_prompt"]["digest"] == sha256_digest(
        "public system"
    )
    assert recorder.records[0]["visible_messages"][1] == {
        "role": "assistant",
        "content": "visible",
    }
    encoded = json.dumps(recorder.records, ensure_ascii=False)
    assert "reasoning_content" not in encoded
    assert "private" not in encoded
    assert recorder.records[0]["function_calls"] == [
        {"call_id": "call:test:1", "name": "profile_get", "canonical_arguments": {}}
    ]


@pytest.mark.asyncio
async def test_recorder_preserves_public_discussion_of_reasoning_terms() -> None:
    class FixedCompletions:
        async def create(self, **_: Any) -> Any:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="A lesson discusses reasoning and 推理过程.",
                            tool_calls=None,
                        )
                    )
                ]
            )

    recorder = EvaluationModelRecorder(
        SimpleNamespace(chat=SimpleNamespace(completions=FixedCompletions())),
        invocation_mode="stub",
    )
    await recorder.chat.completions.create(messages=[], tools=[])
    assert recorder.records[0]["assistant_text"] == (
        "A lesson discusses reasoning and 推理过程."
    )


def test_recorder_projection_never_reads_private_reasoning_values() -> None:
    class PrivateValueTrap(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            if key == "reasoning_content":
                raise AssertionError("private reasoning value must never be read")
            return "visible"

        def __iter__(self) -> Iterator[str]:
            return iter(("content", "reasoning_content"))

        def __len__(self) -> int:
            return 2

    assert public_projection(PrivateValueTrap()) == {"content": "visible"}


@pytest.mark.asyncio
async def test_public_snapshot_hits_are_fixed_and_digest_verified() -> None:
    provider = EvaluationSnapshotProvider(DATASET / "resources" / "e1-mini" / "snapshot.json")
    results = await provider.search("synthetic cuda profiling guide", 5)
    page = await provider.open("https://cuda.example.test/profiling-guide", 500)
    await provider.validate("https://cuda.example.test/profiling-guide")

    assert [item.as_dict() for item in results] == [
        {
            "title": "Synthetic CUDA Profiling Guide",
            "url": "https://cuda.example.test/profiling-guide",
        }
    ]
    assert page["snapshot_version"] == "decisionbench-e1-mini-resources-v1"
    assert page["redirect_count"] == 0
    assert provider.calls == {"search": 1, "open": 1, "validate": 1}
