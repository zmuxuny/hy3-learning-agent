"""Unique active Rules runner for Evaluation Protocol Release 1.0."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .e3_io import current_git_commit
from .integrity import (
    artifact_manifest_digest,
    decision_episode_digest,
    judge_reference_digest,
    rule_result_digest,
    runtime_failure_digest,
)
from .models import (
    DecisionEpisodeV3,
    DecisionEpisodeV4,
    JudgeReferenceV1,
    JudgeReferenceV2,
    RuleResultV2,
    RuleResultV3,
    RuleRunManifestV2,
    RuleRunManifestV3,
    RuntimeFailureV1,
    RuntimeFailureV2,
    RuntimeRunManifestV2,
    RuntimeRunManifestV3,
)
from .privacy import privacy_issues
from .rules import (
    EVALUATOR_VERSION_V2,
    EVALUATOR_VERSION_V3,
    RULE_IMPLEMENTATION_SHA256_V3,
    RULE_PACK_SHA256_V2,
    RULE_PACK_SHA256_V3,
    RULE_PACK_VERSION_V2,
    RULE_PACK_VERSION_V3,
    evaluate_rules_v2,
    evaluate_rules_v3,
)
from .runtime_metadata import git_worktree_clean
from .source_bundles import SOURCE_BUNDLE_VERSION
from .validator import resolve_evidence_path, validate_dataset, validate_episode


class RuleEvaluationV2Error(RuntimeError):
    """A stable, public-safe Rules v2 failure."""

    def __init__(self, code: str, stage: str, episode_id: str, message: str):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.episode_id = episode_id
        self.public_message = message

    def as_dict(self) -> dict[str, str]:
        return {
            "status": "error",
            "error_code": self.code,
            "stage": self.stage,
            "episode_id": self.episode_id,
            "message": self.public_message,
        }


@dataclass(frozen=True, slots=True)
class RuleEvaluationV2Summary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    runtime_failure_ids: tuple[str, ...]
    output: Path
    failed_episode_ids: tuple[str, ...]
    invalid_episode_ids: tuple[str, ...]
    hard_gate_episode_ids: tuple[str, ...]
    formal_evaluation_result: bool
    protocol_eligible: bool = False
    provider_eligible: bool = False
    trusted_benchmark_run: bool = False


def _load(path: Path, *, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuleEvaluationV2Error(
            "document_invalid", "load", "batch", f"{artifact} is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise RuleEvaluationV2Error(
            "document_invalid", "load", "batch", f"{artifact} must be an object"
        )
    return value


def _input_root(value: str | Path) -> Path:
    argument = Path(value).expanduser()
    if argument.is_symlink():
        raise RuleEvaluationV2Error(
            "input_invalid", "load", "batch", "Runtime output cannot be a symlink"
        )
    root = argument.resolve()
    if not root.is_dir():
        raise RuleEvaluationV2Error(
            "input_invalid", "load", "batch", "Runtime output is invalid"
        )
    report = validate_dataset(root)
    if not report.ok:
        raise RuleEvaluationV2Error(
            "input_invalid", "validate", "batch", "Runtime output failed validation"
        )
    return root


def _runtime_manifest(root: Path, *, active: bool = False) -> dict[str, Any]:
    document = _load(root / "run-manifest.json", artifact="Runtime manifest")
    try:
        model = RuntimeRunManifestV3 if active else RuntimeRunManifestV2
        manifest = model.model_validate(document).model_dump(
            mode="json", by_alias=True
        )
    except ValueError as exc:
        raise RuleEvaluationV2Error(
            "manifest_invalid", "load", "batch", "Runtime manifest is invalid"
        ) from exc
    if manifest["manifest_sha256"] != artifact_manifest_digest(manifest):
        raise RuleEvaluationV2Error(
            "manifest_digest", "load", "batch", "Runtime manifest digest differs"
        )
    return manifest


def _inventory(root: Path, directory: str, expected: set[str]) -> None:
    target = root / directory
    actual = {path.stem for path in target.glob("*.json")} if target.is_dir() else set()
    if actual != expected:
        raise RuleEvaluationV2Error(
            "artifact_inventory_mismatch",
            "load",
            "batch",
            f"Runtime {directory} inventory differs from its manifest",
        )


def _rule_evidence(episode: dict[str, Any], result: dict[str, Any]) -> None:
    for check in result["checks"]:
        for path in check["evidence_paths"]:
            if (
                path.startswith("capture.")
                or not resolve_evidence_path(episode, path)[0]
            ):
                raise RuleEvaluationV2Error(
                    "rule_evidence_invalid",
                    "rules",
                    episode["episode_id"],
                    "Rule Evidence Path does not resolve in DecisionEpisode v3",
                )


def _evaluate_rules(
    *,
    input_path: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
    _contract_version: str = "v3",
) -> RuleEvaluationV2Summary:
    """Validate, evaluate, and atomically publish versioned Rule Results."""

    if _contract_version not in {"v3", "v4"}:
        raise ValueError("unsupported internal Rules contract")
    active = _contract_version == "v4"
    root = _input_root(input_path)
    output_path = Path(output).resolve()
    if output_path.exists():
        raise RuleEvaluationV2Error(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    runtime_manifest = _runtime_manifest(root, active=active)
    episode_terminals = {
        item["artifact_id"]: item
        for item in runtime_manifest["terminals"]
        if item["terminal_kind"] == "episode"
    }
    failure_terminals = {
        item["artifact_id"]: item
        for item in runtime_manifest["terminals"]
        if item["terminal_kind"] == "failure"
    }
    _inventory(root, "episodes", set(episode_terminals))
    _inventory(root, "judge-references", set(episode_terminals))
    _inventory(root, "failures", set(failure_terminals))
    if episode_ids and not episode_ids.issubset(episode_terminals):
        raise RuleEvaluationV2Error(
            "episode_not_found",
            "filter",
            "batch",
            "one or more Episode IDs were not found",
        )

    episodes: list[dict[str, Any]] = []
    references: dict[str, dict[str, Any]] = {}
    for episode_id, terminal in sorted(episode_terminals.items()):
        if episode_ids and episode_id not in episode_ids:
            continue
        if track and terminal["track"] != track:
            continue
        episode_document = _load(
            root / "episodes" / f"{episode_id}.json",
            artifact="DecisionEpisode v4" if active else "DecisionEpisode v3",
        )
        reference_document = _load(
            root / "judge-references" / f"{episode_id}.json",
            artifact="JudgeReference",
        )
        try:
            episode_model = DecisionEpisodeV4 if active else DecisionEpisodeV3
            reference_model = JudgeReferenceV2 if active else JudgeReferenceV1
            episode = episode_model.model_validate(episode_document).model_dump(
                mode="json", by_alias=True
            )
            reference = reference_model.model_validate(reference_document).model_dump(
                mode="json", by_alias=True
            )
        except ValueError as exc:
            raise RuleEvaluationV2Error(
                "input_contract_invalid",
                "validate",
                episode_id,
                "Episode or JudgeReference contract is invalid",
            ) from exc
        if validate_episode(episode, source=f"{episode_id}.json"):
            raise RuleEvaluationV2Error(
                "episode_invalid",
                "validate",
                episode_id,
                "DecisionEpisode evidence is invalid",
            )
        if (
            terminal["artifact_sha256"] != decision_episode_digest(episode)
            or terminal["case_spec_sha256"] != episode["case_spec_sha256"]
            or episode["judge_reference_sha256"] != judge_reference_digest(reference)
            or reference["case_spec_sha256"] != episode["case_spec_sha256"]
            or reference["track"] != episode["track"]
        ):
            raise RuleEvaluationV2Error(
                "input_linkage_invalid",
                "validate",
                episode_id,
                "Episode and JudgeReference digest linkage differs",
            )
        episodes.append(episode)
        references[episode_id] = reference
    failures: dict[str, dict[str, Any]] = {}
    if active or episode_ids is None:
        for failure_id, terminal in sorted(failure_terminals.items()):
            if track and terminal["track"] != track:
                continue
            document = _load(
                root / "failures" / f"{failure_id}.json", artifact="Runtime Failure"
            )
            try:
                failure_model = RuntimeFailureV2 if active else RuntimeFailureV1
                failure = failure_model.model_validate(document).model_dump(
                    mode="json", by_alias=True
                )
            except ValueError as exc:
                raise RuleEvaluationV2Error(
                    "failure_invalid",
                    "validate",
                    failure_id,
                    "Runtime Failure contract is invalid",
                ) from exc
            if (
                terminal["artifact_sha256"] != runtime_failure_digest(failure)
                or terminal["case_spec_sha256"] != failure["case_spec_sha256"]
            ):
                raise RuleEvaluationV2Error(
                    "failure_linkage_invalid",
                    "validate",
                    failure_id,
                    "Runtime Failure digest linkage differs",
                )
            failures[failure_id] = failure

    if not episodes and not failures:
        raise RuleEvaluationV2Error(
            "selection_empty",
            "filter",
            "batch",
            "Episode and Runtime Failure selection is empty",
        )

    episodes.sort(key=lambda item: item["episode_id"])
    local_filter = episode_ids is not None or track is not None
    selection_mode = (
        "adhoc_filter"
        if local_filter
        or (active and runtime_manifest["selection_mode"] == "adhoc_filter")
        else "inherited"
    )
    if active:
        try:
            worktree_clean = git_worktree_clean()
        except RuntimeError as exc:
            raise RuleEvaluationV2Error(
                "git_status_unavailable",
                "prepare",
                "batch",
                "current Git worktree status is unavailable",
            ) from exc
    else:
        worktree_clean = False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e31-rules-stage-", dir=output_path.parent
        )
    )
    results: list[dict[str, Any]] = []
    try:
        (stage / "rules").mkdir()
        for episode in episodes:
            episode_id = episode["episode_id"]
            if active:
                result = evaluate_rules_v3(
                    episode,
                    references[episode_id],
                    input_runtime_manifest_sha256=runtime_manifest["manifest_sha256"],
                    input_runtime_formal_evaluation_result=runtime_manifest[
                        "formal_evaluation_result"
                    ],
                    input_runtime_trusted_benchmark_run=runtime_manifest[
                        "trusted_benchmark_run"
                    ],
                    evaluation_protocol_release_sha256=runtime_manifest[
                        "evaluation_protocol_release_sha256"
                    ],
                    benchmark_release_id=runtime_manifest["benchmark_release_id"],
                    benchmark_release_sha256=runtime_manifest[
                        "benchmark_release_sha256"
                    ],
                    runtime_run_id=runtime_manifest["runtime_run_id"],
                    provider_eligible=runtime_manifest["provider_eligible"],
                    selection_mode=selection_mode,
                    worktree_clean=worktree_clean,
                )
            else:
                result = evaluate_rules_v2(episode, references[episode_id])
            _rule_evidence(episode, result)
            try:
                result_model = RuleResultV3 if active else RuleResultV2
                result = result_model.model_validate(result).model_dump(
                    mode="json", by_alias=True
                )
            except ValueError as exc:
                raise RuleEvaluationV2Error(
                    "rule_contract_invalid",
                    "rules",
                    episode_id,
                    "Rule Result contract is invalid",
                ) from exc
            if result["result_sha256"] != rule_result_digest(result):
                raise RuleEvaluationV2Error(
                    "rule_digest_invalid",
                    "rules",
                    episode_id,
                    "Rule Result digest differs",
                )
            (stage / "rules" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(result)
            )
            results.append(result)

        failure_ids = sorted(failures)
        protocol_eligible = bool(
            active
            and runtime_manifest.get("protocol_eligible")
            and worktree_clean
            and all(result["protocol_eligible"] for result in results)
        )
        provider_eligible = bool(
            active and runtime_manifest.get("provider_eligible")
        )
        trusted = bool(
            active
            and runtime_manifest.get("trusted_benchmark_run")
            and selection_mode == "inherited"
            and protocol_eligible
            and provider_eligible
            and all(result["trusted_benchmark_run"] for result in results)
        )
        formal = (
            False
            if active
            else bool(
                not failures
                and results
                and all(result["formal_evaluation_result"] for result in results)
            )
        )
        common_manifest = {
            "input_runtime_manifest_sha256": runtime_manifest["manifest_sha256"],
            "invocation_mode": runtime_manifest["invocation_mode"],
            "requested_episode_ids": sorted(episode_ids or set()),
            "selected_track": track,
            "episode_ids": [item["episode_id"] for item in results],
            "runtime_failure_ids": failure_ids,
            "input_episode_digests": {
                item["episode_id"]: item["episode_sha256"] for item in results
            },
            "input_reference_digests": {
                item["episode_id"]: item["judge_reference_sha256"] for item in results
            },
            "input_runtime_failure_digests": {
                failure_id: failures[failure_id]["failure_sha256"]
                for failure_id in failure_ids
            },
            "runtime_failure_tracks": {
                failure_id: failure_terminals[failure_id]["track"]
                for failure_id in failure_ids
            },
            "rule_result_digests": {
                item["episode_id"]: item["result_sha256"] for item in results
            },
            "formal_evaluation_result": formal,
            "git_commit": current_git_commit(),
            "manifest_sha256": "0" * 64,
        }
        if active:
            manifest = {
                "schema_version": "rule-run-manifest-v3",
                "runtime_run_id": runtime_manifest["runtime_run_id"],
                "evaluation_protocol_release_id": runtime_manifest[
                    "evaluation_protocol_release_id"
                ],
                "evaluation_protocol_release_sha256": runtime_manifest[
                    "evaluation_protocol_release_sha256"
                ],
                "benchmark_release_id": runtime_manifest["benchmark_release_id"],
                "benchmark_release_sha256": runtime_manifest[
                    "benchmark_release_sha256"
                ],
                "case_suite_sha256": runtime_manifest["case_suite_sha256"],
                "input_episode_schema_version": "decision-episode-v4",
                "input_reference_schema_version": "judge-reference-v2",
                "input_failure_schema_version": "runtime-failure-v2",
                "evaluator_version": EVALUATOR_VERSION_V3,
                "evaluator_implementation_version": SOURCE_BUNDLE_VERSION,
                "evaluator_implementation_sha256": RULE_IMPLEMENTATION_SHA256_V3,
                "rule_pack_version": RULE_PACK_VERSION_V3,
                "rule_pack_sha256": RULE_PACK_SHA256_V3,
                "input_runtime_formal_evaluation_result": runtime_manifest[
                    "formal_evaluation_result"
                ],
                "input_runtime_trusted_benchmark_run": runtime_manifest[
                    "trusted_benchmark_run"
                ],
                "selection_mode": selection_mode,
                "result_formal_evaluation_states": {
                    item["episode_id"]: item["formal_evaluation_result"]
                    for item in results
                },
                "result_trusted_benchmark_states": {
                    item["episode_id"]: item["trusted_benchmark_run"]
                    for item in results
                },
                "protocol_eligible": protocol_eligible,
                "provider_eligible": provider_eligible,
                "trusted_benchmark_run": trusted,
                "worktree_clean": worktree_clean,
                **common_manifest,
            }
        else:
            manifest = {
                "schema_version": "rule-run-manifest-v2",
                "input_episode_schema_version": "decision-episode-v3",
                "input_reference_schema_version": "judge-reference-v1",
                "evaluator_version": EVALUATOR_VERSION_V2,
                "rule_pack_version": RULE_PACK_VERSION_V2,
                "rule_pack_sha256": RULE_PACK_SHA256_V2,
                **common_manifest,
            }
        manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
        try:
            manifest_model = RuleRunManifestV3 if active else RuleRunManifestV2
            manifest_model.model_validate(manifest)
        except ValueError as exc:
            raise RuleEvaluationV2Error(
                "manifest_invalid",
                "publish",
                "batch",
                "Rule Run Manifest is invalid",
            ) from exc
        if privacy_issues({"results": results, "manifest": manifest}, file="rules-v2"):
            raise RuleEvaluationV2Error(
                "privacy_rejected",
                "publish",
                "batch",
                "Rule output failed privacy validation",
            )
        (stage / "rule-manifest.json").write_bytes(canonical_json_bytes(manifest))
        report = validate_dataset(stage)
        if not report.ok:
            raise RuleEvaluationV2Error(
                "output_invalid",
                "publish",
                "batch",
                "Rule output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return RuleEvaluationV2Summary(
        episode_ids=tuple(item["episode_id"] for item in results),
        tracks=tuple(
            [item["track"] for item in episodes]
            + [failure_terminals[item]["track"] for item in sorted(failures)]
        ),
        runtime_failure_ids=tuple(sorted(failures)),
        output=output_path,
        failed_episode_ids=tuple(
            item["episode_id"] for item in results if item["status"] == "fail"
        ),
        invalid_episode_ids=tuple(
            item["episode_id"] for item in results if item["status"] == "invalid_input"
        ),
        hard_gate_episode_ids=tuple(
            item["episode_id"] for item in results if item["hard_gates"]
        ),
        formal_evaluation_result=formal,
        protocol_eligible=protocol_eligible,
        provider_eligible=provider_eligible,
        trusted_benchmark_run=trusted,
    )


def _evaluate_run_rules_historical_v2(
    *,
    input_path: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> RuleEvaluationV2Summary:
    """Test-owned compatibility seam for frozen Rule Result v2 fixtures."""

    return _evaluate_rules(
        input_path=input_path,
        output=output,
        episode_ids=episode_ids,
        track=track,
        _contract_version="v3",
    )


def evaluate_active_rules(
    *,
    input_path: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> RuleEvaluationV2Summary:
    """Evaluate active v4 Runtime output into Rule Result v3."""

    return _evaluate_rules(
        input_path=input_path,
        output=output,
        episode_ids=episode_ids,
        track=track,
        _contract_version="v4",
    )
