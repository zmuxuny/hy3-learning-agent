from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from learning_agent_eval import (
    context_summary_digest,
    decision_episode_digest,
    environment_manifest_digest,
    oracle_envelope_digest,
)
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.validator import validate_dataset, validate_episode

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINI_DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"


def _copy_dataset(tmp_path: Path) -> Path:
    destination = tmp_path / "decisionbench-v1"
    shutil.copytree(MINI_DATASET, destination)
    return destination


def _episode_paths(dataset: Path) -> list[Path]:
    return sorted((dataset / "episodes" / "mini").glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _refresh_digests(episode: dict[str, Any]) -> None:
    context = episode.get("state_before", {}).get("context")
    if isinstance(context, dict) and "context_sha256" in context:
        context["context_sha256"] = context_summary_digest(context)
    environment = episode.get("environment")
    if isinstance(environment, dict) and "manifest_sha256" in environment:
        environment["manifest_sha256"] = environment_manifest_digest(environment)
    oracle = episode.get("oracle")
    if isinstance(oracle, dict) and "envelope_sha256" in oracle:
        oracle["envelope_sha256"] = oracle_envelope_digest(oracle)
    provenance = episode.get("provenance")
    if isinstance(provenance, dict) and "episode_sha256" in provenance:
        provenance["episode_sha256"] = decision_episode_digest(episode)


def _write(path: Path, episode: dict[str, Any], *, refresh: bool = True) -> None:
    if refresh:
        _refresh_digests(episode)
    path.write_text(json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _mutate_first(
    dataset: Path,
    mutation: Callable[[dict[str, Any]], None],
    *,
    refresh: bool = True,
) -> Path:
    path = _episode_paths(dataset)[0]
    episode = _load(path)
    mutation(episode)
    _write(path, episode, refresh=refresh)
    return path


def _codes(dataset: Path) -> set[str]:
    return {issue.code for issue in validate_dataset(dataset).issues}


def test_missing_required_field_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    _mutate_first(dataset, lambda episode: episode.pop("trigger"))

    report = validate_dataset(dataset)
    assert not report.ok
    assert any(
        issue.code == "schema.missing" and issue.path == "$.trigger"
        for issue in report.issues
    )


def test_illegal_track_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    _mutate_first(dataset, lambda episode: episode.__setitem__("track", "teaching"))

    report = validate_dataset(dataset)
    assert not report.ok
    assert any(
        issue.code == "schema.enum" and issue.path == "$.track"
        for issue in report.issues
    )


def test_unknown_field_fails_closed(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    _mutate_first(dataset, lambda episode: episode.__setitem__("future_field", {}))

    report = validate_dataset(dataset)
    assert any(
        issue.code == "schema.unknown_field" and issue.path == "$.future_field"
        for issue in report.issues
    )


def test_invalid_rfc3339_calendar_value_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    _mutate_first(
        dataset,
        lambda episode: episode["trigger"].__setitem__(
            "triggered_at", "2026-13-31T09:00:00+08:00"
        ),
    )

    report = validate_dataset(dataset)
    assert any(
        issue.code == "schema.type" and issue.path == "$.trigger.triggered_at"
        for issue in report.issues
    )


def test_unresolvable_evidence_path_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)

    def mutate(episode: dict[str, Any]) -> None:
        episode["oracle"]["must_satisfy"][0]["evidence_paths"][0] = (
            "state_before.facts.does_not_exist"
        )

    _mutate_first(dataset, mutate)
    report = validate_dataset(dataset)
    assert any(
        issue.code == "oracle.invalid_evidence_path"
        and issue.path.endswith(".evidence_paths[0]")
        for issue in report.issues
    )


def test_wrong_episode_digest_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)

    def mutate(episode: dict[str, Any]) -> None:
        episode["provenance"]["episode_sha256"] = "0" * 64

    _mutate_first(dataset, mutate, refresh=False)
    report = validate_dataset(dataset)
    assert [issue.code for issue in report.issues] == ["digest.episode_mismatch"]


def test_duplicate_episode_logical_id_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)

    def mutate(episode: dict[str, Any]) -> None:
        entities = episode["state_before"]["logical_entities"]
        entities[1]["logical_id"] = entities[0]["logical_id"]

    _mutate_first(dataset, mutate)
    assert "episode.duplicate_logical_id" in _codes(dataset)


def test_dev_test_scenario_family_leakage_fails(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    source = _episode_paths(dataset)[0]
    leaked = copy.deepcopy(_load(source))
    leaked["episode_id"] = "A-MINI-LEAK-001"
    leaked["split"] = "test"
    destination = source.with_name("A-MINI-LEAK-001.json")
    _write(destination, leaked)

    report = validate_dataset(dataset)
    leakage = [issue for issue in report.issues if issue.code == "dataset.split_leakage"]
    assert len(leakage) == 2
    assert {issue.file for issue in leakage} == {
        source.relative_to(dataset).as_posix(),
        destination.relative_to(dataset).as_posix(),
    }


def test_error_ordering_is_stable(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    paths = _episode_paths(dataset)
    first = _load(paths[0])
    first["state_before"]["logical_entities"][1]["logical_id"] = first["state_before"][
        "logical_entities"
    ][0]["logical_id"]
    _write(paths[0], first)
    last = _load(paths[-1])
    last["oracle"]["must_satisfy"][0]["evidence_paths"][0] = (
        "state_before.facts.not_present"
    )
    _write(paths[-1], last)

    first_run = validate_dataset(dataset).issues
    second_run = validate_dataset(dataset).issues
    assert first_run == second_run
    assert first_run == tuple(sorted(first_run, key=lambda issue: issue.sort_key()))
    assert [issue.render() for issue in first_run] == [
        issue.render() for issue in second_run
    ]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("reasoning_content", "private trace", "privacy.private_reasoning"),
        ("api_key", "sk-example-shaped-value-1234", "privacy.credential_field"),
        ("authorization", "Bearer eyJsynthetic-but-shaped", "privacy.credential_field"),
        (
            "note",
            "contact synthetic-e0-probe@" + "gmail.com",
            "privacy.real_email",
        ),
        ("full_name", "Identifying Person", "privacy.personal_identifier_field"),
        (
            "note",
            "identifier " + ("1" * 17) + "X",
            "privacy.personal_identifier_value",
        ),
    ],
)
def test_private_or_sensitive_payload_is_rejected(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    dataset = _copy_dataset(tmp_path)

    def mutate(episode: dict[str, Any]) -> None:
        episode["trigger"]["payload"][field] = value

    _mutate_first(dataset, mutate, refresh=False)
    report = validate_dataset(dataset)
    assert code in {issue.code for issue in report.issues}
    assert all(value not in issue.render() for issue in report.issues)


def test_credential_shaped_value_is_rejected_even_under_generic_key() -> None:
    episode = _load(min((MINI_DATASET / "episodes" / "mini").glob("*.json")))
    secret = "sk-abcdefghijklmnopqrstuvwx"
    episode["trigger"]["payload"]["note"] = secret

    issues = validate_episode(episode)
    assert any(issue.code == "privacy.credential_value" for issue in issues)
    assert all(secret not in issue.render() for issue in issues)


def test_cli_success_output_has_stable_track_order(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli_main(["validate-dataset", "--dataset", str(MINI_DATASET)])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.err == ""
    assert captured.out == (
        "dataset_valid episodes=4 "
        "tracks=planning:1,intervention:1,assessment:1,revision:1\n"
    )


def test_cli_failure_is_nonzero_and_never_echoes_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _copy_dataset(tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwx"

    def mutate(episode: dict[str, Any]) -> None:
        episode["trigger"]["payload"]["note"] = secret

    _mutate_first(dataset, mutate, refresh=False)
    status = cli_main(["validate-dataset", "--dataset", str(dataset)])
    captured = capsys.readouterr()

    assert status == 1
    assert "privacy.credential_value" in captured.err
    assert secret not in captured.err
    assert captured.out == ""


def test_duplicate_json_object_key_fails(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "duplicate.json").write_text(
        '{"schema_version":"decision-episode-v1","schema_version":"decision-episode-v1"}',
        encoding="utf-8",
    )

    report = validate_dataset(dataset)
    assert [issue.code for issue in report.issues] == ["json.duplicate_key"]


def test_dataset_with_only_standalone_manifest_has_no_episodes(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    episode = _load(min((MINI_DATASET / "episodes" / "mini").glob("*.json")))
    (dataset / "environment.json").write_text(
        json.dumps(episode["environment"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = validate_dataset(dataset)
    assert [issue.code for issue in report.issues] == ["dataset.no_episodes"]


def test_nested_dataset_symlink_fails_closed(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (dataset / "linked.json").symlink_to(outside)

    report = validate_dataset(dataset)
    assert [issue.code for issue in report.issues] == ["dataset.nested_symlink"]
