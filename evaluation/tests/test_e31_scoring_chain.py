from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from learning_agent_eval.aggregate_v2 import (
    AggregateEvaluationV2Error,
    aggregate_episode_v2,
    aggregate_results_v2,
    validate_aggregate_result_v2,
)
from learning_agent_eval.blinding_v2 import build_blind_judge_input_v2
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.e31_io import load_e31_inputs
from learning_agent_eval.integrity import judge_result_digest, rule_result_digest
from learning_agent_eval.judge_v2 import (
    FixedResponseJudgeProviderV2,
    JudgeEvaluationV2Error,
    _evaluate_one,
    evaluate_judges_v2,
)
from learning_agent_eval.models import (
    AggregateResultV2,
    JudgeResultV2,
    RuleResultV2,
)
from learning_agent_eval.rule_runner_v2 import evaluate_run_rules_v2
from learning_agent_eval.runner_v3 import run_agent_v3
from learning_agent_eval.runtime_metadata import dependency_lock_sha256
from learning_agent_eval.validator import validate_dataset, validate_episode
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v3-engineering"
CASE_MANIFEST = DATASET / "manifest.json"
FIXED_RESPONSES = (
    PROJECT_ROOT / "evaluation" / "fixtures" / "e3-fixed-judge-responses-v1.json"
)
WRONG_ACTION_ID = "episode-cea34a758d8acd7eecbcd12e"
PLANNING_ID = "episode-2427940261d17c68e68d29f9"
RUNTIME_FAILURE_ID = "failure:case-e31-p-provider-failure"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _fixed_payload() -> dict[str, Any]:
    responses = _load(FIXED_RESPONSES)["responses"]
    assert isinstance(responses, list)
    value = responses[0]
    assert isinstance(value, dict)
    return deepcopy(value)


@pytest.fixture(scope="module")
def e31_chain(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("e31-chain")
    runtime = root / "runtime"
    rules = root / "rules"
    judges = root / "judges"
    aggregate = root / "aggregate"
    runtime_summary = run_agent_v3(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        model_mode="stub",
    )
    rule_summary = evaluate_run_rules_v2(input_path=runtime, output=rules)
    judge_summary = evaluate_judges_v2(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_summary = aggregate_results_v2(
        episodes=runtime,
        rules=rules,
        judges=judges,
        output=aggregate,
    )
    return {
        "root": root,
        "runtime": runtime,
        "rules": rules,
        "judges": judges,
        "aggregate": aggregate,
        "runtime_summary": runtime_summary,
        "rule_summary": rule_summary,
        "judge_summary": judge_summary,
        "aggregate_summary": aggregate_summary,
    }


def test_v3_wrong_decision_is_valid_and_receives_critical_fail(
    e31_chain: dict[str, Any],
) -> None:
    episode = _load(e31_chain["runtime"] / "episodes" / f"{WRONG_ACTION_ID}.json")
    rule = _load(e31_chain["rules"] / "rules" / f"{WRONG_ACTION_ID}.json")

    assert validate_episode(episode, source="wrong-action.json") == ()
    assert episode["completeness"]["decision_correctness_evaluated"] is False
    assert rule["status"] == "fail"
    assert "common.action_envelope" in rule["hard_gates"]
    assert any(
        item["status"] == "fail"
        and item["severity"] == "critical"
        and item["check_id"] == "common.action_envelope"
        for item in rule["checks"]
    )


def test_runtime_failure_does_not_rollback_siblings_and_is_propagated(
    e31_chain: dict[str, Any],
) -> None:
    runtime_manifest = _load(e31_chain["runtime"] / "run-manifest.json")
    rule_manifest = _load(e31_chain["rules"] / "rule-manifest.json")
    judge_manifest = _load(e31_chain["judges"] / "run-manifest.json")
    aggregate_manifest = _load(e31_chain["aggregate"] / "run-manifest.json")

    assert len(e31_chain["runtime_summary"].episode_ids) == 6
    assert e31_chain["runtime_summary"].failure_ids == (RUNTIME_FAILURE_ID,)
    assert len(runtime_manifest["terminals"]) == 7
    assert (e31_chain["runtime"] / "failures" / f"{RUNTIME_FAILURE_ID}.json").is_file()
    for manifest in (rule_manifest, judge_manifest, aggregate_manifest):
        assert manifest["runtime_failure_ids"] == [RUNTIME_FAILURE_ID]
        assert set(manifest["input_runtime_failure_digests"]) == {RUNTIME_FAILURE_ID}
    planning = _load(e31_chain["aggregate"] / "tracks" / "planning.json")
    assert planning["runtime_failure_ids"] == [RUNTIME_FAILURE_ID]
    assert planning["score_count"] == 1
    assert planning["mean_score"] == 100


def test_exact_blinding_preserves_business_terms_and_exposes_judge_reference(
    e31_chain: dict[str, Any],
) -> None:
    inputs = load_e31_inputs(episodes=e31_chain["runtime"], rules=e31_chain["rules"])
    bundle = next(
        item for item in inputs.bundles if item.episode["episode_id"] == PLANNING_ID
    )
    blind_a = build_blind_judge_input_v2(
        bundle.episode, bundle.rule_result, bundle.judge_reference
    )
    blind_b = build_blind_judge_input_v2(
        bundle.episode, bundle.rule_result, bundle.judge_reference
    )
    encoded = canonical_json_bytes(blind_a.document)

    assert blind_a == blind_b
    assert b"Good baseline and candidate are ordinary learning-domain terms" in encoded
    assert b"The production planning tool must create a reviewable proposal" in encoded
    for private_key in (
        b"quality_label",
        b"mutation_source",
        b"author_role",
        b"reviewer_role",
        b"adjudication_note",
        b"request_model",
        b"provider_attestation",
        b"endpoint_origin",
    ):
        assert private_key not in encoded
    assert b"case-e31-p-positive" not in encoded
    assert blind_a.judge_id.startswith("judge-")


def test_stub_judge_repairs_exactly_once(
    e31_chain: dict[str, Any], tmp_path: Path
) -> None:
    provider = FixedResponseJudgeProviderV2(
        [{"unexpected": True}, _fixed_payload()],
        frozen_time="2026-08-31T09:00:00+08:00",
    )
    output = tmp_path / "judge-repaired"
    summary = evaluate_judges_v2(
        episodes=e31_chain["runtime"],
        rules=e31_chain["rules"],
        output=output,
        judge_mode="stub",
        provider=provider,
        episode_ids={PLANNING_ID},
    )

    assert provider.calls == 2
    assert summary.repair_attempted_episode_ids == (PLANNING_ID,)
    assert len(provider.requests[0]["messages"]) == 2
    assert len(provider.requests[1]["messages"]) == 3
    result = _load(output / "judge-results" / f"{PLANNING_ID}.json")
    assert result["status"] == "complete"
    assert [item["dimension_id"] for item in result["dimensions"]] == [
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D7",
    ]


def test_two_invalid_responses_produce_unscored_judge_error(
    e31_chain: dict[str, Any], tmp_path: Path
) -> None:
    provider = FixedResponseJudgeProviderV2(
        [{"unexpected": 1}, {"unexpected": 2}],
        frozen_time="2026-08-31T09:00:00+08:00",
    )
    judges = tmp_path / "judge-error"
    aggregate = tmp_path / "aggregate-error"
    summary = evaluate_judges_v2(
        episodes=e31_chain["runtime"],
        rules=e31_chain["rules"],
        output=judges,
        judge_mode="stub",
        provider=provider,
        episode_ids={PLANNING_ID},
    )
    aggregate_results_v2(
        episodes=e31_chain["runtime"],
        rules=e31_chain["rules"],
        judges=judges,
        output=aggregate,
    )

    assert provider.calls == 2
    assert summary.judge_error_episode_ids == (PLANNING_ID,)
    judge = _load(judges / "judge-results" / f"{PLANNING_ID}.json")
    result = _load(aggregate / "episodes" / f"{PLANNING_ID}.json")
    assert judge["status"] == "judge_error"
    assert judge["dimensions"] == []
    assert result["status"] == "judge_error"
    assert result["raw_score"] is None
    assert result["final_score"] is None


def test_rule_invalid_input_never_calls_provider(e31_chain: dict[str, Any]) -> None:
    inputs = load_e31_inputs(episodes=e31_chain["runtime"], rules=e31_chain["rules"])
    bundle = next(
        item for item in inputs.bundles if item.episode["episode_id"] == PLANNING_ID
    )
    rule = deepcopy(bundle.rule_result)
    rule["checks"][0]["status"] = "invalid_input"
    rule["checks"][0]["reason_code"] = "evidence_missing"
    rule["hard_gates"] = []
    rule["status"] = "invalid_input"
    rule["formal_evaluation_result"] = False
    rule["result_sha256"] = rule_result_digest(rule)
    RuleResultV2.model_validate(rule)
    provider = FixedResponseJudgeProviderV2(
        [_fixed_payload()], frozen_time=bundle.episode["environment"]["frozen_time"]
    )

    result, repaired = _evaluate_one(
        episode=bundle.episode,
        rule_result=rule,
        reference=bundle.judge_reference,
        judge_mode="stub",
        provider=provider,
        git_commit="0" * 40,
        worktree_clean=True,
        dependency_digest=dependency_lock_sha256(),
    )

    assert provider.calls == 0
    assert repaired is False
    assert result["status"] == "invalid_input"
    assert result["dimensions"] == []


def test_incomplete_or_digest_mismatched_inputs_fail_before_judge(
    e31_chain: dict[str, Any], tmp_path: Path
) -> None:
    runtime = tmp_path / "tampered-runtime"
    shutil.copytree(e31_chain["runtime"], runtime)
    episode_path = runtime / "episodes" / f"{PLANNING_ID}.json"
    episode = _load(episode_path)
    episode["observable_trace"]["decision_call_refs"] = []
    episode_path.write_bytes(canonical_json_bytes(episode))
    provider = FixedResponseJudgeProviderV2(
        [_fixed_payload()], frozen_time="2026-08-31T09:00:00+08:00"
    )
    output = tmp_path / "must-not-publish"

    with pytest.raises(JudgeEvaluationV2Error):
        evaluate_judges_v2(
            episodes=runtime,
            rules=e31_chain["rules"],
            output=output,
            judge_mode="stub",
            provider=provider,
        )
    assert provider.calls == 0
    assert not output.exists()


def test_rule_hard_gate_precedes_high_judge_score(e31_chain: dict[str, Any]) -> None:
    result = _load(e31_chain["aggregate"] / "episodes" / f"{WRONG_ACTION_ID}.json")
    assert result["raw_score"] == 100
    assert result["final_score"] == 39
    assert result["episode_outcome"] == "fail"
    assert result["applied_caps"] == [
        {
            "cap_id": "critical_hard_gate",
            "maximum_score": 39,
            "source_rule_ids": result["actual_hard_gates"],
        }
    ]


def test_major_cap_and_suggested_gate_do_not_change_actual_gates(
    e31_chain: dict[str, Any],
) -> None:
    inputs = load_e31_inputs(episodes=e31_chain["runtime"], rules=e31_chain["rules"])
    bundle = next(
        item for item in inputs.bundles if item.episode["episode_id"] == PLANNING_ID
    )
    rule = deepcopy(bundle.rule_result)
    major = next(item for item in rule["checks"] if item["severity"] == "major")
    major["status"] = "fail"
    major["reason_code"] = "test_major_failure"
    rule["status"] = "fail"
    rule["result_sha256"] = rule_result_digest(rule)
    RuleResultV2.model_validate(rule)
    judge = _load(e31_chain["judges"] / "judge-results" / f"{PLANNING_ID}.json")
    judge["rule_result_sha256"] = rule["result_sha256"]
    judge["suggested_hard_gates"] = [
        {
            "suggestion_id": "judge-suggestion-1",
            "reason_code": "review_safety_boundary",
            "evidence_paths": ["result.action_class"],
            "public_summary": "A human may review this boundary.",
        }
    ]
    judge["result_sha256"] = judge_result_digest(judge)
    JudgeResultV2.model_validate(judge)

    result = aggregate_episode_v2(
        episode=bundle.episode, rule_result=rule, judge_result=judge
    )

    assert result["actual_hard_gates"] == []
    assert result["episode_outcome"] == "pass"
    assert result["final_score"] == 69
    assert [item["cap_id"] for item in result["applied_caps"]] == ["major_rule_fail"]
    assert result["judge_suggested_hard_gates"]


def test_v2_contracts_reject_unknown_fields_and_stub_formal_claims(
    e31_chain: dict[str, Any],
) -> None:
    result = _load(e31_chain["judges"] / "judge-results" / f"{PLANNING_ID}.json")
    result["unknown"] = True
    with pytest.raises(ValidationError):
        JudgeResultV2.model_validate(result)

    aggregate = _load(e31_chain["aggregate"] / "episodes" / f"{PLANNING_ID}.json")
    aggregate["formal_evaluation_result"] = True
    aggregate["evaluation_status"] = "formal_model_evaluation"
    AggregateResultV2.model_validate(aggregate)
    inputs = load_e31_inputs(episodes=e31_chain["runtime"], rules=e31_chain["rules"])
    bundle = next(
        item for item in inputs.bundles if item.episode["episode_id"] == PLANNING_ID
    )
    judge = _load(e31_chain["judges"] / "judge-results" / f"{PLANNING_ID}.json")
    assert "aggregate_recomputation_mismatch" in validate_aggregate_result_v2(
        aggregate,
        episode=bundle.episode,
        rule_result=bundle.rule_result,
        judge_result=judge,
    )


def test_judge_and_aggregate_outputs_are_byte_deterministic_and_filterable(
    e31_chain: dict[str, Any], tmp_path: Path
) -> None:
    judge_a = tmp_path / "judge-a"
    judge_b = tmp_path / "judge-b"
    aggregate_a = tmp_path / "aggregate-a"
    aggregate_b = tmp_path / "aggregate-b"
    for destination in (judge_a, judge_b):
        evaluate_judges_v2(
            episodes=e31_chain["runtime"],
            rules=e31_chain["rules"],
            output=destination,
            judge_mode="stub",
            stub_response=FIXED_RESPONSES,
            track="planning",
        )
    for destination, judges in ((aggregate_a, judge_a), (aggregate_b, judge_b)):
        aggregate_results_v2(
            episodes=e31_chain["runtime"],
            rules=e31_chain["rules"],
            judges=judges,
            output=destination,
            episode_ids={PLANNING_ID},
        )
    assert {
        path.relative_to(judge_a): path.read_bytes()
        for path in sorted(judge_a.rglob("*"))
        if path.is_file()
    } == {
        path.relative_to(judge_b): path.read_bytes()
        for path in sorted(judge_b.rglob("*"))
        if path.is_file()
    }
    assert {
        path.relative_to(aggregate_a): path.read_bytes()
        for path in sorted(aggregate_a.rglob("*"))
        if path.is_file()
    } == {
        path.relative_to(aggregate_b): path.read_bytes()
        for path in sorted(aggregate_b.rglob("*"))
        if path.is_file()
    }
    assert _load(judge_a / "run-manifest.json")["selected_track"] == "planning"
    assert _load(aggregate_a / "run-manifest.json")["requested_episode_ids"] == [
        PLANNING_ID
    ]


def test_atomic_publication_refuses_existing_output(e31_chain: dict[str, Any]) -> None:
    output = e31_chain["aggregate"]
    before = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    with pytest.raises(AggregateEvaluationV2Error) as error:
        aggregate_results_v2(
            episodes=e31_chain["runtime"],
            rules=e31_chain["rules"],
            judges=e31_chain["judges"],
            output=output,
        )
    assert error.value.code == "output_exists"
    assert before == {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }


def test_all_e31_outputs_validate_and_remain_nonformal(
    e31_chain: dict[str, Any],
) -> None:
    for key in ("runtime", "rules", "judges", "aggregate"):
        report = validate_dataset(e31_chain[key])
        assert report.ok, [item.render() for item in report.issues]
    assert e31_chain["judge_summary"].formal_evaluation_result is False
    assert e31_chain["aggregate_summary"].formal_evaluation_result is False


def test_cli_routes_active_v3_and_preserves_filters(
    e31_chain: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime-filtered"
    rules = tmp_path / "rules-filtered"
    judges = tmp_path / "judges-filtered"
    aggregate = tmp_path / "aggregate-filtered"

    assert (
        cli_main(
            [
                "run-agent",
                "--dataset",
                str(DATASET),
                "--manifest",
                str(CASE_MANIFEST),
                "--output",
                str(runtime),
                "--episode-id",
                PLANNING_ID,
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "evaluate-rules",
                "--input",
                str(runtime),
                "--output",
                str(rules),
                "--track",
                "planning",
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "evaluate-judge",
                "--episodes",
                str(runtime),
                "--rules",
                str(rules),
                "--output",
                str(judges),
                "--judge-mode",
                "stub",
                "--stub-response",
                str(FIXED_RESPONSES),
                "--track",
                "planning",
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "aggregate-results",
                "--episodes",
                str(runtime),
                "--rules",
                str(rules),
                "--judges",
                str(judges),
                "--output",
                str(aggregate),
                "--episode-id",
                PLANNING_ID,
            ]
        )
        == 0
    )
    assert _load(runtime / "run-manifest.json")["selected_case_ids"] == [
        "case-e31-p-positive"
    ]
    assert _load(rules / "rule-manifest.json")["selected_track"] == "planning"
    assert _load(judges / "run-manifest.json")["selected_track"] == "planning"
    assert _load(aggregate / "run-manifest.json")["requested_episode_ids"] == [
        PLANNING_ID
    ]
    output = capsys.readouterr()
    assert "runtime_failures=0" in output.out
    assert output.err == ""
