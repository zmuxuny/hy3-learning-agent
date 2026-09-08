"""Exercise semantic review through the unique active four-stage pipeline."""

import json
from pathlib import Path

from learning_agent_eval.active_aggregate import aggregate_active_results
from learning_agent_eval.active_judge import (
    FixedResponseJudgeProviderV3,
    evaluate_active_judges,
)
from learning_agent_eval.active_rules import evaluate_active_rules
from learning_agent_eval.active_runtime import run_active_runtime
from learning_agent_eval.case_specs import episode_id_for_case
from learning_agent_eval.validator import validate_dataset

ROOT = Path(__file__).resolve().parents[2]


def test_semantic_pending_and_confirmed_complete_active_pipeline(tmp_path):
    dataset = ROOT / "evaluation/datasets/decisionbench-v1.1-regression/calibration"
    case = json.loads((dataset / "cases/case-0001.json").read_text())
    episode_id = episode_id_for_case(case)
    runtime, rules, judges = (tmp_path / name for name in ["runtime", "rules", "judges"])
    run_active_runtime(dataset=dataset, manifest=dataset / "manifest.json", output=runtime, episode_ids={episode_id}, model_mode="stub")
    evaluate_active_rules(input_path=runtime, output=rules)
    payload = json.loads((ROOT / "evaluation/fixtures/e311-fixed-judge-responses-v3.json").read_text())["responses"][0]
    payload["semantic_issues"] = [{"issue_id": "synthetic-critical", "dimension_id": "D4", "severity": "critical", "reason_code": "synthetic-claim", "evidence_paths": ["result.action_class"], "public_summary": "Synthetic semantic concern for the review pipeline test."}]
    provider = FixedResponseJudgeProviderV3([payload], frozen_time="2026-09-01T00:00:00Z")
    evaluate_active_judges(episodes=runtime, rules=rules, output=judges, judge_mode="stub", provider=provider)
    aggregate = tmp_path / "aggregate"
    summary = aggregate_active_results(episodes=runtime, rules=rules, judges=judges, output=aggregate)
    assert "capability.semantic_review_pending" in summary.capability_blockers
    assert summary.failed_episode_ids == (episode_id,) and validate_dataset(aggregate).ok
    episode = json.loads((runtime / "episodes" / f"{episode_id}.json").read_text())
    judge = json.loads((judges / "judge-results" / f"{episode_id}.json").read_text())
    assert judge["status"] == "complete"
    review = [{"episode_id": episode_id, "episode_sha256": episode["provenance"]["episode_sha256"],
               "judge_result_sha256": judge["result_sha256"], "candidate_id": "issue:synthetic-critical",
               "verdict": "confirmed_critical", "reviewer_role": "primary_ai_reviewer", "rationale": "Synthetic confirmed verdict to test cap and evidence binding.", "evidence_paths": ["result.action_class"]}]
    review_path = tmp_path / "reviews.json"
    review_path.write_text(json.dumps(review))
    confirmed = tmp_path / "confirmed"
    summary = aggregate_active_results(episodes=runtime, rules=rules, judges=judges, output=confirmed, semantic_reviews=review_path)
    assert "capability.semantic_review_pending" not in summary.capability_blockers
    assert validate_dataset(confirmed).ok
    csvpath = confirmed / "reviewed-results.csv"
    csvpath.write_text(csvpath.read_text().replace(",39,fail,", ",95,pass,"))
    assert not validate_dataset(confirmed).ok
