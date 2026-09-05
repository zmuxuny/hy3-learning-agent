"""Verify an E4 authoring package without executing a model or endorsing labels."""

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.integrity import case_spec_digest
from learning_agent_eval.privacy import privacy_issues
from learning_agent_eval.validator import validate_dataset

ALLOWED_MUTATIONS = {
    "case_id",
    "case_spec_sha256",
    "runtime_setup.run_id",
    "runtime_setup.session_id",
    "runtime_setup.scripted_turns",
    "private_annotations.quality_label",
    "private_annotations.mutation_source",
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def verify(root):
    cases = {}
    groups = {}
    for name, expected_count, role, split, mode in (
        ("primary", 48, "primary_episode", "test", "real"),
        ("calibration", 24, "calibration_output", "dev", "stub"),
    ):
        suite = load(root / name / "manifest.json")
        rows = [load(root / name / file) for file in suite["case_files"]]
        require(len(rows) == expected_count, f"{name}: wrong Case count")
        require(
            Counter(case["track"] for case in rows)
            == dict.fromkeys(
                ("planning", "intervention", "assessment", "revision"),
                expected_count // 4,
            ),
            "track coverage",
        )
        require(
            all(
                (
                    case["dataset_role"],
                    case["split"],
                    case["runtime_setup"]["invocation_mode"],
                )
                == (role, split, mode)
                for case in rows
            ),
            "role/split/mode mismatch",
        )
        if name == "primary":
            require(
                all(not case["runtime_setup"]["scripted_turns"] for case in rows),
                "Primary contains a supplied response",
            )
        else:
            require(
                Counter(case["private_annotations"]["quality_label"] for case in rows)
                == {"good": 8, "mild": 8, "severe": 8},
                "triplet label counts",
            )
        resource = load(root / name / suite["resource_snapshot_file"])
        pages = {page["url"]: page for page in resource["pages"]}
        for case in rows:
            require(case["case_id"] not in cases, "duplicate Case identity")
            require(case_spec_digest(case) == case["case_spec_sha256"], "Case digest")
            require(
                case["private_annotations"]["reviewer_role"] == "pending_human_review",
                "candidate must not claim human review",
            )
            for artifact in case["runtime_setup"]["seed"].get(
                "submission_artifacts", []
            ):
                require(artifact["url"] in pages, "submission report unavailable")
                content = pages[artifact["url"]]["content"].encode("utf-8")
                require(
                    hashlib.sha256(content).hexdigest() == artifact["content_sha256"],
                    "report raw-byte digest",
                )
            cases[case["case_id"]] = case
        groups[name] = {case["scenario_family_id"] for case in rows}
    require(groups["primary"].isdisjoint(groups["calibration"]), "family split leakage")

    mutation = load(root / "mutation-manifest.json")
    require(
        mutation["manifest_sha256"]
        == sha256_digest(
            {key: value for key, value in mutation.items() if key != "manifest_sha256"}
        ),
        "mutation manifest digest",
    )
    require(
        len(mutation["source_cases"]) == 8 and len(mutation["mutations"]) == 24,
        "mutation inventory",
    )
    sources = {}
    for item in mutation["source_cases"]:
        path = (root / item["source_case_file"]).resolve()
        require(path.is_relative_to(root.resolve()), "source path escapes package")
        source = load(path)
        digest = case_spec_digest(source)
        require(
            digest == source["case_spec_sha256"] == item["source_case_sha256"],
            "source digest",
        )
        require(not privacy_issues(source), "source privacy")
        sources[digest] = source
    reviewed = set()
    for item in mutation["mutations"]:
        case = cases[item["case_id"]]
        require(item["case_id"] not in reviewed, "duplicate mutation")
        reviewed.add(item["case_id"])
        source = sources[item["source_case_sha256"]]
        require(
            case["private_annotations"]["mutation_source"]
            == item["source_case_sha256"],
            "source lineage mismatch",
        )
        require(
            case["case_spec_sha256"] == item["candidate_case_sha256"],
            "mutation candidate digest",
        )
        require(
            set(item["allowed_changed_paths"]) == ALLOWED_MUTATIONS,
            "undeclared mutable surface",
        )
        reconstructed = deepcopy(source)
        seen = set()
        for change in item["actual_changes"]:
            path = change["path"]
            require(
                path in ALLOWED_MUTATIONS and path not in seen,
                "mutation path not allowed or duplicated",
            )
            seen.add(path)
            parent = reconstructed
            keys = path.split(".")
            for key in keys[:-1]:
                parent = parent[key]
            require(parent[keys[-1]] == change["before"], "mutation before value")
            parent[keys[-1]] = change["after"]
        require(reconstructed == case, "mutation input drift outside declared changes")
        require(
            item["review_status"] == "pending_independent_human_review",
            "fabricated review",
        )
    require(
        reviewed
        == {
            key
            for key, case in cases.items()
            if case["dataset_role"] == "calibration_output"
        },
        "mutation coverage",
    )

    lineage = load(root / "split-lineage.json")
    require(
        lineage["inventory_sha256"] == sha256_digest(lineage["cases"]), "lineage digest"
    )
    require(
        len(lineage["cases"]) == 72
        and {item["case_id"] for item in lineage["cases"]} == set(cases),
        "lineage inventory",
    )
    for item in lineage["cases"]:
        case = cases[item["case_id"]]
        require(
            (item["case_sha256"], item["family"], item["track"], item["split"])
            == (
                case["case_spec_sha256"],
                case["scenario_family_id"],
                case["track"],
                case["split"],
            ),
            "lineage binding",
        )
    for name in ("primary", "calibration"):
        report = validate_dataset(root / name)
        require(report.ok, f"{name} contract/privacy/release validation")
    return {
        "primary_inputs": 48,
        "calibration_inputs": 24,
        "source_cases": 8,
        "mutation_reconstruction": "passed",
        "human_review": "pending",
        "formal": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.root), sort_keys=True))


if __name__ == "__main__":
    main()
