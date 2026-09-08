from __future__ import annotations

import importlib.util
import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from learning_agent_eval.action_protocol import (
    ACTION_CLASSES,
    ACTION_DECLARATION_PROTOCOL_DOCUMENT,
    ACTION_DECLARATION_PROTOCOL_SHA256,
    canonical_action_declaration,
    parse_action_declaration,
)
from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
from learning_agent_eval.integrity import (
    artifact_manifest_digest,
    benchmark_release_digest,
    case_spec_digest,
    protocol_release_digest,
    source_bundle_digest,
    trusted_registry_digest,
)
from learning_agent_eval.models import (
    ActionDeclarationProtocolV2,
    DecisionEpisodeV4,
)
from learning_agent_eval.release_governance import (
    ACTIVE_PROTOCOL_SCHEMA_VERSIONS,
    assess_benchmark_release,
    assess_benchmark_release_for_testing,
    case_bindings,
    compute_case_suite_sha256,
    load_production_registry,
    load_protocol_release,
    load_schema_lock,
    mutation_lineage_sha256,
    protocol_release_reason_codes,
    resource_bindings,
    schema_lock_reason_codes,
)
from learning_agent_eval.schemas import ACTIVE_SCHEMA_RELATIVE_ROOT, SCHEMA_FILENAMES, schema_documents
from learning_agent_eval.source_bundles import (
    SOURCE_COMPONENTS,
    build_source_bundle,
)
from learning_agent_eval.validator import validate_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation/datasets/decisionbench-v1.1-regression/engineering"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _rehash_suite(suite: dict[str, Any]) -> None:
    suite["manifest_sha256"] = artifact_manifest_digest(suite)


def _trusted_test_release(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    suite = _load(dataset / "manifest.json")
    release = _load(dataset / "benchmark-release.json")
    protocol = load_protocol_release()
    release["release_status"] = "released"
    release["manifest_sha256"] = benchmark_release_digest(release)
    suite["benchmark_release_sha256"] = release["manifest_sha256"]
    _rehash_suite(suite)
    counts = {
        track: sum(item["track"] == track for item in release["cases"])
        for track in ("planning", "intervention", "assessment", "revision")
    }
    registry = {
        "schema_version": "trusted-benchmark-registry-v1",
        "registry_version": "test-only-trusted-benchmark-registry-v1",
        "entries": [
            {
                "benchmark_release_id": release["benchmark_release_id"],
                "benchmark_release_sha256": release["manifest_sha256"],
                "manifest_relative_path": (
                    "evaluation/tests/fixtures/test-only-benchmark-release.json"
                ),
                "case_suite_sha256": release["case_suite_sha256"],
                "expected_total_cases": release["expected_total_cases"],
                "expected_track_counts": counts,
                "evaluation_protocol_release_id": protocol["protocol_release_id"],
                "evaluation_protocol_release_sha256": protocol["release_sha256"],
            }
        ],
        "registry_sha256": "0" * 64,
    }
    registry["registry_sha256"] = trusted_registry_digest(registry)
    return dataset, suite, release, protocol, registry


def _assess_test(
    dataset: Path,
    suite: dict[str, Any],
    release: dict[str, Any],
    protocol: dict[str, Any],
    registry: dict[str, Any],
):
    return assess_benchmark_release_for_testing(
        dataset_root=dataset,
        suite=suite,
        release=release,
        protocol=protocol,
        registry=registry,
    )


def test_production_registry_does_not_trust_engineering_release() -> None:
    registry = load_production_registry()
    assert registry["registry_version"] == "production-trusted-benchmark-registry-v1"
    assert all(entry["benchmark_release_id"] != "decisionbench-v4-engineering-release" for entry in registry["entries"])
    suite = _load(DATASET / "manifest.json")
    release = _load(DATASET / "benchmark-release.json")
    assessment = assess_benchmark_release(
        dataset_root=DATASET,
        suite=suite,
        release=release,
        filtered=False,
    )
    assert assessment.suite_complete is True
    assert assessment.release_registered is False
    assert assessment.trusted_benchmark_run is False
    assert assessment.reason_codes == ("benchmark.release_untrusted",)


def test_release_candidate_refresh_is_disabled_after_trusted_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_path = PROJECT_ROOT / "evaluation" / "scripts" / "build_e312_releases.py"
    spec = importlib.util.spec_from_file_location("e312_release_builder", builder_path)
    assert spec is not None and spec.loader is not None
    release_builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_builder)
    monkeypatch.setattr(
        release_builder,
        "load_production_registry",
        lambda: {"entries": [{"benchmark_release_id": "registered"}]},
    )
    with pytest.raises(RuntimeError, match="trusted_release_refresh_forbidden"):
        release_builder._refresh_untrusted_candidate()


def test_arbitrary_one_case_real_primary_claim_cannot_create_formal_trust(
    tmp_path: Path,
) -> None:
    dataset, suite, release, _protocol, _ = _trusted_test_release(tmp_path)
    relative = suite["case_files"][0]
    case_path = dataset / relative
    case = _load(case_path)
    case["dataset_role"] = "primary_episode"
    case["case_spec_sha256"] = case_spec_digest(case)
    _write(case_path, case)
    suite["case_files"] = [relative]
    cases = case_bindings(dataset, suite["case_files"])
    resources = resource_bindings(dataset, [suite["resource_snapshot_file"]])
    suite_digest = compute_case_suite_sha256(cases, resources)
    track_counts = {
        track: sum(item["track"] == track for item in cases)
        for track in ("planning", "intervention", "assessment", "revision")
    }
    release.update(
        {
            "release_status": "released",
            "cases": cases,
            "case_suite_sha256": suite_digest,
            "partitions": [
                {
                    "dataset_role": role,
                    "case_ids": (
                        [case["case_id"]] if role == "primary_episode" else []
                    ),
                    "expected_track_counts": (
                        track_counts
                        if role == "primary_episode"
                        else {track: 0 for track in track_counts}
                    ),
                }
                for role in (
                    "calibration_output",
                    "engineering_mini",
                    "primary_episode",
                )
            ],
            "expected_total_cases": 1,
            "mutation_source_lineage_sha256": mutation_lineage_sha256(
                dataset, suite["case_files"]
            ),
            "resource_snapshots": resources,
        }
    )
    release["manifest_sha256"] = benchmark_release_digest(release)
    suite.update(
        {
            "benchmark_release_sha256": release["manifest_sha256"],
            "case_suite_sha256": suite_digest,
            "benchmark_expected_total_cases": 1,
            "benchmark_expected_track_counts": track_counts,
        }
    )
    _rehash_suite(suite)
    assessment = assess_benchmark_release(
        dataset_root=dataset,
        suite=suite,
        release=release,
        filtered=False,
    )
    assert assessment.protocol_match is True
    assert assessment.suite_complete is True
    assert assessment.release_registered is False
    assert assessment.trusted_benchmark_run is False


def test_environment_cannot_replace_the_code_owned_production_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_registry = load_production_registry()
    injected = tmp_path / "untrusted-registry.json"
    injected.write_text('{"entries":[{"untrusted":true}]}', encoding="utf-8")
    monkeypatch.setenv("EVALUATION_TRUSTED_REGISTRY", str(injected))
    monkeypatch.setenv("BENCHMARK_RELEASE_REGISTRY", str(injected))
    assert load_production_registry() == original_registry


def test_only_registered_exact_release_can_pass_trust_assessment(tmp_path: Path) -> None:
    dataset, suite, release, protocol, registry = _trusted_test_release(tmp_path)
    assessment = _assess_test(dataset, suite, release, protocol, registry)
    assert assessment.release_registered is True
    assert assessment.protocol_match is True
    assert assessment.suite_complete is True
    assert assessment.trusted_benchmark_run is True
    assert assessment.reason_codes == ()

    unregistered = deepcopy(registry)
    unregistered["entries"] = []
    unregistered["registry_sha256"] = trusted_registry_digest(unregistered)
    rejected = _assess_test(dataset, suite, release, protocol, unregistered)
    assert rejected.suite_complete is True
    assert rejected.release_registered is False
    assert rejected.trusted_benchmark_run is False


def test_test_registry_cannot_be_smuggled_through_production_assessment(
    tmp_path: Path,
) -> None:
    dataset, suite, release, protocol, registry = _trusted_test_release(tmp_path)
    production = deepcopy(registry)
    production["registry_version"] = "production-trusted-benchmark-registry-v1"
    production["registry_sha256"] = trusted_registry_digest(production)
    with pytest.raises(ValueError, match="test_registry.production_forbidden"):
        _assess_test(dataset, suite, release, protocol, production)


def test_release_trust_fails_closed_for_case_and_protocol_mutations(
    tmp_path: Path,
) -> None:
    mutators = (
        "missing_case",
        "extra_case",
        "replacement",
        "content_same_id",
        "split",
        "track_count",
        "resource",
        "protocol",
    )
    for mutation in mutators:
        dataset, suite, release, protocol, registry = _trusted_test_release(
            tmp_path / mutation
        )
        first_relative = suite["case_files"][0]
        first_path = dataset / first_relative
        if mutation == "missing_case":
            suite["case_files"] = suite["case_files"][:-1]
        elif mutation == "extra_case":
            case = _load(first_path)
            case["case_id"] = "case-e312-extra"
            case["case_spec_sha256"] = case_spec_digest(case)
            extra = dataset / "cases" / "case-e312-extra.json"
            _write(extra, case)
            suite["case_files"] = sorted([*suite["case_files"], "cases/case-e312-extra.json"])
        elif mutation == "replacement":
            first_path.write_bytes((dataset / suite["case_files"][1]).read_bytes())
        elif mutation == "content_same_id":
            case = _load(first_path)
            case["runtime_setup"]["trigger"]["objective"] += " changed"
            case["case_spec_sha256"] = case_spec_digest(case)
            _write(first_path, case)
        elif mutation == "split":
            case = _load(first_path)
            case["split"] = "test" if case["split"] == "dev" else "dev"
            case["case_spec_sha256"] = case_spec_digest(case)
            _write(first_path, case)
        elif mutation == "track_count":
            suite["benchmark_expected_track_counts"]["planning"] += 1
        elif mutation == "resource":
            resource = dataset / suite["resource_snapshot_file"]
            payload = _load(resource)
            payload["e312_audit_probe"] = True
            _write(resource, payload)
        elif mutation == "protocol":
            release["evaluation_protocol_release_sha256"] = "f" * 64
            release["manifest_sha256"] = benchmark_release_digest(release)
            suite["benchmark_release_sha256"] = release["manifest_sha256"]
        _rehash_suite(suite)
        assessment = _assess_test(dataset, suite, release, protocol, registry)
        assert assessment.trusted_benchmark_run is False, mutation


def test_duplicate_case_and_posthoc_filter_are_rejected(tmp_path: Path) -> None:
    dataset, suite, release, protocol, registry = _trusted_test_release(tmp_path)
    duplicate = deepcopy(suite)
    duplicate["case_files"].append(duplicate["case_files"][0])
    _rehash_suite(duplicate)
    with pytest.raises(ValueError):
        _assess_test(dataset, duplicate, release, protocol, registry)
    filtered = assess_benchmark_release_for_testing(
        dataset_root=dataset,
        suite=suite,
        release=release,
        protocol=protocol,
        registry=registry,
        filtered=True,
    )
    assert filtered.trusted_benchmark_run is False
    assert "benchmark.posthoc_filter" in filtered.reason_codes


def test_protocol_release_schema_lock_and_source_bundles_recompute() -> None:
    protocol = load_protocol_release()
    assert protocol["release_sha256"] == protocol_release_digest(protocol)
    assert protocol_release_reason_codes(protocol) == ()
    lock = load_schema_lock()
    assert schema_lock_reason_codes(lock) == ()
    assert {item["relative_path"] for item in lock["entries"]} == {
        f"{ACTIVE_SCHEMA_RELATIVE_ROOT}/{path.name}"
        for path in (PROJECT_ROOT / ACTIVE_SCHEMA_RELATIVE_ROOT).glob("*.schema.json")
    }
    statuses = {item["schema_version"]: item["status"] for item in lock["entries"]}
    assert {
        version for version, status in statuses.items() if status == "active_release"
    } == set(ACTIVE_PROTOCOL_SCHEMA_VERSIONS)
    assert {
        version
        for version, status in statuses.items()
        if status == "historical_frozen"
    } == set(SCHEMA_FILENAMES) - set(ACTIVE_PROTOCOL_SCHEMA_VERSIONS)
    for component in SOURCE_COMPONENTS:
        bundle = build_source_bundle(component)
        assert bundle["bundle_sha256"] == source_bundle_digest(bundle)
        paths = [item["relative_path"] for item in bundle["files"]]
        assert paths == sorted(set(paths))
        assert all(not Path(path).is_absolute() for path in paths)

    incomplete = deepcopy(protocol)
    incomplete["artifact_schemas"] = incomplete["artifact_schemas"][:-1]
    incomplete["release_sha256"] = protocol_release_digest(incomplete)
    assert "protocol.schema_inventory_mismatch" in protocol_release_reason_codes(
        incomplete
    )


def test_schema_lock_is_independent_of_generated_schema_agreement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    schema_root = root / "evaluation" / "schemas"
    shutil.copytree(PROJECT_ROOT / "evaluation" / "schemas", schema_root)
    lock = load_schema_lock()
    target = next(
        item for item in lock["entries"] if item["status"] == "historical_frozen"
    )
    path = root / target["relative_path"]
    path.write_bytes(path.read_bytes() + b"\n")
    filename = Path(target["relative_path"]).name
    assert _load(path) == schema_documents()[filename]
    assert schema_lock_reason_codes(lock, project_root=root) == (
        "schema_lock.digest_mismatch",
    )


def test_source_bundle_changes_only_for_its_documented_conservative_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    shutil.copytree(
        PROJECT_ROOT / "evaluation" / "src" / "learning_agent_eval",
        root / "evaluation" / "src" / "learning_agent_eval",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        PROJECT_ROOT / "backend" / "app",
        root / "backend" / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    before_rules = build_source_bundle("rules", project_root=root)["bundle_sha256"]
    before_aggregate = build_source_bundle("aggregate", project_root=root)[
        "bundle_sha256"
    ]
    rules_dependency = root / "evaluation/src/learning_agent_eval/rules.py"
    rules_dependency.write_bytes(rules_dependency.read_bytes() + b"\n")
    assert build_source_bundle("rules", project_root=root)["bundle_sha256"] != (
        before_rules
    )
    assert build_source_bundle("aggregate", project_root=root)["bundle_sha256"] != (
        before_aggregate
    )
    unrelated = root / "docs" / "note.md"
    unrelated.parent.mkdir()
    unrelated.write_text("unrelated", encoding="utf-8")
    after_unrelated = build_source_bundle("aggregate", project_root=root)[
        "bundle_sha256"
    ]
    unrelated.write_text("changed", encoding="utf-8")
    assert build_source_bundle("aggregate", project_root=root)["bundle_sha256"] == (
        after_unrelated
    )

    aggregate_root = tmp_path / "aggregate-project"
    shutil.copytree(
        PROJECT_ROOT / "evaluation" / "src" / "learning_agent_eval",
        aggregate_root / "evaluation" / "src" / "learning_agent_eval",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    aggregate_before = build_source_bundle(
        "aggregate", project_root=aggregate_root
    )["bundle_sha256"]
    aggregate_dependency = (
        aggregate_root / "evaluation/src/learning_agent_eval/active_aggregate.py"
    )
    aggregate_dependency.write_bytes(aggregate_dependency.read_bytes() + b"\n")
    assert build_source_bundle("aggregate", project_root=aggregate_root)[
        "bundle_sha256"
    ] != aggregate_before


def test_tampered_source_and_protocol_manifests_are_rejected(tmp_path: Path) -> None:
    source = _load(PROJECT_ROOT / "evaluation/releases/source-bundles/rules.json")
    source["files"][0]["sha256"] = "f" * 64
    source["bundle_sha256"] = source_bundle_digest(source)
    source_path = tmp_path / "source.json"
    _write(source_path, source)
    assert any(
        issue.code == "source_bundle.binding_mismatch"
        for issue in validate_dataset(source_path).issues
    )

    protocol = load_protocol_release()
    protocol["rule_pack_sha256"] = "f" * 64
    protocol["release_sha256"] = protocol_release_digest(protocol)
    protocol_path = tmp_path / "protocol.json"
    _write(protocol_path, protocol)
    assert any(
        issue.code == "protocol_release.binding_mismatch"
        for issue in validate_dataset(protocol_path).issues
    )


def test_action_dictionary_and_parser_accept_only_semantic_json_variations() -> None:
    canonical_round_trip = json.loads(
        canonical_json_bytes(ACTION_DECLARATION_PROTOCOL_DOCUMENT)
    )
    validated = ActionDeclarationProtocolV2.model_validate(canonical_round_trip)
    assert set(validated.actions) == set(ACTION_CLASSES)
    reordered = deepcopy(canonical_round_trip)
    reordered["actions"] = dict(reversed(list(reordered["actions"].items())))
    assert set(ActionDeclarationProtocolV2.model_validate(reordered).actions) == set(
        ACTION_CLASSES
    )
    assert sha256_digest(ACTION_DECLARATION_PROTOCOL_DOCUMENT) == (
        ACTION_DECLARATION_PROTOCOL_SHA256
    )
    for action in ACTION_CLASSES:
        canonical = canonical_action_declaration([action])
        spaced = (
            " \n<model-action-v2>\n  { \"action_classes\" : [\""
            + action
            + "\"] }\n</model-action-v2>\nPublic result."
        )
        assert parse_action_declaration(canonical) == ("valid", (action,), "")
        assert parse_action_declaration(spaced) == (
            "valid",
            (action,),
            "Public result.",
        )
        meaning = validated.actions[action]
        assert meaning.decision_semantics
        assert meaning.required_fields == ["action_classes"]
        assert meaning.allowed_fields == ["action_classes"]
        assert meaning.forbidden_fields
        assert meaning.boundary
        assert meaning.positive_example
        assert meaning.negative_example
        assert meaning.missing_declaration_policy == (
            "record_as_scoreable_behavior_failure"
        )

    invalid = (
        '<model-action-v2>{"action_classes":["UNKNOWN"]}</model-action-v2>',
        '<model-action-v2>{"action_classes":["WAIT"],"extra":1}</model-action-v2>',
        '<model-action-v2>{"action_classes":["WAIT","WAIT"]}</model-action-v2>',
        (
            '<model-action-v2>{"action_classes":["WAIT","NO_OP"]}'
            "</model-action-v2>"
        ),
        (
            '<model-action-v2>{"action_classes":["WAIT"],'
            '"action_classes":["NO_OP"]}</model-action-v2>'
        ),
        (
            '<model-action-v2>{"action_classes":["WAIT"]}</model-action-v2>'
            '<model-action-v2>{"action_classes":["WAIT"]}</model-action-v2>'
        ),
    )
    assert all(parse_action_declaration(value)[0] == "invalid" for value in invalid)
    assert parse_action_declaration("Public response only.")[0] == "missing"


def test_business_reasoning_field_is_public_but_model_private_reasoning_is_blocked() -> None:
    from learning_agent_eval.privacy import privacy_issues

    assert privacy_issues({"business_record": {"reasoning": "deduction lesson"}}) == []
    protected = {
        "observable_trace": {
            "model_calls": [{"visible_messages": [{"reasoning": "private"}]}]
        }
    }
    assert {issue.code for issue in privacy_issues(protected)} == {
        "privacy.private_reasoning"
    }


def test_individual_active_episode_cannot_claim_formal_capability() -> None:
    episode_path = next((DATASET / "cases").glob("*.json"))
    assert episode_path.is_file()
    schema = DecisionEpisodeV4.model_json_schema()
    formal = schema["$defs"]["ActiveArtifactProvenanceV1"]["properties"][
        "formal_evaluation_result"
    ]
    assert formal["const"] is False
