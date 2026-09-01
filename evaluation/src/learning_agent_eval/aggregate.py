"""Pure deterministic E3 aggregation with Rule hard gates taking precedence."""

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
from .e3_io import E3InputError, current_git_commit, load_e3_inputs, load_object
from .integrity import (
    aggregate_result_digest,
    artifact_manifest_digest,
    judge_result_digest,
)
from .judge import validate_judge_result
from .models import (
    AggregateResultV1,
    AggregateRunManifestV1,
    AggregateTrackResultV1,
    JudgeResultV1,
    JudgeRunManifestV1,
)
from .privacy import privacy_issues
from .rubric import (
    DIMENSION_WEIGHTS,
    JUDGE_CONFIG_SHA256,
    JUDGE_CONFIG_VERSION,
    JUDGE_PROMPT_SHA256,
    JUDGE_PROMPT_VERSION,
    JUDGE_VERSION,
    REPAIR_LIMIT,
    RUBRIC_SHA256,
    RUBRIC_VERSION,
    TRACK_ANCHOR_SHA256,
    TRACK_ANCHOR_VERSION,
)
from .validator import resolve_evidence_path, validate_dataset

AGGREGATOR_VERSION = "deterministic-aggregator-v1"
TRACK_ORDER = ("planning", "intervention", "assessment", "revision")


class AggregateEvaluationError(E3InputError):
    """A safely renderable aggregation control-plane failure."""


@dataclass(frozen=True, slots=True)
class AggregateEvaluationSummary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    output: Path
    failed_episode_ids: tuple[str, ...]
    invalid_episode_ids: tuple[str, ...]
    judge_error_episode_ids: tuple[str, ...]
    formal_evaluation_result: bool


def _load_judge_manifest(root: Path) -> dict[str, Any]:
    document = load_object(root / "run-manifest.json", artifact="Judge Run Manifest")
    try:
        manifest = JudgeRunManifestV1.model_validate(document).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError as exc:
        raise AggregateEvaluationError(
            "judge_manifest_invalid",
            "load",
            "batch",
            "Judge Run Manifest is invalid",
        ) from exc
    if manifest["manifest_sha256"] != artifact_manifest_digest(manifest):
        raise AggregateEvaluationError(
            "judge_manifest_digest",
            "load",
            "batch",
            "Judge Run Manifest digest mismatch",
        )
    return manifest


def _judge_root(value: str | Path) -> Path:
    argument = Path(value).expanduser()
    if argument.is_symlink():
        raise AggregateEvaluationError(
            "input_invalid",
            "load",
            "batch",
            "Judge output must not be a symbolic link",
        )
    root = argument.resolve()
    if not root.is_dir() or not validate_dataset(root).ok:
        raise AggregateEvaluationError(
            "input_invalid", "validate", "batch", "Judge output failed validation"
        )
    return root


def _load_judge_results(
    *,
    root: Path,
    manifest: Mapping[str, Any],
    bundles: Mapping[str, tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    expected_manifest_metadata = {
        "input_episode_schema_version": "decision-episode-v2",
        "input_rule_schema_version": "rule-result-v1",
        "judge_version": JUDGE_VERSION,
        "judge_config_version": JUDGE_CONFIG_VERSION,
        "judge_config_sha256": JUDGE_CONFIG_SHA256,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "repair_limit": REPAIR_LIMIT,
    }
    if any(
        manifest[key] != expected
        for key, expected in expected_manifest_metadata.items()
    ):
        raise AggregateEvaluationError(
            "judge_manifest_config_mismatch",
            "validate",
            "batch",
            "Judge Run Manifest configuration is unsupported",
        )
    expected_ids = set(manifest["episode_ids"])
    result_root = root / "judge-results"
    files = sorted(result_root.glob("*.json")) if result_root.is_dir() else []
    if {path.stem for path in files} != expected_ids:
        raise AggregateEvaluationError(
            "judge_inventory_mismatch",
            "load",
            "batch",
            "Judge Result inventory does not match its manifest",
        )
    results: dict[str, dict[str, Any]] = {}
    for path in files:
        document = load_object(path, artifact="Judge Result")
        episode_id = str(document.get("episode_id") or path.stem)
        try:
            result = JudgeResultV1.model_validate(document).model_dump(
                mode="json", by_alias=True
            )
        except ValidationError as exc:
            raise AggregateEvaluationError(
                "judge_result_invalid",
                "validate",
                episode_id,
                "Judge Result contract is invalid",
            ) from exc
        if episode_id not in bundles:
            raise AggregateEvaluationError(
                "judge_episode_missing",
                "validate",
                episode_id,
                "Judge Result has no matching Episode and Rule Result",
            )
        episode, rule_result = bundles[episode_id]
        if (
            result["result_sha256"] != judge_result_digest(result)
            or result["result_sha256"]
            != manifest["judge_result_digests"].get(episode_id)
            or result["status"] != manifest["result_statuses"].get(episode_id)
            or result["episode_sha256"]
            != manifest["input_episode_digests"].get(episode_id)
            or result["rule_result_sha256"]
            != manifest["input_rule_result_digests"].get(episode_id)
            or result["blind_input_sha256"]
            != manifest["blind_input_digests"].get(episode_id)
            or result["judge_mode"] != manifest["judge_mode"]
        ):
            raise AggregateEvaluationError(
                "judge_digest_mismatch",
                "validate",
                episode_id,
                "Judge Result digest linkage is invalid",
            )
        errors = validate_judge_result(result, episode=episode, rule_result=rule_result)
        if errors:
            raise AggregateEvaluationError(
                "judge_result_invalid",
                "validate",
                episode_id,
                "Judge Result failed evidence or formal-state validation",
            )
        results[episode_id] = result
    expected_formal = bool(results) and all(
        result["formal_evaluation_result"] for result in results.values()
    )
    if manifest["formal_evaluation_result"] != expected_formal:
        raise AggregateEvaluationError(
            "judge_manifest_formal_mismatch",
            "validate",
            "batch",
            "Judge Run Manifest formal state differs from its results",
        )
    return results


def _rule_failures(rule_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "check_id": check["check_id"],
            "severity": check["severity"],
            "reason_code": check["reason_code"],
            "evidence_paths": check["evidence_paths"],
        }
        for check in rule_result["checks"]
        if check["status"] == "fail"
    ]


def _score_caps(
    *, rule_result: Mapping[str, Any], failures: list[dict[str, Any]]
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
    major_ids = [
        failure["check_id"] for failure in failures if failure["severity"] == "major"
    ]
    if major_ids:
        caps.append(
            {
                "cap_id": "major_rule_fail",
                "maximum_score": 69,
                "source_rule_ids": major_ids,
            }
        )
    return caps


def aggregate_episode(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    judge_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate one already validated digest-linked Episode without side effects."""

    failures = _rule_failures(rule_result)
    result: dict[str, Any] = {
        "schema_version": "aggregate-result-v1",
        "aggregator_version": AGGREGATOR_VERSION,
        "episode_id": episode["episode_id"],
        "track": episode["track"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "judge_result_sha256": judge_result["result_sha256"],
        "rule_failures": failures,
        "actual_hard_gates": list(rule_result["hard_gates"]),
        "judge_suggested_hard_gates": deepcopy(
            judge_result["suggested_hard_gates"]
        ),
        "result_sha256": "0" * 64,
    }
    if rule_result["status"] == "invalid_input":
        status = "invalid_input"
        reason = "rule_invalid_input"
    elif judge_result["status"] == "invalid_input":
        status = "invalid_input"
        reason = str(judge_result["error_code"])
    elif judge_result["status"] == "judge_error":
        status = "judge_error"
        reason = str(judge_result["error_code"])
    else:
        status = "complete"
        reason = None
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
        dimensions: list[dict[str, Any]] = []
        for dimension in judge_result["dimensions"]:
            weight = DIMENSION_WEIGHTS[dimension["dimension_id"]]
            weighted_signal = weight * dimension["level"] / 2
            dimensions.append(
                {
                    "dimension_id": dimension["dimension_id"],
                    "level": dimension["level"],
                    "weight": weight,
                    "weighted_signal": float(weighted_signal),
                    "evidence_paths": list(dimension["evidence_paths"]),
                }
            )
        raw_score = float(sum(item["weighted_signal"] for item in dimensions))
        caps = _score_caps(rule_result=rule_result, failures=failures)
        final_score = min([raw_score, *(float(cap["maximum_score"]) for cap in caps)])
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
    errors = validate_aggregate_result(
        result,
        episode=episode,
        rule_result=rule_result,
        judge_result=judge_result,
    )
    if errors:
        raise AggregateEvaluationError(
            "aggregate_result_invalid",
            "aggregate",
            str(episode["episode_id"]),
            "Aggregate Result failed deterministic validation",
        )
    return result


def validate_aggregate_result(
    result: Mapping[str, Any],
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    judge_result: Mapping[str, Any],
) -> tuple[str, ...]:
    """Recompute every deterministic aggregate field and return stable error codes."""

    errors: set[str] = set()
    try:
        validated = AggregateResultV1.model_validate(result).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError:
        return ("aggregate_contract_invalid",)
    if validated["result_sha256"] != aggregate_result_digest(validated):
        errors.add("aggregate_digest_mismatch")
    if (
        validated["episode_id"] != episode["episode_id"]
        or validated["track"] != episode["track"]
        or validated["episode_sha256"] != episode["provenance"]["episode_sha256"]
        or validated["rule_result_sha256"] != rule_result["result_sha256"]
        or validated["judge_result_sha256"] != judge_result["result_sha256"]
    ):
        errors.add("aggregate_input_linkage_invalid")
    expected_failures = _rule_failures(rule_result)
    expected_caps = _score_caps(rule_result=rule_result, failures=expected_failures)
    if (
        validated["rule_failures"] != expected_failures
        or validated["actual_hard_gates"] != rule_result["hard_gates"]
        or validated["judge_suggested_hard_gates"]
        != judge_result["suggested_hard_gates"]
    ):
        errors.add("aggregate_rule_precedence_invalid")
    for failure in validated["rule_failures"]:
        for path in failure["evidence_paths"]:
            if (
                path.startswith("capture.")
                or not resolve_evidence_path(episode, path)[0]
            ):
                errors.add("aggregate_evidence_invalid")
    if rule_result["status"] == "invalid_input":
        expected_status = "invalid_input"
        expected_reason = "rule_invalid_input"
    elif judge_result["status"] == "invalid_input":
        expected_status = "invalid_input"
        expected_reason = judge_result["error_code"]
    elif judge_result["status"] == "judge_error":
        expected_status = "judge_error"
        expected_reason = judge_result["error_code"]
    else:
        expected_status = "complete"
        expected_reason = None
    if (
        validated["status"] != expected_status
        or validated["invalid_reason_code"] != expected_reason
    ):
        errors.add("aggregate_input_status_invalid")
    if validated["status"] == "complete":
        expected_dimensions: list[dict[str, Any]] = []
        for dimension in judge_result["dimensions"]:
            weight = DIMENSION_WEIGHTS[dimension["dimension_id"]]
            expected_dimensions.append(
                {
                    "dimension_id": dimension["dimension_id"],
                    "level": dimension["level"],
                    "weight": weight,
                    "weighted_signal": float(weight * dimension["level"] / 2),
                    "evidence_paths": dimension["evidence_paths"],
                }
            )
        raw_score = float(sum(item["weighted_signal"] for item in expected_dimensions))
        final_score = min(
            [raw_score, *(float(cap["maximum_score"]) for cap in expected_caps)]
        )
        expected_formal = bool(
            episode["provenance"]["formal_evaluation_result"]
            and rule_result["formal_evaluation_result"]
            and judge_result["formal_evaluation_result"]
        )
        if (
            validated["dimensions"] != expected_dimensions
            or validated["raw_score"] != raw_score
            or validated["applied_caps"] != expected_caps
            or validated["final_score"] != final_score
            or validated["formal_evaluation_result"] != expected_formal
        ):
            errors.add("aggregate_score_invalid")
    elif validated["applied_caps"]:
        errors.add("aggregate_invalid_input_scored")
    if privacy_issues(validated, file="aggregate-result"):
        errors.add("aggregate_privacy_invalid")
    return tuple(sorted(errors))


def _track_result(track: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    results = sorted(results, key=lambda item: item["episode_id"])
    complete = [item for item in results if item["status"] == "complete"]
    invalid = [item for item in results if item["status"] == "invalid_input"]
    judge_errors = [item for item in results if item["status"] == "judge_error"]
    scores = [float(item["final_score"]) for item in complete]
    formal = bool(results) and all(item["formal_evaluation_result"] for item in results)
    document = {
        "schema_version": "aggregate-track-result-v1",
        "aggregator_version": AGGREGATOR_VERSION,
        "track": track,
        "episode_ids": [item["episode_id"] for item in results],
        "complete_episode_ids": [item["episode_id"] for item in complete],
        "failed_episode_ids": [
            item["episode_id"] for item in complete if item["episode_outcome"] == "fail"
        ],
        "invalid_input_episode_ids": [item["episode_id"] for item in invalid],
        "judge_error_episode_ids": [item["episode_id"] for item in judge_errors],
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
        AggregateTrackResultV1.model_validate(document)
    except ValidationError as exc:
        raise AggregateEvaluationError(
            "track_result_invalid",
            "aggregate",
            "batch",
            "Track Aggregate Result is invalid",
        ) from exc
    return document


def aggregate_results(
    *,
    episodes: str | Path,
    rules: str | Path,
    judges: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> AggregateEvaluationSummary:
    """Validate, aggregate, and atomically publish Episode and per-track results."""

    output_path = Path(output).resolve()
    if output_path.exists():
        raise AggregateEvaluationError(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    try:
        inputs = load_e3_inputs(episodes=episodes, rules=rules)
    except E3InputError as exc:
        raise AggregateEvaluationError(
            exc.code, exc.stage, exc.episode_id, exc.public_message
        ) from exc
    judge_root = _judge_root(judges)
    judge_manifest = _load_judge_manifest(judge_root)
    all_bundles = {
        bundle.episode["episode_id"]: (bundle.episode, bundle.rule_result)
        for bundle in inputs.bundles
    }
    judge_results = _load_judge_results(
        root=judge_root,
        manifest=judge_manifest,
        bundles=all_bundles,
    )
    selected_ids = set(judge_results)
    if episode_ids:
        if not episode_ids.issubset(selected_ids):
            raise AggregateEvaluationError(
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
            if all_bundles[episode_id][0]["track"] == track
        }
    if not selected_ids:
        raise AggregateEvaluationError(
            "selection_empty", "filter", "batch", "Episode selection is empty"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e3-aggregate-stage-",
            dir=output_path.parent,
        )
    )
    aggregate_documents: list[dict[str, Any]] = []
    track_documents: list[dict[str, Any]] = []
    try:
        (stage / "episodes").mkdir()
        (stage / "tracks").mkdir()
        for episode_id in sorted(selected_ids):
            episode, rule_result = all_bundles[episode_id]
            document = aggregate_episode(
                episode=episode,
                rule_result=rule_result,
                judge_result=judge_results[episode_id],
            )
            (stage / "episodes" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(document)
            )
            aggregate_documents.append(document)
        by_track: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for document in aggregate_documents:
            by_track[document["track"]].append(document)
        for track_name in TRACK_ORDER:
            if track_name not in by_track:
                continue
            document = _track_result(track_name, by_track[track_name])
            (stage / "tracks" / f"{track_name}.json").write_bytes(
                canonical_json_bytes(document)
            )
            track_documents.append(document)
        formal = bool(aggregate_documents) and all(
            document["formal_evaluation_result"] for document in aggregate_documents
        )
        manifest = {
            "schema_version": "aggregate-run-manifest-v1",
            "aggregator_version": AGGREGATOR_VERSION,
            "input_judge_manifest_sha256": judge_manifest["manifest_sha256"],
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
            "requested_episode_ids": sorted(episode_ids or set()),
            "selected_track": track,
            "episode_ids": [document["episode_id"] for document in aggregate_documents],
            "input_episode_digests": {
                document["episode_id"]: document["episode_sha256"]
                for document in aggregate_documents
            },
            "input_rule_result_digests": {
                document["episode_id"]: document["rule_result_sha256"]
                for document in aggregate_documents
            },
            "input_judge_result_digests": {
                document["episode_id"]: document["judge_result_sha256"]
                for document in aggregate_documents
            },
            "aggregate_result_digests": {
                document["episode_id"]: document["result_sha256"]
                for document in aggregate_documents
            },
            "result_statuses": {
                document["episode_id"]: document["status"]
                for document in aggregate_documents
            },
            "track_result_digests": {
                document["track"]: document["result_sha256"]
                for document in track_documents
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
            AggregateRunManifestV1.model_validate(manifest)
        except ValidationError as exc:
            raise AggregateEvaluationError(
                "manifest_invalid",
                "publish",
                "batch",
                "Aggregate Run Manifest is invalid",
            ) from exc
        if privacy_issues(
            {
                "episodes": aggregate_documents,
                "tracks": track_documents,
                "manifest": manifest,
            },
            file="aggregate-output",
        ):
            raise AggregateEvaluationError(
                "privacy_rejected",
                "publish",
                "batch",
                "Aggregate artifacts failed privacy validation",
            )
        (stage / "run-manifest.json").write_bytes(canonical_json_bytes(manifest))
        output_report = validate_dataset(stage)
        if not output_report.ok:
            raise AggregateEvaluationError(
                "output_invalid",
                "publish",
                "batch",
                "Aggregate output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    failed_ids = tuple(
        document["episode_id"]
        for document in aggregate_documents
        if document["episode_outcome"] == "fail"
    )
    invalid_ids = tuple(
        document["episode_id"]
        for document in aggregate_documents
        if document["status"] == "invalid_input"
    )
    error_ids = tuple(
        document["episode_id"]
        for document in aggregate_documents
        if document["status"] == "judge_error"
    )
    return AggregateEvaluationSummary(
        episode_ids=tuple(document["episode_id"] for document in aggregate_documents),
        tracks=tuple(document["track"] for document in aggregate_documents),
        output=output_path,
        failed_episode_ids=failed_ids,
        invalid_episode_ids=invalid_ids,
        judge_error_episode_ids=error_ids,
        formal_evaluation_result=formal,
    )
