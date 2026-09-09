"""Freeze reviewed E7 cases; reuse CaseSpec scaffolding and no historical facts."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from build_e6_test import build_inputs
from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
from learning_agent_eval.integrity import case_spec_digest
from learning_agent_eval.case_authoring import write_candidate_suite
from build_e6_repair_releases import bind, freeze

ROOT = Path.cwd()
design = ROOT / "evaluation/case-design/e7-family-design.json"
cases, resource = build_inputs(
    design_path=design,
    resource_version="e7-fixed-resources-v1",
    resource_prefix="e7",
    frozen_date="2026-09-09",
    composite_abstention=True,
)
review = []
for c in cases:
    if (
        c["track"] == "planning"
        and c["runtime_setup"]["seed"].get("planning_readiness") == "ready"
    ):
        minutes = c["runtime_setup"]["seed"]["weekly_minutes"]
        c["runtime_setup"]["seed"]["planning_confirmed_facts"] = [
            {"key": "deadline", "value": "2026-09-22", "source": "user"},
            {"key": "weekly_minutes", "value": str(minutes), "source": "user"},
        ]
    c["private_annotations"].update(
        reviewer_role="primary_ai_reviewer",
        adjudication_note="Content reviewed before calls: input facts, multiple-action envelope, local resources, independent track states and expected effects checked. Author and reviewer share execution context; no independent human review.",
    )
    c["case_spec_sha256"] = case_spec_digest(c)
    review.append(
        dict(
            case_id=c["case_id"],
            case_sha256=c["case_spec_sha256"],
            track=c["track"],
            verdict="accepted",
            reviewer_role="primary_ai_reviewer",
            checks=[
                "new topic and trigger versus historical families",
                "facts and resource examples manually recomputed",
                "decision envelope and no unintended write",
                "deadline and budget explicit when ready",
                "no Agent output or quality label supplied",
            ],
        )
    )
with TemporaryDirectory() as temp:
    snapshot = Path(temp) / "snapshot.json"
    snapshot.write_bytes(canonical_json_bytes(resource))
    for fid in sorted({c["scenario_family_id"] for c in cases}):
        selected = [c for c in cases if c["scenario_family_id"] == fid]
        name = "e7-test-" + selected[0]["case_id"].rsplit("-", 1)[0]
        target = ROOT / "evaluation/datasets" / name
        write_candidate_suite(
            target, cases=selected, resource_snapshot=snapshot, dataset_version=name
        )
        bind(target, name + "-release", name)
        freeze(target)
review_doc = dict(
    version="e7-test-content-review-v1",
    design_sha256=sha256_digest(json.loads(design.read_text())),
    resource_sha256=resource["manifest_sha256"],
    rows=review,
)
review_doc["review_sha256"] = sha256_digest(review_doc)
(ROOT / "evaluation/case-design/e7-test-content-review.json").write_bytes(
    canonical_json_bytes(review_doc)
)
print(
    "content-reviewed and registered",
    len(cases),
    "cases",
    len({c["scenario_family_id"] for c in cases}),
    "families",
)
