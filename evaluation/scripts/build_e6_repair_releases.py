"""Build active protocol candidates; freeze Benchmarks without rewriting history.

Run build after source changes, bind each new dataset directory, then freeze only
when calibration and independent test review are complete. Registration freezes
all bindings of that protocol identity. A later method change needs another protocol identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import build_e312_releases as legacy
from learning_agent_eval.action_protocol import ACTION_DECLARATION_PROTOCOL_DOCUMENT
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    benchmark_release_digest,
    protocol_release_digest,
    schema_lock_digest,
    trusted_registry_digest,
)
from learning_agent_eval.models import (
    BenchmarkReleaseManifestV1,
    CaseSuiteManifestV2,
    EvaluationProtocolReleaseV1,
    SchemaLockManifestV1,
    TrustedBenchmarkRegistryV1,
)
from learning_agent_eval.release_governance import (
    ACTIVE_PROTOCOL_RELEASE_ID,
    ACTIVE_PROTOCOL_SCHEMA_VERSIONS,
    ACTIVE_PROTOCOL_VERSION,
    PROTOCOL_RELEASE_PATH,
    SCHEMA_LOCK_PATH,
    SOURCE_BUNDLE_ROOT,
    SUPPORTED_PROTOCOL_VERSIONS,
    case_bindings,
    compute_case_suite_sha256,
    load_production_registry,
    load_protocol_release,
    mutation_lineage_sha256,
    protocol_release_reason_codes,
    resource_bindings,
    verify_protocol_asset_bytes,
)
from learning_agent_eval.schemas import (
    ACTIVE_SCHEMA_RELATIVE_ROOT,
    SCHEMA_FILENAMES,
    schema_documents,
)
from learning_agent_eval.source_bundles import SOURCE_COMPONENTS, build_source_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_ROOT = PROJECT_ROOT / "evaluation/releases"
TRACKS = ("planning", "intervention", "assessment", "revision")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(document)
    if not path.exists() or path.read_bytes() != payload:
        path.write_bytes(payload)


def check_active():
    protocol = load_protocol_release()
    reasons = protocol_release_reason_codes(protocol)
    if reasons:
        raise RuntimeError("active_protocol_drift:" + ",".join(reasons))
    return protocol


def build():
    registry = load_production_registry()
    if any(
        e["evaluation_protocol_release_id"] == ACTIVE_PROTOCOL_RELEASE_ID
        for e in registry["entries"]
    ):
        check_active()
        print(
            f"protocol_{ACTIVE_PROTOCOL_VERSION}_verified registered=true assets_unchanged=true"
        )
        return
    # Every older protocol asset remains untouched.
    entries = []
    generated = schema_documents()
    for version, filename in sorted(SCHEMA_FILENAMES.items()):
        path = PROJECT_ROOT / ACTIVE_SCHEMA_RELATIVE_ROOT / filename
        document = generated[filename]
        write(path, document)
        entries.append(
            {
                "schema_version": version,
                "relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
                "schema_id": document["$id"],
                "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "frozen_in": ACTIVE_PROTOCOL_RELEASE_ID,
                "status": "active_release"
                if version in ACTIVE_PROTOCOL_SCHEMA_VERSIONS
                else "historical_frozen",
            }
        )
    lock = {
        "schema_version": "schema-lock-manifest-v1",
        "lock_version": f"evaluation-schema-lock-{ACTIVE_PROTOCOL_VERSION}",
        "entries": entries,
        "manifest_sha256": "0" * 64,
    }
    lock["manifest_sha256"] = schema_lock_digest(lock)
    lock = SchemaLockManifestV1.model_validate(lock).model_dump(mode="json")
    write(SCHEMA_LOCK_PATH, lock)
    bundles = {
        component: build_source_bundle(component) for component in SOURCE_COMPONENTS
    }
    for component, bundle in bundles.items():
        write(SOURCE_BUNDLE_ROOT / f"{component}.json", bundle)
    action_path = f"evaluation/releases/protocol-{ACTIVE_PROTOCOL_VERSION}/model-action-declaration-v2.json"
    write(PROJECT_ROOT / action_path, ACTION_DECLARATION_PROTOCOL_DOCUMENT)
    protocol = load(RELEASE_ROOT / "evaluation-protocol-release-1.0.json")
    protocol.update(
        {
            "protocol_release_id": ACTIVE_PROTOCOL_RELEASE_ID,
            "protocol_version": ACTIVE_PROTOCOL_VERSION,
            "schema_lock_version": lock["lock_version"],
            "schema_lock_sha256": lock["manifest_sha256"],
            "action_protocol_relative_path": action_path,
            "artifact_schemas": [
                {
                    key: entry[key]
                    for key in (
                        "schema_version",
                        "relative_path",
                        "schema_id",
                        "raw_sha256",
                    )
                }
                for entry in entries
                if entry["schema_version"] in ACTIVE_PROTOCOL_SCHEMA_VERSIONS
            ],
            "source_bundles": [
                {
                    key: bundle[key]
                    for key in ("component", "bundle_version", "bundle_sha256")
                }
                for bundle in bundles.values()
            ],
        }
    )
    for field in (
        "canonicalization_version",
        "canonicalization_sha256",
        "action_protocol_version",
        "action_protocol_sha256",
        "rubric_version",
        "rubric_sha256",
        "track_anchor_version",
        "track_anchor_sha256",
        "rule_pack_version",
        "rule_pack_sha256",
        "judge_version",
        "judge_config_version",
        "judge_config_sha256",
        "judge_prompt_version",
        "judge_prompt_sha256",
        "aggregator_version",
        "blinding_policy_version",
        "blinding_policy_sha256",
        "evidence_path_policy_version",
        "evidence_path_policy_sha256",
    ):
        constant = field.upper().replace(
            "ACTION_PROTOCOL", "ACTION_DECLARATION_PROTOCOL"
        )
        if field.startswith(("rule_pack", "judge_", "aggregator")):
            constant += "_V3"
        protocol[field] = getattr(legacy, constant)
    protocol["release_sha256"] = protocol_release_digest(protocol)
    protocol = EvaluationProtocolReleaseV1.model_validate(protocol).model_dump(
        mode="json"
    )
    write(PROTOCOL_RELEASE_PATH, protocol)
    check_active()
    print(
        f"protocol_{ACTIVE_PROTOCOL_VERSION}_built sha256={protocol['release_sha256']} registered=false"
    )


def dataset_paths(dataset):
    root = Path(dataset).resolve()
    if PROJECT_ROOT not in root.parents:
        raise RuntimeError("dataset_must_be_in_project_copy")
    suite_path = root / "manifest.json"
    suite = load(suite_path)
    return root, suite_path, suite


def require_mutable(root, release_id, registry):
    relative = (root / "benchmark-release.json").relative_to(PROJECT_ROOT).as_posix()
    if any(
        e["benchmark_release_id"] == release_id
        or e["manifest_relative_path"] == relative
        for e in registry["entries"]
    ):
        raise RuntimeError("registered_benchmark_update_forbidden")
    path = root / "benchmark-release.json"
    if path.exists():
        old = load(path)
        if old.get("release_status") == "released":
            raise RuntimeError("released_benchmark_update_forbidden")
        if old.get("evaluation_protocol_release_id") != ACTIVE_PROTOCOL_RELEASE_ID:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", relative],
                cwd=PROJECT_ROOT,
                capture_output=True,
                check=False,
            )
            if tracked.returncode == 0:
                raise RuntimeError(
                    "historical_dataset_update_forbidden_clone_to_new_directory"
                )


def bind(dataset, release_id, benchmark_version):
    protocol = check_active()
    registry = load_production_registry()
    root, suite_path, suite = dataset_paths(dataset)
    require_mutable(root, release_id, registry)
    cases = case_bindings(root, suite["case_files"])
    resources = resource_bindings(root, [suite["resource_snapshot_file"]])
    counts = {track: sum(c["track"] == track for c in cases) for track in TRACKS}
    roles = ["calibration_output", "engineering_mini", "primary_episode"]
    if any(c["dataset_role"] == "protocol_pilot" for c in cases):
        roles.append("protocol_pilot")
    release = {
        "schema_version": "benchmark-release-manifest-v1",
        "benchmark_release_id": release_id,
        "benchmark_version": benchmark_version,
        "release_status": "engineering" if "protocol_pilot" in roles else "candidate",
        "evaluation_protocol_release_id": protocol["protocol_release_id"],
        "evaluation_protocol_release_sha256": protocol["release_sha256"],
        "case_schema_version": "case-spec-v2",
        "case_ordering": "fixed_ordinal",
        "case_suite_digest_rule": "ordered-case-bindings-and-resource-v1",
        "cases": cases,
        "case_suite_sha256": compute_case_suite_sha256(cases, resources),
        "partitions": [
            {
                "dataset_role": role,
                "case_ids": sorted(
                    c["case_id"] for c in cases if c["dataset_role"] == role
                ),
                "expected_track_counts": {
                    track: sum(
                        c["track"] == track and c["dataset_role"] == role for c in cases
                    )
                    for track in TRACKS
                },
            }
            for role in roles
        ],
        "expected_total_cases": len(cases),
        "mutation_source_lineage_sha256": mutation_lineage_sha256(
            root, suite["case_files"]
        ),
        "resource_snapshots": resources,
        "protocol_bindings": legacy._protocol_bindings(protocol),
        "canonicalization_version": protocol["canonicalization_version"],
        "digest_algorithm": "sha256",
        "manifest_sha256": "0" * 64,
    }
    release["manifest_sha256"] = benchmark_release_digest(release)
    release = BenchmarkReleaseManifestV1.model_validate(release).model_dump(mode="json")
    suite.update(
        {
            "dataset_version": benchmark_version,
            "evaluation_protocol_release_id": protocol["protocol_release_id"],
            "evaluation_protocol_release_sha256": protocol["release_sha256"],
            "benchmark_release_file": "benchmark-release.json",
            "benchmark_release_id": release_id,
            "benchmark_release_sha256": release["manifest_sha256"],
            "case_suite_sha256": release["case_suite_sha256"],
            "benchmark_expected_total_cases": len(cases),
            "benchmark_expected_track_counts": counts,
        }
    )
    suite["manifest_sha256"] = artifact_manifest_digest(suite)
    suite = CaseSuiteManifestV2.model_validate(suite).model_dump(mode="json")
    write(root / "benchmark-release.json", release)
    write(suite_path, suite)
    print(
        f"benchmark_bound id={release_id} cases={len(cases)} status={release['release_status']}"
    )


def freeze(dataset):
    protocol = check_active()
    registry = load_production_registry()
    root, suite_path, suite = dataset_paths(dataset)
    release = load(root / "benchmark-release.json")
    require_mutable(root, release["benchmark_release_id"], registry)
    # Recompute the complete candidate rather than accepting a caller's digest.
    from learning_agent_eval.release_governance import _assess_with_registry

    assessment = _assess_with_registry(
        dataset_root=root,
        suite=suite,
        release=release,
        protocol=protocol,
        registry=registry,
        filtered=False,
    )
    if not assessment.protocol_match or not assessment.suite_complete:
        raise RuntimeError("candidate_binding_incomplete")
    release["release_status"] = "released"
    release["manifest_sha256"] = benchmark_release_digest(release)
    release = BenchmarkReleaseManifestV1.model_validate(release).model_dump(mode="json")
    suite["benchmark_release_sha256"] = release["manifest_sha256"]
    suite["manifest_sha256"] = artifact_manifest_digest(suite)
    suite = CaseSuiteManifestV2.model_validate(suite).model_dump(mode="json")
    entry = {
        "benchmark_release_id": release["benchmark_release_id"],
        "benchmark_release_sha256": release["manifest_sha256"],
        "manifest_relative_path": (root / "benchmark-release.json")
        .relative_to(PROJECT_ROOT)
        .as_posix(),
        "case_suite_sha256": release["case_suite_sha256"],
        "expected_total_cases": release["expected_total_cases"],
        "expected_track_counts": suite["benchmark_expected_track_counts"],
        "evaluation_protocol_release_id": protocol["protocol_release_id"],
        "evaluation_protocol_release_sha256": protocol["release_sha256"],
    }
    registry["entries"] = sorted(
        [*registry["entries"], entry], key=lambda e: e["benchmark_release_id"]
    )
    registry["registry_sha256"] = trusted_registry_digest(registry)
    registry = TrustedBenchmarkRegistryV1.model_validate(registry).model_dump(
        mode="json"
    )
    write(root / "benchmark-release.json", release)
    write(suite_path, suite)
    write(RELEASE_ROOT / "trusted-benchmark-registry-v1.json", registry)
    load_production_registry()
    print(
        f"benchmark_frozen id={release['benchmark_release_id']} cases={release['expected_total_cases']} protocol={protocol['release_sha256']}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    verification = sub.add_parser("verify")
    verification.add_argument(
        "--protocol-version",
        choices=SUPPORTED_PROTOCOL_VERSIONS,
        help="verify historical assets without switching the active runtime",
    )
    candidate = sub.add_parser("bind")
    candidate.add_argument("--dataset", required=True)
    candidate.add_argument("--release-id", required=True)
    candidate.add_argument("--benchmark-version", required=True)
    frozen = sub.add_parser("freeze")
    frozen.add_argument("--dataset", required=True)
    args = parser.parse_args()
    if args.command == "build":
        build()
    elif args.command == "verify":
        if args.protocol_version is not None:
            print(
                json.dumps(
                    verify_protocol_asset_bytes(args.protocol_version), sort_keys=True
                )
            )
        else:
            check_active()
            load_production_registry()
            print("active_protocol_and_registry_verified")
    elif args.command == "bind":
        bind(args.dataset, args.release_id, args.benchmark_version)
    else:
        freeze(args.dataset)


if __name__ == "__main__":
    main()
