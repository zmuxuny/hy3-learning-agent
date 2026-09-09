"""Run four controlled tracks through Runtime, Rules, Judge and Aggregate offline.

This is an engineering smoke check. The fixed Judge response is not a quality
label, a real Hy3 call, or part of E7's product or method experiment.
"""

from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
from learning_agent_eval.active_runtime import run_active_runtime
from learning_agent_eval.active_rules import evaluate_active_rules
from learning_agent_eval.active_judge import (
    FixedResponseJudgeProviderV3,
    evaluate_active_judges,
)
from learning_agent_eval.active_aggregate import aggregate_active_results
from learning_agent_eval.case_specs import episode_id_for_case
from learning_agent_eval.validator import validate_dataset
from learning_agent_eval.e3_io import current_git_commit


def run(output):
    root = Path(__file__).resolve().parents[2]
    dataset = root / "evaluation/datasets/decisionbench-v1.16-regression/calibration"
    cases = {}
    for path in sorted((dataset / "cases").glob("*.json")):
        case = json.loads(path.read_text())
        if case["case_id"].endswith("-good"):
            cases.setdefault(case["track"], case)
    assert len(cases) == 4
    output.mkdir(parents=True, exist_ok=False)
    runtime, rules, judges, aggregates = (
        output / x for x in ("runtime", "rules", "judges", "aggregates")
    )
    run_active_runtime(
        dataset=dataset,
        manifest=dataset / "manifest.json",
        output=runtime,
        episode_ids={episode_id_for_case(c) for c in cases.values()},
        model_mode="stub",
    )
    evaluate_active_rules(input_path=runtime, output=rules)
    payload = json.loads(
        (root / "evaluation/fixtures/e311-fixed-judge-responses-v3.json").read_text()
    )["responses"][0]
    payload["audit_checks"] = [
        dict(
            claim="Controlled response captured",
            verification="Read public fixture output",
            consistent=True,
            evidence_paths=["result.user_visible_output"],
        )
    ]
    evaluate_active_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        provider=FixedResponseJudgeProviderV3(
            [copy.deepcopy(payload) for _ in cases], frozen_time="2026-09-09T00:00:00Z"
        ),
    )
    aggregate_active_results(
        episodes=runtime, rules=rules, judges=judges, output=aggregates
    )
    checks = {}
    for directory in (runtime, rules, judges, aggregates):
        validation = validate_dataset(directory)
        assert validation.ok, [issue.render() for issue in validation.issues]
        checks[directory.name] = True
    terminal = json.loads((runtime / "run-manifest.json").read_text())
    assert len(terminal["terminals"]) == 4 and all(
        x["terminal_kind"] == "episode" for x in terminal["terminals"]
    )
    results = [
        json.loads(p.read_text()) for p in (judges / "judge-results").glob("*.json")
    ]
    assert len(results) == 4 and all(x["status"] == "complete" for x in results)
    receipt = dict(
        version="e7-offline-mini-receipt-v1",
        source_commit=current_git_commit(),
        cases={t: c["case_id"] for t, c in cases.items()},
        schema_validation=checks,
        runtime_episodes=4,
        valid_fixed_judges=4,
        real_api_calls=0,
        formal_capability_result=False,
    )
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
