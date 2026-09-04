"""Build E3.1.2 release assets without rewriting historical Schemas."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from learning_agent_eval.action_protocol import (
    ACTION_DECLARATION_PROTOCOL_DOCUMENT,
    ACTION_DECLARATION_PROTOCOL_SHA256,
    ACTION_DECLARATION_PROTOCOL_VERSION,
)
from learning_agent_eval.active_aggregate import AGGREGATOR_VERSION_V3
from learning_agent_eval.active_judge import JUDGE_SOURCE_BUNDLE_SHA256_V3
from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    benchmark_release_digest,
    protocol_release_digest,
    schema_lock_digest,
    source_bundle_digest,
    trusted_registry_digest,
)
from learning_agent_eval.models import (
    BenchmarkReleaseManifestV1,
    CaseSuiteManifestV2,
    EvaluationProtocolReleaseV1,
    SchemaLockManifestV1,
    SourceBundleManifestV1,
    TrustedBenchmarkRegistryV1,
)
from learning_agent_eval.release_governance import (
    ACTIVE_PROTOCOL_SCHEMA_VERSIONS,
    BLINDING_POLICY_SHA256,
    BLINDING_POLICY_VERSION,
    CANONICALIZATION_SHA256,
    CANONICALIZATION_VERSION,
    EVIDENCE_PATH_POLICY_SHA256,
    EVIDENCE_PATH_POLICY_VERSION,
    assess_benchmark_release,
    case_bindings,
    compute_case_suite_sha256,
    load_benchmark_release,
    load_production_registry,
    load_protocol_release,
    load_schema_lock,
    mutation_lineage_sha256,
    protocol_release_reason_codes,
    resource_bindings,
    schema_lock_reason_codes,
)
from learning_agent_eval.rubric import (
    JUDGE_CONFIG_SHA256_V3,
    JUDGE_CONFIG_VERSION_V3,
    JUDGE_PROMPT_SHA256_V3,
    JUDGE_PROMPT_VERSION_V3,
    JUDGE_VERSION_V3,
    RUBRIC_SHA256,
    RUBRIC_VERSION,
    TRACK_ANCHOR_SHA256,
    TRACK_ANCHOR_VERSION,
)
from learning_agent_eval.rules import RULE_PACK_SHA256_V3, RULE_PACK_VERSION_V3
from learning_agent_eval.schemas import SCHEMA_FILENAMES, schema_documents
from learning_agent_eval.source_bundles import (
    SOURCE_BUNDLE_VERSION,
    SOURCE_COMPONENTS,
    build_source_bundle,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "evaluation" / "schemas"
RELEASE_ROOT = PROJECT_ROOT / "evaluation" / "releases"
DATASET_ROOT = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v4-engineering"
SUITE_PATH = DATASET_ROOT / "manifest.json"
BENCHMARK_PATH = DATASET_ROOT / "benchmark-release.json"

ACTIVE_SCHEMA_VERSIONS = ACTIVE_PROTOCOL_SCHEMA_VERSIONS


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object: {path}")
    return value


def _pretty(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != payload:
        path.write_bytes(payload)


def _frozen_in(version: str) -> str:
    if version in ACTIVE_SCHEMA_VERSIONS:
        return "evaluation-protocol-release-1.0"
    if version.startswith(("decision-episode-v1", "acceptable", "environment-manifest-v1")):
        return "E0"
    if version.startswith("decision-episode-v2") or version == "rule-result-v1":
        return "E2"
    if version.endswith("-v1"):
        return "E3"
    return "E3.1"


def _schema_assets() -> tuple[list[dict[str, str]], dict[str, Any]]:
    generated = schema_documents()
    existing_lock_path = RELEASE_ROOT / "schema-lock-v1.json"
    existing_historical: dict[str, dict[str, Any]] = {}
    if existing_lock_path.exists():
        existing_lock = SchemaLockManifestV1.model_validate(
            _load(existing_lock_path)
        ).model_dump(mode="json")
        existing_historical = {
            item["schema_version"]: item
            for item in existing_lock["entries"]
            if item["status"] == "historical_frozen"
        }
    for version, filename in SCHEMA_FILENAMES.items():
        path = SCHEMA_ROOT / filename
        payload = _pretty(generated[filename])
        if version not in ACTIVE_SCHEMA_VERSIONS and _load(path) != generated[filename]:
            raise RuntimeError(f"historical_schema_drift:{filename}")
        previous = existing_historical.get(version)
        if previous is not None and hashlib.sha256(path.read_bytes()).hexdigest() != (
            previous["raw_sha256"]
        ):
            raise RuntimeError(f"historical_schema_lock_drift:{filename}")
        if version in ACTIVE_SCHEMA_VERSIONS:
            _write(path, payload)
    entries = []
    for version, filename in sorted(SCHEMA_FILENAMES.items()):
        path = SCHEMA_ROOT / filename
        schema = _load(path)
        entries.append(
            {
                "relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
                "schema_version": version,
                "schema_id": schema["$id"],
                "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "frozen_in": _frozen_in(version),
                "status": (
                    "active_release"
                    if version in ACTIVE_SCHEMA_VERSIONS
                    else "historical_frozen"
                ),
            }
        )
    entries.sort(key=lambda item: item["schema_version"])
    lock: dict[str, Any] = {
        "schema_version": "schema-lock-manifest-v1",
        "lock_version": "evaluation-schema-lock-v1",
        "entries": entries,
        "manifest_sha256": "0" * 64,
    }
    lock["manifest_sha256"] = schema_lock_digest(lock)
    lock = SchemaLockManifestV1.model_validate(lock).model_dump(mode="json")
    if existing_historical:
        new_historical = {
            item["schema_version"]: item
            for item in lock["entries"]
            if item["status"] == "historical_frozen"
        }
        if new_historical != existing_historical:
            raise RuntimeError("historical_schema_lock_update_forbidden")
    _write(RELEASE_ROOT / "schema-lock-v1.json", canonical_json_bytes(lock))
    return entries, lock


def _source_assets() -> dict[str, dict[str, Any]]:
    bundles: dict[str, dict[str, Any]] = {}
    for component in SOURCE_COMPONENTS:
        document = build_source_bundle(component)
        if document["bundle_sha256"] != source_bundle_digest(document):
            raise RuntimeError("source_bundle_digest_mismatch")
        validated = SourceBundleManifestV1.model_validate(document).model_dump(
            mode="json"
        )
        _write(
            RELEASE_ROOT / "source-bundles" / f"{component}.json",
            canonical_json_bytes(validated),
        )
        bundles[component] = validated
    if bundles["judge"]["bundle_sha256"] != JUDGE_SOURCE_BUNDLE_SHA256_V3:
        raise RuntimeError("judge_source_bundle_constant_mismatch")
    return bundles


def _protocol(
    schema_entries: list[dict[str, str]],
    lock: dict[str, Any],
    bundles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    bound_versions = sorted(ACTIVE_SCHEMA_VERSIONS)
    by_version = {item["schema_version"]: item for item in schema_entries}
    release: dict[str, Any] = {
        "schema_version": "evaluation-protocol-release-v1",
        "protocol_release_id": "evaluation-protocol-release-1.0",
        "protocol_version": "1.0",
        "release_status": "active",
        "active_chain": {
            "case_spec": "case-spec-v2",
            "decision_episode": "decision-episode-v4",
            "runtime_failure": "runtime-failure-v2",
            "runtime_manifest": "runtime-run-manifest-v3",
            "rule_result": "rule-result-v3",
            "rule_manifest": "rule-run-manifest-v3",
            "judge_result": "judge-result-v3",
            "judge_manifest": "judge-run-manifest-v3",
            "aggregate_result": "aggregate-result-v3",
            "aggregate_track_result": "aggregate-track-result-v3",
            "aggregate_manifest": "aggregate-run-manifest-v3",
        },
        "artifact_schemas": [
            {
                "schema_version": version,
                "relative_path": by_version[version]["relative_path"],
                "schema_id": by_version[version]["schema_id"],
                "raw_sha256": by_version[version]["raw_sha256"],
            }
            for version in bound_versions
        ],
        "schema_lock_version": "evaluation-schema-lock-v1",
        "schema_lock_sha256": lock["manifest_sha256"],
        "canonicalization_version": CANONICALIZATION_VERSION,
        "canonicalization_sha256": CANONICALIZATION_SHA256,
        "digest_algorithm": "sha256",
        "action_protocol_version": ACTION_DECLARATION_PROTOCOL_VERSION,
        "action_protocol_relative_path": (
            "evaluation/releases/model-action-declaration-v2.json"
        ),
        "action_protocol_sha256": ACTION_DECLARATION_PROTOCOL_SHA256,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "rule_pack_version": RULE_PACK_VERSION_V3,
        "rule_pack_sha256": RULE_PACK_SHA256_V3,
        "judge_version": JUDGE_VERSION_V3,
        "judge_config_version": JUDGE_CONFIG_VERSION_V3,
        "judge_config_sha256": JUDGE_CONFIG_SHA256_V3,
        "judge_prompt_version": JUDGE_PROMPT_VERSION_V3,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V3,
        "aggregator_version": AGGREGATOR_VERSION_V3,
        "source_bundles": [
            {
                "component": component,
                "bundle_version": SOURCE_BUNDLE_VERSION,
                "bundle_sha256": bundles[component]["bundle_sha256"],
            }
            for component in SOURCE_COMPONENTS
        ],
        "blinding_policy_version": BLINDING_POLICY_VERSION,
        "blinding_policy_sha256": BLINDING_POLICY_SHA256,
        "evidence_path_policy_version": EVIDENCE_PATH_POLICY_VERSION,
        "evidence_path_policy_sha256": EVIDENCE_PATH_POLICY_SHA256,
        "formal_capability_policy": {
            "single_artifact_claim": "forbidden",
            "trusted_registry_required": True,
            "posthoc_filter_policy": "blocks_formal_capability",
            "runtime_failure_policy": "disclose_and_block_formal_capability",
            "invalid_input_policy": "disclose_exclude_from_mean_and_block",
            "judge_error_policy": "disclose_exclude_from_mean_and_block",
            "track_reporting_policy": "four_tracks_no_overall",
        },
        "release_sha256": "0" * 64,
    }
    release["release_sha256"] = protocol_release_digest(release)
    release = EvaluationProtocolReleaseV1.model_validate(release).model_dump(mode="json")
    _write(
        RELEASE_ROOT / "evaluation-protocol-release-1.0.json",
        canonical_json_bytes(release),
    )
    return release


def _protocol_bindings(protocol: dict[str, Any]) -> dict[str, str]:
    bundles = {
        item["component"]: item["bundle_sha256"]
        for item in protocol["source_bundles"]
    }
    return {
        "schema_set_sha256": sha256_digest(protocol["artifact_schemas"]),
        "action_protocol_sha256": protocol["action_protocol_sha256"],
        "rubric_sha256": protocol["rubric_sha256"],
        "track_anchor_sha256": protocol["track_anchor_sha256"],
        "rule_pack_sha256": protocol["rule_pack_sha256"],
        "judge_prompt_sha256": protocol["judge_prompt_sha256"],
        "runtime_source_bundle_sha256": bundles["runtime"],
        "rules_source_bundle_sha256": bundles["rules"],
        "judge_source_bundle_sha256": bundles["judge"],
        "aggregate_source_bundle_sha256": bundles["aggregate"],
    }


def _benchmark(protocol: dict[str, Any]) -> dict[str, Any]:
    suite = _load(SUITE_PATH)
    cases = case_bindings(DATASET_ROOT, suite["case_files"])
    resources = resource_bindings(DATASET_ROOT, [suite["resource_snapshot_file"]])
    case_suite_digest = compute_case_suite_sha256(cases, resources)
    roles = ("calibration_output", "engineering_mini", "primary_episode")
    partitions = []
    for role in roles:
        selected = [item for item in cases if item["dataset_role"] == role]
        partitions.append(
            {
                "dataset_role": role,
                "case_ids": sorted(item["case_id"] for item in selected),
                "expected_track_counts": {
                    name: sum(item["track"] == name for item in selected)
                    for name in ("planning", "intervention", "assessment", "revision")
                },
            }
        )
    release: dict[str, Any] = {
        "schema_version": "benchmark-release-manifest-v1",
        "benchmark_release_id": "decisionbench-v4-engineering-release-v1",
        "benchmark_version": "decisionbench-v4-engineering-v1",
        "release_status": "engineering",
        "evaluation_protocol_release_id": protocol["protocol_release_id"],
        "evaluation_protocol_release_sha256": protocol["release_sha256"],
        "case_schema_version": "case-spec-v2",
        "case_ordering": "fixed_ordinal",
        "case_suite_digest_rule": "ordered-case-bindings-and-resource-v1",
        "cases": cases,
        "case_suite_sha256": case_suite_digest,
        "partitions": partitions,
        "expected_total_cases": len(cases),
        "mutation_source_lineage_sha256": mutation_lineage_sha256(
            DATASET_ROOT, suite["case_files"]
        ),
        "resource_snapshots": resources,
        "protocol_bindings": _protocol_bindings(protocol),
        "canonicalization_version": CANONICALIZATION_VERSION,
        "digest_algorithm": "sha256",
        "manifest_sha256": "0" * 64,
    }
    release["manifest_sha256"] = benchmark_release_digest(release)
    release = BenchmarkReleaseManifestV1.model_validate(release).model_dump(mode="json")
    _write(BENCHMARK_PATH, canonical_json_bytes(release))
    suite.update(
        {
            "evaluation_protocol_release_id": protocol["protocol_release_id"],
            "evaluation_protocol_release_sha256": protocol["release_sha256"],
            "benchmark_release_file": "benchmark-release.json",
            "benchmark_release_id": release["benchmark_release_id"],
            "benchmark_release_sha256": release["manifest_sha256"],
            "case_suite_sha256": case_suite_digest,
            "benchmark_expected_total_cases": len(cases),
            "benchmark_expected_track_counts": {
                name: sum(item["track"] == name for item in cases)
                for name in ("planning", "intervention", "assessment", "revision")
            },
            "manifest_sha256": "0" * 64,
        }
    )
    suite["manifest_sha256"] = artifact_manifest_digest(suite)
    suite = CaseSuiteManifestV2.model_validate(suite).model_dump(mode="json")
    _write(SUITE_PATH, canonical_json_bytes(suite))
    return release


def _empty_registry() -> None:
    registry: dict[str, Any] = {
        "schema_version": "trusted-benchmark-registry-v1",
        "registry_version": "production-trusted-benchmark-registry-v1",
        "entries": [],
        "registry_sha256": "0" * 64,
    }
    registry["registry_sha256"] = trusted_registry_digest(registry)
    registry = TrustedBenchmarkRegistryV1.model_validate(registry).model_dump(mode="json")
    _write(
        RELEASE_ROOT / "trusted-benchmark-registry-v1.json",
        canonical_json_bytes(registry),
    )


def _release_is_committed() -> bool:
    """Return whether Release 1.0 is already immutable in the current HEAD."""

    completed = subprocess.run(
        [
            "git",
            "cat-file",
            "-e",
            "HEAD:evaluation/releases/evaluation-protocol-release-1.0.json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _verify_committed_release() -> int:
    """Verify Release 1.0 after publication without rewriting any asset."""

    protocol = load_protocol_release()
    lock = load_schema_lock()
    if protocol_release_reason_codes(protocol):
        raise RuntimeError("committed_protocol_release_drift")
    if schema_lock_reason_codes(lock):
        raise RuntimeError("committed_schema_lock_drift")
    generated = schema_documents()
    for filename, expected in generated.items():
        if _load(SCHEMA_ROOT / filename) != expected:
            raise RuntimeError(f"committed_schema_model_drift:{filename}")
    for component in SOURCE_COMPONENTS:
        committed = _load(RELEASE_ROOT / "source-bundles" / f"{component}.json")
        if committed != build_source_bundle(component):
            raise RuntimeError(f"committed_source_bundle_drift:{component}")
    if _load(RELEASE_ROOT / "model-action-declaration-v2.json") != (
        ACTION_DECLARATION_PROTOCOL_DOCUMENT
    ):
        raise RuntimeError("committed_action_protocol_drift")
    suite = CaseSuiteManifestV2.model_validate(_load(SUITE_PATH)).model_dump(mode="json")
    benchmark = load_benchmark_release(DATASET_ROOT, suite)
    assessment = assess_benchmark_release(
        dataset_root=DATASET_ROOT,
        suite=suite,
        release=benchmark,
        filtered=False,
    )
    if not assessment.protocol_match or not assessment.suite_complete:
        raise RuntimeError("committed_engineering_benchmark_drift")
    load_production_registry()
    print("e312_release_assets_verified immutable_release=1.0")
    return 0


def main() -> int:
    if _release_is_committed():
        return _verify_committed_release()
    schemas, lock = _schema_assets()
    bundles = _source_assets()
    _write(
        RELEASE_ROOT / "model-action-declaration-v2.json",
        canonical_json_bytes(ACTION_DECLARATION_PROTOCOL_DOCUMENT),
    )
    protocol = _protocol(schemas, lock, bundles)
    _benchmark(protocol)
    _empty_registry()
    print("e312_release_assets_built production_registry_entries=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
