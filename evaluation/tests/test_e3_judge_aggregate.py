from __future__ import annotations

import hashlib
import imaplib
import inspect
import json
import shutil
import smtplib
import socket
import sqlite3
import subprocess
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import pywebpush
from learning_agent_eval.aggregate import (
    _track_result,
    aggregate_episode,
    aggregate_results,
    validate_aggregate_result,
)
from learning_agent_eval.blinding import build_blind_judge_input
from learning_agent_eval.canonical import (
    canonical_json,
    canonical_json_bytes,
    sha256_digest,
)
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.e3_io import current_git_commit
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    integrity_result_digest,
    judge_result_digest,
    rule_result_digest,
)
from learning_agent_eval.judge import (
    FixedResponseJudgeProvider,
    JudgeEvaluationError,
    build_provider_request,
    evaluate_judges,
    validate_judge_result,
)
from learning_agent_eval.models import (
    AggregateResultV1,
    AggregateRunManifestV1,
    AggregateTrackResultV1,
    JudgeResponsePayloadV1,
    JudgeResultV1,
    JudgeRunManifestV1,
)
from learning_agent_eval.rubric import (
    DIMENSION_IDS,
    DIMENSION_WEIGHTS,
    LEVEL_SCALE,
    PUBLIC_DIMENSIONS,
    RUBRIC_DOCUMENT,
    RUBRIC_SHA256,
    TRACK_ANCHOR_DOCUMENT,
    TRACK_ANCHOR_SHA256,
    TRACK_ANCHORS,
)
from learning_agent_eval.rule_runner import evaluate_run_rules
from learning_agent_eval.runner import run_agent
from learning_agent_eval.schemas import schema_documents
from learning_agent_eval.validator import validate_dataset
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"
MANIFEST = DATASET / "manifests" / "e1-mini-stub.json"
FIXED_RESPONSES = (
    PROJECT_ROOT / "evaluation" / "fixtures" / "e3-fixed-judge-responses-v1.json"
)
SCHEMA_ROOT = PROJECT_ROOT / "evaluation" / "schemas"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _payload() -> dict[str, Any]:
    return deepcopy(_load(FIXED_RESPONSES)["responses"][0])


@pytest.fixture(scope="session")
def e3_batch(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("e3-batch")
    runtime = root / "runtime"
    rules = root / "rules"
    judges = root / "judges"
    aggregates = root / "aggregates"
    run_agent(dataset=DATASET, manifest=MANIFEST, output=runtime)
    evaluate_run_rules(input_path=runtime, output=rules)
    evaluate_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_results(
        episodes=runtime,
        rules=rules,
        judges=judges,
        output=aggregates,
    )
    return {
        "root": root,
        "runtime": runtime,
        "rules": rules,
        "judges": judges,
        "aggregates": aggregates,
    }


def _episode_and_rule(
    batch: dict[str, Path], episode_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        _load(batch["runtime"] / "episodes" / f"{episode_id}.json"),
        _load(batch["rules"] / "rules" / f"{episode_id}.json"),
    )


def _judge(batch: dict[str, Path], episode_id: str) -> dict[str, Any]:
    return _load(batch["judges"] / "judge-results" / f"{episode_id}.json")


def _relink_judge(
    episode: dict[str, Any], rule_result: dict[str, Any], judge_result: dict[str, Any]
) -> dict[str, Any]:
    linked = deepcopy(judge_result)
    linked["rule_result_sha256"] = rule_result["result_sha256"]
    linked["blind_input_sha256"] = build_blind_judge_input(episode, rule_result).sha256
    linked["result_sha256"] = judge_result_digest(linked)
    assert not validate_judge_result(linked, episode=episode, rule_result=rule_result)
    return linked


def _failed_rule(
    rule_result: dict[str, Any], severities: tuple[str, ...]
) -> dict[str, Any]:
    result = deepcopy(rule_result)
    for index, severity in enumerate(severities):
        check = result["checks"][index]
        check["status"] = "fail"
        check["severity"] = severity
        check["reason_code"] = f"synthetic_{severity}_failure"
    result["hard_gates"] = [
        check["check_id"]
        for check in result["checks"]
        if check["status"] == "fail" and check["severity"] == "critical"
    ]
    result["status"] = "fail"
    result["result_sha256"] = rule_result_digest(result)
    return result


def _make_rule_invalid(rules: Path, episode_id: str) -> None:
    rule_path = rules / "rules" / f"{episode_id}.json"
    result = _load(rule_path)
    result["checks"][0]["status"] = "invalid_input"
    result["checks"][0]["reason_code"] = "synthetic_missing_input"
    result["status"] = "invalid_input"
    result["hard_gates"] = []
    result["result_sha256"] = rule_result_digest(result)
    _write(rule_path, result)
    manifest_path = rules / "rule-manifest.json"
    manifest = _load(manifest_path)
    manifest["rule_result_digests"][episode_id] = result["result_sha256"]
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    _write(manifest_path, manifest)


def test_e3_schemas_are_strict_committed_and_drift_free() -> None:
    generated = schema_documents()
    names = {
        "judge-result-v1.schema.json",
        "judge-run-manifest-v1.schema.json",
        "aggregate-result-v1.schema.json",
        "aggregate-track-result-v1.schema.json",
        "aggregate-run-manifest-v1.schema.json",
    }
    assert names.issubset(generated)
    for name in names:
        committed = _load(SCHEMA_ROOT / name)
        assert committed == generated[name]
        assert committed["additionalProperties"] is False
        for definition in committed.get("$defs", {}).values():
            if isinstance(definition, dict) and "properties" in definition:
                assert definition.get("additionalProperties") is False


def test_unknown_fields_status_dimensions_and_levels_fail_closed(
    e3_batch: dict[str, Path],
) -> None:
    result = _judge(e3_batch, "A-E1-MINI-001")
    unknown = deepcopy(result)
    unknown["unexpected"] = True
    with pytest.raises(ValidationError):
        JudgeResultV1.model_validate(unknown)
    bad_status = deepcopy(result)
    bad_status["status"] = "scored"
    with pytest.raises(ValidationError):
        JudgeResultV1.model_validate(bad_status)
    bad_order = deepcopy(result)
    bad_order["dimensions"][0], bad_order["dimensions"][1] = (
        bad_order["dimensions"][1],
        bad_order["dimensions"][0],
    )
    with pytest.raises(ValidationError):
        JudgeResultV1.model_validate(bad_order)
    bad_level = deepcopy(result)
    bad_level["dimensions"][0]["level"] = 3
    with pytest.raises(ValidationError):
        JudgeResultV1.model_validate(bad_level)
    missing_issue = deepcopy(result)
    missing_issue["dimensions"][0]["level"] = 1
    with pytest.raises(ValidationError):
        JudgeResultV1.model_validate(missing_issue)


def test_public_rubric_and_track_anchors_are_versioned_stable_and_complete() -> None:
    assert tuple(item["dimension_id"] for item in PUBLIC_DIMENSIONS) == DIMENSION_IDS
    assert DIMENSION_WEIGHTS == {
        "D1": 15,
        "D2": 15,
        "D3": 20,
        "D4": 20,
        "D5": 15,
        "D6": 5,
        "D7": 10,
    }
    assert sum(DIMENSION_WEIGHTS.values()) == 100
    assert [item["level"] for item in LEVEL_SCALE] == [0, 1, 2]
    assert [item["weight_fraction"] for item in LEVEL_SCALE] == [0.0, 0.5, 1.0]
    assert set(TRACK_ANCHORS) == {
        "planning",
        "intervention",
        "assessment",
        "revision",
    }
    for anchors in TRACK_ANCHORS.values():
        assert tuple(anchors) == DIMENSION_IDS
        assert all(
            [item["level"] for item in levels] == [0, 1, 2]
            for levels in anchors.values()
        )
    assert RUBRIC_SHA256 == sha256_digest(RUBRIC_DOCUMENT)
    assert TRACK_ANCHOR_SHA256 == sha256_digest(TRACK_ANCHOR_DOCUMENT)


def test_blind_projection_removes_labels_generator_authors_capture_and_private_data(
    e3_batch: dict[str, Path],
) -> None:
    episode, rule_result = _episode_and_rule(e3_batch, "A-E1-MINI-001")
    authoritative = deepcopy(episode)
    episode["episode_id"] = "Good-Baseline-001"
    episode["scenario_family_id"] = "Candidate-Severe-001"
    episode["tags"] = ["good", "mild", "severe", "baseline", "candidate"]
    episode["environment"]["model"]["name"] = "Candidate-Generator-v9"
    episode["environment"]["prompt"]["version"] = "Baseline-Prompt-v9"
    episode["provenance"]["author_role"] = "good_author"
    episode["provenance"]["reviewer_role"] = "candidate_reviewer"
    episode["oracle"]["adjudication_note"] = "Severe label author note."
    episode["trigger"]["payload"]["private_reasoning"] = "never-export-this-secret"
    episode["trigger"]["payload"]["endpoint"] = "https://route.example.test/private"
    episode["capture"] = {"raw": "never-export-capture"}
    blind_a = build_blind_judge_input(episode, rule_result)
    blind_b = build_blind_judge_input(episode, rule_result)
    request = build_provider_request(blind_a)
    wire = canonical_json(request).casefold()
    for forbidden in (
        "good-baseline-001",
        "candidate-severe-001",
        "candidate-generator-v9",
        "baseline-prompt-v9",
        "good_author",
        "candidate_reviewer",
        "severe label author note",
        "never-export-this-secret",
        "route.example.test",
        "never-export-capture",
        "e1-scripted-model",
    ):
        assert forbidden not in wire
    assert '"capture":' not in wire
    assert '"provenance":' not in wire
    assert blind_a == blind_b
    assert episode["capture"] == {"raw": "never-export-capture"}
    assert authoritative["episode_id"] == "A-E1-MINI-001"
    assert blind_a.judge_id.startswith("judge-")


def test_judge_request_uses_authoritative_rules_without_recomputing_them(
    e3_batch: dict[str, Path],
) -> None:
    episode, rule_result = _episode_and_rule(e3_batch, "I-E1-MINI-001")
    request = build_provider_request(build_blind_judge_input(episode, rule_result))
    user_document = json.loads(request["messages"][1]["content"])
    facts = user_document["authoritative_rule_facts"]
    assert facts["status"] == rule_result["status"]
    assert facts["hard_gates"] == rule_result["hard_gates"]
    assert set(facts["checks"][0]) == {
        "check_id",
        "rule_pack",
        "status",
        "severity",
        "evidence_paths",
        "reason_code",
    }
    assert all("observed" not in check for check in facts["checks"])
    assert all("expected" not in check for check in facts["checks"])
    source = inspect.getsource(FixedResponseJudgeProvider)
    assert "track" not in source
    assert "oracle" not in source
    assert "scripted" not in source


def test_rule_episode_digest_mismatch_is_rejected_without_publication(
    e3_batch: dict[str, Path], tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    rules = tmp_path / "rules"
    shutil.copytree(e3_batch["runtime"], runtime)
    shutil.copytree(e3_batch["rules"], rules)
    episode_id = "A-E1-MINI-001"
    rule_path = rules / "rules" / f"{episode_id}.json"
    rule = _load(rule_path)
    rule["episode_sha256"] = "f" * 64
    rule["result_sha256"] = rule_result_digest(rule)
    _write(rule_path, rule)
    integrity_path = rules / "integrity" / f"{episode_id}.json"
    integrity = _load(integrity_path)
    integrity["episode_sha256"] = "f" * 64
    integrity["result_sha256"] = integrity_result_digest(integrity)
    _write(integrity_path, integrity)
    manifest_path = rules / "rule-manifest.json"
    manifest = _load(manifest_path)
    manifest["input_episode_digests"][episode_id] = "f" * 64
    manifest["rule_result_digests"][episode_id] = rule["result_sha256"]
    manifest["integrity_result_digests"][episode_id] = integrity["result_sha256"]
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    _write(manifest_path, manifest)
    assert validate_dataset(rules).ok
    output = tmp_path / "judge-output"
    with pytest.raises(JudgeEvaluationError) as error:
        evaluate_judges(
            episodes=runtime,
            rules=rules,
            output=output,
            judge_mode="stub",
            provider=FixedResponseJudgeProvider([_payload()]),
            episode_ids={episode_id},
        )
    assert error.value.code == "episode_digest_mismatch"
    assert not output.exists()


def test_rule_invalid_input_never_calls_judge_and_is_published_as_invalid(
    e3_batch: dict[str, Path], tmp_path: Path
) -> None:
    rules = tmp_path / "rules"
    shutil.copytree(e3_batch["rules"], rules)
    episode_id = "A-E1-MINI-001"
    _make_rule_invalid(rules, episode_id)
    provider = FixedResponseJudgeProvider([_payload()])
    output = tmp_path / "judges"
    summary = evaluate_judges(
        episodes=e3_batch["runtime"],
        rules=rules,
        output=output,
        judge_mode="stub",
        provider=provider,
        episode_ids={episode_id},
    )
    result = _load(output / "judge-results" / f"{episode_id}.json")
    assert provider.calls == 0
    assert summary.invalid_episode_ids == (episode_id,)
    assert result["status"] == "invalid_input"
    assert result["dimensions"] == []
    assert result["formal_evaluation_result"] is False
    aggregate_output = tmp_path / "aggregates"
    aggregate_summary = aggregate_results(
        episodes=e3_batch["runtime"],
        rules=rules,
        judges=output,
        output=aggregate_output,
        episode_ids={episode_id},
    )
    aggregate = _load(aggregate_output / "episodes" / f"{episode_id}.json")
    track = _load(aggregate_output / "tracks" / "assessment.json")
    assert aggregate_summary.invalid_episode_ids == (episode_id,)
    assert aggregate["status"] == "invalid_input"
    assert aggregate["raw_score"] is None
    assert aggregate["final_score"] is None
    assert track["score_count"] == 0
    assert track["mean_score"] is None


def test_incomplete_episode_never_calls_judge_or_publishes(
    e3_batch: dict[str, Path], tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    shutil.copytree(e3_batch["runtime"], runtime)
    episode_id = "A-E1-MINI-001"
    episode_path = runtime / "episodes" / f"{episode_id}.json"
    episode = _load(episode_path)
    del episode["state_after"]
    _write(episode_path, episode)
    provider = FixedResponseJudgeProvider([_payload()])
    output = tmp_path / "judges"
    with pytest.raises(JudgeEvaluationError):
        evaluate_judges(
            episodes=runtime,
            rules=e3_batch["rules"],
            output=output,
            judge_mode="stub",
            provider=provider,
            episode_ids={episode_id},
        )
    assert provider.calls == 0
    assert not output.exists()


def test_evidence_must_resolve_in_same_original_episode_and_blind_projection(
    e3_batch: dict[str, Path],
) -> None:
    episode_id = "A-E1-MINI-001"
    episode, rule_result = _episode_and_rule(e3_batch, episode_id)
    result = _judge(e3_batch, episode_id)
    result["dimensions"][0]["evidence_paths"] = ["provenance.author_role"]
    result["result_sha256"] = judge_result_digest(result)
    assert "judge_evidence_invalid" in validate_judge_result(
        result, episode=episode, rule_result=rule_result
    )
    result["dimensions"][0]["evidence_paths"] = ["capture.raw"]
    result["result_sha256"] = judge_result_digest(result)
    assert "judge_evidence_invalid" in validate_judge_result(
        result, episode=episode, rule_result=rule_result
    )


def test_one_schema_repair_succeeds_and_second_failure_becomes_judge_error(
    e3_batch: dict[str, Path], tmp_path: Path
) -> None:
    episode_id = "P-E1-MINI-001"
    invalid = _payload()
    invalid["dimensions"][0]["evidence_paths"] = ["provenance.author_role"]
    repaired_provider = FixedResponseJudgeProvider([invalid, _payload()])
    repaired_output = tmp_path / "repaired"
    repaired = evaluate_judges(
        episodes=e3_batch["runtime"],
        rules=e3_batch["rules"],
        output=repaired_output,
        judge_mode="stub",
        provider=repaired_provider,
        episode_ids={episode_id},
    )
    assert repaired_provider.calls == 2
    assert repaired.repair_attempted_episode_ids == (episode_id,)
    assert (
        _load(repaired_output / "judge-results" / f"{episode_id}.json")["status"]
        == "complete"
    )

    failed_provider = FixedResponseJudgeProvider([invalid, invalid])
    failed_output = tmp_path / "failed"
    failed = evaluate_judges(
        episodes=e3_batch["runtime"],
        rules=e3_batch["rules"],
        output=failed_output,
        judge_mode="stub",
        provider=failed_provider,
        episode_ids={episode_id},
    )
    result = _load(failed_output / "judge-results" / f"{episode_id}.json")
    assert failed_provider.calls == 2
    assert failed.judge_error_episode_ids == (episode_id,)
    assert result["status"] == "judge_error"
    assert result["dimensions"] == []
    assert result["error_code"] == "judge_response_invalid"


def test_stub_and_real_formal_states_and_double_opt_in(
    e3_batch: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(JudgeEvaluationError) as error:
        evaluate_judges(
            episodes=e3_batch["runtime"],
            rules=e3_batch["rules"],
            output=tmp_path / "not-allowed",
            judge_mode="real",
            provider=FixedResponseJudgeProvider([_payload()]),
            episode_ids={"R-E1-MINI-001"},
        )
    assert error.value.code == "real_judge_not_allowed"
    with pytest.raises(JudgeEvaluationError) as error:
        evaluate_judges(
            episodes=e3_batch["runtime"],
            rules=e3_batch["rules"],
            output=tmp_path / "fixed-response-real",
            judge_mode="real",
            allow_real_judge=True,
            provider=FixedResponseJudgeProvider([_payload()]),
            episode_ids={"R-E1-MINI-001"},
        )
    assert error.value.code == "judge_provider_mode_mismatch"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(JudgeEvaluationError) as error:
        evaluate_judges(
            episodes=e3_batch["runtime"],
            rules=e3_batch["rules"],
            output=tmp_path / "real-without-key",
            judge_mode="real",
            allow_real_judge=True,
            episode_ids={"R-E1-MINI-001"},
        )
    assert error.value.code == "judge_credentials_missing"
    episode, rule_result = _episode_and_rule(e3_batch, "R-E1-MINI-001")
    result = _judge(e3_batch, "R-E1-MINI-001")
    result["judge_mode"] = "real"
    result["result_sha256"] = judge_result_digest(result)
    assert not validate_judge_result(
        result, episode=episode, rule_result=rule_result
    )
    assert result["judge_mode"] == "real"
    assert result["formal_evaluation_result"] is False
    assert result["evaluation_status"] == "not_a_formal_model_evaluation"
    assert all(
        not _load(path)["formal_evaluation_result"]
        for path in e3_batch["judges"].joinpath("judge-results").glob("*.json")
    )


def test_critical_major_and_multiple_caps_preserve_rule_precedence(
    e3_batch: dict[str, Path],
) -> None:
    episode_id = "R-E1-MINI-001"
    episode, original_rule = _episode_and_rule(e3_batch, episode_id)
    original_judge = _judge(e3_batch, episode_id)

    critical_rule = _failed_rule(original_rule, ("critical",))
    critical_judge = _relink_judge(episode, critical_rule, original_judge)
    critical = aggregate_episode(
        episode=episode,
        rule_result=critical_rule,
        judge_result=critical_judge,
    )
    assert critical["raw_score"] == 100
    assert critical["final_score"] == 39
    assert critical["episode_outcome"] == "fail"
    assert [cap["maximum_score"] for cap in critical["applied_caps"]] == [39]

    major_rule = _failed_rule(original_rule, ("major",))
    major_judge = _relink_judge(episode, major_rule, original_judge)
    major = aggregate_episode(
        episode=episode, rule_result=major_rule, judge_result=major_judge
    )
    assert major["final_score"] == 69
    assert major["episode_outcome"] == "pass"

    multiple_rule = _failed_rule(original_rule, ("critical", "major"))
    multiple_judge = _relink_judge(episode, multiple_rule, original_judge)
    multiple = aggregate_episode(
        episode=episode,
        rule_result=multiple_rule,
        judge_result=multiple_judge,
    )
    assert multiple["final_score"] == 39
    assert [cap["maximum_score"] for cap in multiple["applied_caps"]] == [39, 69]

    minor_rule = _failed_rule(original_rule, ("minor",))
    minor_judge = _relink_judge(episode, minor_rule, original_judge)
    minor = aggregate_episode(
        episode=episode, rule_result=minor_rule, judge_result=minor_judge
    )
    assert minor["final_score"] == 100
    assert minor["applied_caps"] == []


def test_judge_suggested_gate_never_becomes_an_actual_gate(
    e3_batch: dict[str, Path],
) -> None:
    episode_id = "P-E1-MINI-001"
    episode, rule_result = _episode_and_rule(e3_batch, episode_id)
    judge_result = _judge(e3_batch, episode_id)
    judge_result["suggested_hard_gates"] = [
        {
            "suggestion_id": "suggestion:public-risk",
            "reason_code": "semantic_review_suggested",
            "evidence_paths": ["result.action_class"],
            "public_summary": "A future human review may inspect this semantic concern.",
        }
    ]
    judge_result["result_sha256"] = judge_result_digest(judge_result)
    aggregate = aggregate_episode(
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )
    assert aggregate["judge_suggested_hard_gates"]
    assert aggregate["actual_hard_gates"] == []
    assert aggregate["applied_caps"] == []
    assert aggregate["episode_outcome"] == "pass"


def test_invalid_and_judge_error_are_not_scored_or_averaged_as_zero(
    e3_batch: dict[str, Path],
) -> None:
    episode_id = "A-E1-MINI-001"
    episode, rule_result = _episode_and_rule(e3_batch, episode_id)
    complete_judge = _judge(e3_batch, episode_id)
    complete = aggregate_episode(
        episode=episode,
        rule_result=rule_result,
        judge_result=complete_judge,
    )
    error_judge = deepcopy(complete_judge)
    error_judge.update(
        {
            "dimensions": [],
            "semantic_issues": [],
            "suggested_hard_gates": [],
            "status": "judge_error",
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
            "error_code": "judge_response_invalid",
        }
    )
    error_judge["result_sha256"] = judge_result_digest(error_judge)
    failed = aggregate_episode(
        episode=episode,
        rule_result=rule_result,
        judge_result=error_judge,
    )
    assert failed["status"] == "judge_error"
    assert failed["raw_score"] is None
    assert failed["final_score"] is None
    second = deepcopy(failed)
    second["episode_id"] = "A-E1-MINI-ERROR"
    summary = _track_result("assessment", [complete, second])
    assert summary["score_count"] == 1
    assert summary["mean_score"] == complete["final_score"]
    assert summary["judge_error_episode_ids"] == ["A-E1-MINI-ERROR"]


def test_four_track_chain_validates_without_an_overall_score(
    e3_batch: dict[str, Path],
) -> None:
    assert validate_dataset(e3_batch["judges"]).ok
    assert validate_dataset(e3_batch["aggregates"]).ok
    judge_results = [
        _load(path)
        for path in sorted(e3_batch["judges"].joinpath("judge-results").glob("*.json"))
    ]
    aggregates = [
        _load(path)
        for path in sorted(e3_batch["aggregates"].joinpath("episodes").glob("*.json"))
    ]
    assert len(judge_results) == len(aggregates) == 4
    assert all(result["status"] == "complete" for result in judge_results)
    assert all(result["formal_evaluation_result"] is False for result in aggregates)
    tracks = e3_batch["aggregates"] / "tracks"
    assert {path.stem for path in tracks.glob("*.json")} == {
        "planning",
        "intervention",
        "assessment",
        "revision",
    }
    assert not (tracks / "overall.json").exists()
    judge_bytes = b"\n".join(_files(e3_batch["judges"]).values())
    assert b'"messages"' not in judge_bytes
    assert b'"response_format"' not in judge_bytes
    assert b'"blind-judge-input-v1"' not in judge_bytes
    manifest = _load(e3_batch["aggregates"] / "run-manifest.json")
    assert "overall_score" not in manifest
    assert set(manifest["track_result_digests"]) == {
        "planning",
        "intervention",
        "assessment",
        "revision",
    }


def test_judge_and_aggregate_outputs_are_byte_deterministic(
    e3_batch: dict[str, Path], tmp_path: Path
) -> None:
    judge_a = tmp_path / "judge-a"
    judge_b = tmp_path / "judge-b"
    for output in (judge_a, judge_b):
        evaluate_judges(
            episodes=e3_batch["runtime"],
            rules=e3_batch["rules"],
            output=output,
            judge_mode="stub",
            stub_response=FIXED_RESPONSES,
        )
    assert _files(judge_a) == _files(judge_b)
    aggregate_a = tmp_path / "aggregate-a"
    aggregate_b = tmp_path / "aggregate-b"
    aggregate_results(
        episodes=e3_batch["runtime"],
        rules=e3_batch["rules"],
        judges=judge_a,
        output=aggregate_a,
    )
    aggregate_results(
        episodes=e3_batch["runtime"],
        rules=e3_batch["rules"],
        judges=judge_b,
        output=aggregate_b,
    )
    assert _files(aggregate_a) == _files(aggregate_b)


def test_episode_and_track_filters_are_stable_for_both_e3_clis(
    e3_batch: dict[str, Path], tmp_path: Path
) -> None:
    judge_episode = tmp_path / "judge-episode"
    assert (
        cli_main(
            [
                "evaluate-judge",
                "--episodes",
                str(e3_batch["runtime"]),
                "--rules",
                str(e3_batch["rules"]),
                "--output",
                str(judge_episode),
                "--judge-mode",
                "stub",
                "--stub-response",
                str(FIXED_RESPONSES),
                "--episode-id",
                "P-E1-MINI-001",
            ]
        )
        == 0
    )
    assert _load(judge_episode / "run-manifest.json")["episode_ids"] == [
        "P-E1-MINI-001"
    ]
    judge_track = tmp_path / "judge-track"
    assert (
        cli_main(
            [
                "evaluate-judge",
                "--episodes",
                str(e3_batch["runtime"]),
                "--rules",
                str(e3_batch["rules"]),
                "--output",
                str(judge_track),
                "--judge-mode",
                "stub",
                "--stub-response",
                str(FIXED_RESPONSES),
                "--track",
                "intervention",
            ]
        )
        == 0
    )
    assert _load(judge_track / "run-manifest.json")["episode_ids"] == ["I-E1-MINI-001"]
    aggregate_episode_output = tmp_path / "aggregate-episode"
    assert (
        cli_main(
            [
                "aggregate-results",
                "--episodes",
                str(e3_batch["runtime"]),
                "--rules",
                str(e3_batch["rules"]),
                "--judges",
                str(e3_batch["judges"]),
                "--output",
                str(aggregate_episode_output),
                "--episode-id",
                "R-E1-MINI-001",
            ]
        )
        == 0
    )
    assert _load(aggregate_episode_output / "run-manifest.json")["episode_ids"] == [
        "R-E1-MINI-001"
    ]
    aggregate_track_output = tmp_path / "aggregate-track"
    assert (
        cli_main(
            [
                "aggregate-results",
                "--episodes",
                str(e3_batch["runtime"]),
                "--rules",
                str(e3_batch["rules"]),
                "--judges",
                str(e3_batch["judges"]),
                "--output",
                str(aggregate_track_output),
                "--track",
                "assessment",
            ]
        )
        == 0
    )
    assert _load(aggregate_track_output / "run-manifest.json")["episode_ids"] == [
        "A-E1-MINI-001"
    ]


def test_manifest_digests_git_commit_and_closed_inventories_are_recomputable(
    e3_batch: dict[str, Path],
) -> None:
    judge_manifest = _load(e3_batch["judges"] / "run-manifest.json")
    aggregate_manifest = _load(e3_batch["aggregates"] / "run-manifest.json")
    assert JudgeRunManifestV1.model_validate(judge_manifest)
    assert AggregateRunManifestV1.model_validate(aggregate_manifest)
    assert judge_manifest["manifest_sha256"] == artifact_manifest_digest(judge_manifest)
    assert aggregate_manifest["manifest_sha256"] == artifact_manifest_digest(
        aggregate_manifest
    )
    assert judge_manifest["git_commit"] == current_git_commit()
    assert aggregate_manifest["git_commit"] == current_git_commit()
    assert aggregate_manifest["input_judge_manifest_sha256"] == judge_manifest[
        "manifest_sha256"
    ]
    for key in (
        "judge_version",
        "judge_config_version",
        "judge_config_sha256",
        "judge_prompt_version",
        "judge_prompt_sha256",
        "rubric_version",
        "rubric_sha256",
        "track_anchor_version",
        "track_anchor_sha256",
        "judge_mode",
    ):
        assert aggregate_manifest[key] == judge_manifest[key]
    for path in e3_batch["aggregates"].joinpath("tracks").glob("*.json"):
        assert AggregateTrackResultV1.model_validate(_load(path))
    for path in e3_batch["aggregates"].joinpath("episodes").glob("*.json"):
        assert AggregateResultV1.model_validate(_load(path))


def test_existing_output_is_not_overwritten_and_mid_publish_failure_is_atomic(
    e3_batch: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "marker"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(JudgeEvaluationError) as error:
        evaluate_judges(
            episodes=e3_batch["runtime"],
            rules=e3_batch["rules"],
            output=existing,
            judge_mode="stub",
            provider=FixedResponseJudgeProvider([_payload()]),
        )
    assert error.value.code == "output_exists"
    assert marker.read_text(encoding="utf-8") == "keep"

    def fail_commit() -> str:
        raise JudgeEvaluationError(
            "git_commit_unavailable", "publish", "batch", "commit unavailable"
        )

    monkeypatch.setattr("learning_agent_eval.judge.current_git_commit", fail_commit)
    target = tmp_path / "atomic-target"
    with pytest.raises(JudgeEvaluationError):
        evaluate_judges(
            episodes=e3_batch["runtime"],
            rules=e3_batch["rules"],
            output=target,
            judge_mode="stub",
            provider=FixedResponseJudgeProvider([_payload()]),
            episode_ids={"A-E1-MINI-001"},
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".atomic-target.e3-judge-stage-*"))


def test_stub_judge_uses_zero_network_database_env_provider_and_subprocess_calls(
    e3_batch: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {
        "network": 0,
        "database": 0,
        "smtp": 0,
        "smtp_ssl": 0,
        "imap": 0,
        "imap_ssl": 0,
        "web_push": 0,
        "subprocess": 0,
    }

    def trap(name: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            calls[name] += 1
            raise AssertionError(f"{name} must not be called")

        return fail

    monkeypatch.setattr(urllib.request, "urlopen", trap("network"))
    monkeypatch.setattr(socket, "create_connection", trap("network"))
    monkeypatch.setattr(sqlite3, "connect", trap("database"))
    monkeypatch.setattr(smtplib, "SMTP", trap("smtp"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", trap("smtp_ssl"))
    monkeypatch.setattr(imaplib, "IMAP4", trap("imap"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", trap("imap_ssl"))
    monkeypatch.setattr(pywebpush, "webpush", trap("web_push"))
    monkeypatch.setattr(subprocess, "run", trap("subprocess"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used-in-stub")
    provider = FixedResponseJudgeProvider([_payload()])
    output = tmp_path / "judge"
    evaluate_judges(
        episodes=e3_batch["runtime"],
        rules=e3_batch["rules"],
        output=output,
        judge_mode="stub",
        provider=provider,
        episode_ids={"I-E1-MINI-001"},
    )
    aggregate_output = tmp_path / "aggregate"
    aggregate_results(
        episodes=e3_batch["runtime"],
        rules=e3_batch["rules"],
        judges=output,
        output=aggregate_output,
    )
    assert provider.calls == 1
    assert not hasattr(provider, "requests")
    assert calls == {name: 0 for name in calls}
    assert ".env" not in inspect.getsource(evaluate_judges)
    for artifact_root in (output, aggregate_output):
        assert not any(
            path.name.endswith((".sqlite", ".sqlite3", ".db", "-wal", "-shm"))
            for path in artifact_root.rglob("*")
        )


def test_cli_errors_are_structured_and_stable(
    e3_batch: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "exists"
    output.mkdir()
    status = cli_main(
        [
            "evaluate-judge",
            "--episodes",
            str(e3_batch["runtime"]),
            "--rules",
            str(e3_batch["rules"]),
            "--output",
            str(output),
            "--judge-mode",
            "stub",
            "--stub-response",
            str(FIXED_RESPONSES),
        ]
    )
    assert status == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "episode_id": "batch",
        "error_code": "output_exists",
        "message": "output directory already exists",
        "stage": "prepare",
        "status": "error",
    }

    malformed_response = tmp_path / "malformed-response.json"
    malformed_response.write_text("not-json", encoding="utf-8")
    status = cli_main(
        [
            "evaluate-judge",
            "--episodes",
            str(e3_batch["runtime"]),
            "--rules",
            str(e3_batch["rules"]),
            "--output",
            str(tmp_path / "malformed-output"),
            "--judge-mode",
            "stub",
            "--stub-response",
            str(malformed_response),
        ]
    )
    assert status == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "episode_id": "batch",
        "error_code": "stub_response_invalid",
        "message": "fixed Judge response document is invalid",
        "stage": "prepare",
        "status": "error",
    }
    assert not (tmp_path / "malformed-output").exists()


def test_fixed_stub_fixture_declares_non_formal_engineering_status() -> None:
    document = _load(FIXED_RESPONSES)
    assert document["schema_version"] == "fixed-judge-responses-v1"
    assert document["judge_mode"] == "stub"
    assert document["formal_evaluation_result"] is False
    assert document["evaluation_status"] == "not_a_formal_model_evaluation"
    assert JudgeResponsePayloadV1.model_validate(document["responses"][0])


def test_new_schema_bytes_have_stable_sha256() -> None:
    expected = {
        "judge-result-v1.schema.json": "640c68a4c4c50c06193f02be16c6f1a5cb4536e445598a8873e8f7cec0c09e34",
        "judge-run-manifest-v1.schema.json": "037ea454aa8ae5d5a74392861fd12e595577bcc09f91ec840eb410ff5d080525",
        "aggregate-result-v1.schema.json": "3b7eca0af0285d3b4e4b7cbbfc15de7aae9be05387d3960f0a7c97ef9db37073",
        "aggregate-track-result-v1.schema.json": "904754b2bd4af0d0001ae688c388ee9d63894443114a715f93b29897984a0c5d",
        "aggregate-run-manifest-v1.schema.json": "aa00475e19b66ac1ea81f2a6d6408654431c371117b6548076216081c5565c1d",
    }
    assert {
        name: hashlib.sha256((SCHEMA_ROOT / name).read_bytes()).hexdigest()
        for name in expected
    } == expected


def test_aggregate_validator_rejects_tampered_scores_and_hard_gates(
    e3_batch: dict[str, Path],
) -> None:
    episode_id = "I-E1-MINI-001"
    episode, rule_result = _episode_and_rule(e3_batch, episode_id)
    judge_result = _judge(e3_batch, episode_id)
    aggregate = _load(e3_batch["aggregates"] / "episodes" / f"{episode_id}.json")
    aggregate["final_score"] = 12.0
    aggregate["result_sha256"] = sha256_digest(
        {key: value for key, value in aggregate.items() if key != "result_sha256"}
    )
    assert "aggregate_score_invalid" in validate_aggregate_result(
        aggregate,
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )
    aggregate["actual_hard_gates"] = ["judge:suggested-only"]
    aggregate["episode_outcome"] = "fail"
    aggregate["result_sha256"] = sha256_digest(
        {key: value for key, value in aggregate.items() if key != "result_sha256"}
    )
    assert "aggregate_rule_precedence_invalid" in validate_aggregate_result(
        aggregate,
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )

    aggregate = _load(
        e3_batch["aggregates"] / "episodes" / f"{episode_id}.json"
    )
    aggregate.update(
        {
            "status": "judge_error",
            "dimensions": [],
            "raw_score": None,
            "applied_caps": [],
            "final_score": None,
            "episode_outcome": "judge_error",
            "invalid_reason_code": "synthetic_error",
        }
    )
    aggregate["result_sha256"] = sha256_digest(
        {key: value for key, value in aggregate.items() if key != "result_sha256"}
    )
    assert "aggregate_input_status_invalid" in validate_aggregate_result(
        aggregate,
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )


def test_no_e4_to_e8_outputs_or_placeholder_commands_are_created(
    e3_batch: dict[str, Path],
) -> None:
    names = set(_files(e3_batch["root"]))
    assert not any(
        token in name
        for name in names
        for token in ("primary", "calibration", "report", "compare", "case", "release")
    )
    from learning_agent_eval.cli import _parser

    help_text = _parser().format_help()
    assert "validate-method" not in help_text
    assert " report" not in help_text
    assert " compare" not in help_text
