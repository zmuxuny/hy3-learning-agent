"""Pure deterministic aggregation for the active DecisionEpisode v3 path."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .canonical import canonical_json_bytes
from .e3_io import current_git_commit
from .e31_io import E31InputError, load_e31_inputs, load_object
from .integrity import (
    aggregate_result_digest,
    artifact_manifest_digest,
    judge_result_digest,
)
from .judge_v2 import validate_judge_result_v2
from .models import (
    AggregateResultV2,
    AggregateRunManifestV2,
    AggregateTrackResultV2,
    JudgeResultV2,
    JudgeRunManifestV2,
)
from .privacy import privacy_issues
from .rubric import (
    DIMENSION_WEIGHTS,
    JUDGE_CONFIG_SHA256_V2,
    JUDGE_CONFIG_VERSION_V2,
    JUDGE_PROMPT_SHA256_V2,
    JUDGE_PROMPT_VERSION_V2,
    JUDGE_VERSION_V2,
    REPAIR_LIMIT,
    RUBRIC_SHA256,
    RUBRIC_VERSION,
    TRACK_ANCHOR_SHA256,
    TRACK_ANCHOR_VERSION,
)
from .validator import resolve_evidence_path, validate_dataset

AGGREGATOR_VERSION_V2 = "deterministic-aggregator-v2"
TRACK_ORDER = ("planning", "intervention", "assessment", "revision")


class AggregateEvaluationV2Error(E31InputError):
    """A stable, public-safe Aggregate v2 control-plane error."""


@dataclass(frozen=True, slots=True)
class AggregateEvaluationV2Summary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    runtime_failure_ids: tuple[str, ...]
    output: Path
    failed_episode_ids: tuple[str, ...]
    invalid_episode_ids: tuple[str, ...]
    judge_error_episode_ids: tuple[str, ...]
    formal_evaluation_result: bool


def _judge_root(value: str | Path) -> Path:
    argument = Path(value).expanduser()
    if argument.is_symlink():
        raise AggregateEvaluationV2Error(
            "input_invalid", "load", "batch", "Judge output cannot be a symlink"
        )
    root = argument.resolve()
    if not root.is_dir() or not validate_dataset(root).ok:
        raise AggregateEvaluationV2Error(
            "input_invalid", "validate", "batch", "Judge output failed validation"
        )
    return root


def _inventory(root: Path, directory: str, expected: set[str]) -> None:
    target = root / directory
    actual = {path.stem for path in target.glob("*.json")} if target.is_dir() else set()
    if actual != expected:
        raise AggregateEvaluationV2Error(
            "judge_inventory_mismatch",
            "load",
            "batch",
            "Judge Result inventory differs from its manifest",
        )


def _load_judges(
    *,
    root: Path,
    inputs: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    document = load_object(root / "run-manifest.json", artifact="Judge manifest")
    try:
        manifest = JudgeRunManifestV2.model_validate(document).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError as exc:
        raise AggregateEvaluationV2Error(
            "judge_manifest_invalid",
            "load",
            "batch",
            "Judge Run Manifest v2 is invalid",
        ) from exc
    if manifest["manifest_sha256"] != artifact_manifest_digest(manifest):
        raise AggregateEvaluationV2Error(
            "judge_manifest_digest",
            "load",
            "batch",
            "Judge Run Manifest v2 digest differs",
        )
    expected_config = {
        "input_episode_schema_version": "decision-episode-v3",
        "input_rule_schema_version": "rule-result-v2",
        "input_reference_schema_version": "judge-reference-v1",
        "judge_version": JUDGE_VERSION_V2,
        "judge_config_version": JUDGE_CONFIG_VERSION_V2,
        "judge_config_sha256": JUDGE_CONFIG_SHA256_V2,
        "judge_prompt_version": JUDGE_PROMPT_VERSION_V2,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V2,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "repair_limit": REPAIR_LIMIT,
        "input_runtime_manifest_sha256": inputs.runtime_manifest["manifest_sha256"],
        "input_rule_manifest_sha256": inputs.rule_manifest["manifest_sha256"],
    }
    if any(manifest[key] != value for key, value in expected_config.items()):
        raise AggregateEvaluationV2Error(
            "judge_manifest_config_mismatch",
            "validate",
            "batch",
            "Judge Run Manifest v2 configuration or inputs differ",
        )
    bundles = {item.episode["episode_id"]: item for item in inputs.bundles}
    judge_ids = set(manifest["episode_ids"])
    if not judge_ids.issubset(bundles):
        raise AggregateEvaluationV2Error(
            "judge_episode_missing",
            "validate",
            "batch",
            "Judge output contains an unknown Episode",
        )
    _inventory(root, "judge-results", judge_ids)
    for failure_id in manifest["runtime_failure_ids"]:
        if (
            failure_id not in inputs.runtime_failures
            or manifest["input_runtime_failure_digests"].get(failure_id)
            != inputs.runtime_failures[failure_id]["failure_sha256"]
            or manifest["runtime_failure_tracks"].get(failure_id)
            != inputs.runtime_failure_tracks[failure_id]
        ):
            raise AggregateEvaluationV2Error(
                "judge_failure_linkage_invalid",
                "validate",
                failure_id,
                "Judge runtime Failure linkage differs",
            )
    results: dict[str, dict[str, Any]] = {}
    for episode_id in sorted(judge_ids):
        result_document = load_object(
            root / "judge-results" / f"{episode_id}.json",
            artifact="Judge Result v2",
        )
        try:
            result = JudgeResultV2.model_validate(result_document).model_dump(
                mode="json", by_alias=True
            )
        except ValidationError as exc:
            raise AggregateEvaluationV2Error(
                "judge_result_invalid",
                "validate",
                episode_id,
                "Judge Result v2 contract is invalid",
            ) from exc
        bundle = bundles[episode_id]
        if (
            result["result_sha256"] != judge_result_digest(result)
            or result["result_sha256"]
            != manifest["judge_result_digests"].get(episode_id)
            or result["status"] != manifest["result_statuses"].get(episode_id)
            or result["episode_sha256"]
            != manifest["input_episode_digests"].get(episode_id)
            or result["rule_result_sha256"]
            != manifest["input_rule_result_digests"].get(episode_id)
            or result["judge_reference_sha256"]
            != manifest["input_reference_digests"].get(episode_id)
            or result["blind_input_sha256"]
            != manifest["blind_input_digests"].get(episode_id)
            or result["judge_mode"] != manifest["judge_mode"]
            or validate_judge_result_v2(
                result,
                episode=bundle.episode,
                rule_result=bundle.rule_result,
                reference=bundle.judge_reference,
            )
        ):
            raise AggregateEvaluationV2Error(
                "judge_result_invalid",
                "validate",
                episode_id,
                "Judge Result v2 digest, evidence, or input linkage differs",
            )
        results[episode_id] = result
    expected_formal = (
        not manifest["runtime_failure_ids"]
        and bool(results)
        and all(item["formal_evaluation_result"] for item in results.values())
    )
    if manifest["formal_evaluation_result"] != expected_formal:
        raise AggregateEvaluationV2Error(
            "judge_manifest_formal_mismatch",
            "validate",
            "batch",
            "Judge Run Manifest v2 formal state differs from its results",
        )
    return manifest, results


def _rule_failures(rule_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "check_id": check["check_id"],
            "severity": check["severity"],
            "reason_code": check["reason_code"],
            "evidence_paths": list(check["evidence_paths"]),
        }
        for check in rule_result["checks"]
        if check["status"] == "fail"
    ]


def _score_caps(
    rule_result: Mapping[str, Any], failures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    caps: list[dict[str, Any]] = []
    hard_gates = list(rule_result["hard_gates"])
    if hard_gates:
        caps.append(
            {
                "cap_id": "critical_hard_gate",
                "maximum_score": 39,
                "source_rule_ids": hard_gates,
            }
        )
    major_ids = [item["check_id"] for item in failures if item["severity"] == "major"]
    if major_ids:
        caps.append(
            {
                "cap_id": "major_rule_fail",
                "maximum_score": 69,
                "source_rule_ids": major_ids,
            }
        )
    return caps


def _aggregate_document(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    judge_result: Mapping[str, Any],
) -> dict[str, Any]:
    failures = _rule_failures(rule_result)
    result: dict[str, Any] = {
        "schema_version": "aggregate-result-v2",
        "aggregator_version": AGGREGATOR_VERSION_V2,
        "episode_id": episode["episode_id"],
        "track": episode["track"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "judge_result_sha256": judge_result["result_sha256"],
        "judge_reference_sha256": episode["judge_reference_sha256"],
        "rule_failures": failures,
        "actual_hard_gates": list(rule_result["hard_gates"]),
        "judge_suggested_hard_gates": deepcopy(judge_result["suggested_hard_gates"]),
        "result_sha256": "0" * 64,
    }
    if rule_result["status"] == "invalid_input":
        status, reason = "invalid_input", "rule_invalid_input"
    elif judge_result["status"] == "invalid_input":
        status, reason = "invalid_input", judge_result["error_code"]
    elif judge_result["status"] == "judge_error":
        status, reason = "judge_error", judge_result["error_code"]
    else:
        status, reason = "complete", None
    result["status"] = status
    if status != "complete":
        result.update(
            {
                "dimensions": [],
                "raw_score": None,
                "applied_caps": [],
                "final_score": None,
                "episode_outcome": status,
                "formal_evaluation_result": False,
                "evaluation_status": "not_a_formal_model_evaluation",
                "invalid_reason_code": reason,
            }
        )
    else:
        dimensions = []
        for dimension in judge_result["dimensions"]:
            weight = DIMENSION_WEIGHTS[dimension["dimension_id"]]
            dimensions.append(
                {
                    "dimension_id": dimension["dimension_id"],
                    "level": dimension["level"],
                    "weight": weight,
                    "weighted_signal": float(weight * dimension["level"] / 2),
                    "evidence_paths": list(dimension["evidence_paths"]),
                }
            )
        raw_score = float(sum(item["weighted_signal"] for item in dimensions))
        caps = _score_caps(rule_result, failures)
        final_score = min([raw_score, *(float(item["maximum_score"]) for item in caps)])
        formal = bool(
            episode["provenance"]["formal_evaluation_result"]
            and rule_result["formal_evaluation_result"]
            and judge_result["formal_evaluation_result"]
        )
        result.update(
            {
                "dimensions": dimensions,
                "raw_score": raw_score,
                "applied_caps": caps,
                "final_score": final_score,
                "episode_outcome": ("fail" if rule_result["hard_gates"] else "pass"),
                "formal_evaluation_result": formal,
                "evaluation_status": (
                    "formal_model_evaluation"
                    if formal
                    else "not_a_formal_model_evaluation"
                ),
                "invalid_reason_code": None,
            }
        )
    result["result_sha256"] = aggregate_result_digest(result)
    return result


def validate_aggregate_result_v2(
    result: Mapping[str, Any],
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    judge_result: Mapping[str, Any],
) -> tuple[str, ...]:
    """Recompute all deterministic fields and input links."""

    try:
        validated = AggregateResultV2.model_validate(result).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError:
        return ("aggregate_contract_invalid",)
    expected = _aggregate_document(
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )
    errors: set[str] = set()
    if validated["result_sha256"] != aggregate_result_digest(validated):
        errors.add("aggregate_digest_mismatch")
    if validated != expected:
        errors.add("aggregate_recomputation_mismatch")
    for failure in validated["rule_failures"]:
        if any(
            path.startswith("capture.") or not resolve_evidence_path(episode, path)[0]
            for path in failure["evidence_paths"]
        ):
            errors.add("aggregate_evidence_invalid")
    if privacy_issues(validated, file="aggregate-result-v2"):
        errors.add("aggregate_privacy_invalid")
    return tuple(sorted(errors))


def aggregate_episode_v2(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    judge_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate one validated tuple without I/O, model, database, or network."""

    result = _aggregate_document(
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )
    if validate_aggregate_result_v2(
        result,
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    ):
        raise AggregateEvaluationV2Error(
            "aggregate_result_invalid",
            "aggregate",
            str(episode["episode_id"]),
            "Aggregate Result v2 failed deterministic validation",
        )
    return result


def _track_result(
    track: str,
    results: list[dict[str, Any]],
    runtime_failure_ids: list[str],
) -> dict[str, Any]:
    results = sorted(results, key=lambda item: item["episode_id"])
    complete = [item for item in results if item["status"] == "complete"]
    invalid = [item for item in results if item["status"] == "invalid_input"]
    judge_errors = [item for item in results if item["status"] == "judge_error"]
    scores = [float(item["final_score"]) for item in complete]
    formal = (
        bool(results)
        and not runtime_failure_ids
        and all(item["formal_evaluation_result"] for item in results)
    )
    document = {
        "schema_version": "aggregate-track-result-v2",
        "aggregator_version": AGGREGATOR_VERSION_V2,
        "track": track,
        "episode_ids": [item["episode_id"] for item in results],
        "complete_episode_ids": [item["episode_id"] for item in complete],
        "failed_episode_ids": [
            item["episode_id"] for item in complete if item["episode_outcome"] == "fail"
        ],
        "invalid_input_episode_ids": [item["episode_id"] for item in invalid],
        "judge_error_episode_ids": [item["episode_id"] for item in judge_errors],
        "runtime_failure_ids": sorted(runtime_failure_ids),
        "score_count": len(scores),
        "mean_score": round(sum(scores) / len(scores), 6) if scores else None,
        "formal_evaluation_result": formal,
        "evaluation_status": (
            "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
        ),
        "result_sha256": "0" * 64,
    }
    document["result_sha256"] = aggregate_result_digest(document)
    try:
        AggregateTrackResultV2.model_validate(document)
    except ValidationError as exc:
        raise AggregateEvaluationV2Error(
            "track_result_invalid",
            "aggregate",
            track,
            "Track Aggregate Result v2 is invalid",
        ) from exc
    return document


def aggregate_results_v2(
    *,
    episodes: str | Path,
    rules: str | Path,
    judges: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> AggregateEvaluationV2Summary:
    """Validate, aggregate, and atomically publish v3 per-Episode/per-track data."""

    output_path = Path(output).resolve()
    if output_path.exists():
        raise AggregateEvaluationV2Error(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    try:
        inputs = load_e31_inputs(episodes=episodes, rules=rules)
        judge_root = _judge_root(judges)
    except E31InputError as exc:
        raise AggregateEvaluationV2Error(
            exc.code, exc.stage, exc.artifact_id, exc.public_message
        ) from exc
    judge_manifest, judge_results = _load_judges(root=judge_root, inputs=inputs)
    bundles = {item.episode["episode_id"]: item for item in inputs.bundles}
    selected_ids = set(judge_results)
    if episode_ids:
        if not episode_ids.issubset(selected_ids):
            raise AggregateEvaluationV2Error(
                "episode_not_found",
                "filter",
                "batch",
                "one or more Episode IDs were not found in Judge output",
            )
        selected_ids &= episode_ids
    if track:
        selected_ids = {
            episode_id
            for episode_id in selected_ids
            if bundles[episode_id].episode["track"] == track
        }
    if not selected_ids:
        raise AggregateEvaluationV2Error(
            "selection_empty", "filter", "batch", "Episode selection is empty"
        )
    selected_failures = {
        failure_id: inputs.runtime_failures[failure_id]
        for failure_id in judge_manifest["runtime_failure_ids"]
        if episode_ids is None
        and (track is None or inputs.runtime_failure_tracks[failure_id] == track)
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e31-aggregate-stage-",
            dir=output_path.parent,
        )
    )
    documents: list[dict[str, Any]] = []
    tracks: list[dict[str, Any]] = []
    try:
        (stage / "episodes").mkdir()
        (stage / "tracks").mkdir()
        for episode_id in sorted(selected_ids):
            bundle = bundles[episode_id]
            result = aggregate_episode_v2(
                episode=bundle.episode,
                rule_result=bundle.rule_result,
                judge_result=judge_results[episode_id],
            )
            (stage / "episodes" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(result)
            )
            documents.append(result)
        by_track: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for result in documents:
            by_track[result["track"]].append(result)
        failures_by_track: dict[str, list[str]] = defaultdict(list)
        for failure_id in selected_failures:
            failures_by_track[inputs.runtime_failure_tracks[failure_id]].append(
                failure_id
            )
        for track_name in TRACK_ORDER:
            if track_name not in by_track and track_name not in failures_by_track:
                continue
            result = _track_result(
                track_name,
                by_track[track_name],
                failures_by_track[track_name],
            )
            (stage / "tracks" / f"{track_name}.json").write_bytes(
                canonical_json_bytes(result)
            )
            tracks.append(result)
        formal = (
            not selected_failures
            and bool(documents)
            and all(item["formal_evaluation_result"] for item in documents)
        )
        failure_ids = sorted(selected_failures)
        manifest = {
            "schema_version": "aggregate-run-manifest-v2",
            "aggregator_version": AGGREGATOR_VERSION_V2,
            "judge_version": judge_manifest["judge_version"],
            "judge_config_version": judge_manifest["judge_config_version"],
            "judge_config_sha256": judge_manifest["judge_config_sha256"],
            "judge_prompt_version": judge_manifest["judge_prompt_version"],
            "judge_prompt_sha256": judge_manifest["judge_prompt_sha256"],
            "rubric_version": judge_manifest["rubric_version"],
            "rubric_sha256": judge_manifest["rubric_sha256"],
            "track_anchor_version": judge_manifest["track_anchor_version"],
            "track_anchor_sha256": judge_manifest["track_anchor_sha256"],
            "judge_mode": judge_manifest["judge_mode"],
            "input_judge_manifest_sha256": judge_manifest["manifest_sha256"],
            "requested_episode_ids": sorted(episode_ids or set()),
            "selected_track": track,
            "episode_ids": [item["episode_id"] for item in documents],
            "runtime_failure_ids": failure_ids,
            "input_episode_digests": {
                item["episode_id"]: item["episode_sha256"] for item in documents
            },
            "input_rule_result_digests": {
                item["episode_id"]: item["rule_result_sha256"] for item in documents
            },
            "input_judge_result_digests": {
                item["episode_id"]: item["judge_result_sha256"] for item in documents
            },
            "input_reference_digests": {
                item["episode_id"]: item["judge_reference_sha256"] for item in documents
            },
            "input_runtime_failure_digests": {
                failure_id: selected_failures[failure_id]["failure_sha256"]
                for failure_id in failure_ids
            },
            "runtime_failure_tracks": {
                failure_id: inputs.runtime_failure_tracks[failure_id]
                for failure_id in failure_ids
            },
            "aggregate_result_digests": {
                item["episode_id"]: item["result_sha256"] for item in documents
            },
            "result_statuses": {
                item["episode_id"]: item["status"] for item in documents
            },
            "track_result_digests": {
                item["track"]: item["result_sha256"] for item in tracks
            },
            "formal_evaluation_result": formal,
            "evaluation_status": (
                "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
            ),
            "git_commit": current_git_commit(),
            "manifest_sha256": "0" * 64,
        }
        manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
        try:
            AggregateRunManifestV2.model_validate(manifest)
        except ValidationError as exc:
            raise AggregateEvaluationV2Error(
                "manifest_invalid",
                "publish",
                "batch",
                "Aggregate Run Manifest v2 is invalid",
            ) from exc
        if privacy_issues(
            {"episodes": documents, "tracks": tracks, "manifest": manifest},
            file="aggregate-output-v2",
        ):
            raise AggregateEvaluationV2Error(
                "privacy_rejected",
                "publish",
                "batch",
                "Aggregate v2 artifacts failed privacy validation",
            )
        (stage / "run-manifest.json").write_bytes(canonical_json_bytes(manifest))
        if not validate_dataset(stage).ok:
            raise AggregateEvaluationV2Error(
                "output_invalid",
                "publish",
                "batch",
                "Aggregate v2 output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return AggregateEvaluationV2Summary(
        episode_ids=tuple(item["episode_id"] for item in documents),
        tracks=tuple(item["track"] for item in documents),
        runtime_failure_ids=tuple(failure_ids),
        output=output_path,
        failed_episode_ids=tuple(
            item["episode_id"]
            for item in documents
            if item["episode_outcome"] == "fail"
        ),
        invalid_episode_ids=tuple(
            item["episode_id"]
            for item in documents
            if item["status"] == "invalid_input"
        ),
        judge_error_episode_ids=tuple(
            item["episode_id"] for item in documents if item["status"] == "judge_error"
        ),
        formal_evaluation_result=formal,
    )
