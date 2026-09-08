"""Offline, fixed-denominator reporting of a frozen E6 run; never calls a model."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from statistics import mean

from learning_agent_eval.integrity import case_spec_digest
from learning_agent_eval.semantic_adjudication import verify_reviewed_csv
from learning_agent_eval.validator import validate_dataset

TRACKS = ("planning", "intervention", "assessment", "revision")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_rows(rows, expected_track_counts):
    """Nulls stay unscored; zero is a score; every frozen case stays in rates."""
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate_case")
    if Counter(row["track"] for row in rows) != Counter(expected_track_counts):
        raise ValueError("fixed_denominator_mismatch")
    result = {}
    for track, denominator in expected_track_counts.items():
        selected = [row for row in rows if row["track"] == track]
        complete = [row for row in selected if row["status"] == "complete"]
        if any(row["reviewed_score"] is None for row in complete):
            raise ValueError("complete_score_missing")
        if any(row["reviewed_score"] is not None for row in selected if row["status"] != "complete"):
            raise ValueError("unscored_status_has_score")
        passes = sum(row["reviewed_outcome"] == "pass" for row in complete)
        result[track] = {
            "expected_cases": denominator,
            "status_counts": dict(sorted(Counter(row["status"] for row in selected).items())),
            "outcome_counts": dict(sorted(Counter(row["reviewed_outcome"] for row in selected).items())),
            "score_count": len(complete),
            "pass_count": passes,
            "pass_rate_all_cases": passes / denominator,
            "pass_rate_scored_cases": passes / len(complete) if complete else None,
            "raw_score_mean": mean(row["raw_score"] for row in complete) if complete else None,
            "rule_capped_score_mean": mean(row["rule_capped_score"] for row in complete) if complete else None,
            "reviewed_score_mean": mean(row["reviewed_score"] for row in complete) if complete else None,
            "dimension_level_means": {
                key: mean(row["dimensions"][key] for row in complete)
                for key in sorted(complete[0]["dimensions"])
            } if complete else {},
            "confirmed_critical_cases": sum(bool(row["confirmed_critical"]) for row in complete),
            "pending_review_cases": sum(bool(row["pending_candidates"]) for row in selected),
        }
    return result


def build_report(dataset, run):
    for directory in (dataset, *(run / name for name in ("runtime", "rules", "judge", "adjudicated"))):
        if not validate_dataset(directory).ok:
            raise ValueError(f"invalid_artifact_directory:{directory.name}")
    suite = read(dataset / "manifest.json")
    cases = [read(dataset / relative) for relative in suite["case_files"]]
    runtime = read(run / "runtime/run-manifest.json")
    aggregate = read(run / "adjudicated/run-manifest.json")
    if (runtime["case_suite_sha256"] != suite["case_suite_sha256"]
            or aggregate["input_runtime_manifest_sha256"] != runtime["manifest_sha256"]):
        raise ValueError("run_binding_mismatch")
    episodes = {doc["episode_id"]: doc for doc in map(read, sorted((run / "runtime/episodes").glob("*.json")))}
    judges = {doc["episode_id"]: doc for doc in map(read, sorted((run / "judge/judge-results").glob("*.json")))}
    aggregates = list(map(read, sorted((run / "adjudicated/episodes").glob("*.json"))))
    reviews = {
        row["episode_id"]: row for row in verify_reviewed_csv(
            run / "adjudicated/reviewed-results.csv", aggregates, judges, episodes
        )
    }
    scores = {doc["episode_id"]: doc for doc in aggregates}
    terminals = {doc["case_id"]: doc for doc in runtime["terminals"]}
    if set(terminals) - {case["case_id"] for case in cases}:
        raise ValueError("unknown_case")
    rows = []
    for case in cases:
        terminal = terminals.get(case["case_id"])
        row = {
            "case_id": case["case_id"], "scenario_family_id": case["scenario_family_id"],
            "track": case["track"], "difficulty": case["difficulty"],
            "terminal_kind": "not_attempted", "artifact_id": None,
            "status": "not_attempted", "reviewed_outcome": "not_attempted",
            "raw_score": None, "rule_capped_score": None, "reviewed_score": None,
            "dimensions": {}, "confirmed_critical": [], "pending_candidates": [],
            "actual_hard_gates": [], "action_class": None, "run_status": None,
        }
        if terminal:
            if terminal["case_spec_sha256"] != case_spec_digest(case) or terminal["track"] != case["track"]:
                raise ValueError("case_binding_mismatch")
            row.update(terminal_kind=terminal["terminal_kind"], artifact_id=terminal["artifact_id"])
            if terminal["terminal_kind"] == "failure":
                row.update(status="runtime_failure", reviewed_outcome="runtime_failure")
            else:
                identity = terminal["artifact_id"]
                score, review, episode = scores[identity], reviews[identity], episodes[identity]
                row.update({key: review[key] for key in (
                    "raw_score", "rule_capped_score", "reviewed_score", "reviewed_outcome",
                    "confirmed_critical", "pending_candidates",
                )})
                row.update(
                    status=score["status"],
                    dimensions={dim["dimension_id"]: dim["level"] for dim in score["dimensions"]},
                    actual_hard_gates=score["actual_hard_gates"],
                    action_class=episode["result"]["action_class"],
                    run_status=episode["result"]["layers"]["run_status"],
                )
        rows.append(row)
    tracks = summarize_rows(rows, suite["benchmark_expected_track_counts"])
    return {
        "report_version": "e6-fixed-denominator-report-v1",
        "runtime_source_commit": runtime["git_commit"],
        "runtime_manifest_sha256": runtime["manifest_sha256"],
        "aggregate_manifest_sha256": aggregate["manifest_sha256"],
        "protocol_release_sha256": runtime["evaluation_protocol_release_sha256"],
        "benchmark_release_sha256": runtime["benchmark_release_sha256"],
        "case_suite_sha256": suite["case_suite_sha256"],
        "expected_cases": suite["benchmark_expected_total_cases"],
        "formal_capability_result": aggregate["formal_capability_result"],
        "capability_blockers": aggregate["capability_blockers"],
        "semantic_review_identity": "primary_ai_self_review_not_independent_human",
        "mean_denominator": "complete scored episodes only; zero retained; failures remain in all-case rates",
        "tracks": tracks, "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.dataset, args.run)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.output / "cases.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report["cases"][0]))
        writer.writeheader()
        for row in report["cases"]:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in row.items()})
    print(json.dumps({"expected_cases": report["expected_cases"], "formal_capability_result": report["formal_capability_result"], "tracks": report["tracks"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
