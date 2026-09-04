from __future__ import annotations

import hashlib
import json
from pathlib import Path

from learning_agent_eval.models import SCHEMA_DIALECT
from learning_agent_eval.schemas import schema_documents
from learning_agent_eval.validator import validate_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "evaluation" / "schemas"
MINI_DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"


def test_committed_json_schemas_match_source_models() -> None:
    generated = schema_documents()
    assert set(generated) == {
        "decision-episode-v1.schema.json",
        "decision-episode-v2.schema.json",
        "acceptable-action-envelope-v1.schema.json",
        "environment-manifest-v1.schema.json",
        "rule-result-v1.schema.json",
        "judge-result-v1.schema.json",
        "judge-run-manifest-v1.schema.json",
        "aggregate-result-v1.schema.json",
        "aggregate-track-result-v1.schema.json",
        "aggregate-run-manifest-v1.schema.json",
        "case-spec-v1.schema.json",
        "judge-reference-v1.schema.json",
        "provider-attestation-v1.schema.json",
        "environment-manifest-v2.schema.json",
        "decision-episode-v3.schema.json",
        "runtime-failure-v1.schema.json",
        "runtime-run-manifest-v2.schema.json",
        "case-suite-manifest-v1.schema.json",
        "rule-result-v2.schema.json",
        "rule-run-manifest-v2.schema.json",
        "judge-result-v2.schema.json",
        "judge-run-manifest-v2.schema.json",
        "aggregate-result-v2.schema.json",
        "aggregate-track-result-v2.schema.json",
        "aggregate-run-manifest-v2.schema.json",
        "case-spec-v2.schema.json",
        "judge-reference-v2.schema.json",
        "decision-episode-v4.schema.json",
        "runtime-failure-v2.schema.json",
        "runtime-run-manifest-v3.schema.json",
        "case-suite-manifest-v2.schema.json",
        "rule-result-v3.schema.json",
        "rule-run-manifest-v3.schema.json",
        "judge-result-v3.schema.json",
        "judge-run-manifest-v3.schema.json",
        "aggregate-result-v3.schema.json",
        "aggregate-track-result-v3.schema.json",
        "aggregate-run-manifest-v3.schema.json",
        "action-declaration-protocol-v2.schema.json",
        "benchmark-release-manifest-v1.schema.json",
        "evaluation-protocol-release-v1.schema.json",
        "schema-lock-manifest-v1.schema.json",
        "source-bundle-manifest-v1.schema.json",
        "trusted-benchmark-registry-v1.schema.json",
    }
    for filename, expected in generated.items():
        committed = json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
        assert committed == expected
        assert committed["$schema"] == SCHEMA_DIALECT
        assert committed["additionalProperties"] is False
        for definition in committed.get("$defs", {}).values():
            if isinstance(definition, dict) and "properties" in definition:
                assert definition.get("additionalProperties") is False


def test_three_e0_v1_schema_files_remain_byte_frozen() -> None:
    expected = {
        "decision-episode-v1.schema.json": "741e750b63709de7b00a23f36099c9a4033595a0cd3f69ee327e95516dbc2b6e",
        "acceptable-action-envelope-v1.schema.json": "0ff84c32eeab476630b4a855bab1260e01e2222577942fc3effe49096ff586f1",
        "environment-manifest-v1.schema.json": "bb6be778edfd78d701d98045b5bc453604c056b67eace47290b0ad458f894f67",
    }
    assert {
        name: hashlib.sha256((SCHEMA_ROOT / name).read_bytes()).hexdigest()
        for name in expected
    } == expected


def test_e2_schema_files_remain_byte_frozen() -> None:
    expected = {
        "decision-episode-v2.schema.json": "de766dd1f9111fb542cfad165dc4e4edbe9d6e8fec6e2bd568b2233263ae939d",
        "rule-result-v1.schema.json": "d41fa588bc36251510cb87a7da3a1204114e6f1b551f6c067f6e2b3709e88bd8",
    }
    assert {
        name: hashlib.sha256((SCHEMA_ROOT / name).read_bytes()).hexdigest()
        for name in expected
    } == expected


def test_decision_episode_schema_requires_all_versioned_sections() -> None:
    schema = schema_documents()["decision-episode-v1.schema.json"]
    assert set(schema["required"]) == {
        "schema_version",
        "episode_id",
        "scenario_family_id",
        "track",
        "split",
        "difficulty",
        "tags",
        "trigger",
        "state_before",
        "environment",
        "observable_trace",
        "result",
        "oracle",
        "provenance",
    }


def test_four_manual_protocol_fixtures_validate_with_one_per_track() -> None:
    report = validate_dataset(MINI_DATASET)

    assert report.ok, [issue.render() for issue in report.issues]
    assert report.stats.episodes == 4
    assert dict(report.stats.by_track) == {
        "planning": 1,
        "intervention": 1,
        "assessment": 1,
        "revision": 1,
    }

    episodes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((MINI_DATASET / "episodes" / "mini").glob("*.json"))
    ]
    assert {episode["track"] for episode in episodes} == {
        "planning",
        "intervention",
        "assessment",
        "revision",
    }
    for episode in episodes:
        provenance = episode["provenance"]
        assert provenance["source_type"] == "manual_protocol_fixture"
        assert provenance["construction_method"] == "hand_authored"
        assert provenance["author_role"] == "evaluation_protocol_author"
        assert provenance["dataset_role"] == "protocol_mini_fixture"
        assert provenance["runtime_executed"] is False
        assert provenance["formal_evaluation_result"] is False
        assert provenance["evaluation_status"] == "not_a_formal_model_evaluation"
        assert len(episode["oracle"]["allowed_action_classes"]) >= 2
