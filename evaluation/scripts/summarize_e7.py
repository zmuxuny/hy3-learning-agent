"""Recompute E7 fixed-denominator comparisons from original evidence and reviews."""

from __future__ import annotations
import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from learning_agent_eval.active_aggregate import _aggregate_document
from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.integrity import judge_result_digest
from learning_agent_eval.semantic_adjudication import reviewed_results
from evaluate_e7_trace import verify_bundle

TRACKS = ("planning", "intervention", "assessment", "revision")
DIMENSIONS = tuple(f"D{i}" for i in range(1, 8))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_pairs(rows):
    """Every expected case remains visible; paired means use only two valid scores."""
    pairs = []
    keys = sorted({(r["case_id"], r["track"]) for r in rows})
    for case, track in keys:
        arms = {r["arm"]: r for r in rows if r["case_id"] == case}
        if len(arms) != 2 or sum(r["case_id"] == case for r in rows) != 2:
            raise ValueError("exactly two arm slots required per case")
        a, b = arms["baseline"], arms["candidate"]
        valid = a["score"] is not None and b["score"] is not None
        delta = b["score"] - a["score"] if valid else None
        pairs.append(
            dict(
                case_id=case,
                track=track,
                baseline_status=a["status"],
                candidate_status=b["status"],
                baseline_score=a["score"],
                candidate_score=b["score"],
                baseline_outcome=a["outcome"],
                candidate_outcome=b["outcome"],
                valid_pair=valid,
                score_delta=delta,
                change="unscored_pair"
                if not valid
                else "improved"
                if delta > 0
                else "regressed"
                if delta < 0
                else "tied",
                outcome_transition=f"{a['outcome']} -> {b['outcome']}",
                dimension_deltas={
                    d: b["dimensions"][d] - a["dimensions"][d] for d in DIMENSIONS
                }
                if valid
                else {},
            )
        )
    tracks = {}
    for track in TRACKS:
        p = [x for x in pairs if x["track"] == track]
        valid = [x for x in p if x["valid_pair"]]
        arms = {}
        for arm in ("baseline", "candidate"):
            r = [x for x in rows if x["track"] == track and x["arm"] == arm]
            scored = [x for x in r if x["score"] is not None]
            arms[arm] = dict(
                slots=len(r),
                scored=len(scored),
                outcomes=dict(Counter(x["outcome"] for x in r)),
                score_mean=mean(x["score"] for x in scored) if scored else None,
                pass_rate_all=sum(x["outcome"] == "pass" for x in r) / len(r)
                if r
                else None,
            )
        tracks[track] = dict(
            expected_pairs=len(p),
            valid_pairs=len(valid),
            changes=dict(Counter(x["change"] for x in p)),
            paired_score_delta_mean=mean(x["score_delta"] for x in valid)
            if valid
            else None,
            dimension_delta_means={
                d: mean(x["dimension_deltas"][d] for x in valid) for d in DIMENSIONS
            }
            if valid
            else {},
            arms=arms,
        )
    return dict(
        expected_pairs=len(pairs),
        valid_pairs=sum(p["valid_pair"] for p in pairs),
        changes=dict(Counter(p["change"] for p in pairs)),
        tracks=tracks,
        pairs=pairs,
    )


def compute(root, index):
    reviews = read(root / "semantic-reviews.json")
    rows = []
    for slot in index["batches"]:
        run, judge_dir = root / slot["run"], root / slot["judge"]
        runtime, rules = (
            read(run / "runtime/run-manifest.json"),
            read(run / "rules/rule-manifest.json"),
        )
        jm = read(judge_dir / "comparison-manifest.json")
        if jm["source_runtime_manifest_sha256"] != runtime["manifest_sha256"]:
            raise ValueError("comparison_runtime_binding_invalid")
        selected = [t for t in runtime["terminals"] if t["case_id"] in slot["case_ids"]]
        if len(selected) != len(slot["case_ids"]) or {
            r["case_id"] for r in jm["rows"]
        } != set(slot["case_ids"]):
            raise ValueError("fixed_comparison_inventory_invalid")
        for terminal in selected:
            case, identity = terminal["case_id"], terminal["artifact_id"]
            row = dict(
                kind=slot["kind"],
                arm=slot["arm"],
                repeat=slot["repeat"],
                case_id=case,
                track=terminal["track"],
                run=slot["run"],
                judge=slot["judge"],
                episode_id=identity,
                status="runtime_failure",
                score=None,
                raw_score=None,
                outcome="runtime_failure",
                dimensions={},
                rule_failures=[],
                confirmed_critical=[],
                pending=[],
                source_protocol_eligible=runtime["protocol_eligible"],
                source_provider_eligible=runtime["provider_eligible"],
                source_trusted=runtime["trusted_benchmark_run"],
            )
            if terminal["terminal_kind"] == "episode":
                episode = read(run / "runtime/episodes" / f"{identity}.json")
                rule = read(run / "rules/rules" / f"{identity}.json")
                reference = read(run / "runtime/judge-references" / f"{identity}.json")
                verify_bundle(terminal, episode, rule, reference, runtime, rules)
                judge = read(judge_dir / f"{identity}.json")
                if (
                    judge_result_digest(judge) != judge["result_sha256"]
                    or judge["episode_sha256"]
                    != episode["provenance"]["episode_sha256"]
                    or judge["rule_result_sha256"] != rule["result_sha256"]
                    or judge["judge_reference_sha256"] != reference["reference_sha256"]
                ):
                    raise ValueError("judge_binding_invalid")
                aggregate = _aggregate_document(
                    episode=episode, rule_result=rule, judge_result=judge
                )
                selected_reviews = [
                    r
                    for r in reviews.get(slot["judge"], [])
                    if r["episode_id"] == identity
                ]
                review = reviewed_results(
                    [aggregate],
                    {identity: judge},
                    {identity: episode},
                    selected_reviews,
                )[0]
                row.update(
                    status=aggregate["status"],
                    score=review["reviewed_score"],
                    raw_score=review["raw_score"],
                    outcome=review["reviewed_outcome"],
                    dimensions={
                        d["dimension_id"]: d["level"] for d in judge["dimensions"]
                    },
                    rule_failures=[x["check_id"] for x in aggregate["rule_failures"]],
                    confirmed_critical=review["confirmed_critical"],
                    pending=review["pending_candidates"],
                    episode_sha256=episode["provenance"]["episode_sha256"],
                    judge_result_sha256=judge["result_sha256"],
                )
            rows.append(row)
    product = summarize_pairs([r for r in rows if r["kind"] == "product"])
    stability = []
    method = [r for r in rows if r["kind"] == "method"]
    for case in sorted({r["case_id"] for r in method}):
        for arm in ("baseline", "candidate"):
            repeated = sorted(
                [r for r in method if r["case_id"] == case and r["arm"] == arm],
                key=lambda r: r["repeat"],
            )
            valid = [r for r in repeated if r["score"] is not None]
            stability.append(
                dict(
                    case_id=case,
                    arm=arm,
                    label=case.rsplit("-", 1)[-1],
                    slots=len(repeated),
                    valid=len(valid),
                    scores=[r["score"] for r in repeated],
                    outcomes=[r["outcome"] for r in repeated],
                    dimensions=[r["dimensions"] for r in repeated],
                    score_population_sd=pstdev(r["score"] for r in valid)
                    if len(valid) == len(repeated)
                    else None,
                    outcome_agreement=len({r["outcome"] for r in valid}) == 1
                    if len(valid) == len(repeated)
                    else None,
                )
            )
    return dict(
        version="e7-evidence-comparison-report-v1",
        formal_capability_result=False,
        formal_reason="cross-protocol common-Judge comparison; original per-batch source eligibility retained",
        index_sha256=sha256_digest(index),
        reviews_sha256=sha256_digest(reviews),
        product=product,
        method_stability=stability,
        rows=rows,
    )


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for row in rows:
            w.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (list, dict))
                    else v
                    for k, v in row.items()
                }
            )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    report = compute(a.evidence, read(a.evidence / "experiment-index.json"))
    a.output.mkdir(parents=True, exist_ok=False)
    (a.output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    write_csv(a.output / "slots.csv", report["rows"])
    write_csv(a.output / "pairs.csv", report["product"]["pairs"])
    write_csv(a.output / "method-stability.csv", report["method_stability"])
    print(
        json.dumps(
            {
                k: report["product"][k]
                for k in ["expected_pairs", "valid_pairs", "changes"]
            }
        )
    )
