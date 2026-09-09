from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from learning_agent_eval.action_protocol import (
    ACTION_CLASSES,
    ACTION_DECLARATION_PROTOCOL_SHA256,
    parse_action_declaration,
    render_action_declaration,
)
from learning_agent_eval.active_aggregate import (
    AGGREGATOR_IMPLEMENTATION_SHA256_V3,
    aggregate_active_results,
    aggregate_episode_v3,
)
from learning_agent_eval.active_judge import (
    FixedResponseJudgeProviderV3,
    _evaluate_one_v3,
    evaluate_active_judges,
)
from learning_agent_eval.active_rules import (
    RuleEvaluationV2Error,
    evaluate_active_rules,
)
from learning_agent_eval.active_runtime import run_active_runtime
from learning_agent_eval.blinding_v2 import build_blind_judge_input_v3
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.case_specs import episode_id_for_case
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.e3_io import current_git_commit
from learning_agent_eval.e31_io import load_e311_inputs
from learning_agent_eval.exporter_v4 import (
    ACTION_EFFECT_TYPES_V2,
    ACTION_MAPPING_SHA256_V2,
    _effects_and_result,
)
from learning_agent_eval.integrity import (
    aggregate_result_digest,
    artifact_manifest_digest,
    decision_episode_digest,
    rule_result_digest,
)
from learning_agent_eval.models import (
    AggregateRunManifestV3,
    CaseSpecV2,
    DecisionEpisodeV4,
    RuleResultV3,
    RuleRunManifestV3,
)
from learning_agent_eval.rules import (
    RULE_IMPLEMENTATION_SHA256_V3,
    evaluate_rules_v3,
)
from learning_agent_eval.runtime_metadata import dependency_lock_sha256
from learning_agent_eval.schemas import ACTIVE_SCHEMA_RELATIVE_ROOT, schema_documents
from learning_agent_eval.validator import validate_dataset, validate_episode
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v4-engineering"
CASE_MANIFEST = DATASET / "manifest.json"
FIXED_RESPONSES = (
    PROJECT_ROOT / "evaluation" / "fixtures" / "active-fixed-judge-responses-v3.json"
)
RUNTIME_FAILURE_ID = "failure:case-e31-p-provider-failure"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _files(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def e311_chain(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("e311-chain")
    runtime = root / "runtime"
    rules = root / "rules"
    judges = root / "judges"
    aggregate = root / "aggregate"
    runtime_summary = run_active_runtime(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        model_mode="stub",
    )
    rule_summary = evaluate_active_rules(input_path=runtime, output=rules)
    judge_summary = evaluate_active_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_summary = aggregate_active_results(
        episodes=runtime,
        rules=rules,
        judges=judges,
        output=aggregate,
    )
    by_family: dict[str, str] = {}
    for path in (runtime / "episodes").glob("*.json"):
        episode = _load(path)
        by_family[episode["scenario_family_id"]] = episode["episode_id"]
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
        "by_family": by_family,
    }


def _artifact(chain: dict[str, Any], kind: str, family: str) -> dict[str, Any]:
    episode_id = chain["by_family"][family]
    directory = {
        "episode": chain["runtime"] / "episodes",
        "rule": chain["rules"] / "rules",
        "judge": chain["judges"] / "judge-results",
        "aggregate": chain["aggregate"] / "episodes",
    }[kind]
    return _load(directory / f"{episode_id}.json")


def test_active_contract_schemas_are_strict_and_committed() -> None:
    names = {
        "case-spec-v2.schema.json",
        "judge-reference-v2.schema.json",
        "decision-episode-v4.schema.json",
        "runtime-failure-v2.schema.json",
        "case-suite-manifest-v2.schema.json",
        "runtime-run-manifest-v3.schema.json",
        "rule-result-v3.schema.json",
        "rule-run-manifest-v3.schema.json",
        "judge-result-v3.schema.json",
        "judge-run-manifest-v3.schema.json",
        "aggregate-result-v3.schema.json",
        "aggregate-track-result-v3.schema.json",
        "aggregate-run-manifest-v3.schema.json",
    }
    generated = schema_documents()
    assert names <= set(generated)
    for name in names:
        committed = _load(PROJECT_ROOT / ACTIVE_SCHEMA_RELATIVE_ROOT / name)
        assert committed == generated[name]
        assert committed["additionalProperties"] is False


def test_all_fourteen_actions_have_exact_nonheuristic_projection() -> None:
    for action in ACTION_CLASSES:
        text = render_action_declaration("Public answer.", [action])
        status, declared, public_text = parse_action_declaration(text)
        assert (status, declared, public_text) == (
            "valid",
            (action,),
            "Public answer.",
        )
        call = {
            "call_id": "model-call:001",
            "call_purpose": "decision",
            "decision_relevant": True,
            "status": "completed",
            "tool_call_refs": [],
            "action_declaration_status": "valid",
            "declared_action_classes": [action],
        }
        attempts, effects, actions, issues = _effects_and_result(
            episode_id="episode-action-coverage",
            calls=[call],
            trace={"operations": [], "tool_invocations": []},
            state_after={"logical_entities": []},
            state_delta={"changes": []},
        )
        assert actions == [action]
        assert issues == []
        assert attempts[0]["declared_action_classes"] == [action]
        assert [item["effect_type"] for item in effects] == [
            ACTION_EFFECT_TYPES_V2[action]
        ]

    combined = render_action_declaration(
        "Please clarify.", ["INSUFFICIENT_EVIDENCE", "REQUEST_CLARIFICATION"]
    )
    assert parse_action_declaration(combined)[0] == "valid"
    with pytest.raises(ValueError, match="ambiguous"):
        render_action_declaration("Invalid combination.", ACTION_CLASSES)


def test_declared_action_and_observed_tool_mismatch_remains_scoreable() -> None:
    attempts, effects, actions, issues = _effects_and_result(
        episode_id="episode-action-tool-mismatch",
        calls=[
            {
                "call_id": "model-call:001",
                "call_purpose": "decision",
                "decision_relevant": True,
                "tool_call_refs": ["invocation:001"],
                "action_declaration_status": "valid",
                "declared_action_classes": ["WAIT"],
            }
        ],
        trace={
            "operations": [],
            "tool_invocations": [
                {
                    "invocation_id": "invocation:001",
                    "tool_name": "notification.send",
                    "observation_status": "completed",
                    "durable_status": "committed",
                    "operation_refs": [],
                    "result": {},
                }
            ],
        },
        state_after={"logical_entities": []},
        state_delta={"changes": []},
    )
    assert attempts[0]["declared_action_classes"] == ["WAIT"]
    assert effects[0]["action_classes"] == ["INTERVENE_MESSAGE"]
    assert actions == ["WAIT", "INTERVENE_MESSAGE"]
    assert issues == ["action.declaration_effect_mismatch"]


def test_full_active_chain_keeps_every_terminal_and_validates(
    e311_chain: dict[str, Any],
) -> None:
    assert len(e311_chain["runtime_summary"].episode_ids) == 11
    assert e311_chain["runtime_summary"].failure_ids == (RUNTIME_FAILURE_ID,)
    failure = _load(
        e311_chain["runtime"] / "failures" / f"{RUNTIME_FAILURE_ID}.json"
    )
    assert failure["isolation_evidence"] is None
    assert failure["protocol_eligible"] is False
    for key in ("runtime", "rules", "judges", "aggregate"):
        report = validate_dataset(e311_chain[key])
        assert report.ok, [item.render() for item in report.issues]
    for summary in (
        e311_chain["runtime_summary"],
        e311_chain["rule_summary"],
        e311_chain["judge_summary"],
        e311_chain["aggregate_summary"],
    ):
        assert summary.formal_evaluation_result is False
    manifests = [
        _load(e311_chain["runtime"] / "run-manifest.json"),
        _load(e311_chain["rules"] / "rule-manifest.json"),
        _load(e311_chain["judges"] / "run-manifest.json"),
        _load(e311_chain["aggregate"] / "run-manifest.json"),
    ]
    assert all(item["git_commit"] == current_git_commit() for item in manifests)
    assert all(item["trusted_benchmark_run"] is False for item in manifests)
    aggregate_manifest = manifests[-1]
    assert aggregate_manifest["formal_capability_result"] is False
    assert aggregate_manifest["capability_blockers"] == [
        "capability.run_not_trusted",
        "capability.runtime_failure",
    ]
    assert "mean_score" not in aggregate_manifest


def test_wrong_cross_track_combined_and_missing_actions_remain_scoreable(
    e311_chain: dict[str, Any],
) -> None:
    expectations = {
        "family-e31-a-wrong-action": ["REVISION_REQUIRED"],
        "family-e311-a-cross-track-change": ["APPLY_REVERSIBLE_PATCH"],
        "family-e311-a-combined-actions": [
            "INSUFFICIENT_EVIDENCE",
            "REQUEST_CLARIFICATION",
        ],
        "family-e311-a-missing-action-declaration": ["NO_OP"],
    }
    for family, actions in expectations.items():
        episode = _artifact(e311_chain, "episode", family)
        rule = _artifact(e311_chain, "rule", family)
        aggregate = _artifact(e311_chain, "aggregate", family)
        assert validate_episode(episode, source=f"{family}.json") == ()
        assert episode["result"]["action_classes"] == actions
        assert rule["status"] == "fail"
        assert rule["hard_gates"]
        assert aggregate["status"] == "complete"
        assert aggregate["final_score"] == 39
        assert aggregate["episode_outcome"] == "fail"

    missing = _artifact(
        e311_chain, "episode", "family-e311-a-missing-action-declaration"
    )
    assert missing["result"]["classification_issues"] == ["action.declaration_missing"]
    cross_rule = _artifact(e311_chain, "rule", "family-e311-a-cross-track-change")
    prohibition = next(
        item
        for item in cross_rule["checks"]
        if item["check_id"] == "case.e311.assessment.prohibited-plan-change"
    )
    assert prohibition["status"] == "fail"
    assert prohibition["observed"]["constraint_kind"] == "must_not"
    assert prohibition["observed"]["proposition_holds"] is True
    assert prohibition["expected"]["required_proposition_value"] is False


def test_guard_block_and_quiz_review_writes_are_complete_behaviors(
    e311_chain: dict[str, Any],
) -> None:
    guard = _artifact(e311_chain, "episode", "family-e31-i-guard-blocked")
    assert guard["result"]["guard"]["status"] == "blocked"
    assert guard["result"]["layers"]["durable_status"] == "blocked"
    blocked = guard["result"]["layers"]["final_effects"][0]
    assert blocked["status"] == "blocked"
    assert blocked["entity_refs"] == []
    assert guard["isolation_evidence"]["recording_sink_attempts"] == 0

    quiz = _artifact(e311_chain, "episode", "family-e311-a-quiz-review-action")
    assert quiz["result"]["action_classes"] == ["INTERVENE_QUIZ_OR_REVIEW"]
    assert quiz["result"]["classification_issues"] == []
    assert [
        item["tool_name"] for item in quiz["observable_trace"]["tool_invocations"]
    ] == ["quiz.create", "review.schedule"]
    assert {
        entity["entity_type"]
        for entity in quiz["state_after"]["logical_entities"]
        if entity["logical_id"]
        in {
            ref
            for effect in quiz["result"]["layers"]["final_effects"]
            for ref in effect["entity_refs"]
        }
    } == {"quiz", "review"}


def test_parallel_children_have_unique_start_order_and_causal_parent_call(
    e311_chain: dict[str, Any],
) -> None:
    episode = _artifact(e311_chain, "episode", "family-e31-p-child-hierarchy")
    calls = episode["observable_trace"]["model_calls"]
    assert [item["ordinal"] for item in calls] == list(range(1, len(calls) + 1))
    assert len({item["call_id"] for item in calls}) == len(calls)
    children = [item for item in calls if item["call_purpose"] == "subagent_decision"]
    assert len(children) == 2
    assert {item["parent_call_id"] for item in children} == {"model-call:001"}
    assert len({item["run_id"] for item in children}) == 2


def test_path_exact_blinding_preserves_business_words_and_hides_case_metadata(
    e311_chain: dict[str, Any],
) -> None:
    inputs = load_e311_inputs(episodes=e311_chain["runtime"], rules=e311_chain["rules"])
    bundle = next(
        item
        for item in inputs.bundles
        if item.episode["scenario_family_id"] == "family-e31-p-positive"
    )
    first = build_blind_judge_input_v3(
        bundle.episode, bundle.rule_result, bundle.judge_reference
    )
    second = build_blind_judge_input_v3(
        bundle.episode, bundle.rule_result, bundle.judge_reference
    )
    encoded = canonical_json_bytes(first.document)
    assert first == second
    assert b"Good baseline and candidate are ordinary learning-domain terms" in encoded
    assert first.document["judge_reference"]["predicate_semantics"] == (
        "constraint-proposition-v1"
    )
    assert any(
        item["public_statement"]
        for item in first.document["judge_reference"]["constraints"]
    )
    for private_key in (
        b"quality_label",
        b"mutation_source",
        b"author_role",
        b"reviewer_role",
        b"adjudication_note",
        b"provider_attestation",
        b"request_model",
    ):
        assert private_key not in encoded
    assert b"case-e31-p-positive" not in encoded


def test_casespec_v2_freezes_constraint_proposition_semantics() -> None:
    case = _load(DATASET / "cases" / "case-e31-a-positive.json")
    one_kind = deepcopy(case)
    one_kind["judge_criteria"]["constraints"] = [
        item
        for item in one_kind["judge_criteria"]["constraints"]
        if item["kind"] == "must_satisfy"
    ]
    with pytest.raises(ValidationError, match="both must_satisfy and must_not"):
        CaseSpecV2.model_validate(one_kind)
    assert case["judge_criteria"]["predicate_semantics"] == (
        "constraint-proposition-v1"
    )


def test_prohibited_file_access_is_a_critical_scoreable_failure(
    e311_chain: dict[str, Any],
) -> None:
    episode = deepcopy(_artifact(e311_chain, "episode", "family-e31-a-positive"))
    episode["isolation_evidence"]["prohibited_file_access"] = 1
    episode["result"]["layers"]["protocol_eligibility"] = "invalid"
    episode["provenance"]["protocol_eligible"] = False
    episode["provenance"]["episode_sha256"] = decision_episode_digest(episode)
    assert validate_episode(episode, source="prohibited-file-access.json") == ()

    inputs = load_e311_inputs(episodes=e311_chain["runtime"], rules=e311_chain["rules"])
    reference = next(
        item.judge_reference
        for item in inputs.bundles
        if item.episode["episode_id"] == episode["episode_id"]
    )
    runtime_manifest = _load(e311_chain["runtime"] / "run-manifest.json")
    result = evaluate_rules_v3(
        episode,
        reference,
        input_runtime_manifest_sha256=runtime_manifest["manifest_sha256"],
        input_runtime_formal_evaluation_result=False,
        selection_mode="inherited",
        worktree_clean=False,
    )
    check = next(
        item
        for item in result["checks"]
        if item["check_id"] == "isolation.prohibited_file_access"
    )
    assert check["status"] == "fail"
    assert check["severity"] == "critical"
    assert "isolation.prohibited_file_access" in result["hard_gates"]


def test_formal_state_cannot_be_recovered_by_posthoc_cli_filter(
    e311_chain: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    positive_id = e311_chain["by_family"]["family-e31-p-positive"]
    rules = tmp_path / "filtered-rules"
    judges = tmp_path / "filtered-judges"
    aggregate = tmp_path / "filtered-aggregate"
    track_rules = tmp_path / "track-rules"
    assert (
        cli_main(
            [
                "evaluate-rules",
                "--input",
                str(e311_chain["runtime"]),
                "--output",
                str(rules),
                "--episode-id",
                positive_id,
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "evaluate-judge",
                "--episodes",
                str(e311_chain["runtime"]),
                "--rules",
                str(rules),
                "--output",
                str(judges),
                "--judge-mode",
                "stub",
                "--stub-response",
                str(FIXED_RESPONSES),
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "aggregate-results",
                "--episodes",
                str(e311_chain["runtime"]),
                "--rules",
                str(rules),
                "--judges",
                str(judges),
                "--output",
                str(aggregate),
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "evaluate-rules",
                "--input",
                str(e311_chain["runtime"]),
                "--output",
                str(track_rules),
                "--track",
                "assessment",
            ]
        )
        == 2
    )
    capsys.readouterr()

    rule_manifest = _load(rules / "rule-manifest.json")
    judge_manifest = _load(judges / "run-manifest.json")
    aggregate_manifest = _load(aggregate / "run-manifest.json")
    assert rule_manifest["selection_mode"] == "adhoc_filter"
    assert rule_manifest["requested_episode_ids"] == [positive_id]
    for manifest in (rule_manifest, judge_manifest, aggregate_manifest):
        assert manifest["formal_evaluation_result"] is False
        assert manifest["runtime_failure_ids"] == [RUNTIME_FAILURE_ID]
    assert aggregate_manifest["formal_capability_result"] is False
    assert "capability.posthoc_filter" in aggregate_manifest["capability_blockers"]
    assert _load(track_rules / "rule-manifest.json")["selected_track"] == ("assessment")

    promoted = _load(rules / "rules" / f"{positive_id}.json")
    promoted["formal_evaluation_result"] = True
    with pytest.raises(ValidationError, match="Input should be False"):
        RuleResultV3.model_validate(promoted)
    retroactive_selection = deepcopy(rule_manifest)
    retroactive_selection["selection_mode"] = "inherited"
    with pytest.raises(ValidationError, match="selection mode"):
        RuleRunManifestV3.model_validate(retroactive_selection)


@pytest.mark.parametrize(
    ("family_id", "track"),
    (("family-e31-a-positive", None), (None, "assessment")),
)
def test_runtime_filtered_partition_remains_protocol_valid_but_nonformal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family_id: str | None,
    track: str | None,
) -> None:
    for module_name in (
        "active_runtime",
        "active_rules",
        "active_judge",
        "active_aggregate",
    ):
        monkeypatch.setattr(
            f"learning_agent_eval.{module_name}.git_worktree_clean", lambda: True
        )
    episode_ids = None
    if family_id is not None:
        case = _load(DATASET / "cases" / "case-e31-a-positive.json")
        episode_ids = {episode_id_for_case(case)}
    suffix = family_id or track
    runtime = tmp_path / f"runtime-{suffix}"
    rules = tmp_path / f"rules-{suffix}"
    judges = tmp_path / f"judges-{suffix}"
    aggregate = tmp_path / f"aggregate-{suffix}"
    run_active_runtime(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        episode_ids=episode_ids,
        track=track,
        model_mode="stub",
    )
    evaluate_active_rules(input_path=runtime, output=rules)
    evaluate_active_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_active_results(
        episodes=runtime,
        rules=rules,
        judges=judges,
        output=aggregate,
    )
    for root in (runtime, rules, judges, aggregate):
        report = validate_dataset(root)
        assert report.ok, [item.render() for item in report.issues]
    manifests = (
        _load(runtime / "run-manifest.json"),
        _load(rules / "rule-manifest.json"),
        _load(judges / "run-manifest.json"),
        _load(aggregate / "run-manifest.json"),
    )
    assert all(item["protocol_eligible"] is True for item in manifests)
    assert all(item["trusted_benchmark_run"] is False for item in manifests)
    assert all(item["formal_evaluation_result"] is False for item in manifests)
    assert manifests[-1]["formal_capability_result"] is False
    assert "capability.posthoc_filter" in manifests[-1]["capability_blockers"]


def test_failure_only_partition_reaches_aggregate_without_judge_call(
    tmp_path: Path,
) -> None:
    case = _load(DATASET / "cases" / "case-e31-p-provider-failure.json")
    expected_episode_id = episode_id_for_case(case)
    runtime = tmp_path / "runtime-failure-only"
    rules = tmp_path / "rules-failure-only"
    judges = tmp_path / "judges-failure-only"
    aggregate = tmp_path / "aggregate-failure-only"
    run_active_runtime(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        episode_ids={expected_episode_id},
        model_mode="stub",
    )
    evaluate_active_rules(input_path=runtime, output=rules)

    class NoCallProvider:
        mode = "stub"
        calls = 0

        def complete(self, _: dict[str, Any]) -> Any:
            self.calls += 1
            raise AssertionError("failure-only Judge must not call a Provider")

    provider = NoCallProvider()
    evaluate_active_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        provider=provider,
    )
    aggregate_active_results(
        episodes=runtime,
        rules=rules,
        judges=judges,
        output=aggregate,
    )
    assert provider.calls == 0
    for root in (runtime, rules, judges, aggregate):
        report = validate_dataset(root)
        assert report.ok, [item.render() for item in report.issues]
    assert not list((aggregate / "episodes").glob("*.json"))
    planning = _load(aggregate / "tracks" / "planning.json")
    assert planning["score_count"] == 0
    assert planning["mean_score"] is None
    assert planning["runtime_failure_ids"] == [RUNTIME_FAILURE_ID]


def test_active_judge_error_is_disclosed_and_excluded_from_track_mean(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    episode_id = e311_chain["by_family"]["family-e31-a-positive"]
    episode = _artifact(e311_chain, "episode", "family-e31-a-positive")
    provider = FixedResponseJudgeProviderV3(
        [{"invalid": 1}, {"still_invalid": 2}],
        frozen_time=episode["environment"]["frozen_time"],
    )
    judges = tmp_path / "judge-error"
    aggregate = tmp_path / "aggregate-error"
    summary = evaluate_active_judges(
        episodes=e311_chain["runtime"],
        rules=e311_chain["rules"],
        output=judges,
        judge_mode="stub",
        provider=provider,
        episode_ids={episode_id},
    )
    aggregate_active_results(
        episodes=e311_chain["runtime"],
        rules=e311_chain["rules"],
        judges=judges,
        output=aggregate,
    )
    assert provider.calls == 2
    assert summary.judge_error_episode_ids == (episode_id,)
    assessment = _load(aggregate / "tracks" / "assessment.json")
    assert assessment["judge_error_episode_ids"] == [episode_id]
    assert assessment["score_count"] == 0
    assert assessment["mean_score"] is None
    manifest = _load(aggregate / "run-manifest.json")
    assert "capability.judge_error" in manifest["capability_blockers"]
    assert manifest["formal_capability_result"] is False


def test_active_invalid_input_is_unscored_and_never_calls_judge(
    e311_chain: dict[str, Any]
) -> None:
    inputs = load_e311_inputs(
        episodes=e311_chain["runtime"], rules=e311_chain["rules"]
    )
    bundle = next(
        item
        for item in inputs.bundles
        if item.episode["scenario_family_id"] == "family-e31-a-positive"
    )
    rule = deepcopy(bundle.rule_result)
    rule["checks"][0]["status"] = "invalid_input"
    rule["checks"][0]["reason_code"] = "evidence_missing"
    rule["hard_gates"] = []
    rule["status"] = "invalid_input"
    rule["result_sha256"] = rule_result_digest(rule)
    RuleResultV3.model_validate(rule)
    provider = FixedResponseJudgeProviderV3(
        [{"must_not_be_used": True}],
        frozen_time=bundle.episode["environment"]["frozen_time"],
    )
    judge, repaired = _evaluate_one_v3(
        episode=bundle.episode,
        rule_result=rule,
        reference=bundle.judge_reference,
        rule_manifest=inputs.rule_manifest,
        judge_mode="stub",
        provider=provider,
        selection_mode="adhoc_filter",
        git_commit=current_git_commit(),
        worktree_clean=False,
        dependency_digest=dependency_lock_sha256(),
    )
    assert provider.calls == 0
    assert repaired is False
    assert judge["status"] == "invalid_input"
    assert judge["dimensions"] == []
    aggregate = aggregate_episode_v3(
        episode=bundle.episode,
        rule_result=rule,
        judge_result=judge,
        judge_manifest=_load(e311_chain["judges"] / "run-manifest.json"),
        selection_mode="adhoc_filter",
        worktree_clean=False,
    )
    assert aggregate["status"] == "invalid_input"
    assert aggregate["raw_score"] is None
    assert aggregate["final_score"] is None


def test_trusted_run_and_publishable_capability_are_independent_states(
    e311_chain: dict[str, Any]
) -> None:
    engineering = _load(e311_chain["aggregate"] / "run-manifest.json")
    forged = deepcopy(engineering)
    forged.update(
        {
            "capability_blockers": [],
            "formal_capability_result": True,
            "formal_evaluation_result": True,
            "evaluation_status": "formal_model_evaluation",
        }
    )
    with pytest.raises(ValidationError, match="formal capability"):
        AggregateRunManifestV3.model_validate(forged)

    trusted_with_failure = deepcopy(engineering)
    trusted_with_failure.update(
        {
            "input_judge_manifest_trusted_benchmark_run": True,
            "judge_mode": "real",
            "selection_mode": "inherited",
            "worktree_clean": True,
            "protocol_eligible": True,
            "provider_eligible": True,
            "trusted_benchmark_run": True,
            "result_trusted_benchmark_states": {
                episode_id: True for episode_id in engineering["episode_ids"]
            },
            "runtime_terminals": [
                {
                    **terminal,
                    "protocol_eligible": True,
                    "provider_eligible": True,
                }
                for terminal in engineering["runtime_terminals"]
            ],
            "capability_blockers": ["capability.runtime_failure"],
            "formal_capability_result": False,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
        }
    )
    trusted_with_failure["manifest_sha256"] = artifact_manifest_digest(
        trusted_with_failure
    )
    validated = AggregateRunManifestV3.model_validate(trusted_with_failure)
    assert validated.trusted_benchmark_run is True
    assert validated.formal_capability_result is False

    publishable = deepcopy(trusted_with_failure)
    failure_track = publishable["runtime_failure_tracks"].pop(RUNTIME_FAILURE_ID)
    assert failure_track == "planning"
    publishable["input_runtime_failure_digests"].pop(RUNTIME_FAILURE_ID)
    publishable["runtime_failure_ids"] = []
    publishable["runtime_terminals"] = [
        terminal
        for terminal in publishable["runtime_terminals"]
        if terminal["artifact_id"] != RUNTIME_FAILURE_ID
    ]
    publishable["expected_total_cases"] = len(publishable["episode_ids"])
    publishable["expected_track_counts"]["planning"] -= 1
    publishable.update(
        {
            "judge_mode": "real",
            "capability_blockers": [],
            "formal_capability_result": True,
            "formal_evaluation_result": True,
            "evaluation_status": "formal_model_evaluation",
        }
    )
    publishable["manifest_sha256"] = artifact_manifest_digest(publishable)
    validated_publishable = AggregateRunManifestV3.model_validate(publishable)
    assert validated_publishable.trusted_benchmark_run is True
    assert validated_publishable.formal_capability_result is True


def test_unregistered_aggregate_cannot_self_promote_to_formal(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    forged_root = tmp_path / "unregistered-formal-aggregate"
    shutil.copytree(e311_chain["aggregate"], forged_root)
    manifest = _load(forged_root / "run-manifest.json")

    for path in sorted((forged_root / "episodes").glob("*.json")):
        result = _load(path)
        result.update(
            {
                "input_judge_manifest_trusted_benchmark_run": True,
                "worktree_clean": True,
                "protocol_eligible": True,
                "provider_eligible": True,
                "trusted_benchmark_run": True,
            }
        )
        result["result_sha256"] = aggregate_result_digest(result)
        path.write_bytes(canonical_json_bytes(result))
        manifest["aggregate_result_digests"][result["episode_id"]] = result[
            "result_sha256"
        ]
        manifest["result_trusted_benchmark_states"][result["episode_id"]] = True

    for path in sorted((forged_root / "tracks").glob("*.json")):
        result = _load(path)
        result.update(
            {
                "input_judge_manifest_trusted_benchmark_run": True,
                "runtime_failure_ids": [],
                "result_trusted_benchmark_states": {
                    episode_id: True for episode_id in result["episode_ids"]
                },
                "worktree_clean": True,
                "protocol_eligible": True,
                "provider_eligible": True,
                "trusted_benchmark_run": True,
            }
        )
        result["result_sha256"] = aggregate_result_digest(result)
        path.write_bytes(canonical_json_bytes(result))
        manifest["track_result_digests"][result["track"]] = result["result_sha256"]

    failure_track = manifest["runtime_failure_tracks"].pop(RUNTIME_FAILURE_ID)
    assert failure_track == "planning"
    manifest["input_runtime_failure_digests"].pop(RUNTIME_FAILURE_ID)
    manifest["runtime_failure_ids"] = []
    manifest["runtime_terminals"] = [
        {
            **terminal,
            "protocol_eligible": True,
            "provider_eligible": True,
        }
        for terminal in manifest["runtime_terminals"]
        if terminal["artifact_id"] != RUNTIME_FAILURE_ID
    ]
    manifest["expected_total_cases"] = len(manifest["episode_ids"])
    manifest["expected_track_counts"] = {
        track: sum(value == track for value in manifest["episode_tracks"].values())
        for track in ("planning", "intervention", "assessment", "revision")
    }
    manifest.update(
        {
            "judge_mode": "real",
            "input_judge_manifest_trusted_benchmark_run": True,
            "worktree_clean": True,
            "protocol_eligible": True,
            "provider_eligible": True,
            "trusted_benchmark_run": True,
            "capability_blockers": [],
            "formal_capability_result": True,
            "formal_evaluation_result": True,
            "evaluation_status": "formal_model_evaluation",
        }
    )
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    AggregateRunManifestV3.model_validate(manifest)
    (forged_root / "run-manifest.json").write_bytes(canonical_json_bytes(manifest))

    report = validate_dataset(forged_root)
    assert report.ok is False
    assert "manifest.aggregate_v3_capability_mismatch" in {
        issue.code for issue in report.issues
    }


def test_deleting_runtime_failure_breaks_closed_inventory_before_rules(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime-with-deleted-failure"
    output = tmp_path / "rules-must-not-publish"
    shutil.copytree(e311_chain["runtime"], runtime)
    (runtime / "failures" / f"{RUNTIME_FAILURE_ID}.json").unlink()
    report = validate_dataset(runtime)
    assert not report.ok
    assert "manifest.runtime_v3_inventory_mismatch" in {
        item.code for item in report.issues
    }
    with pytest.raises(RuleEvaluationV2Error, match="failed validation"):
        evaluate_active_rules(input_path=runtime, output=output)
    assert not output.exists()


def test_track_partition_remains_auditable_but_never_formal(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    rules = tmp_path / "track-rules"
    judges = tmp_path / "track-judges"
    aggregate = tmp_path / "track-aggregate"
    evaluate_active_rules(
        input_path=e311_chain["runtime"], output=rules, track="assessment"
    )
    evaluate_active_judges(
        episodes=e311_chain["runtime"],
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_active_results(
        episodes=e311_chain["runtime"],
        rules=rules,
        judges=judges,
        output=aggregate,
    )
    for root in (rules, judges, aggregate):
        report = validate_dataset(root)
        assert report.ok, [item.render() for item in report.issues]
    manifest = _load(aggregate / "run-manifest.json")
    assert manifest["selected_track"] is None
    assert manifest["selection_mode"] == "adhoc_filter"
    assert manifest["formal_capability_result"] is False
    assert manifest["trusted_benchmark_run"] is False
    assert {
        "capability.case_inventory_incomplete",
        "capability.four_tracks_incomplete",
        "capability.posthoc_filter",
        "capability.run_not_trusted",
        "capability.track_inventory_mismatch",
    } <= set(manifest["capability_blockers"])


def test_source_digests_are_executable_and_tampering_fails_closed(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    rule_manifest = _load(e311_chain["rules"] / "rule-manifest.json")
    aggregate_manifest = _load(e311_chain["aggregate"] / "run-manifest.json")
    assert rule_manifest["evaluator_implementation_sha256"] == (
        RULE_IMPLEMENTATION_SHA256_V3
    )
    assert aggregate_manifest["aggregator_implementation_sha256"] == (
        AGGREGATOR_IMPLEMENTATION_SHA256_V3
    )

    tampered = tmp_path / "tampered-rules"
    shutil.copytree(e311_chain["rules"], tampered)
    manifest_path = tampered / "rule-manifest.json"
    manifest = _load(manifest_path)
    manifest["evaluator_implementation_sha256"] = "0" * 64
    manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    report = validate_dataset(tampered)
    assert not report.ok
    assert "manifest.rule_v3_implementation_mismatch" in {
        item.code for item in report.issues
    }

    tampered_aggregate = tmp_path / "tampered-aggregate"
    shutil.copytree(e311_chain["aggregate"], tampered_aggregate)
    aggregate_path = tampered_aggregate / "run-manifest.json"
    aggregate = _load(aggregate_path)
    aggregate["aggregate_source_bundle_sha256"] = "0" * 64
    aggregate["aggregator_implementation_sha256"] = "0" * 64
    aggregate["manifest_sha256"] = artifact_manifest_digest(aggregate)
    aggregate_path.write_bytes(canonical_json_bytes(aggregate))
    aggregate_report = validate_dataset(tampered_aggregate)
    assert not aggregate_report.ok
    assert "manifest.aggregate_v3_implementation_mismatch" in {
        item.code for item in aggregate_report.issues
    }


def test_two_complete_active_chains_are_byte_deterministic(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime-second"
    rules = tmp_path / "rules-second"
    judges = tmp_path / "judges-second"
    aggregate = tmp_path / "aggregate-second"
    run_active_runtime(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        model_mode="stub",
    )
    evaluate_active_rules(input_path=runtime, output=rules)
    evaluate_active_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_active_results(
        episodes=runtime,
        rules=rules,
        judges=judges,
        output=aggregate,
    )
    assert _files(runtime) == _files(e311_chain["runtime"])
    assert _files(rules) == _files(e311_chain["rules"])
    assert _files(judges) == _files(e311_chain["judges"])
    assert _files(aggregate) == _files(e311_chain["aggregate"])


def test_active_outputs_have_zero_external_calls_and_no_sqlite(
    e311_chain: dict[str, Any],
) -> None:
    counters = (
        "network_calls",
        "smtp_calls",
        "smtp_ssl_calls",
        "web_push_calls",
        "imap_calls",
        "imap_ssl_calls",
        "prohibited_file_access",
        "outside_sqlite_access",
        "subprocess_calls",
        "published_sqlite_files",
    )
    for path in (e311_chain["runtime"] / "episodes").glob("*.json"):
        isolation = _load(path)["isolation_evidence"]
        assert {key: isolation[key] for key in counters} == {key: 0 for key in counters}
    for root in (
        e311_chain["runtime"],
        e311_chain["rules"],
        e311_chain["judges"],
        e311_chain["aggregate"],
    ):
        assert not [
            path
            for path in root.rglob("*")
            if path.is_file()
            and (
                path.suffix in {".db", ".sqlite", ".sqlite3"}
                or path.name.endswith(("-wal", "-shm"))
            )
        ]


def test_active_models_reject_unknown_fields_and_protocol_digest_drift(
    e311_chain: dict[str, Any],
) -> None:
    episode = _artifact(e311_chain, "episode", "family-e31-a-positive")
    episode["unknown"] = True
    with pytest.raises(ValidationError):
        DecisionEpisodeV4.model_validate(episode)
    assert (
        ACTION_DECLARATION_PROTOCOL_SHA256
        == episode["observable_trace"]["model_calls"][0]["action_protocol_sha256"]
    )
    assert ACTION_MAPPING_SHA256_V2 == episode["result"]["action_mapping_sha256"]
