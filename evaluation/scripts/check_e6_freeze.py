"""Offline E6 content, lineage and production registration audit; never pays."""

import argparse
import json
from collections import Counter
from pathlib import Path

from build_e6_test import ROOT, TRACKS, reviewed_inputs
from learning_agent_eval.release_governance import (
    assess_benchmark_release,
    load_protocol_release,
    protocol_release_reason_codes,
)
from learning_agent_eval.validator import validate_dataset

DATASET = ROOT / "evaluation/datasets/decisionbench-v1-e6-test"
REVIEW = ROOT / "evaluation/case-design/e6-content-review.json"


def check(dataset=DATASET, *, require_trusted=True):
    expected, resource = reviewed_inputs(REVIEW)
    suite = json.loads((dataset / "manifest.json").read_text())
    release = json.loads((dataset / "benchmark-release.json").read_text())
    observed = [
        json.loads((dataset / relative).read_text()) for relative in suite["case_files"]
    ]
    if observed != sorted(expected, key=lambda c: c["case_id"]):
        raise ValueError("reviewed_case_content_mismatch")
    if json.loads((dataset / suite["resource_snapshot_file"]).read_text()) != resource:
        raise ValueError("reviewed_resource_mismatch")
    if not validate_dataset(dataset).ok:
        raise ValueError("dataset_invalid")
    families = {c["scenario_family_id"] for c in observed}
    if (
        len(observed) != 48
        or len(families) != 12
        or Counter(c["track"] for c in observed) != Counter({t: 12 for t in TRACKS})
    ):
        raise ValueError("fixed_denominator_mismatch")
    for family in families:
        cases = [c for c in observed if c["scenario_family_id"] == family]
        if {c["track"] for c in cases} != set(TRACKS) or any(
            c["split"] != "test"
            or c["dataset_role"] != "primary_episode"
            or c["runtime_setup"]["scripted_turns"]
            or c["private_annotations"]["mutation_source"]
            or c["private_annotations"]["quality_label"]
            for c in cases
        ):
            raise ValueError("test_lineage_invalid")
    historical_families, historical_inputs = set(), set()
    for directory in (
        "decisionbench-v1-candidate",
        "decisionbench-v4-engineering",
        "decisionbench-v3-engineering",
    ):
        for path in (ROOT / "evaluation/datasets" / directory).rglob("case-*.json"):
            case = json.loads(path.read_text())
            if "runtime_setup" in case:
                historical_families.add(case["scenario_family_id"])
                historical_inputs.add(case["runtime_setup"]["trigger"]["objective"])
    if families & historical_families or any(
        c["runtime_setup"]["trigger"]["objective"] in historical_inputs
        for c in observed
    ):
        raise ValueError("historical_family_or_input_overlap")
    pages = {p["url"]: p for p in resource["pages"]}
    import hashlib

    for case in observed:
        for artifact in case["runtime_setup"]["seed"].get("submission_artifacts", []):
            if (
                artifact["content_sha256"]
                != hashlib.sha256(
                    pages[artifact["url"]]["content"].encode()
                ).hexdigest()
            ):
                raise ValueError("evidence_resource_mismatch")
    protocol = load_protocol_release()
    if protocol_release_reason_codes(protocol):
        raise ValueError("protocol_source_drift")
    trust = assess_benchmark_release(
        dataset_root=dataset, suite=suite, release=release, filtered=False
    )
    if (
        not trust.protocol_match
        or not trust.suite_complete
        or (require_trusted and not trust.trusted_benchmark_run)
    ):
        raise ValueError(f"release_binding_invalid:{trust.reason_codes}")
    if require_trusted:
        plan = json.loads((ROOT / "evaluation/case-design/e6-run-plan.json").read_text())
        if (
            plan["benchmark_release_sha256"] != release["manifest_sha256"]
            or plan["case_suite_sha256"] != release["case_suite_sha256"]
            or plan["protocol_release_sha256"] != protocol["release_sha256"]
            or plan["execution"]["ordered_case_ids"]
            != [c["case_id"] for c in release["cases"]]
            or plan["execution"]["expected_cases"] != 48
            or plan["execution"]["expected_track_counts"] != {t: 12 for t in TRACKS}
        ):
            raise ValueError("run_plan_binding_mismatch")
        from learning_agent_eval.release_governance import load_production_registry

        if plan["registry_sha256"] != load_production_registry()["registry_sha256"]:
            raise ValueError("run_plan_registry_mismatch")
        decision = ROOT / plan["method_decision_file"]
        if (
            hashlib.sha256(decision.read_bytes()).hexdigest()
            != plan["method_decision_raw_sha256"]
        ):
            raise ValueError("run_plan_method_decision_mismatch")
    return {
        "cases": 48,
        "families": 12,
        "track_counts": {t: 12 for t in TRACKS},
        "content_review": "primary_ai_self_review",
        "independent_human_review": False,
        "historical_family_overlap": 0,
        "exact_historical_objective_overlap": 0,
        "mechanism_independence_claim": False,
        "test_outputs_used_for_tuning": False,
        "release_registered": trust.release_registered,
        "suite_sha256": release["case_suite_sha256"],
        "protocol_sha256": protocol["release_sha256"],
        "formal_capability_result": False,
        "paid_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--allow-unregistered", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            check(args.dataset, require_trusted=not args.allow_unregistered),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
