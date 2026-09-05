"""Prepare four real-mode protocol pilot Cases without contacting a provider."""

import argparse
import json
from pathlib import Path

from learning_agent_eval.case_authoring import write_candidate_suite
from learning_agent_eval.integrity import case_spec_digest

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "evaluation/datasets/decisionbench-v4-engineering"


def pilot_cases():
    cases = []
    for track in ("p", "i", "a", "r"):
        case = json.loads((DATA / f"cases/case-e31-{track}-positive.json").read_text())
        source = f"{case['case_id']}@{case['case_spec_sha256']}"
        case.update(
            case_id=f"protocol-pilot-{track}",
            scenario_family_id=f"protocol-pilot-{track}",
            dataset_role="protocol_pilot",
            split="dev",
            tags=["protocol-pilot", "synthetic-environment"],
        )
        case["runtime_setup"].update(invocation_mode="real", scripted_turns=[])
        case["private_annotations"].update(
            author_role="assistant_case_author",
            reviewer_role="pending_human_review",
            mutation_source=source,
            adjudication_note="Protocol trial draft derived from engineering input; no independent content review or capability claim.",
        )
        case["case_spec_sha256"] = case_spec_digest(case)
        cases.append(case)
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_candidate_suite(
        args.output,
        cases=pilot_cases(),
        resource_snapshot=DATA / "resources/snapshot.json",
        dataset_version="protocol-pilot-v1",
    )
    print("prepared=4 provider_calls=0 role=protocol_pilot review=pending formal=false")


if __name__ == "__main__":
    main()
