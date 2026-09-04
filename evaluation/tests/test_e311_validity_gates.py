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
from learning_agent_eval.aggregate_v2 import (
    AGGREGATOR_IMPLEMENTATION_SHA256_V3,
    aggregate_results_v3,
)
from learning_agent_eval.blinding_v2 import build_blind_judge_input_v3
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.case_specs import episode_id_for_case
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.e31_io import load_e311_inputs
from learning_agent_eval.exporter_v4 import (
    ACTION_EFFECT_TYPES_V2,
    ACTION_MAPPING_SHA256_V2,
    _effects_and_result,
)
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    decision_episode_digest,
)
from learning_agent_eval.judge_v2 import evaluate_judges_v3
from learning_agent_eval.models import (
    CaseSpecV2,
    DecisionEpisodeV4,
    RuleResultV3,
    RuleRunManifestV3,
)
from learning_agent_eval.rule_runner_v2 import evaluate_run_rules_v3
from learning_agent_eval.rules import (
    RULE_IMPLEMENTATION_SHA256_V3,
    evaluate_rules_v3,
)
from learning_agent_eval.runner_v3 import run_agent_v4
from learning_agent_eval.schemas import schema_documents
from learning_agent_eval.validator import validate_dataset, validate_episode
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v4-engineering"
CASE_MANIFEST = DATASET / "manifest.json"
FIXED_RESPONSES = (
    PROJECT_ROOT / "evaluation" / "fixtures" / "e311-fixed-judge-responses-v3.json"
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
    runtime_summary = run_agent_v4(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        model_mode="stub",
    )
    rule_summary = evaluate_run_rules_v3(input_path=runtime, output=rules)
    judge_summary = evaluate_judges_v3(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_summary = aggregate_results_v3(
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
        committed = _load(PROJECT_ROOT / "evaluation" / "schemas" / name)
        assert committed == generated[name]
        assert committed["additionalProperties"] is False


def test_all_fourteen_actions_have_exact_nonheuristic_projection() -> None:
    text = render_action_declaration("Public answer.", ACTION_CLASSES)
    status, declared, public_text = parse_action_declaration(text)
    assert status == "valid"
    assert declared == ACTION_CLASSES
    assert public_text == "Public answer."

    call = {
        "call_id": "model-call:001",
        "call_purpose": "decision",
        "decision_relevant": True,
        "status": "completed",
        "tool_call_refs": [],
        "action_declaration_status": "valid",
        "declared_action_classes": list(ACTION_CLASSES),
    }
    attempts, effects, actions, issues = _effects_and_result(
        episode_id="episode-action-coverage",
        calls=[call],
        trace={"operations": [], "tool_invocations": []},
        state_after={"logical_entities": []},
        state_delta={"changes": []},
    )
    assert actions == list(ACTION_CLASSES)
    assert issues == []
    assert attempts[0]["declared_action_classes"] == list(ACTION_CLASSES)
    assert [item["effect_type"] for item in effects] == [
        ACTION_EFFECT_TYPES_V2[action] for action in ACTION_CLASSES
    ]


def test_full_active_chain_keeps_every_terminal_and_validates(
    e311_chain: dict[str, Any],
) -> None:
    assert len(e311_chain["runtime_summary"].episode_ids) == 11
    assert e311_chain["runtime_summary"].failure_ids == (RUNTIME_FAILURE_ID,)
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
    assert "mean_score" not in _load(e311_chain["aggregate"] / "run-manifest.json")


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
    assert _load(track_rules / "rule-manifest.json")["selected_track"] == ("assessment")

    promoted = _load(rules / "rules" / f"{positive_id}.json")
    promoted["formal_evaluation_result"] = True
    with pytest.raises(ValidationError, match="monotonically inherit Runtime"):
        RuleResultV3.model_validate(promoted)
    retroactive_selection = deepcopy(rule_manifest)
    retroactive_selection["selection_mode"] = "inherited"
    with pytest.raises(ValidationError, match="selection mode"):
        RuleRunManifestV3.model_validate(retroactive_selection)


def test_failure_only_partition_reaches_aggregate_without_judge_call(
    tmp_path: Path,
) -> None:
    case = _load(DATASET / "cases" / "case-e31-p-provider-failure.json")
    expected_episode_id = episode_id_for_case(case)
    runtime = tmp_path / "runtime-failure-only"
    rules = tmp_path / "rules-failure-only"
    judges = tmp_path / "judges-failure-only"
    aggregate = tmp_path / "aggregate-failure-only"
    run_agent_v4(
        dataset=DATASET,
        manifest=CASE_MANIFEST,
        output=runtime,
        episode_ids={expected_episode_id},
        model_mode="stub",
    )
    evaluate_run_rules_v3(input_path=runtime, output=rules)

    class NoCallProvider:
        mode = "stub"
        calls = 0

        def complete(self, _: dict[str, Any]) -> Any:
            self.calls += 1
            raise AssertionError("failure-only Judge must not call a Provider")

    provider = NoCallProvider()
    evaluate_judges_v3(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        provider=provider,
    )
    aggregate_results_v3(
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


def test_judge_and_aggregate_are_byte_deterministic(
    e311_chain: dict[str, Any], tmp_path: Path
) -> None:
    judges = tmp_path / "judges-second"
    aggregate = tmp_path / "aggregate-second"
    evaluate_judges_v3(
        episodes=e311_chain["runtime"],
        rules=e311_chain["rules"],
        output=judges,
        judge_mode="stub",
        stub_response=FIXED_RESPONSES,
    )
    aggregate_results_v3(
        episodes=e311_chain["runtime"],
        rules=e311_chain["rules"],
        judges=judges,
        output=aggregate,
    )
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
