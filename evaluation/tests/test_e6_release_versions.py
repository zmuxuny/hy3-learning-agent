"""Active protocol governance regression; run in a repository-external copy."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from learning_agent_eval.integrity import protocol_release_digest
from learning_agent_eval.models import EvaluationProtocolReleaseV1
from learning_agent_eval.release_governance import (
    ACTIVE_PROTOCOL_VERSION,
    load_production_registry,
    load_protocol_release,
    protocol_release_reason_codes,
    verify_protocol_asset_bytes,
)

ROOT = Path(__file__).resolve().parents[2]


def builder():
    scripts = ROOT / "evaluation/scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "repair_release_builder", scripts / "build_e6_repair_releases.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_protocol_identity_cannot_mix_versions():
    protocol = load_protocol_release()
    protocol["protocol_version"] = "1.0"
    protocol["release_sha256"] = protocol_release_digest(protocol)
    with pytest.raises(ValueError, match="identity and version"):
        EvaluationProtocolReleaseV1.model_validate(protocol)


def test_historical_release_and_all_old_schema_bytes_preserved():
    lock = json.loads((ROOT / "evaluation/releases/schema-lock-v1.json").read_text())
    for entry in lock["entries"]:
        assert (
            hashlib.sha256((ROOT / entry["relative_path"]).read_bytes()).hexdigest()
            == entry["raw_sha256"]
        )
    assert builder().legacy._verify_committed_release() == 0


def test_new_build_does_not_rewrite_registered_release(monkeypatch):
    module = builder()
    protocol = load_protocol_release()
    registry = load_production_registry()
    monkeypatch.setattr(
        module,
        "load_production_registry",
        lambda: {
            "entries": [
                *registry["entries"],
                {"evaluation_protocol_release_id": protocol["protocol_release_id"]},
            ]
        },
    )

    def forbidden_write(*args):
        pytest.fail("registered protocol must never be rewritten")

    monkeypatch.setattr(module, "write", forbidden_write)
    module.build()
    monkeypatch.setattr(
        module,
        "check_active",
        lambda: (_ for _ in ()).throw(RuntimeError("active_protocol_drift")),
    )
    with pytest.raises(RuntimeError, match="active_protocol_drift"):
        module.build()


def test_old_registered_benchmark_cannot_be_rebound():
    module = builder()
    registry = load_production_registry()
    entry = registry["entries"][0]
    root = ROOT / entry["manifest_relative_path"]
    with pytest.raises(RuntimeError, match="registered_benchmark_update_forbidden"):
        module.require_mutable(
            root.parent, "new-identity-does-not-permit-old-path", registry
        )
    with pytest.raises(RuntimeError, match="registered_benchmark_update_forbidden"):
        module.require_mutable(
            ROOT / "evaluation/datasets/new-path",
            entry["benchmark_release_id"],
            registry,
        )


def test_active_release_recomputes_with_new_schema_root():
    protocol = load_protocol_release()
    assert (
        protocol["protocol_release_id"]
        == f"evaluation-protocol-release-{ACTIVE_PROTOCOL_VERSION}"
    )
    assert protocol_release_reason_codes(protocol) == ()
    assert all(
        f"/protocol-{ACTIVE_PROTOCOL_VERSION}/" in e["relative_path"]
        for e in protocol["artifact_schemas"]
    )


def test_stub_runtime_rules_judge_preserve_active_protocol_identity(tmp_path):
    from learning_agent_eval.active_judge import (
        FixedResponseJudgeProviderV3,
        evaluate_active_judges,
    )
    from learning_agent_eval.active_rules import evaluate_active_rules
    from learning_agent_eval.active_runtime import run_active_runtime
    from learning_agent_eval.case_specs import episode_id_for_case
    from learning_agent_eval.validator import validate_dataset

    dataset = (
        ROOT
        / f"evaluation/datasets/decisionbench-v{ACTIVE_PROTOCOL_VERSION}-regression/calibration"
    )
    case = json.loads((dataset / "cases/case-0001.json").read_text())
    episode_id = episode_id_for_case(case)
    runtime, rules, judges = (
        tmp_path / name for name in ("runtime", "rules", "judges")
    )
    run_active_runtime(
        dataset=dataset,
        manifest=dataset / "manifest.json",
        output=runtime,
        episode_ids={episode_id},
        model_mode="stub",
    )
    evaluate_active_rules(input_path=runtime, output=rules)
    payload = json.loads(
        (ROOT / "evaluation/fixtures/e311-fixed-judge-responses-v3.json").read_text()
    )["responses"][0]
    provider = FixedResponseJudgeProviderV3(
        [payload], frozen_time="2026-09-08T00:00:00Z"
    )
    evaluate_active_judges(
        episodes=runtime,
        rules=rules,
        output=judges,
        judge_mode="stub",
        provider=provider,
    )
    protocol = load_protocol_release()
    expected_id, expected_sha = (
        protocol["protocol_release_id"],
        protocol["release_sha256"],
    )
    checked = []
    for directory in (runtime, rules, judges):
        report = validate_dataset(directory)
        assert report.ok, [issue.render() for issue in report.issues]
        for path in directory.rglob("*.json"):
            document = json.loads(path.read_text())
            for record in (document, document.get("provenance", {})):
                if "evaluation_protocol_release_id" in record:
                    assert record["evaluation_protocol_release_id"] == expected_id
                    assert record["evaluation_protocol_release_sha256"] == expected_sha
                    checked.append(document["schema_version"])
    assert {
        "decision-episode-v4",
        "runtime-run-manifest-v3",
        "rule-result-v3",
        "rule-run-manifest-v3",
        "judge-result-v3",
        "judge-run-manifest-v3",
    } <= set(checked)
    judge = json.loads((judges / "judge-results" / f"{episode_id}.json").read_text())
    assert judge["status"] == "complete"


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_historical_protocol_assets_verify_without_active_source_rebinding(version):
    result = verify_protocol_asset_bytes(version)
    assert result["asset_bytes_verified"] is True
    assert result["protocol_release_id"] == f"evaluation-protocol-release-{version}"
    assert result["execution_requires_recorded_commit"] is True
    assert load_protocol_release()["protocol_version"] == ACTIVE_PROTOCOL_VERSION
