"""Production E6 registration, review and tamper checks; no real model calls."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.integrity import case_spec_digest
from learning_agent_eval.release_governance import assess_benchmark_release, _assess_with_registry, load_protocol_release, load_production_registry

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "evaluation/scripts"
DATASET = ROOT / "evaluation/datasets/decisionbench-v1-e6-test"


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "e6_freeze_check", SCRIPTS / "check_e6_freeze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registered_e6_inputs_match_review_and_disjoint_lineage(checker):
    # This checker is frozen to the original E6 release; the active method must
    # reject using that historical registration as current formal eligibility.
    with pytest.raises(ValueError, match="release_binding_invalid"):
        checker.check()
    suite = json.loads((DATASET / "manifest.json").read_text())
    release = json.loads((DATASET / "benchmark-release.json").read_text())
    historical = _assess_with_registry(dataset_root=DATASET, suite=suite, release=release,
        protocol=load_protocol_release("1.0"), registry=load_production_registry(), filtered=False)
    assert historical.release_registered and historical.suite_complete
    assert len(release["cases"]) == 48


def test_review_cannot_cover_rehashed_changed_input(checker, tmp_path):
    dataset = tmp_path / "changed"
    shutil.copytree(DATASET, dataset)
    case_path = dataset / "cases/case-0001.json"
    case = json.loads(case_path.read_text())
    case["runtime_setup"]["seed"]["submission_content"] = "替换了已审核的证据"
    case["case_spec_sha256"] = case_spec_digest(case)
    case_path.write_bytes(canonical_json_bytes(case))
    with pytest.raises(ValueError, match="reviewed_case_content_mismatch"):
        checker.check(dataset)


def test_filtered_registered_e6_batch_is_not_trusted():
    suite = json.loads((DATASET / "manifest.json").read_text())
    release = json.loads((DATASET / "benchmark-release.json").read_text())
    trust = assess_benchmark_release(
        dataset_root=DATASET, suite=suite, release=release, filtered=True
    )
    assert trust.release_registered is False
    assert "benchmark.protocol_mismatch" in trust.reason_codes
    assert trust.trusted_benchmark_run is False
    assert "benchmark.posthoc_filter" in trust.reason_codes


def test_tampered_review_digest_is_rejected(checker, tmp_path):
    review = json.loads(checker.REVIEW.read_text())
    review["rows"][0]["rationale"] = "伪造复核理由"
    path = tmp_path / "review.json"
    path.write_bytes(canonical_json_bytes(review))
    with pytest.raises(ValueError, match="review digest mismatch"):
        checker.reviewed_inputs(path)
