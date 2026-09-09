"""Trust-root verification for the active Evaluation Protocol and Benchmark."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .canonical import sha256_digest
from .integrity import (
    benchmark_release_digest,
    case_spec_digest,
    protocol_release_digest,
    trusted_registry_digest,
)
from .models import (
    BenchmarkReleaseManifestV1,
    CaseSuiteManifestV2,
    EvaluationProtocolReleaseV1,
    TrustedBenchmarkRegistryV1,
)
from .source_bundles import SOURCE_COMPONENTS, build_source_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = PROJECT_ROOT / "evaluation" / "releases"
SUPPORTED_PROTOCOL_VERSIONS = ("1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.17")
ACTIVE_PROTOCOL_VERSION = "1.17"
ACTIVE_PROTOCOL_RELEASE_ID = f"evaluation-protocol-release-{ACTIVE_PROTOCOL_VERSION}"
PROTOCOL_RELEASE_PATH = RELEASE_ROOT / f"{ACTIVE_PROTOCOL_RELEASE_ID}.json"
PRODUCTION_REGISTRY_PATH = RELEASE_ROOT / "trusted-benchmark-registry-v1.json"
SCHEMA_LOCK_PATH = RELEASE_ROOT / f"schema-lock-{ACTIVE_PROTOCOL_VERSION}.json"
SOURCE_BUNDLE_ROOT = RELEASE_ROOT / "source-bundles" / ACTIVE_PROTOCOL_VERSION
CANONICALIZATION_VERSION = "canonical-json-v1"
CANONICALIZATION_DOCUMENT = {
    "version": CANONICALIZATION_VERSION,
    "encoding": "utf-8",
    "unicode": "NFC",
    "object_keys": "lexicographic",
    "separators": [",", ":"],
    "integral_float": "integer",
    "negative_zero": 0,
    "non_finite": "reject",
    "array_order": "significant",
}
CANONICALIZATION_SHA256 = sha256_digest(CANONICALIZATION_DOCUMENT)
BLINDING_POLICY_VERSION = "judge-path-blinding-v3-returned-actions"
BLINDING_POLICY_SHA256 = sha256_digest(
    {
        "version": BLINDING_POLICY_VERSION,
        "drop": [
            "case_private_annotations",
            "case_tags",
            "provider_attestation",
            "provenance",
            "routing_and_credentials",
            "source_identity",
        ],
        "preserve": "ordinary_business_language_and_observable_evidence",
        "identity": "deterministic_opaque_projection",
    }
)
EVIDENCE_PATH_POLICY_VERSION = "episode-v4-evidence-path-v1"
EVIDENCE_PATH_POLICY_SHA256 = sha256_digest(
    {
        "version": EVIDENCE_PATH_POLICY_VERSION,
        "authority": "decision-episode-v4",
        "capture": "forbidden",
        "blind_projection_paths": "forbidden",
        "resolution": "same_episode_exact_path",
    }
)
ACTIVE_PROTOCOL_SCHEMA_VERSIONS = frozenset(
    {
        "action-declaration-protocol-v2",
        "aggregate-result-v3",
        "aggregate-run-manifest-v3",
        "aggregate-track-result-v3",
        "benchmark-release-manifest-v1",
        "case-spec-v2",
        "case-suite-manifest-v2",
        "decision-episode-v4",
        "environment-manifest-v2",
        "evaluation-protocol-release-v1",
        "judge-reference-v2",
        "judge-result-v3",
        "judge-run-manifest-v3",
        "provider-attestation-v1",
        "rule-result-v3",
        "rule-run-manifest-v3",
        "runtime-failure-v2",
        "runtime-run-manifest-v3",
        "schema-lock-manifest-v1",
        "source-bundle-manifest-v1",
        "trusted-benchmark-registry-v1",
    }
)


class ReleaseGovernanceError(ValueError):
    """A release artifact is absent, malformed, or digest-inconsistent."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BenchmarkTrustAssessment:
    benchmark_release_id: str
    benchmark_release_sha256: str
    protocol_release_id: str
    protocol_release_sha256: str
    release_registered: bool
    protocol_match: bool
    suite_complete: bool
    trusted_benchmark_run: bool
    reason_codes: tuple[str, ...]


def _load_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseGovernanceError(code) from exc
    if not isinstance(value, dict):
        raise ReleaseGovernanceError(code)
    return value


def _contained(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseGovernanceError("release.path_not_contained")
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ReleaseGovernanceError("release.path_not_contained")
    if target.is_symlink():
        raise ReleaseGovernanceError("release.symlink_forbidden")
    return target


def load_protocol_release(protocol_version: str | None = None) -> dict[str, Any]:
    """Load an explicitly supported repository release; default to the active one."""

    version = ACTIVE_PROTOCOL_VERSION if protocol_version is None else protocol_version
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ReleaseGovernanceError("protocol_release.version_unsupported")
    path = (
        PROTOCOL_RELEASE_PATH
        if protocol_version is None
        else RELEASE_ROOT / f"evaluation-protocol-release-{version}.json"
    )
    raw = _load_object(path, code="protocol_release.invalid")
    try:
        release = EvaluationProtocolReleaseV1.model_validate(raw).model_dump(
            mode="json"
        )
    except ValidationError as exc:
        raise ReleaseGovernanceError("protocol_release.invalid") from exc
    if release["protocol_version"] != version:
        raise ReleaseGovernanceError("protocol_release.identity_mismatch")
    if release["release_sha256"] != protocol_release_digest(release):
        raise ReleaseGovernanceError("protocol_release.digest_mismatch")
    return release


def verify_protocol_asset_bytes(protocol_version: str) -> dict[str, Any]:
    """Verify a release against its own immutable assets, not current source code.

    Historical execution still requires the recorded Git commit. This verifies
    its schema, action document and source-manifest bytes independently of the
    active protocol; it neither changes the active version nor grants run trust.
    """
    from .integrity import schema_lock_digest, source_bundle_digest
    from .models import SchemaLockManifestV1, SourceBundleManifestV1

    protocol = load_protocol_release(protocol_version)
    lock_filename = (
        "schema-lock-v1.json"
        if protocol_version == "1.0"
        else f"schema-lock-{protocol_version}.json"
    )
    lock = SchemaLockManifestV1.model_validate(
        _load_object(
            RELEASE_ROOT / lock_filename, code="historical.schema_lock_invalid"
        )
    ).model_dump(mode="json")
    if (
        lock["manifest_sha256"] != schema_lock_digest(lock)
        or protocol["schema_lock_sha256"] != lock["manifest_sha256"]
        or protocol["schema_lock_version"] != lock["lock_version"]
    ):
        raise ReleaseGovernanceError("historical.schema_lock_binding_mismatch")
    by_version = {entry["schema_version"]: entry for entry in lock["entries"]}
    for entry in lock["entries"]:
        payload = _contained(PROJECT_ROOT, entry["relative_path"]).read_bytes()
        document = json.loads(payload)
        if (
            hashlib.sha256(payload).hexdigest() != entry["raw_sha256"]
            or document.get("$id") != entry["schema_id"]
        ):
            raise ReleaseGovernanceError("historical.schema_bytes_changed")
    for binding in protocol["artifact_schemas"]:
        locked = by_version.get(binding["schema_version"], {})
        if any(locked.get(key) != value for key, value in binding.items()):
            raise ReleaseGovernanceError("historical.protocol_schema_binding_mismatch")
    action = _load_object(
        _contained(PROJECT_ROOT, protocol["action_protocol_relative_path"]),
        code="historical.action_invalid",
    )
    if sha256_digest(action) != protocol["action_protocol_sha256"]:
        raise ReleaseGovernanceError("historical.action_binding_mismatch")
    bundle_root = RELEASE_ROOT / "source-bundles"
    if protocol_version != "1.0":
        bundle_root = bundle_root / protocol_version
    for binding in protocol["source_bundles"]:
        bundle = SourceBundleManifestV1.model_validate(
            _load_object(
                bundle_root / f"{binding['component']}.json",
                code="historical.source_bundle_invalid",
            )
        ).model_dump(mode="json")
        if bundle["bundle_sha256"] != source_bundle_digest(bundle) or any(
            bundle[key] != value for key, value in binding.items()
        ):
            raise ReleaseGovernanceError("historical.source_bundle_binding_mismatch")
    return {
        "protocol_release_id": protocol["protocol_release_id"],
        "protocol_release_sha256": protocol["release_sha256"],
        "asset_bytes_verified": True,
        "execution_requires_recorded_commit": True,
    }


def load_production_registry() -> dict[str, Any]:
    """Load only the code-owned production registry; callers cannot replace it."""

    raw = _load_object(PRODUCTION_REGISTRY_PATH, code="trusted_registry.invalid")
    try:
        registry = TrustedBenchmarkRegistryV1.model_validate(raw).model_dump(
            mode="json"
        )
    except ValidationError as exc:
        raise ReleaseGovernanceError("trusted_registry.invalid") from exc
    if registry["registry_version"] != "production-trusted-benchmark-registry-v1":
        raise ReleaseGovernanceError("trusted_registry.not_production")
    if registry["registry_sha256"] != trusted_registry_digest(registry):
        raise ReleaseGovernanceError("trusted_registry.digest_mismatch")
    for entry in registry["entries"]:
        release_raw = _load_object(
            _contained(PROJECT_ROOT, entry["manifest_relative_path"]),
            code="trusted_registry.release_invalid",
        )
        try:
            release = BenchmarkReleaseManifestV1.model_validate(release_raw).model_dump(
                mode="json"
            )
        except ValidationError as exc:
            raise ReleaseGovernanceError("trusted_registry.release_invalid") from exc
        observed_counts = {
            track: sum(item["track"] == track for item in release["cases"])
            for track in ("planning", "intervention", "assessment", "revision")
        }
        if (
            release["manifest_sha256"] != benchmark_release_digest(release)
            or release["release_status"] != "released"
            or entry["benchmark_release_id"] != release["benchmark_release_id"]
            or entry["benchmark_release_sha256"] != release["manifest_sha256"]
            or entry["case_suite_sha256"] != release["case_suite_sha256"]
            or entry["expected_total_cases"] != release["expected_total_cases"]
            or entry["expected_track_counts"] != observed_counts
            or entry["evaluation_protocol_release_id"]
            != release["evaluation_protocol_release_id"]
            or entry["evaluation_protocol_release_sha256"]
            != release["evaluation_protocol_release_sha256"]
        ):
            raise ReleaseGovernanceError("trusted_registry.release_binding_mismatch")
    return registry


def load_registered_benchmark_release(
    benchmark_release_id: str, benchmark_release_sha256: str
) -> dict[str, Any] | None:
    """Resolve a Benchmark only through the fixed production trust registry."""

    registry = load_production_registry()
    entry = next(
        (
            item
            for item in registry["entries"]
            if item["benchmark_release_id"] == benchmark_release_id
            and item["benchmark_release_sha256"] == benchmark_release_sha256
        ),
        None,
    )
    if entry is None:
        return None
    raw = _load_object(
        _contained(PROJECT_ROOT, entry["manifest_relative_path"]),
        code="trusted_registry.release_invalid",
    )
    try:
        return BenchmarkReleaseManifestV1.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        raise ReleaseGovernanceError("trusted_registry.release_invalid") from exc


def load_schema_lock() -> dict[str, Any]:
    """Load and verify the independent repository Schema lock."""

    from .integrity import schema_lock_digest
    from .models import SchemaLockManifestV1

    raw = _load_object(SCHEMA_LOCK_PATH, code="schema_lock.invalid")
    try:
        lock = SchemaLockManifestV1.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        raise ReleaseGovernanceError("schema_lock.invalid") from exc
    if lock["manifest_sha256"] != schema_lock_digest(lock):
        raise ReleaseGovernanceError("schema_lock.digest_mismatch")
    return lock


def schema_lock_reason_codes(
    lock: Mapping[str, Any], *, project_root: Path = PROJECT_ROOT
) -> tuple[str, ...]:
    """Verify every locked Schema against repository bytes and the registry."""

    from .schemas import ACTIVE_SCHEMA_RELATIVE_ROOT, SCHEMA_FILENAMES

    reasons: set[str] = set()
    entries = lock.get("entries")
    if not isinstance(entries, list):
        return ("schema_lock.entries_invalid",)
    expected_versions = set(SCHEMA_FILENAMES)
    observed_versions: set[str] = set()
    observed_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            reasons.add("schema_lock.entry_invalid")
            continue
        version = entry.get("schema_version")
        relative = entry.get("relative_path")
        if not isinstance(version, str) or not isinstance(relative, str):
            reasons.add("schema_lock.entry_invalid")
            continue
        observed_versions.add(version)
        observed_paths.add(relative)
        expected_filename = SCHEMA_FILENAMES.get(version)
        if relative != f"{ACTIVE_SCHEMA_RELATIVE_ROOT}/{expected_filename}":
            reasons.add("schema_lock.path_mismatch")
            continue
        try:
            path = _contained(project_root, relative)
            payload = path.read_bytes()
            schema = json.loads(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ReleaseGovernanceError):
            reasons.add("schema_lock.schema_unreadable")
            continue
        if (
            hashlib.sha256(payload).hexdigest() != entry.get("raw_sha256")
            or not isinstance(schema, Mapping)
            or schema.get("$id") != entry.get("schema_id")
        ):
            reasons.add("schema_lock.digest_mismatch")
    if observed_versions != expected_versions or len(entries) != len(expected_versions):
        reasons.add("schema_lock.inventory_mismatch")
    expected_paths = {
        f"{ACTIVE_SCHEMA_RELATIVE_ROOT}/{filename}"
        for filename in SCHEMA_FILENAMES.values()
    }
    if observed_paths != expected_paths:
        reasons.add("schema_lock.inventory_mismatch")
    return tuple(sorted(reasons))


def protocol_release_reason_codes(
    protocol: Mapping[str, Any], *, project_root: Path = PROJECT_ROOT
) -> tuple[str, ...]:
    """Recompute Protocol Release bindings against current repository bytes."""

    from .action_protocol import (
        ACTION_DECLARATION_PROTOCOL_SHA256,
        ACTION_DECLARATION_PROTOCOL_VERSION,
    )
    from .active_aggregate import AGGREGATOR_VERSION_V3
    from .active_judge import JUDGE_SOURCE_BUNDLE_SHA256_V3
    from .rubric import (
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
    from .rules import RULE_PACK_SHA256_V3, RULE_PACK_VERSION_V3

    reasons: set[str] = set()
    try:
        lock = load_schema_lock()
    except ReleaseGovernanceError:
        lock = None
        reasons.add("protocol.schema_lock_invalid")
    if lock is not None and schema_lock_reason_codes(lock, project_root=project_root):
        reasons.add("protocol.schema_lock_invalid")
    if lock is not None and protocol.get("schema_lock_sha256") != lock.get(
        "manifest_sha256"
    ):
        reasons.add("protocol.schema_lock_mismatch")
    from .schemas import ACTIVE_SCHEMA_RELATIVE_ROOT, SCHEMA_FILENAMES

    schema_root = project_root / ACTIVE_SCHEMA_RELATIVE_ROOT
    schema_bindings = protocol.get("artifact_schemas", [])
    observed_schema_versions = [
        item.get("schema_version")
        for item in schema_bindings
        if isinstance(item, Mapping)
    ]
    if observed_schema_versions != sorted(ACTIVE_PROTOCOL_SCHEMA_VERSIONS):
        reasons.add("protocol.schema_inventory_mismatch")
    for binding in schema_bindings:
        if not isinstance(binding, Mapping):
            reasons.add("protocol.schema_binding_invalid")
            continue
        try:
            payload = _contained(
                project_root, str(binding["relative_path"])
            ).read_bytes()
        except (KeyError, OSError, ReleaseGovernanceError):
            reasons.add("protocol.schema_binding_invalid")
            continue
        expected_filename = SCHEMA_FILENAMES.get(str(binding.get("schema_version")))
        try:
            schema = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            schema = None
        if (
            expected_filename is None
            or binding["relative_path"]
            != f"{ACTIVE_SCHEMA_RELATIVE_ROOT}/{expected_filename}"
            or not str(binding["relative_path"]).startswith(
                schema_root.relative_to(project_root).as_posix() + "/"
            )
            or hashlib.sha256(payload).hexdigest() != binding.get("raw_sha256")
            or not isinstance(schema, Mapping)
            or schema.get("$id") != binding.get("schema_id")
        ):
            reasons.add("protocol.schema_binding_invalid")
    expected_scalars = {
        "canonicalization_version": CANONICALIZATION_VERSION,
        "canonicalization_sha256": CANONICALIZATION_SHA256,
        "action_protocol_version": ACTION_DECLARATION_PROTOCOL_VERSION,
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
        "blinding_policy_version": BLINDING_POLICY_VERSION,
        "blinding_policy_sha256": BLINDING_POLICY_SHA256,
        "evidence_path_policy_version": EVIDENCE_PATH_POLICY_VERSION,
        "evidence_path_policy_sha256": EVIDENCE_PATH_POLICY_SHA256,
    }
    if any(protocol.get(key) != value for key, value in expected_scalars.items()):
        reasons.add("protocol.component_binding_mismatch")
    try:
        from .action_protocol import ACTION_DECLARATION_PROTOCOL_DOCUMENT

        action_path = _contained(
            project_root, str(protocol["action_protocol_relative_path"])
        )
        action_document = _load_object(
            action_path, code="protocol_release.action_protocol_invalid"
        )
        if (
            action_document != ACTION_DECLARATION_PROTOCOL_DOCUMENT
            or sha256_digest(action_document) != protocol["action_protocol_sha256"]
        ):
            reasons.add("protocol.action_protocol_binding_mismatch")
    except (KeyError, ReleaseGovernanceError):
        reasons.add("protocol.action_protocol_binding_mismatch")
    bound_bundles = {
        item.get("component"): item
        for item in protocol.get("source_bundles", [])
        if isinstance(item, Mapping)
    }
    for component in SOURCE_COMPONENTS:
        actual = build_source_bundle(component, project_root=project_root)
        bound = bound_bundles.get(component)
        if not isinstance(bound, Mapping) or (
            bound.get("bundle_version") != actual["bundle_version"]
            or bound.get("bundle_sha256") != actual["bundle_sha256"]
        ):
            reasons.add(f"protocol.source_bundle_{component}_mismatch")
    if (
        JUDGE_SOURCE_BUNDLE_SHA256_V3
        != build_source_bundle("judge", project_root=project_root)["bundle_sha256"]
    ):
        reasons.add("protocol.judge_runtime_bundle_mismatch")
    return tuple(sorted(reasons))


def load_benchmark_release(
    dataset_root: Path, suite: Mapping[str, Any]
) -> dict[str, Any]:
    """Load the Release bound by the active CaseSuite, never an arbitrary CLI path."""

    release_path = _contained(dataset_root, str(suite["benchmark_release_file"]))
    raw = _load_object(release_path, code="benchmark_release.invalid")
    try:
        release = BenchmarkReleaseManifestV1.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        raise ReleaseGovernanceError("benchmark_release.invalid") from exc
    if release["manifest_sha256"] != benchmark_release_digest(release):
        raise ReleaseGovernanceError("benchmark_release.digest_mismatch")
    if (
        suite["benchmark_release_id"] != release["benchmark_release_id"]
        or suite["benchmark_release_sha256"] != release["manifest_sha256"]
    ):
        raise ReleaseGovernanceError("benchmark_release.suite_binding_mismatch")
    return release


def case_bindings(
    dataset_root: Path, case_files: Sequence[str]
) -> list[dict[str, Any]]:
    """Recompute stable ordered Case bindings from authoritative CaseSpecs."""

    bindings: list[dict[str, Any]] = []
    for ordinal, relative in enumerate(case_files, 1):
        path = _contained(dataset_root, relative)
        case = _load_object(path, code="benchmark_release.case_invalid")
        digest = case_spec_digest(case)
        if case.get("case_spec_sha256") != digest:
            raise ReleaseGovernanceError("benchmark_release.case_digest_mismatch")
        bindings.append(
            {
                "ordinal": ordinal,
                "relative_path": relative,
                "case_id": case.get("case_id"),
                "case_spec_sha256": digest,
                "dataset_role": case.get("dataset_role"),
                "split": case.get("split"),
                "track": case.get("track"),
            }
        )
    return bindings


def resource_bindings(
    dataset_root: Path, relative_paths: Sequence[str]
) -> list[dict[str, Any]]:
    """Bind public resource snapshots by exact bytes and declared version."""

    bindings: list[dict[str, Any]] = []
    for relative in sorted(set(relative_paths)):
        path = _contained(dataset_root, relative)
        document = _load_object(path, code="benchmark_release.resource_invalid")
        bindings.append(
            {
                "snapshot_version": document.get("snapshot_version"),
                "relative_path": relative,
                "snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return bindings


def compute_case_suite_sha256(
    cases: Sequence[Mapping[str, Any]], resources: Sequence[Mapping[str, Any]]
) -> str:
    """Hash the fixed ordered Cases and sorted resource bindings."""

    return sha256_digest(
        {
            "rule": "ordered-case-bindings-and-resource-v1",
            "cases": [dict(item) for item in cases],
            "resource_snapshots": [dict(item) for item in resources],
        }
    )


def mutation_lineage_sha256(dataset_root: Path, case_files: Sequence[str]) -> str:
    """Bind private authoring lineage without publishing it to the Judge."""

    lineage: list[dict[str, object]] = []
    for relative in case_files:
        case = _load_object(
            _contained(dataset_root, relative), code="benchmark_release.case_invalid"
        )
        annotations = case.get("private_annotations")
        lineage.append(
            {
                "case_id": case.get("case_id"),
                "private_annotations": annotations,
                "tags": case.get("tags"),
            }
        )
    return sha256_digest(lineage)


def _protocol_schema_set_sha256(protocol: Mapping[str, Any]) -> str:
    return sha256_digest(protocol["artifact_schemas"])


def _expected_protocol_bindings(protocol: Mapping[str, Any]) -> dict[str, str]:
    bundles = {
        item["component"]: item["bundle_sha256"] for item in protocol["source_bundles"]
    }
    return {
        "schema_set_sha256": _protocol_schema_set_sha256(protocol),
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


def _assess_with_registry(
    *,
    dataset_root: Path,
    suite: Mapping[str, Any],
    release: Mapping[str, Any],
    protocol: Mapping[str, Any],
    registry: Mapping[str, Any],
    filtered: bool,
) -> BenchmarkTrustAssessment:
    reasons: set[str] = set()
    release_digest = benchmark_release_digest(release)
    protocol_digest = protocol_release_digest(protocol)
    if release.get("manifest_sha256") != release_digest:
        reasons.add("benchmark.release_digest_mismatch")
    if protocol.get("release_sha256") != protocol_digest:
        reasons.add("benchmark.protocol_digest_mismatch")
    if registry.get("registry_sha256") != trusted_registry_digest(registry):
        reasons.add("benchmark.registry_digest_mismatch")
    try:
        actual_cases = case_bindings(dataset_root, suite["case_files"])
        actual_resources = resource_bindings(
            dataset_root, [str(suite["resource_snapshot_file"])]
        )
        actual_suite_digest = compute_case_suite_sha256(actual_cases, actual_resources)
        actual_lineage = mutation_lineage_sha256(dataset_root, suite["case_files"])
    except (KeyError, ReleaseGovernanceError):
        actual_cases = []
        actual_resources = []
        actual_suite_digest = ""
        actual_lineage = ""
        reasons.add("benchmark.suite_unreadable")

    protocol_match = bool(
        release.get("evaluation_protocol_release_id")
        == protocol.get("protocol_release_id")
        and release.get("evaluation_protocol_release_sha256") == protocol_digest
        and suite.get("evaluation_protocol_release_id")
        == protocol.get("protocol_release_id")
        and suite.get("evaluation_protocol_release_sha256") == protocol_digest
        and release.get("protocol_bindings") == _expected_protocol_bindings(protocol)
    )
    if not protocol_match:
        reasons.add("benchmark.protocol_mismatch")

    observed_track_counts = {
        track: sum(item.get("track") == track for item in actual_cases)
        for track in ("planning", "intervention", "assessment", "revision")
    }
    suite_complete = bool(
        not filtered
        and release.get("cases") == actual_cases
        and release.get("resource_snapshots") == actual_resources
        and release.get("case_suite_sha256") == actual_suite_digest
        and suite.get("case_suite_sha256") == actual_suite_digest
        and suite.get("benchmark_release_id") == release.get("benchmark_release_id")
        and suite.get("benchmark_release_sha256") == release_digest
        and release.get("expected_total_cases") == len(actual_cases)
        and suite.get("benchmark_expected_total_cases") == len(actual_cases)
        and suite.get("benchmark_expected_track_counts") == observed_track_counts
        and release.get("mutation_source_lineage_sha256") == actual_lineage
    )
    if filtered:
        reasons.add("benchmark.posthoc_filter")
    if not suite_complete:
        reasons.add("benchmark.suite_mismatch")

    entries = registry.get("entries")
    release_registered = bool(
        release.get("release_status") == "released"
        and release.get("manifest_sha256") == release_digest
        and protocol.get("release_sha256") == protocol_digest
        and registry.get("registry_sha256") == trusted_registry_digest(registry)
        and isinstance(entries, list)
        and any(
            entry.get("benchmark_release_id") == release.get("benchmark_release_id")
            and entry.get("benchmark_release_sha256") == release_digest
            and entry.get("case_suite_sha256") == release.get("case_suite_sha256")
            and entry.get("expected_total_cases") == release.get("expected_total_cases")
            and entry.get("expected_track_counts") == observed_track_counts
            and entry.get("evaluation_protocol_release_id")
            == protocol.get("protocol_release_id")
            and entry.get("evaluation_protocol_release_sha256") == protocol_digest
            for entry in entries
            if isinstance(entry, Mapping)
        )
    )
    if not release_registered:
        reasons.add("benchmark.release_untrusted")
    trusted = release_registered and protocol_match and suite_complete
    return BenchmarkTrustAssessment(
        benchmark_release_id=str(release.get("benchmark_release_id") or "unknown"),
        benchmark_release_sha256=release_digest,
        protocol_release_id=str(protocol.get("protocol_release_id") or "unknown"),
        protocol_release_sha256=protocol_digest,
        release_registered=release_registered,
        protocol_match=protocol_match,
        suite_complete=suite_complete,
        trusted_benchmark_run=trusted,
        reason_codes=tuple(sorted(reasons)),
    )


def assess_benchmark_release(
    *,
    dataset_root: Path,
    suite: Mapping[str, Any],
    release: Mapping[str, Any],
    filtered: bool,
) -> BenchmarkTrustAssessment:
    """Assess trust using only repository-fixed production release roots."""

    return _assess_with_registry(
        dataset_root=dataset_root,
        suite=suite,
        release=release,
        protocol=load_protocol_release(),
        registry=load_production_registry(),
        filtered=filtered,
    )


def assess_benchmark_release_for_testing(
    *,
    dataset_root: Path,
    suite: Mapping[str, Any],
    release: Mapping[str, Any],
    protocol: Mapping[str, Any],
    registry: Mapping[str, Any],
    filtered: bool = False,
) -> BenchmarkTrustAssessment:
    """Explicit test-only seam; production CLI never imports or accepts it."""

    try:
        validated_registry = TrustedBenchmarkRegistryV1.model_validate(
            registry
        ).model_dump(mode="json")
    except ValidationError as exc:
        raise ReleaseGovernanceError("test_registry.invalid") from exc
    if validated_registry["registry_version"] != (
        "test-only-trusted-benchmark-registry-v1"
    ):
        raise ReleaseGovernanceError("test_registry.production_forbidden")
    if validated_registry["registry_sha256"] != trusted_registry_digest(
        validated_registry
    ):
        raise ReleaseGovernanceError("test_registry.digest_mismatch")
    validated_protocol = EvaluationProtocolReleaseV1.model_validate(
        protocol
    ).model_dump(mode="json")
    validated_release = BenchmarkReleaseManifestV1.model_validate(release).model_dump(
        mode="json"
    )
    return _assess_with_registry(
        dataset_root=dataset_root,
        suite=CaseSuiteManifestV2.model_validate(suite).model_dump(mode="json"),
        release=validated_release,
        protocol=validated_protocol,
        registry=validated_registry,
        filtered=filtered,
    )
