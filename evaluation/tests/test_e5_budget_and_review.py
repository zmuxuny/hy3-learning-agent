"""Paid request bounds and the semantic Critical path that Rule-only v3 lacks."""

import json
from copy import deepcopy
from io import BytesIO

import pytest
from learning_agent_eval.active_judge import OpenAICompatibleHy3JudgeProviderV3
from learning_agent_eval.model_budget import ModelBudget, ModelBudgetExceeded
from learning_agent_eval.semantic_adjudication import (
    reviewed_results,
    verify_aggregate_review_csv,
    verify_reviewed_csv,
    write_reviewed_csv,
)


def test_real_judge_reserves_before_transport_and_settles_invalid_output(tmp_path, monkeypatch):
    ledger = tmp_path / "budget.json"
    budget = ModelBudget.create(ledger, limit_micro_cny=1_000_000)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    requests = []

    def transport(request, timeout):
        row = json.loads(ledger.read_text())["requests"][-1]
        assert row["status"] == "reserved"
        body = json.loads(request.data)
        assert body["max_tokens"] == row["output_limit"] == 8192
        assert body["n"] == 1 and timeout == 120
        assert row["input_limit"] == len(request.data) + 2048
        requests.append(body)
        return BytesIO(json.dumps({
            "id": "provider-fixture", "model": "hy3",
            "choices": [{"message": {"content": "broken JSON"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        }).encode())

    monkeypatch.setattr("urllib.request.urlopen", transport)
    provider = OpenAICompatibleHy3JudgeProviderV3(budget_ledger=ledger)
    request = {"messages": [{"role": "user", "content": "synthetic"}], "response_format": {"type": "json_object"}}
    reply = provider.complete(request)
    assert reply.finish_reason == "length"
    provider.complete(request)  # A structure repair is a separate prepaid request.
    assert len(requests) == 2
    assert budget.summary()["charged_micro_cny"] == 1800
    assert len(json.loads(ledger.read_text())["requests"]) == 2
    with pytest.raises(ValueError, match="input_limit"):
        provider.complete({**request, "messages": [{"role": "user", "content": "x" * 200_000}]})
    assert len(requests) == 2


@pytest.mark.parametrize("response", [None, {}, {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}])
def test_judge_provider_failures_keep_reservation_or_actual_usage(tmp_path, monkeypatch, response):
    ledger = tmp_path / "budget.json"
    budget = ModelBudget.create(ledger, limit_micro_cny=1_000_000)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")

    def transport(*args, **kwargs):
        if response is None:
            raise OSError("synthetic transport failure")
        return BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("urllib.request.urlopen", transport)
    reply = OpenAICompatibleHy3JudgeProviderV3(budget_ledger=ledger).complete({"messages": [], "response_format": {}})
    assert reply.status == "provider_error"
    row = json.loads(ledger.read_text())["requests"][0]
    assert row["outcome"] == "provider_error"
    if response and "usage" in response:
        assert row["charged_micro_cny"] == 180 and row["status"] == "settled"
    else:
        assert row["charged_micro_cny"] > 8192 * 4
        assert budget.summary()["unsettled"] == 1


def test_variable_reservations_share_old_ledger_without_reset(tmp_path):
    ledger = tmp_path / "budget.json"
    budget = ModelBudget.create(ledger, limit_micro_cny=300_000)
    budget.reserve(scope="historical-agent", call_id="1")
    ticket = budget.reserve(scope="judge", call_id="2", input_limit=100, output_limit=100)
    budget.settle(ticket, None)
    with pytest.raises(ModelBudgetExceeded):
        budget.reserve(scope="experience", call_id="3")
    assert budget.summary()["requests"] == 2


def review_fixture():
    episode = {"provenance": {"episode_sha256": "e" * 64}, "result": {"user_visible_output": "Synthetic claim of tests passing without evidence"}}
    finding = {"issue_id": "fabrication", "severity": "critical", "evidence_paths": ["result.user_visible_output"]}
    judge = {"result_sha256": "a" * 64, "semantic_issues": [finding], "suggested_hard_gates": []}
    aggregate = {
        "episode_id": "example", "track": "assessment", "episode_sha256": "e" * 64,
        "judge_result_sha256": "a" * 64, "result_sha256": "b" * 64,
        "status": "complete", "episode_outcome": "pass", "actual_hard_gates": [], "final_score": 95, "raw_score": 95,
    }
    review = {"episode_id": "example", "episode_sha256": "e" * 64, "judge_result_sha256": "a" * 64,
              "candidate_id": "issue:fabrication", "verdict": "confirmed_critical", "reviewer_role": "primary_ai_reviewer",
              "rationale": "The cited output claims a successful check without supporting evidence.", "evidence_paths": ["result.user_visible_output"]}
    return episode, judge, aggregate, review


def test_semantic_only_critical_requires_review_then_caps_without_faking_rule():
    episode, judge, aggregate, review = review_fixture()
    before = deepcopy(aggregate)
    row = reviewed_results([aggregate], {"example": judge}, {"example": episode})[0]
    assert row["reviewed_outcome"] == "review_required"
    row = reviewed_results([aggregate], {"example": judge}, {"example": episode}, [review])[0]
    assert row["reviewed_score"] == 39 and row["reviewed_outcome"] == "fail"
    assert row["rule_only_outcome"] == "pass" and aggregate == before
    review["verdict"] = "dismissed"
    row = reviewed_results([aggregate], {"example": judge}, {"example": episode}, [review])[0]
    assert row["reviewed_score"] == 95 and row["reviewed_outcome"] == "pass"
    aggregate["actual_hard_gates"] = ["rule-critical"]
    aggregate["episode_outcome"] = "fail"
    assert reviewed_results([aggregate], {"example": judge}, {"example": episode}, [review])[0]["reviewed_outcome"] == "fail"


@pytest.mark.parametrize("field,value", [("judge_result_sha256", "c" * 64), ("evidence_paths", ["missing.path"]), ("rationale", ""), ("candidate_id", "invented")])
def test_semantic_review_rejects_stale_or_unfounded_adjudication(field, value):
    episode, judge, aggregate, review = review_fixture()
    review[field] = value
    with pytest.raises(ValueError):
        reviewed_results([aggregate], {"example": judge}, {"example": episode}, [review])


def test_unflagged_semantic_critical_can_be_added_by_explicit_reviewer():
    episode, judge, aggregate, review = review_fixture()
    judge["semantic_issues"] = []
    review["candidate_id"] = "reviewer:fabrication"
    row = reviewed_results([aggregate], {"example": judge}, {"example": episode}, [review])[0]
    assert row["reviewed_outcome"] == "fail" and row["reviewed_score"] == 39
    assert row["reviews"][0]["reviewer_role"] == "primary_ai_reviewer"


def test_review_csv_recomputed_before_use(tmp_path):
    episode, judge, aggregate, review = review_fixture()
    aggregate["judge_suggested_hard_gates"] = []
    path = tmp_path / "reviewed-results.csv"
    rows = reviewed_results([aggregate], {"example": judge}, {"example": episode})
    write_reviewed_csv(path, rows)
    assert verify_aggregate_review_csv(path, [aggregate])
    rows = reviewed_results([aggregate], {"example": judge}, {"example": episode}, [review])
    write_reviewed_csv(path, rows)
    assert not verify_aggregate_review_csv(path, [aggregate])
    assert verify_reviewed_csv(path, [aggregate], {"example": judge}, {"example": episode}) == rows
    path.write_text(path.read_text().replace(",39,fail,", ",95,pass,"))
    with pytest.raises(ValueError, match="mismatch"):
        verify_reviewed_csv(path, [aggregate], {"example": judge}, {"example": episode})
    with pytest.raises(ValueError, match="mismatch"):
        verify_aggregate_review_csv(path, [aggregate])
