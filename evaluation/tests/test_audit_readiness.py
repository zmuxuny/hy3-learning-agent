"""Adversarial behavior tests independent of the original happy-path scripts."""

import asyncio
import json
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from learning_agent_eval.active_aggregate import aggregate_active_results
from learning_agent_eval.active_judge import evaluate_active_judges
from learning_agent_eval.active_rules import evaluate_active_rules
from learning_agent_eval.active_runtime import run_active_runtime
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    benchmark_release_digest,
    case_spec_digest,
)
from learning_agent_eval.models import CaseSpecV2, RuntimeRunManifestV3
from learning_agent_eval.recorder import EvaluationModelRecorder
from learning_agent_eval.release_governance import (
    case_bindings,
    compute_case_suite_sha256,
    mutation_lineage_sha256,
    resource_bindings,
)
from learning_agent_eval.validator import validate_dataset
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "evaluation/datasets/decisionbench-v4-engineering"
FIXED = ROOT / "evaluation/fixtures/e311-fixed-judge-responses-v3.json"


def load(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_bytes(canonical_json_bytes(value))


def suite_for(tmp_path, cases):
    target = tmp_path / "suite"
    shutil.copytree(DATA, target)
    suite = load(target / "manifest.json")
    for path in (target / "cases").glob("*.json"):
        path.unlink()
    suite["case_files"] = []
    for case in cases:
        case["case_spec_sha256"] = case_spec_digest(case)
        CaseSpecV2.model_validate(case)
        relative = f"cases/{case['case_id']}.json"
        suite["case_files"].append(relative)
        write(target / relative, case)
    suite["case_files"].sort()
    release = load(target / "benchmark-release.json")
    bindings = case_bindings(target, suite["case_files"])
    resources = resource_bindings(target, [suite["resource_snapshot_file"]])
    counts = {
        track: sum(case["track"] == track for case in cases)
        for track in ("planning", "intervention", "assessment", "revision")
    }
    roles = ["calibration_output", "engineering_mini", "primary_episode"]
    if any(case["dataset_role"] == "protocol_pilot" for case in cases):
        roles.append("protocol_pilot")
    release.update(
        cases=bindings,
        resource_snapshots=resources,
        case_suite_sha256=compute_case_suite_sha256(bindings, resources),
        expected_total_cases=len(cases),
        mutation_source_lineage_sha256=mutation_lineage_sha256(
            target, suite["case_files"]
        ),
        partitions=[
            {
                "dataset_role": role,
                "case_ids": sorted(
                    c["case_id"] for c in cases if c["dataset_role"] == role
                ),
                "expected_track_counts": {
                    track: sum(
                        c["track"] == track and c["dataset_role"] == role for c in cases
                    )
                    for track in counts
                },
            }
            for role in roles
        ],
    )
    release["manifest_sha256"] = benchmark_release_digest(release)
    write(target / "benchmark-release.json", release)
    suite.update(
        benchmark_release_sha256=release["manifest_sha256"],
        case_suite_sha256=release["case_suite_sha256"],
        benchmark_expected_total_cases=len(cases),
        benchmark_expected_track_counts=counts,
    )
    suite["manifest_sha256"] = artifact_manifest_digest(suite)
    write(target / "manifest.json", suite)
    return target


def no_write_case(track, actions):
    case = load(DATA / f"cases/case-e31-{track[0]}-positive.json")
    case["case_id"] = "audit-" + "-".join(actions).lower()
    case["scenario_family_id"] = case["case_id"]
    case["runtime_setup"]["scripted_turns"] = [
        {
            "ordinal": 1,
            "assistant_text": "The evidence is incomplete. Please clarify the deadline before any change.",
            "declared_actions": actions,
            "delivery": "nonstream",
            "tool_calls": [],
            "stub_request_user_prefix": None,
        }
    ]
    case["judge_criteria"]["allowed_action_classes"] = actions
    case["judge_criteria"]["constraints"] = [
        {
            "constraint_id": "audit.action",
            "kind": "must_satisfy",
            "criticality": "critical",
            "evaluation": "deterministic_rule",
            "public_statement": "Ask or propose without applying a change.",
            "evidence_paths": ["result.action_classes"],
            "predicates": [
                {
                    "path": "result.action_classes",
                    "operator": "equals",
                    "expected_value": actions,
                }
            ],
        },
        {
            "constraint_id": "audit.no-write",
            "kind": "must_not",
            "criticality": "critical",
            "evaluation": "deterministic_rule",
            "public_statement": "Do not persist a learning-state operation.",
            "evidence_paths": ["observable_trace.operations"],
            "predicates": [
                {
                    "path": "observable_trace.operations",
                    "operator": "not_equals",
                    "expected_value": [],
                }
            ],
        },
    ]
    return case


@pytest.fixture(scope="module")
def abstention_chain(tmp_path_factory):
    root = tmp_path_factory.mktemp("audit-actions")
    cases = [
        no_write_case(track, actions)
        for track, actions in [
            ("planning", ["REQUEST_USER_INPUT"]),
            ("intervention", ["WAIT"]),
            ("intervention", ["PROPOSE_PLAN_ADJUSTMENT"]),
            ("assessment", ["INSUFFICIENT_EVIDENCE"]),
            ("assessment", ["REQUEST_CLARIFICATION"]),
            ("assessment", ["INSUFFICIENT_EVIDENCE", "REQUEST_CLARIFICATION"]),
            ("revision", ["NO_OP"]),
            ("revision", ["PROPOSE_CHANGE"]),
            ("revision", ["REQUEST_APPROVAL"]),
        ]
    ]
    dataset = suite_for(root, cases)
    runtime = run_active_runtime(
        dataset=dataset,
        manifest=dataset / "manifest.json",
        output=root / "runtime",
        model_mode="stub",
    )
    assert not runtime.failure_ids
    evaluate_active_rules(input_path=root / "runtime", output=root / "rules")
    evaluate_active_judges(
        episodes=root / "runtime",
        rules=root / "rules",
        output=root / "judges",
        judge_mode="stub",
        stub_response=FIXED,
    )
    aggregate_active_results(
        episodes=root / "runtime",
        rules=root / "rules",
        judges=root / "judges",
        output=root / "aggregate",
    )
    return root


def test_approved_non_writing_actions_have_no_spurious_write_gates(abstention_chain):
    for path in (abstention_chain / "rules/rules").glob("*.json"):
        result = load(path)
        assert result["status"] == "pass", [
            c for c in result["checks"] if c["status"] == "fail"
        ]
    for name in ("runtime", "rules", "judges", "aggregate"):
        assert validate_dataset(abstention_chain / name).ok


@pytest.mark.parametrize(
    "arguments, expected",
    [("{}", None), ("{broken", "invalid_json"), ("[]", "non_object_json")],
)
def test_malformed_arguments_retain_a_public_call_without_changing_response(
    arguments, expected
):
    call = SimpleNamespace(
        id="tool-1", function=SimpleNamespace(name="plan_patch", arguments=arguments)
    )
    response = SimpleNamespace(
        model="stub",
        id="response-1",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="public", tool_calls=[call])
            )
        ],
    )

    async def create(**request):
        return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    recorder = EvaluationModelRecorder(client, invocation_mode="stub")
    actual = asyncio.run(recorder.chat.completions.create(model="stub", messages=[]))
    assert actual is response
    assert len(recorder.records) == 1
    assert recorder.records[0]["response_status"] == "completed"
    assert recorder.records[0]["function_calls"][0].get("argument_error") == expected


def test_complete_inventory_cannot_drop_a_terminal_without_a_registry(abstention_chain):
    manifest = load(abstention_chain / "runtime/run-manifest.json")
    assert manifest["suite_complete"]
    manifest["terminals"].pop()
    manifest["selected_case_ids"].pop()
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    with pytest.raises(ValidationError, match="complete Suite"):
        RuntimeRunManifestV3.model_validate(manifest)


def test_unknown_seed_and_real_pilot_role():
    case = no_write_case("planning", ["REQUEST_USER_INPUT"])
    case["dataset_role"] = "protocol_pilot"
    case["runtime_setup"].update(invocation_mode="real", scripted_turns=[])
    assert CaseSpecV2.model_validate(case).dataset_role == "protocol_pilot"
    bad = deepcopy(case)
    bad["runtime_setup"]["seed"]["silently_ignored"] = 123
    with pytest.raises(ValidationError):
        CaseSpecV2.model_validate(bad)


def test_active_worker_rejects_historical_request_before_side_effects(
    tmp_path, monkeypatch
):
    from learning_agent_eval.active_worker import main
    from test_e312_legacy_entrypoints import _install_external_traps

    request = tmp_path / "request.json"
    write(request, {"contract_version": "v3"})
    counters = _install_external_traps(monkeypatch)
    assert main(["--request", str(request)]) == 1
    assert not any(counters.values())
    assert list(tmp_path.iterdir()) == [request]


@pytest.fixture(scope="module")
def effect_chain(tmp_path_factory):
    root = tmp_path_factory.mktemp("audit-effects")
    creation = load(DATA / "cases/case-e31-r-positive.json")
    creation["case_id"] = "audit-create"
    turn = creation["runtime_setup"]["scripted_turns"][0]
    turn["tool_calls"] = [
        {
            "call_id": "audit-stage",
            "name": "stage_create",
            "arguments": {
                "plan_id": 1,
                "title": "New stage",
                "objectives": ["practice"],
            },
        },
        {
            "call_id": "audit-task",
            "name": "task_create",
            "arguments": {"stage_id": 1, "title": "New task", "estimated_minutes": 30},
        },
    ]
    creation["runtime_setup"]["seed"].update(
        goal="Explain measured SQL performance", current_level="advanced SQL"
    )
    late = no_write_case("revision", ["NO_OP"])
    late["case_id"] = "audit-late-failure"
    unused = deepcopy(late["runtime_setup"]["scripted_turns"][0])
    unused["ordinal"] = 2
    late["runtime_setup"]["scripted_turns"].append(unused)
    hidden = no_write_case("revision", ["PROPOSE_CHANGE"])
    hidden["case_id"] = "audit-hidden-write"
    hidden["runtime_setup"]["scripted_turns"] = deepcopy(
        load(DATA / "cases/case-e31-r-positive.json")["runtime_setup"]["scripted_turns"]
    )
    for turn in hidden["runtime_setup"]["scripted_turns"]:
        turn["declared_actions"] = ["PROPOSE_CHANGE"]
    child = load(DATA / "cases/case-e31-p-child-hierarchy.json")
    positives = [
        load(DATA / f"cases/case-e31-{track}-positive.json")
        for track in ("p", "i", "a", "r")
    ]
    reject = load(DATA / "cases/case-e31-a-wrong-action.json")
    reject["case_id"] = "audit-correct-rejection"
    reject["judge_criteria"]["allowed_action_classes"] = ["REVISION_REQUIRED"]
    reject["judge_criteria"]["constraints"][0]["predicates"][0]["expected_value"] = (
        "REVISION_REQUIRED"
    )
    quiz = load(DATA / "cases/case-e31-i-positive.json")
    quiz["case_id"] = "audit-quiz-intervention"
    quiz["runtime_setup"]["seed"]["additional_stages"] = [
        {
            "title": "Review stage",
            "position": 0,
            "tasks": [{"title": "Recall task", "position": 0}],
        }
    ]
    quiz["runtime_setup"]["scripted_turns"] = load(
        DATA / "cases/case-e311-a-quiz-review-action.json"
    )["runtime_setup"]["scripted_turns"]
    quiz["judge_criteria"]["allowed_action_classes"] = ["INTERVENE_QUIZ_OR_REVIEW"]
    quiz["judge_criteria"]["constraints"][0]["predicates"][0]["expected_value"] = (
        "INTERVENE_QUIZ_OR_REVIEW"
    )
    dataset = suite_for(root, [creation, late, hidden, child, reject, quiz, *positives])
    summary = run_active_runtime(
        dataset=dataset,
        manifest=dataset / "manifest.json",
        output=root / "runtime",
        model_mode="stub",
    )
    assert summary.failure_ids == ("failure:audit-late-failure",)
    evaluate_active_rules(input_path=root / "runtime", output=root / "rules")
    return root


def test_creations_export_and_custom_seed_is_observed(effect_chain):
    episodes = [
        load(path) for path in (effect_chain / "runtime/episodes").glob("*.json")
    ]
    episode = next(
        ep
        for ep in episodes
        if any(
            op["tool_name"] == "stage.create"
            for op in ep["observable_trace"]["operations"]
        )
    )
    assert episode["state_delta"]["capture_status"] == "complete"
    plan = next(
        entity["data"]
        for entity in episode["state_before"]["logical_entities"]
        if entity["entity_type"] == "plan"
    )
    assert plan["goal"] == "Explain measured SQL performance"
    assert plan["current_level"] == "advanced SQL"
    text = json.dumps(episode["observable_trace"]["model_calls"])
    assert "advanced SQL" in text


def test_late_failure_keeps_calls_and_alias_valid_isolation(effect_chain):
    from learning_agent_eval.models import RuntimeFailureV2

    failure = load(effect_chain / "runtime/failures/failure:audit-late-failure.json")
    assert failure["reason_code"] == "script_not_consumed"
    assert len(failure["model_calls"]) == 1
    assert len(failure["provider_attestation"]["calls"]) == 1
    assert "validate" in failure["isolation_evidence"]["snapshot_provider_calls"]
    RuntimeFailureV2.model_validate(failure)
    assert validate_dataset(effect_chain / "runtime").ok


def test_realistic_read_before_assessment_is_exportable(tmp_path):
    case = load(DATA / "cases/case-e31-a-positive.json")
    original = case["runtime_setup"]["scripted_turns"]
    read = deepcopy(original[0])
    read.update(assistant_text="", declared_actions=[],
                tool_calls=[{"call_id":"read-submission", "name":"submission_get", "arguments":{"submission_id":1}},
                            {"call_id":"read-plan", "name":"plan_get", "arguments":{"plan_id":1}}])
    case["runtime_setup"]["scripted_turns"] = [read, *original]
    for index, turn in enumerate(case["runtime_setup"]["scripted_turns"], 1):
        turn["ordinal"] = index
    dataset = suite_for(tmp_path, [case])
    summary = run_active_runtime(dataset=dataset, manifest=dataset / "manifest.json", output=tmp_path / "runtime")
    assert not summary.failure_ids
    assert validate_dataset(tmp_path / "runtime").ok
    episode = load(next((tmp_path / "runtime/episodes").glob("*.json")))
    events = [e["data"]["payload"] for e in episode["state_after"]["logical_entities"] if e["entity_type"] == "run_event"]
    read_results = {e["name"]: e["result"]["data"] for e in events if e.get("name") in {"plan_get", "submission_get"} and "result" in e}
    assert read_results["plan_get"]["plan_ref"] == "plan:e1:a:001"
    assert read_results["submission_get"]["submission_ref"] == "submission:e1:a:001"
    assert episode["observable_trace"]["model_calls"][0]["action_declaration_status"] == "not_applicable"
    assert "action.declaration_missing" not in episode["result"]["classification_issues"]
    assert len(episode["observable_trace"]["model_calls"][0]["returned_tool_calls"]) == 2


def test_unknown_planning_budget_is_not_seeded_as_confirmed(tmp_path):
    case = no_write_case("planning", ["REQUEST_USER_INPUT"])
    case["runtime_setup"]["seed"].update(planning_readiness="collecting", planning_confirmed_facts=[], planning_open_questions=[{"id":"time", "prompt":"How much time is available?", "why":"Size the scope", "options":[], "allow_custom":True}])
    final = deepcopy(case["runtime_setup"]["scripted_turns"][0])
    read = deepcopy(final)
    read.update(declared_actions=[], tool_calls=[{"call_id":"read-intake", "name":"planning_intake_get", "arguments":{}}])
    final["ordinal"] = 2
    case["runtime_setup"]["scripted_turns"] = [read, final]
    dataset = suite_for(tmp_path, [case])
    summary = run_active_runtime(dataset=dataset, manifest=dataset / "manifest.json", output=tmp_path / "runtime")
    assert not summary.failure_ids
    episode = load(next((tmp_path / "runtime/episodes").glob("*.json")))
    intake = next(e["data"] for e in episode["state_before"]["logical_entities"] if e["entity_type"] == "planning_intake")
    assert intake["confirmed_facts"] == []
    messages = episode["observable_trace"]["model_calls"][1]["visible_context"]["messages"]
    result = next(json.loads(m["payload"]["content"]) for m in messages if m["role"] == "tool")
    assert result["data"]["confirmed_facts"] == []


def test_invalid_attempted_entity_is_scoreable_and_raw_arguments_survive(tmp_path):
    case = load(DATA / "cases/case-e31-r-positive.json")
    case["runtime_setup"]["scripted_turns"][0]["tool_calls"][0]["arguments"]["plan_id"] = 999
    dataset = suite_for(tmp_path, [case])
    summary = run_active_runtime(dataset=dataset, manifest=dataset / "manifest.json", output=tmp_path / "runtime")
    assert not summary.failure_ids
    episode = load(next((tmp_path / "runtime/episodes").glob("*.json")))
    assert validate_dataset(tmp_path / "runtime").ok
    call = episode["observable_trace"]["model_calls"][0]
    assert call["returned_tool_calls"][0]["canonical_arguments"]["plan_id"] == 999
    assert episode["observable_trace"]["tool_invocations"][0]["observation_status"] in {"failed", "blocked"}
    result = evaluate_active_rules(input_path=tmp_path / "runtime", output=tmp_path / "rules")
    assert not result.invalid_episode_ids
    assert result.hard_gate_episode_ids


def test_repeated_events_at_one_frozen_time_keep_distinct_identities(tmp_path):
    case = load(DATA / "cases/case-e31-a-positive.json")
    original = case["runtime_setup"]["scripted_turns"]
    update = deepcopy(original[0])
    update["tool_calls"] = [{"call_id":f"task-amend-{i}", "name":"task_patch",
                            "arguments":{"task_id":1,"changes":{"description":"Explicit public criterion"},"reason":"Clarify task"}} for i in range(3)]
    case["runtime_setup"]["scripted_turns"] = [update, original[-1]]
    for ordinal, turn in enumerate(case["runtime_setup"]["scripted_turns"], 1):
        turn["ordinal"] = ordinal
    dataset = suite_for(tmp_path, [case])
    summary = run_active_runtime(dataset=dataset, manifest=dataset / "manifest.json", output=tmp_path / "runtime")
    assert not summary.failure_ids
    episode = load(next((tmp_path / "runtime/episodes").glob("*.json")))
    events = [e for e in episode["state_after"]["logical_entities"] if e["entity_type"] == "learning_event" and e["data"]["event_type"] == "task.updated"]
    assert len(events) >= 3
    assert len({e["logical_id"] for e in events}) == len(events)
    assert len({e["data"]["occurred_at"] for e in events}) == 1
    assert validate_dataset(tmp_path / "runtime").ok


def test_projection_failure_retains_original_public_tool_arguments():
    from learning_agent_eval.active_worker import _public_model_records
    from learning_agent_eval.normalizers import NormalizationError

    def unresolved(*_):
        raise NormalizationError("unresolved", "run_id")

    source = {"ordinal": 1, "call_id": "model-call:001", "run_id":"unseen-child", "parent_run_id":None,
              "parent_call_id":None,"function_calls":[{"call_id":"question", "name":"planning_intake_update", "canonical_arguments":{"open_questions":[{"id":"deadline"}]}}]}
    records = _public_model_records([source], SimpleNamespace(resolve=unresolved), active=True, failure=True)
    assert records[0]["run_id"] == "unseen-child"
    assert records[0]["function_calls"] == source["function_calls"]


def test_proposal_cannot_hide_a_real_write(effect_chain):
    results = [load(path) for path in (effect_chain / "rules/rules").glob("*.json")]
    assert any("case.audit.no-write" in result["hard_gates"] for result in results)


def test_judge_request_hides_model_identity_in_delta(effect_chain):
    from learning_agent_eval.active_judge import FixedResponseJudgeProviderV3

    provider = FixedResponseJudgeProviderV3.from_file(
        FIXED, frozen_time="2026-01-02T03:04:05Z"
    )
    evaluate_active_judges(
        episodes=effect_chain / "runtime",
        rules=effect_chain / "rules",
        output=effect_chain / "judges",
        judge_mode="stub",
        provider=provider,
    )
    assert provider.requests
    assert "e1-scripted-model" not in json.dumps(provider.requests)


def test_all_materialized_action_classes_are_scoreable(effect_chain):
    for path in (effect_chain / "runtime/episodes").glob("*.json"):
        episode = load(path)
        if episode["result"]["action_classes"] in [
            ["PROPOSE_CHANGE", "APPLY_REVERSIBLE_PATCH"]
        ]:
            continue
        if any(
            op["tool_name"] == "stage.create"
            for op in episode["observable_trace"]["operations"]
        ):
            continue  # This Case deliberately still requests a different revision scope.
        result = load(effect_chain / "rules/rules" / path.name)
        assert result["status"] == "pass", (
            path.name,
            [c for c in result["checks"] if c["status"] == "fail"],
        )
