"""Publish an untrusted Case candidate using the same release bindings as Runtime."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .canonical import canonical_json_bytes, sha256_digest
from .case_specs import validate_case_spec_v2
from .integrity import artifact_manifest_digest, benchmark_release_digest
from .models import BenchmarkReleaseManifestV1, CaseSuiteManifestV2
from .release_governance import (
    CANONICALIZATION_VERSION,
    case_bindings,
    compute_case_suite_sha256,
    load_protocol_release,
    mutation_lineage_sha256,
    resource_bindings,
)


def write_candidate_suite(
    output: Path, *, cases: list[dict], resource_snapshot: Path, dataset_version: str
) -> Path:
    """Create an engineering candidate, never register it or claim review."""
    validated = [validate_case_spec_v2(case) for case in cases]
    if (
        not validated
        or len({case["runtime_setup"]["invocation_mode"] for case in validated}) != 1
    ):
        raise ValueError("a candidate needs Cases using one invocation mode")
    if output.exists():
        raise FileExistsError(output)
    protocol = load_protocol_release()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".case-candidate-", dir=output.parent) as temporary:
        root = Path(temporary) / "suite"
        (root / "cases").mkdir(parents=True)
        (root / "resources").mkdir()
        (root / "resources/snapshot.json").write_bytes(resource_snapshot.read_bytes())
        for ordinal, case in enumerate(sorted(validated, key=lambda item: item["case_id"]), 1):
            # Stable safe filenames avoid interpreting random SHA digit runs
            # as phone/identity numbers in the public privacy guard.
            (root / "cases" / f"case-{ordinal:04d}.json").write_bytes(
                canonical_json_bytes(case)
            )
        paths = sorted(
            path.relative_to(root).as_posix() for path in (root / "cases").iterdir()
        )
        bindings = case_bindings(root, paths)
        resources = resource_bindings(root, ["resources/snapshot.json"])
        counts = {
            track: sum(case["track"] == track for case in validated)
            for track in ("planning", "intervention", "assessment", "revision")
        }
        bundles = {
            item["component"]: item["bundle_sha256"]
            for item in protocol["source_bundles"]
        }
        roles = ["calibration_output", "engineering_mini", "primary_episode"]
        if any(case["dataset_role"] == "protocol_pilot" for case in validated):
            roles.append("protocol_pilot")
        release = {
            "schema_version": "benchmark-release-manifest-v1",
            "benchmark_release_id": f"{dataset_version}-release",
            "benchmark_version": dataset_version,
            "release_status": "engineering",
            "evaluation_protocol_release_id": protocol["protocol_release_id"],
            "evaluation_protocol_release_sha256": protocol["release_sha256"],
            "case_schema_version": "case-spec-v2",
            "case_ordering": "fixed_ordinal",
            "case_suite_digest_rule": "ordered-case-bindings-and-resource-v1",
            "cases": bindings,
            "case_suite_sha256": compute_case_suite_sha256(bindings, resources),
            "partitions": [
                {
                    "dataset_role": role,
                    "case_ids": sorted(
                        c["case_id"] for c in validated if c["dataset_role"] == role
                    ),
                    "expected_track_counts": {
                        track: sum(
                            c["track"] == track and c["dataset_role"] == role
                            for c in validated
                        )
                        for track in counts
                    },
                }
                for role in roles
            ],
            "expected_total_cases": len(validated),
            "mutation_source_lineage_sha256": mutation_lineage_sha256(root, paths),
            "resource_snapshots": resources,
            "protocol_bindings": {
                "schema_set_sha256": sha256_digest(protocol["artifact_schemas"]),
                **{
                    key: protocol[key]
                    for key in (
                        "action_protocol_sha256",
                        "rubric_sha256",
                        "track_anchor_sha256",
                        "rule_pack_sha256",
                        "judge_prompt_sha256",
                    )
                },
                **{
                    f"{component}_source_bundle_sha256": digest
                    for component, digest in bundles.items()
                },
            },
            "canonicalization_version": CANONICALIZATION_VERSION,
            "digest_algorithm": "sha256",
            "manifest_sha256": "0" * 64,
        }
        release["manifest_sha256"] = benchmark_release_digest(release)
        BenchmarkReleaseManifestV1.model_validate(release)
        (root / "benchmark-release.json").write_bytes(canonical_json_bytes(release))
        suite = {
            "schema_version": "case-suite-manifest-v2",
            "dataset_version": dataset_version,
            "case_schema_version": "case-spec-v2",
            "case_files": paths,
            "default_model_mode": validated[0]["runtime_setup"]["invocation_mode"],
            "resource_snapshot_file": "resources/snapshot.json",
            "evaluation_protocol_release_id": protocol["protocol_release_id"],
            "evaluation_protocol_release_sha256": protocol["release_sha256"],
            "benchmark_release_file": "benchmark-release.json",
            "benchmark_release_id": release["benchmark_release_id"],
            "benchmark_release_sha256": release["manifest_sha256"],
            "case_suite_sha256": release["case_suite_sha256"],
            "benchmark_expected_total_cases": len(validated),
            "benchmark_expected_track_counts": counts,
            "manifest_sha256": "0" * 64,
        }
        suite["manifest_sha256"] = artifact_manifest_digest(suite)
        CaseSuiteManifestV2.model_validate(suite)
        (root / "manifest.json").write_bytes(canonical_json_bytes(suite))
        from .validator import validate_dataset

        if not validate_dataset(root).ok:
            raise ValueError("candidate failed privacy, contract or release validation")
        os.replace(root, output)
    return output
