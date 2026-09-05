"""Verify an E4 authoring package without executing a model or endorsing labels."""

import argparse
import csv
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


def review_status(case):
    role = case["private_annotations"]["reviewer_role"]
    require(
        role in {"pending_human_review", "delegated_ai_reviewer"},
        "unsupported reviewer identity",
    )
    return "ai_reviewed" if role == "delegated_ai_reviewer" else "pending_review"


def input_digest(case):
    setup = deepcopy(case["runtime_setup"])
    for key in ("run_id", "session_id", "scripted_turns"):
        del setup[key]
    return sha256_digest(
        {
            "runtime_setup": setup,
            "identity_bindings": case["identity_bindings"],
            "judge_criteria": case["judge_criteria"],
            "track": case["track"],
            "family": case["scenario_family_id"],
            "split": case["split"],
        }
    )


def verify_review(root, cases, sources, resources, *, require_review):
    worksheet = list(
        csv.DictReader((root / "review-worksheet.csv").open(encoding="utf-8"))
    )
    require(
        len(worksheet) == 72 and {r["case_id"] for r in worksheet} == set(cases),
        "worksheet coverage",
    )
    for row in worksheet:
        case = cases[row["case_id"]]
        require(
            (
                row["case_sha256"],
                row["track"],
                row["review_status"],
                row["reviewer_role"],
                row["review_record"],
            )
            == (
                case["case_spec_sha256"],
                case["track"],
                review_status(case),
                case["private_annotations"]["reviewer_role"],
                f"case:{case['case_id']}"
                if review_status(case) == "ai_reviewed"
                else "",
            ),
            "worksheet content binding",
        )
    reviewed = {
        key for key, case in cases.items() if review_status(case) == "ai_reviewed"
    }
    path = root / "content-review.json"
    if not reviewed:
        require(
            not require_review and not path.exists(),
            "AI content review required or inconsistent",
        )
        return "pending"
    require(reviewed == set(cases) and path.exists(), "missing or incomplete AI review")
    review = load(path)
    require(
        review["review_sha256"]
        == sha256_digest({k: v for k, v in review.items() if k != "review_sha256"}),
        "review manifest digest",
    )
    require(review["schema_version"] == "e4-content-review-v1", "review format")
    require(
        review["reviewer_role"] == "delegated_ai_reviewer"
        and review["independent_human_review"] is False
        and review["author_reviewer_context_shared"] is True,
        "review independence claim",
    )
    require(
        review["formal"] is False and review["method_validation_performed"] is False,
        "content review cannot endorse method or formal result",
    )
    require(not privacy_issues(review), "review privacy")
    expected = {f"case:{key}": value for key, value in cases.items()}
    expected.update(
        {f"source:{case['scenario_family_id']}": case for case in sources.values()}
    )
    records = review["records"]
    require(
        len(records) == 80 and {r["review_id"] for r in records} == set(expected),
        "review coverage",
    )
    for row in records:
        case = expected[row["review_id"]]
        require(
            (row["case_id"], row["case_sha256"], row["track"], row["quality_label"])
            == (
                case["case_id"],
                case["case_spec_sha256"],
                case["track"],
                case["private_annotations"]["quality_label"],
            ),
            "review content digest or identity",
        )
        resource_name = "primary" if case["split"] == "test" else "calibration"
        require(
            row["resource_snapshot_sha256"] == resources[resource_name],
            "review resource binding",
        )
        require(review_status(case) == "ai_reviewed", "source review identity")
        require(
            row["verdict"] in {"accepted", "accepted_after_revision"},
            "unresolved content review",
        )
        require(
            row["frozen_time"] == case["runtime_setup"]["frozen_time"]
            and row["timezone"] == case["runtime_setup"]["timezone"],
            "review clock binding",
        )
        for key in (
            "visible_input_assessment",
            "seed_time_assessment",
            "allowed_action_assessment",
            "prohibited_action_assessment",
            "oracle_assessment",
            "privacy_assessment",
            "resource_assessment",
            "adjudication",
        ):
            require(
                isinstance(row.get(key), str) and len(row[key].strip()) >= 12,
                f"missing substantive review: {key}",
            )
        require(
            set(row["allowed_action_classes"])
            == set(case["judge_criteria"]["allowed_action_classes"]),
            "review action boundary",
        )
        evidence = row["evidence_paths"]
        required = {
            "runtime_setup.seed",
            "runtime_setup.state_before.facts",
            "runtime_setup.frozen_time",
            "judge_criteria",
        }
        if case["split"] == "dev":
            required.add("runtime_setup.scripted_turns")
        require(required <= set(evidence), "review evidence coverage")
        for location in evidence:
            value = case
            for key in location.split("."):
                require(
                    isinstance(value, dict) and key in value, "review evidence path"
                )
                value = value[key]
    triplets = review["triplet_reviews"]
    families = {case["scenario_family_id"] for case in sources.values()}
    require(
        len(triplets) == 8 and {r["family"] for r in triplets} == families,
        "triplet review coverage",
    )
    for row in triplets:
        family = row["family"]
        source = next(c for c in sources.values() if c["scenario_family_id"] == family)
        require(
            row["source_case_sha256"] == source["case_spec_sha256"],
            "triplet source binding",
        )
        variants = {
            c["private_annotations"]["quality_label"]: c
            for c in cases.values()
            if c["scenario_family_id"] == family
        }
        require(
            row["variant_case_sha256"]
            == {q: c["case_spec_sha256"] for q, c in variants.items()},
            "triplet variant binding",
        )
        require(row["verdict"] == "accepted_for_e5_validation", "triplet adjudication")
        for key in (
            "good_basis",
            "mild_defect",
            "severe_defect",
            "mutation_boundary",
            "distinction_basis",
        ):
            require(
                isinstance(row.get(key), str) and len(row[key].strip()) >= 12,
                f"missing triplet rationale: {key}",
            )
    return "complete"


def verify(root, *, require_review=False):
    cases = {}
    groups = {}
    resources = {}
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
        resource_file = (
            "resource-snapshot.json"
            if name == "primary"
            else "calibration-resource-snapshot.json"
        )
        require(resource == load(root / resource_file), "package resource copy drift")
        resources[name] = resource["manifest_sha256"]
        pages = {page["url"]: page for page in resource["pages"]}
        for case in rows:
            require(case["case_id"] not in cases, "duplicate Case identity")
            require(case_spec_digest(case) == case["case_spec_sha256"], "Case digest")
            review_status(case)
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
        require(source["scenario_family_id"] == item["family"], "source family binding")
        sources[digest] = source
    require(
        len(sources) == 8
        and len({s["scenario_family_id"] for s in sources.values()}) == 8,
        "duplicate source",
    )
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
            item["invariant_input_sha256"]
            == input_digest(source)
            == input_digest(case),
            "mutation invariant digest",
        )
        require(
            item["quality_label"] == case["private_annotations"]["quality_label"],
            "mutation label",
        )
        require(
            item["review_status"] == review_status(case),
            "mutation review status",
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
        require(item["review_status"] == review_status(case), "lineage review status")
    ai_review = verify_review(
        root, cases, sources, resources, require_review=require_review
    )
    for name in ("primary", "calibration"):
        report = validate_dataset(root / name)
        require(report.ok, f"{name} contract/privacy/release validation")
    return {
        "primary_inputs": 48,
        "calibration_inputs": 24,
        "source_cases": 8,
        "mutation_reconstruction": "passed",
        "ai_content_review": ai_review,
        "independent_human_review": "not_performed",
        "formal": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--require-review", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            verify(args.root, require_review=args.require_review), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
