"""Seen E6 mechanism regressions; these projections are not new test Episodes."""

import json
import urllib.error
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from learning_agent_eval.active_judge import (
    JudgeProviderReplyV2,
    OpenAICompatibleHy3JudgeProviderV3,
    _record_judge_attempt,
    build_provider_request_v3,
)
from learning_agent_eval.blinding_v2 import BlindJudgeInputV3
from learning_agent_eval.exporter_v4 import _invocation_action
from learning_agent_eval.model_budget import ModelBudget
from learning_agent_eval.rules import _active_track_checks

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/e6-seen-rule-regressions.json"


def case(name):
    row = json.loads(FIXTURE.read_text())[name]
    return row["episode"], row["reference"]


def checks(e, r):
    return {c["check_id"]: c for c in _active_track_checks(e, r)}


@pytest.mark.parametrize("channels", [["in_app"], [], ["in_app", "in_app"]])
def test_sent_in_app_uses_internal_evidence_and_rejects_duplicates(channels):
    e, r = case("formal-f02-intervention")
    for i in e["observable_trace"]["tool_invocations"]:
        if i["tool_name"] == "notification.send":
            i["canonical_args"]["channels"] = channels
    c = checks(e, r)
    assert c["intervention.allowed_chain"]["status"] == "pass"
    assert c["intervention.receipt_semantics"]["status"] == "not_applicable"
    assert c["intervention.replay_idempotent"]["status"] == "pass"
    duplicate = deepcopy(next(n for n in e["state_after"]["logical_entities"] if n["entity_type"] == "notification"))
    duplicate["logical_id"] = "notification:duplicate"
    e["state_after"]["logical_entities"].append(duplicate)
    e["state_delta"]["changes"].append({"entity_ref": duplicate["logical_id"]})
    assert checks(e, r)["intervention.replay_idempotent"]["status"] == "fail"


def test_external_send_still_requires_outbox_receipt():
    e, r = case("formal-f02-intervention")
    for i in e["observable_trace"]["tool_invocations"]:
        if i["tool_name"] == "notification.send":
            i["canonical_args"]["channels"] = ["email"]
    c = checks(e, r)
    assert c["intervention.allowed_chain"]["status"] == "fail"
    assert c["intervention.receipt_semantics"]["status"] == "fail"


def test_waiting_approval_is_incomplete_not_a_committed_verdict_violation():
    e, r = case("formal-f05-assessment")
    c = checks(e, r)
    for name in ["score_threshold_verdict", "feedback_present", "operation_delta_alignment"]:
        assert c[f"assessment.{name}"]["status"] == "not_applicable"
    assert c["assessment.invocation_durable"]["status"] == "fail"
    submission = next(n for n in e["state_after"]["logical_entities"] if n["entity_type"] == "submission")
    e["state_delta"]["changes"].append({"entity_ref": submission["logical_id"], "operation_alignment": "unmatched", "operation_refs": []})
    assert checks(e, r)["assessment.operation_delta_alignment"]["status"] == "fail"


def test_committed_assessment_still_checks_verdict_and_operation_alignment():
    e, r = case("formal-f12-assessment")
    c = checks(e, r)
    assert c["assessment.score_threshold_verdict"]["status"] == "pass"
    assert c["assessment.operation_delta_alignment"]["status"] == "pass"
    for n in e["state_after"]["logical_entities"]:
        if n["entity_type"] == "submission":
            n["data"]["status"] = "accepted"
    assert checks(e, r)["assessment.score_threshold_verdict"]["status"] == "fail"


def test_abstention_applicability_is_independent_of_the_case_envelope():
    e, r = case("formal-f05-assessment")
    e["result"]["action_classes"] = ["INSUFFICIENT_EVIDENCE", "REQUEST_CLARIFICATION"]
    e["observable_trace"]["tool_invocations"] = []
    e["observable_trace"]["operations"] = []
    e["state_delta"]["changes"] = []
    r["allowed_action_classes"] = ["ACCEPT"]
    result = checks(e, r)
    assert result["assessment.score_threshold_verdict"]["status"] == "not_applicable"
    assert result["assessment.abstention_preserves_state"]["status"] == "pass"
    submission = next(n for n in e["state_after"]["logical_entities"] if n["entity_type"] == "submission")
    e["state_delta"]["changes"].append({"entity_ref": submission["logical_id"]})
    assert checks(e, r)["assessment.abstention_preserves_state"]["status"] == "fail"


def test_confirming_intake_is_not_a_request_for_user_input():
    assert _invocation_action({"tool_name": "planning.intake.update", "canonical_args": {"readiness": "ready", "open_questions": []}}) is None
    assert _invocation_action({"tool_name": "planning.intake.update", "canonical_args": {"open_questions": [{"question": "Available time?"}]}}) == "REQUEST_USER_INPUT"
    assert _invocation_action({"tool_name": "task.patch"}) == "APPLY_REVERSIBLE_PATCH"


@pytest.mark.parametrize("family,present", [("01", True), ("07", True), ("10", True), ("12", True), ("02", False), ("04", False), ("11", False)])
def test_returned_proposal_queued_behind_approval_is_visible_but_intake_alone_is_not(family, present):
    e, r = case(f"formal-f{family}-planning")
    result = checks(e, r)
    assert (result["planning.proposal_present"]["status"] == "pass") is present
    if present:
        assert result["planning.proposal_present"]["observed"]["artifact_kind"] == "returned_unexecuted_draft"
        assert not any(n["entity_type"] == "plan_proposal" for n in e["state_after"]["logical_entities"])


@pytest.mark.parametrize("error,category,http_status", [
    (TimeoutError("SECRET"), "transport_timeout", None),
    (urllib.error.HTTPError("https://secret.invalid", 429, "SECRET", {}, None), "http_error", 429),
    (urllib.error.URLError("SECRET"), "transport_error", None),
])
def test_provider_failures_keep_safe_cause_and_unknown_usage_reservation(tmp_path, monkeypatch, error, category, http_status):
    ledger = tmp_path / "budget.json"
    ModelBudget.create(ledger, limit_micro_cny=500_000)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://provider.example.test/v1")
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 180
        raise error
    monkeypatch.setattr("urllib.request.urlopen", fail)
    reply = OpenAICompatibleHy3JudgeProviderV3(budget_ledger=ledger).complete({"messages": [], "response_format": {}})
    assert (reply.failure_category, reply.http_status) == (category, http_status)
    assert "SECRET" not in repr(reply)
    rows = json.loads(ledger.read_text())["requests"]
    assert len(rows) == 1 and rows[0]["status"] == "unknown_usage_reserved"


def test_truncated_json_is_not_misattributed_to_privacy(tmp_path):
    path = tmp_path / "attempts.jsonl"
    reply = JudgeProviderReplyV2(content='{"dimensions":[', request_model="hy3", response_model="hy3", provider_request_id="test", requested_at="2026-09-08T00:00:00Z", responded_at=None, status="completed", finish_reason="length")
    _record_judge_attempt(path, {"episode_id": "test", "provenance": {"episode_sha256": "a"*64}}, SimpleNamespace(sha256="b"*64), 0, reply, ["response_output_truncated"])
    row = json.loads(path.read_text())
    assert row["response_projection_status"] == "unparseable_json"
    assert row["response_withheld_for_privacy"] is False
    assert row["public_response"] is None and row["raw_response_text_bytes"] > 0


def test_judge_schema_only_offers_actual_episode_paths():
    e = {"result": {"action_class": "WAIT"}, "observable_trace": {"model_calls": [{"assistant_text": "wait", "returned_tool_calls": []}], "tool_invocations": [], "guard_decisions": []}}
    request = build_provider_request_v3(BlindJudgeInputV3(judge_id="test", document={"episode": e}, sha256="a"*64))
    schema = request["response_format"]["json_schema"]["schema"]
    for definition in schema["$defs"].values():
        paths = definition.get("properties", {}).get("evidence_paths")
        if paths:
            assert paths["items"] == {"$ref": "#/$defs/EpisodeEvidencePath"}
            offered = schema["$defs"]["EpisodeEvidencePath"]["enum"]
            assert "common.action_envelope" not in offered
            assert "observable_trace.model_calls[0].returned_tool_calls" in offered
