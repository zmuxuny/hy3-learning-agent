"""Verify experiment denominators with independent ties and missing repeats."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_e5_experiments.py"


def test_ties_rule_contribution_and_missing_repeat_have_fixed_denominators(tmp_path):
    spec = importlib.util.spec_from_file_location("e5_experiment_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = []
    repeats = []
    for group in range(8):
        track = module.TRACKS[group // 2]
        for label in ["good", "mild", "severe"]:
            episode_id = f"g{group}-{label}"
            cases.append({"episode_id": episode_id, "track": track, "group": f"g{group}", "label": label})
            if label != "severe":
                repeats.append(episode_id)
            for repeat in range(1, 2 if label == "severe" else 6):
                judge_dir = tmp_path / f"judge-r{repeat}-{track}" / "judge-results"
                aggregate_dir = tmp_path / f"aggregate-r{repeat}-{track}" / "episodes"
                judge_dir.mkdir(parents=True, exist_ok=True)
                aggregate_dir.mkdir(parents=True, exist_ok=True)
                dimensions = [{"dimension_id": f"D{i}", "level": 1 if label == "mild" and i == 7 else 2} for i in range(1, 8)]
                judge = {"provider_attestation": {"calls": [{"status": "completed"}]}, "status": "complete", "dimensions": dimensions, "suggested_hard_gates": [], "semantic_issues": [], "result_sha256": "a" * 64}
                aggregate = {"final_score": 39 if label == "severe" else 95 if label == "mild" else 100,
                             "episode_outcome": "fail" if label == "severe" else "pass", "actual_hard_gates": ["fixture"] if label == "severe" else []}
                (judge_dir / f"{episode_id}.json").write_text(json.dumps(judge))
                (aggregate_dir / f"{episode_id}.json").write_text(json.dumps(aggregate))
    design = {"cases": cases, "repeat_ids": repeats}
    design["design_sha256"] = module.sha256_digest(design)
    (tmp_path / "design.json").write_text(json.dumps(design))
    module.summarize(tmp_path)
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["complete_evaluations"] == 88 and metrics["complete_repeat_cases"] == 16
    assert metrics["discrimination"]["raw_score"]["good_gt_severe"] == {"numerator": 0, "rate": 0.0}
    assert metrics["discrimination"]["rule_capped_score"]["strict_order"] == {"numerator": 8, "rate": 1.0}
    assert metrics["mean_conclusion_agreement"] == 1 and metrics["mean_score_std_population"] == 0
    (tmp_path / "judge-r5-planning/judge-results/g0-mild.json").unlink()
    module.summarize(tmp_path)
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["complete_evaluations"] == 87 and metrics["not_run"] == 1
    assert not metrics["experiment_complete"] and metrics["complete_repeat_cases"] == 15
    assert metrics["mean_conclusion_agreement"] == pytest.approx(15.8 / 16)

    path = tmp_path / "judge-r5-planning/judge-results/g0-mild.json"
    path.write_text(json.dumps({"status": "judge_error", "error_code": "judge_budget_exhausted", "provider_attestation": {"calls": []}, "dimensions": [], "semantic_issues": [], "suggested_hard_gates": [], "result_sha256": "c" * 64}))
    module.summarize(tmp_path)
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["not_run"] == 0 and metrics["not_attempted_evaluations"] == 1
    assert not metrics["experiment_complete"]
