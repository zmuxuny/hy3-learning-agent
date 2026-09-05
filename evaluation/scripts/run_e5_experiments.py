"""Fixed Calibration design, paid active-chain execution, and explicit denominators.

Outputs must live outside the checkout during real execution. No API keys or
expanded Judge prompts are written. This is an experiment driver, not another
Runtime/Judge implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from learning_agent_eval.active_aggregate import aggregate_active_results
from learning_agent_eval.active_judge import evaluate_active_judges
from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.case_specs import episode_id_for_case
from learning_agent_eval.e3_io import current_git_commit
from learning_agent_eval.rubric import (
    DIMENSION_WEIGHTS,
    JUDGE_CONFIG_DOCUMENT_V3,
    JUDGE_PROMPT_SHA256_V3,
)
from learning_agent_eval.runtime_metadata import git_worktree_clean
from learning_agent_eval.semantic_adjudication import verify_reviewed_csv

ROOT = Path(__file__).resolve().parents[2]
TRACKS = ("planning", "intervention", "assessment", "revision")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, document):
    Path(path).write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def prepare(output: Path, runtime: Path, rules: Path):
    if (output / "design.json").exists():
        raise ValueError("design already exists")
    cases = [read(path) for path in sorted((ROOT / "evaluation/datasets/decisionbench-v1-candidate/calibration/cases").glob("*.json"))]
    labels = []
    for case in cases:
        episode_id = episode_id_for_case(case)
        episode = read(runtime / "episodes" / f"{episode_id}.json")
        rule = read(rules / "rules" / f"{episode_id}.json")
        labels.append({
            "episode_id": episode_id, "case_id": case["case_id"], "track": case["track"],
            "label": case["private_annotations"]["quality_label"],
            "group": case["case_id"].rsplit("-", 1)[0],
            "episode_sha256": episode["provenance"]["episode_sha256"], "rule_sha256": rule["result_sha256"],
        })
    assert len(labels) == 24 and len({item["group"] for item in labels}) == 8
    repeat_ids = [item["episode_id"] for item in labels if item["label"] in {"good", "mild"}]
    assert len(repeat_ids) == 16
    document = {
        "experiment_version": "e5-calibration-v2", "source_commit": current_git_commit(),
        "runtime_path": os.path.relpath(runtime, output),
        "config": JUDGE_CONFIG_DOCUMENT_V3, "prompt_sha256": JUDGE_PROMPT_SHA256_V3,
        "runtime_manifest_sha256": read(runtime / "run-manifest.json")["manifest_sha256"],
        "rule_manifest_sha256": read(rules / "rule-manifest.json")["manifest_sha256"],
        "cases": labels, "repeat_ids": repeat_ids, "repeats": 5,
        "planned_evaluations": 88, "concurrent_tracks": 2,
        "metric_policy": "docs/E5验收工作记录.md:实验预设", "formal": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    document["design_sha256"] = sha256_digest(document)
    write(output / "design.json", document)


def execute(output: Path, runtime: Path, rules: Path, ledger: Path, phase: str):
    design = read(output / "design.json")
    if design["design_sha256"] != sha256_digest({k: v for k, v in design.items() if k != "design_sha256"}):
        raise ValueError("experiment_design_digest_invalid")
    if not git_worktree_clean() or current_git_commit() != design["source_commit"]:
        raise ValueError("paid experiment requires its clean, fixed source commit")
    for item in design["cases"]:
        if (read(runtime / "episodes" / f"{item['episode_id']}.json")["provenance"]["episode_sha256"] != item["episode_sha256"]
                or read(rules / "rules" / f"{item['episode_id']}.json")["result_sha256"] != item["rule_sha256"]):
            raise ValueError("fixed input changed")
    rounds = [1] if phase == "discrimination" else [2, 3, 4, 5]

    def run(track):
        for repeat in rounds:
            name = f"r{repeat}-{track}"
            judge_dir, aggregate_dir = output / f"judge-{name}", output / f"aggregate-{name}"
            selected = {item["episode_id"] for item in design["cases"] if item["track"] == track and (repeat == 1 or item["episode_id"] in design["repeat_ids"])}
            try:
                summary = evaluate_active_judges(
                    episodes=runtime, rules=rules, output=judge_dir, judge_mode="real",
                    allow_real_judge=True, budget_ledger=ledger, episode_ids=selected,
                )
                aggregate_active_results(episodes=runtime, rules=rules, judges=judge_dir, output=aggregate_dir)
                write(output / f"execution-{name}.json", {"status": "finished", "episode_ids": list(summary.episode_ids), "judge_errors": list(summary.judge_error_episode_ids)})
                print(f"{name}: completed={len(summary.episode_ids)} errors={len(summary.judge_error_episode_ids)}", flush=True)
            except Exception as exc:
                write(output / f"execution-{name}.json", {"status": "execution_failure", "error_type": type(exc).__name__, "error_code": getattr(exc, "code", "experiment_driver_error")})
                raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, TRACKS))


def summarize(output: Path):
    design = read(output / "design.json")
    if design["design_sha256"] != sha256_digest({k: v for k, v in design.items() if k != "design_sha256"}):
        raise ValueError("experiment_design_digest_invalid")
    details = []
    results = {}
    reviewed_cache = {}
    for csvpath in sorted(output.glob("*/reviewed-results.csv")):
        folder = csvpath.parent
        if not folder.name.startswith(("aggregate-r", "adjudicated-r")):
            continue
        suffix = folder.name.split("-", 1)[1]
        judge_root = output / f"judge-{suffix}" / "judge-results"
        aggregates = [read(p) for p in sorted((folder / "episodes").glob("*.json"))]
        judges = {a["episode_id"]: read(judge_root / f"{a['episode_id']}.json") for a in aggregates}
        runtime_root = output / design["runtime_path"]
        episodes = {a["episode_id"]: read(runtime_root / "episodes" / f"{a['episode_id']}.json") for a in aggregates}
        rows = verify_reviewed_csv(csvpath, aggregates, judges, episodes)
        reviewed_cache[folder.name] = {row["episode_id"]: row for row in rows}
    for item in design["cases"]:
        for repeat in range(1, 6 if item["episode_id"] in design["repeat_ids"] else 2):
            path = output / f"judge-r{repeat}-{item['track']}" / "judge-results" / f"{item['episode_id']}.json"
            judge = read(path) if path.exists() else None
            attempted = bool(judge and judge.get("provider_attestation", {}).get("calls"))
            valid = judge is not None and judge["status"] == "complete"
            levels = [d["level"] for d in judge["dimensions"]] if valid else None
            score = sum(DIMENSION_WEIGHTS[d["dimension_id"]] * d["level"] / 2 for d in judge["dimensions"]) if valid else None
            aggpath = output / f"aggregate-r{repeat}-{item['track']}" / "episodes" / f"{item['episode_id']}.json"
            agg = read(aggpath) if aggpath.exists() else None
            suggested = bool(judge and (judge["suggested_hard_gates"] or any(i["severity"] == "critical" for i in judge["semantic_issues"])))
            conclusion = None if not valid else "review_required" if suggested else "pass" if score >= 70 else "fail"
            if agg and agg["actual_hard_gates"]:
                conclusion = "fail"
            reviewed = None
            for prefix in ["adjudicated", "aggregate"]:
                key = f"{prefix}-r{repeat}-{item['track']}"
                if key in reviewed_cache:
                    reviewed = reviewed_cache[key].get(item["episode_id"])
                    break
            row = {**item, "repeat": repeat, "provider_attempted": attempted, "status": judge["status"] if judge else "not_run", "levels": levels,
                   "raw_score": score, "rule_capped_score": agg["final_score"] if agg else None,
                   "rule_only_outcome": agg["episode_outcome"] if agg else None,
                   "conclusion_before_review": conclusion,
                   "conclusion": reviewed["reviewed_outcome"] if reviewed and valid else conclusion,
                   "reviewed_score": float(reviewed["reviewed_score"]) if reviewed and reviewed["reviewed_score"] not in (None, "") else None,
                   "gate_suggested": bool(judge["suggested_hard_gates"]) if valid else None, "critical_signal": suggested if valid else None,
                   "judge_result_sha256": judge["result_sha256"] if judge else None}
            details.append(row)
            results[(item["episode_id"], repeat)] = row
    groups = {}
    for row in details:
        if row["repeat"] == 1:
            groups.setdefault(row["group"], {})[row["label"]] = row
    discrimination = {}
    for score_field in ["raw_score", "rule_capped_score", "reviewed_score"]:
        comparisons = []
        for group, rows in groups.items():
            g, m, s = (rows[k][score_field] for k in ["good", "mild", "severe"])
            comparisons.append({"group": group, "good": g, "mild": m, "severe": s,
                                "good_gt_mild": g is not None and m is not None and g > m,
                                "good_gt_severe": g is not None and s is not None and g > s,
                                "mild_gt_severe": m is not None and s is not None and m > s,
                                "strict_order": all(x is not None for x in [g, m, s]) and g > m > s})
        discrimination[score_field] = {"groups": comparisons, "denominator": 8,
            **{key: {"numerator": sum(c[key] for c in comparisons), "rate": sum(c[key] for c in comparisons) / 8} for key in ["strict_order", "good_gt_mild", "good_gt_severe", "mild_gt_severe"]}}
    repetition = []
    for episode_id in design["repeat_ids"]:
        rows = [results[(episode_id, i)] for i in range(1, 6)]
        valid = [r for r in rows if r["status"] == "complete"]
        def agreement(values):
            return max(Counter(values).values(), default=0) / 5
        scores = [r["raw_score"] for r in valid]
        repetition.append({"episode_id": episode_id, "valid": len(valid), "denominator": 5,
                           "conclusion_agreement": agreement(r["conclusion"] for r in valid),
                           "dimension_agreement": [agreement(r["levels"][i] for r in valid) for i in range(7)],
                           "gate_agreement": agreement(r["gate_suggested"] for r in valid),
                           "score_std_population": statistics.pstdev(scores) if len(scores) == 5 else None,
                           "score_range": max(scores)-min(scores) if len(scores) == 5 else None})
    stdevs = [r["score_std_population"] for r in repetition if r["valid"] == 5]
    report = {
        "design_sha256": design["design_sha256"], "discrimination": discrimination, "repetition": repetition,
        "not_attempted_evaluations": sum(not r["provider_attempted"] for r in details),
        "planned_evaluations": 88, "complete_evaluations": sum(r["status"] == "complete" for r in details),
        "pending_semantic_reviews": sum(r["conclusion"] == "review_required" for r in details),
        "terminal_judge_errors": sum(r["status"] == "judge_error" for r in details), "not_run": sum(r["status"] == "not_run" for r in details),
        "repeat_cases_denominator": 16, "complete_repeat_cases": len(stdevs),
        "mean_conclusion_agreement": sum(r["conclusion_agreement"] for r in repetition) / 16,
        "mean_dimension_agreement": sum(sum(r["dimension_agreement"]) for r in repetition) / (16*7),
        "mean_gate_agreement": sum(r["gate_agreement"] for r in repetition) / 16,
        "mean_score_std_population": statistics.mean(stdevs) if stdevs else None,
        "critical_and_defect_recall": "requires_evidence_bound_content_review",
        "experiment_complete": all(r["provider_attempted"] and r["status"] != "not_run" for r in details),
    }
    write(output / "metrics.json", report)
    write(output / "per-evaluation.json", details)
    print(json.dumps({k:v for k,v in report.items() if k not in {"discrimination", "repetition"}}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "discrimination", "repeat", "summarize"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--budget-ledger", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.output, args.runtime, args.rules)
    elif args.action == "summarize":
        summarize(args.output)
    else:
        execute(args.output, args.runtime, args.rules, args.budget_ledger, args.action)


if __name__ == "__main__":
    main()
