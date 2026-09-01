"""Atomic control plane for E2 completeness and deterministic Rules."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .completeness import check_episode_integrity
from .integrity import artifact_manifest_digest
from .models import (
    E2RunOutputManifest,
    IntegrityResultV1,
    RuleResultV1,
    RuleRunManifestV1,
)
from .privacy import privacy_issues
from .rules import (
    EVALUATOR_VERSION,
    RULE_PACK_SHA256,
    RULE_PACK_VERSION,
    evaluate_rules,
)
from .validator import resolve_evidence_path, validate_dataset, validate_episode

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class RuleEvaluationError(RuntimeError):
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
class RuleEvaluationSummary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    output: Path
    failed_episode_ids: tuple[str, ...]
    invalid_episode_ids: tuple[str, ...]
    hard_gate_episode_ids: tuple[str, ...]
    formal_evaluation_result: bool


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuleEvaluationError(
            "document_invalid", "load", "batch", "evaluation document is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise RuleEvaluationError(
            "document_invalid", "load", "batch", "evaluation document must be an object"
        )
    return value


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RuleEvaluationError(
            "git_commit_unavailable",
            "prepare",
            "batch",
            "current Git commit is unavailable",
        )
    return value


def _validate_rule_evidence(
    episode: dict[str, Any], result: dict[str, Any]
) -> None:
    for check in result["checks"]:
        for evidence_path in check["evidence_paths"]:
            if evidence_path.startswith("capture.") or not resolve_evidence_path(
                episode, evidence_path
            )[0]:
                raise RuleEvaluationError(
                    "rule_evidence_invalid",
                    "rules",
                    episode["episode_id"],
                    "Rule Evidence Path does not resolve in DecisionEpisode v2",
                )


def evaluate_run_rules(
    *,
    input_path: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> RuleEvaluationSummary:
    input_argument = Path(input_path).expanduser()
    if input_argument.is_symlink():
        raise RuleEvaluationError(
            "input_invalid", "load", "batch", "Runtime output must not be a symbolic link"
        )
    input_root = input_argument.resolve()
    output_path = Path(output).resolve()
    if output_path.exists():
        raise RuleEvaluationError(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    if not input_root.is_dir():
        raise RuleEvaluationError(
            "input_invalid", "load", "batch", "Runtime output directory is invalid"
        )
    report = validate_dataset(input_root)
    if not report.ok:
        raise RuleEvaluationError(
            "input_invalid", "validate", "batch", "Runtime output failed validation"
        )
    manifest_document = _load(input_root / "run-manifest.json")
    try:
        manifest = E2RunOutputManifest.model_validate(manifest_document).model_dump(
            mode="json", by_alias=True
        )
    except ValueError as exc:
        raise RuleEvaluationError(
            "manifest_invalid", "load", "batch", "E2 Runtime manifest is invalid"
        ) from exc
    if manifest["manifest_sha256"] != artifact_manifest_digest(manifest):
        raise RuleEvaluationError(
            "manifest_digest", "load", "batch", "E2 Runtime manifest digest mismatch"
        )
    episodes: list[dict[str, Any]] = []
    episode_root = input_root / "episodes"
    actual_files = sorted(episode_root.glob("*.json")) if episode_root.is_dir() else []
    if {path.stem for path in actual_files} != set(manifest["episode_ids"]):
        raise RuleEvaluationError(
            "episode_inventory_mismatch",
            "load",
            "batch",
            "Runtime Episode inventory does not match its manifest",
        )
    for path in actual_files:
        episode = _load(path)
        episode_id = str(episode.get("episode_id") or path.stem)
        if episode.get("schema_version") != "decision-episode-v2":
            raise RuleEvaluationError(
                "episode_version_invalid",
                "load",
                episode_id,
                "Rules accept DecisionEpisode v2 only",
            )
        if episode_ids and episode_id not in episode_ids:
            continue
        if track and episode.get("track") != track:
            continue
        if validate_episode(episode, source=path.name):
            raise RuleEvaluationError(
                "episode_invalid",
                "validate",
                episode_id,
                "Runtime Episode failed completeness validation",
            )
        if manifest["episode_digests"].get(episode_id) != episode["provenance"][
            "episode_sha256"
        ]:
            raise RuleEvaluationError(
                "episode_digest_mismatch",
                "validate",
                episode_id,
                "Runtime Episode digest does not match its manifest",
            )
        episodes.append(episode)
    if episode_ids:
        found = {episode["episode_id"] for episode in episodes}
        if found != episode_ids:
            raise RuleEvaluationError(
                "episode_not_found",
                "filter",
                "batch",
                "one or more Episode IDs were not found",
            )
    if not episodes:
        raise RuleEvaluationError(
            "selection_empty", "filter", "batch", "Episode selection is empty"
        )
    episodes.sort(key=lambda item: item["episode_id"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e2-rules-stage-", dir=output_path.parent
        )
    )
    rule_results: list[dict[str, Any]] = []
    integrity_results: list[dict[str, Any]] = []
    try:
        (stage / "rules").mkdir()
        (stage / "integrity").mkdir()
        for episode in episodes:
            episode_id = episode["episode_id"]
            integrity = check_episode_integrity(episode)
            try:
                IntegrityResultV1.model_validate(integrity)
            except ValueError as exc:
                raise RuleEvaluationError(
                    "integrity_contract_invalid",
                    "integrity",
                    episode_id,
                    "Integrity Result contract is invalid",
                ) from exc
            if integrity["status"] != "valid":
                raise RuleEvaluationError(
                    "integrity_failed",
                    "integrity",
                    episode_id,
                    "Invalid Episode cannot enter Rule evaluation",
                )
            rule_result = evaluate_rules(episode)
            _validate_rule_evidence(episode, rule_result)
            try:
                RuleResultV1.model_validate(rule_result)
            except ValueError as exc:
                raise RuleEvaluationError(
                    "rule_contract_invalid",
                    "rules",
                    episode_id,
                    "Rule Result contract is invalid",
                ) from exc
            if privacy_issues(
                {"integrity": integrity, "rules": rule_result}, file=episode_id
            ):
                raise RuleEvaluationError(
                    "privacy_rejected",
                    "rules",
                    episode_id,
                    "Rule artifacts failed privacy checks",
                )
            (stage / "integrity" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(integrity)
            )
            (stage / "rules" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(rule_result)
            )
            integrity_results.append(integrity)
            rule_results.append(rule_result)
        formal = all(
            bool(episode["provenance"]["formal_evaluation_result"])
            for episode in episodes
        )
        rule_manifest = {
            "schema_version": "rule-run-manifest-v1",
            "input_episode_schema_version": "decision-episode-v2",
            "evaluator_version": EVALUATOR_VERSION,
            "rule_pack_version": RULE_PACK_VERSION,
            "rule_pack_sha256": RULE_PACK_SHA256,
            "invocation_mode": manifest["invocation_mode"],
            "formal_evaluation_result": formal,
            "episode_ids": [episode["episode_id"] for episode in episodes],
            "input_episode_digests": {
                episode["episode_id"]: episode["provenance"]["episode_sha256"]
                for episode in episodes
            },
            "rule_result_digests": {
                result["episode_id"]: result["result_sha256"]
                for result in rule_results
            },
            "integrity_result_digests": {
                result["episode_id"]: result["result_sha256"]
                for result in integrity_results
            },
            "git_commit": _git_commit(),
            "manifest_sha256": "0" * 64,
        }
        rule_manifest["manifest_sha256"] = artifact_manifest_digest(rule_manifest)
        try:
            RuleRunManifestV1.model_validate(rule_manifest)
        except ValueError as exc:
            raise RuleEvaluationError(
                "manifest_invalid",
                "publish",
                "batch",
                "Rule output manifest is invalid",
            ) from exc
        (stage / "rule-manifest.json").write_bytes(
            canonical_json_bytes(rule_manifest)
        )
        output_report = validate_dataset(stage)
        if not output_report.ok:
            raise RuleEvaluationError(
                "output_invalid",
                "publish",
                "batch",
                "Rule output failed final validation",
            )
        os.replace(stage, output_path)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    failed = tuple(
        result["episode_id"]
        for result in rule_results
        if result["status"] == "fail"
    )
    invalid = tuple(
        result["episode_id"]
        for result in rule_results
        if result["status"] == "invalid_input"
    )
    hard_gates = tuple(
        result["episode_id"]
        for result in rule_results
        if result["hard_gates"]
    )
    return RuleEvaluationSummary(
        episode_ids=tuple(episode["episode_id"] for episode in episodes),
        tracks=tuple(str(episode["track"]) for episode in episodes),
        output=output_path,
        failed_episode_ids=failed,
        invalid_episode_ids=invalid,
        hard_gate_episode_ids=hard_gates,
        formal_evaluation_result=formal,
    )
