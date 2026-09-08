"""Rejected Judge responses retain billing and safe evidence through publication."""

import hashlib
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
from learning_agent_eval.active_judge import (
    OpenAICompatibleHy3JudgeProviderV3,
    build_provider_request_v3,
    evaluate_active_judges,
)
from learning_agent_eval.active_rules import evaluate_active_rules
from learning_agent_eval.active_runtime import run_active_runtime
from learning_agent_eval.blinding_v2 import BlindJudgeInputV3
from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.case_specs import episode_id_for_case
from learning_agent_eval.judge_request_projection import unpack_shared_values
from learning_agent_eval.model_budget import ModelBudget
from learning_agent_eval.validator import validate_dataset

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def judge_inputs(tmp_path_factory):
    """One isolated scripted Episode supplies the real Judge artifact contracts."""
    root = tmp_path_factory.mktemp("e5-judge-failure-inputs")
    dataset = ROOT / "evaluation/datasets/decisionbench-v1.1-regression/calibration"
    case = json.loads((dataset / "cases/case-0001.json").read_text())
    episode_id = episode_id_for_case(case)
    runtime, rules = root / "runtime", root / "rules"
    run_active_runtime(
        dataset=dataset,
        manifest=dataset / "manifest.json",
        output=runtime,
        episode_ids={episode_id},
        model_mode="stub",
    )
    evaluate_active_rules(input_path=runtime, output=rules)
    payload = json.loads(
        (ROOT / "evaluation/fixtures/e311-fixed-judge-responses-v3.json").read_text()
    )["responses"][0]
    return {"runtime": runtime, "rules": rules, "episode_id": episode_id, "payload": payload}


def _envelope(content):
    return {
        "id": "synthetic-judge-response",
        "model": "hy3",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def _evaluate(tmp_path, monkeypatch, inputs, envelopes):
    ledger = tmp_path / "synthetic-budget.json"
    budget = ModelBudget.create(ledger, limit_micro_cny=1_000_000)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    requests = []

    def transport(request, timeout):
        row = json.loads(ledger.read_text())["requests"][-1]
        assert row["status"] == "reserved"
        assert row["ticket"] == len(requests) + 1
        assert row["input_limit"] == len(request.data) + 2048
        assert timeout == 180
        assert len(requests) < len(envelopes), "unexpected unbudgeted repair"
        requests.append(json.loads(request.data))
        return BytesIO(json.dumps(envelopes[len(requests) - 1]).encode())

    monkeypatch.setattr("urllib.request.urlopen", transport)
    provider = OpenAICompatibleHy3JudgeProviderV3(budget_ledger=ledger)
    output = tmp_path / "judges"
    summary = evaluate_active_judges(
        episodes=inputs["runtime"],
        rules=inputs["rules"],
        output=output,
        judge_mode="real",
        allow_real_judge=True,
        provider=provider,
    )
    result = json.loads(
        (output / "judge-results" / f"{inputs['episode_id']}.json").read_text()
    )
    evidence_bytes = (tmp_path / "judges-attempts.jsonl").read_bytes()
    attempts = [json.loads(line) for line in evidence_bytes.splitlines()]
    billing = json.loads(ledger.read_text())["requests"]
    assert len(requests) == provider.calls == len(envelopes)
    assert len(result["provider_attestation"]["calls"]) == len(envelopes)
    assert [row["budget_ticket"] for row in attempts] == [row["ticket"] for row in billing]
    assert all(row["provider_attempted"] for row in attempts)
    assert validate_dataset(output).ok
    return summary, result, attempts, requests, billing, budget.summary(), evidence_bytes


def test_non_object_provider_response_keeps_attempt_and_unknown_usage_reservation(
    tmp_path, monkeypatch, judge_inputs
):
    _, result, attempts, _, billing, budget, _ = _evaluate(
        tmp_path, monkeypatch, judge_inputs, [[]]
    )
    assert result["status"] == "judge_error"
    assert result["error_code"] == "judge_provider_error"
    assert result["provider_attestation"]["calls"][0]["status"] == "provider_error"
    assert attempts[0]["provider_status"] == "provider_error"
    assert attempts[0]["validation_error_codes"] == ["provider_error"]
    assert billing[0]["outcome"] == "provider_error"
    assert billing[0]["status"] == "unknown_usage_reserved"
    assert billing[0]["charged_micro_cny"] == billing[0]["input_limit"] + 12288 * 4
    assert budget["requests"] == budget["unsettled"] == 1


def test_public_content_before_invalid_provider_metadata_retains_one_paid_attempt(
    tmp_path, monkeypatch, judge_inputs
):
    envelope = _envelope(judge_inputs["payload"])
    del envelope["model"]
    _, result, attempts, _, billing, budget, _ = _evaluate(
        tmp_path, monkeypatch, judge_inputs, [envelope]
    )
    assert result["status"] == "judge_error" and result["dimensions"] == []
    assert attempts[0]["public_response"] == judge_inputs["payload"]
    assert attempts[0]["provider_status"] == "provider_error"
    assert billing[0]["status"] == "settled" and billing[0]["outcome"] == "provider_error"
    assert billing[0]["charged_micro_cny"] == budget["charged_micro_cny"] == 180
    assert budget["requests"] == 1 and budget["unsettled"] == 0


@pytest.mark.parametrize(
    "rejected,error",
    [
        ('{"reasoning_content":"SYNTHETIC_PRIVATE_REASONING_SENTINEL",', "response_json_invalid"),
        ('{"invalid":NaN}', "response_schema_invalid"),
    ],
    ids=["malformed-private-reasoning", "nonfinite-json"],
)
def test_unparseable_public_content_keeps_digest_and_allows_paid_repair_publication(
    tmp_path, monkeypatch, judge_inputs, rejected, error
):
    summary, result, attempts, requests, billing, budget, evidence = _evaluate(
        tmp_path,
        monkeypatch,
        judge_inputs,
        [_envelope(rejected), _envelope(judge_inputs["payload"])],
    )
    assert result["status"] == "complete"
    assert summary.repair_attempted_episode_ids == (judge_inputs["episode_id"],)
    first = attempts[0]
    assert first["validation_error_codes"] == [error]
    assert first["public_response"] is None and not first["response_withheld_for_privacy"]
    assert first["response_projection_status"] == "unparseable_json"
    assert first["raw_response_text_sha256"] == hashlib.sha256(rejected.encode()).hexdigest()
    assert first["raw_response_text_bytes"] == len(rejected.encode())
    assert attempts[1]["public_response"] == judge_inputs["payload"]
    assert b"SYNTHETIC_PRIVATE_REASONING_SENTINEL" not in evidence
    assert "SYNTHETIC_PRIVATE_REASONING_SENTINEL" not in json.dumps(requests[1])
    assert [row["outcome"] for row in billing] == ["response_invalid", "complete"]
    assert budget["requests"] == 2 and budget["charged_micro_cny"] == 360


def test_private_invalid_path_is_withheld_from_evidence_and_repair(
    tmp_path, monkeypatch, judge_inputs
):
    sensitive_path = "result.sk-" + "A" * 24
    rejected = deepcopy(judge_inputs["payload"])
    rejected["dimensions"][0]["evidence_paths"] = [sensitive_path]
    _, result, attempts, requests, _, budget, evidence = _evaluate(
        tmp_path,
        monkeypatch,
        judge_inputs,
        [_envelope(json.dumps(rejected)), _envelope(judge_inputs["payload"])],
    )
    assert result["status"] == "complete"
    first = attempts[0]
    assert first["validation_error_codes"] == [
        "response_evidence_invalid", "response_privacy_invalid"
    ]
    assert first["response_projection_status"] == "privacy_withheld"
    assert first["public_response"] is None and first["invalid_evidence_paths"] == []
    repair = json.loads(requests[1]["messages"][-1]["content"])
    assert repair["invalid_evidence_paths"] == []
    assert sensitive_path.encode() not in evidence
    assert sensitive_path not in json.dumps(requests[1])
    assert budget["requests"] == 2 and budget["charged_micro_cny"] == 360


def test_safe_invalid_path_retains_public_failure_and_exact_repair_hint(
    tmp_path, monkeypatch, judge_inputs
):
    rejected = deepcopy(judge_inputs["payload"])
    rejected["dimensions"][0]["evidence_paths"] = ["result.does_not_exist"]
    _, result, attempts, requests, _, budget, _ = _evaluate(
        tmp_path,
        monkeypatch,
        judge_inputs,
        [_envelope(json.dumps(rejected)), _envelope(judge_inputs["payload"])],
    )
    assert result["status"] == "complete"
    assert attempts[0]["public_response"] == rejected
    assert not attempts[0]["response_withheld_for_privacy"]
    assert attempts[0]["invalid_evidence_paths"] == ["result.does_not_exist"]
    repair = json.loads(requests[1]["messages"][-1]["content"])
    assert repair["invalid_evidence_paths"] == ["result.does_not_exist"]
    assert budget["requests"] == 2 and budget["charged_micro_cny"] == 360


def test_wire_instructions_cover_escaped_business_marker_objects():
    document = {
        "episode": {
            "schema_version": "decision-episode-v4",
            "result": {"business_object": {"$shared": "v1"}},
            "observable_trace": {"model_calls": [], "tool_invocations": [], "guard_decisions": []},
        },
        "repeated_public_data": ["public evidence " * 80] * 2,
    }
    request = build_provider_request_v3(
        BlindJudgeInputV3(judge_id="synthetic", document=document, sha256=sha256_digest(document))
    )
    instructions = request["messages"][0]["content"]
    wire = json.loads(request["messages"][1]["content"])
    assert "$shared" in instructions and "$literal" in instructions
    assert wire["document"]["episode"]["result"]["business_object"] == {
        "$literal": [["$shared", "v1"]]
    }
    assert unpack_shared_values(wire) == document
