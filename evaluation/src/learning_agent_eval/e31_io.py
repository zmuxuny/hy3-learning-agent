"""Strict digest-joined input loading for the active E3.1 evaluation path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .integrity import (
    artifact_manifest_digest,
    decision_episode_digest,
    judge_reference_digest,
    rule_result_digest,
    runtime_failure_digest,
)
from .models import (
    DecisionEpisodeV3,
    JudgeReferenceV1,
    RuleResultV2,
    RuleRunManifestV2,
    RuntimeFailureV1,
    RuntimeRunManifestV2,
)
from .validator import resolve_evidence_path, validate_dataset, validate_episode


class E31InputError(RuntimeError):
    """A stable, public-safe E3.1 input error."""

    def __init__(self, code: str, stage: str, artifact_id: str, message: str):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.artifact_id = artifact_id
        self.public_message = message

    def as_dict(self) -> dict[str, str]:
        return {
            "status": "error",
            "error_code": self.code,
            "stage": self.stage,
            "episode_id": self.artifact_id,
            "message": self.public_message,
        }


@dataclass(frozen=True, slots=True)
class E31InputBundle:
    episode: dict[str, Any]
    rule_result: dict[str, Any]
    judge_reference: dict[str, Any]


@dataclass(frozen=True, slots=True)
class E31Inputs:
    bundles: tuple[E31InputBundle, ...]
    runtime_failures: dict[str, dict[str, Any]]
    runtime_failure_tracks: dict[str, str]
    runtime_manifest: dict[str, Any]
    rule_manifest: dict[str, Any]


def load_object(path: Path, *, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise E31InputError(
            "document_invalid", "load", "batch", f"{artifact} is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise E31InputError(
            "document_invalid", "load", "batch", f"{artifact} must be an object"
        )
    return value


def input_root(value: str | Path, *, artifact: str) -> Path:
    argument = Path(value).expanduser()
    if argument.is_symlink():
        raise E31InputError(
            "input_invalid", "load", "batch", f"{artifact} cannot be a symlink"
        )
    root = argument.resolve()
    if not root.is_dir():
        raise E31InputError(
            "input_invalid", "load", "batch", f"{artifact} directory is invalid"
        )
    report = validate_dataset(root)
    if not report.ok:
        raise E31InputError(
            "input_invalid", "validate", "batch", f"{artifact} failed validation"
        )
    return root


def _manifest(path: Path, *, model: Any, artifact: str) -> dict[str, Any]:
    document = load_object(path, artifact=artifact)
    try:
        manifest = model.model_validate(document).model_dump(mode="json", by_alias=True)
    except ValueError as exc:
        raise E31InputError(
            "manifest_invalid", "load", "batch", f"{artifact} is invalid"
        ) from exc
    if manifest["manifest_sha256"] != artifact_manifest_digest(manifest):
        raise E31InputError(
            "manifest_digest", "load", "batch", f"{artifact} digest differs"
        )
    return manifest


def _inventory(root: Path, directory: str, expected: set[str]) -> None:
    target = root / directory
    actual = {path.stem for path in target.glob("*.json")} if target.is_dir() else set()
    if actual != expected:
        raise E31InputError(
            "artifact_inventory_mismatch",
            "load",
            "batch",
            f"{directory} inventory differs from its manifest",
        )


def load_e31_inputs(
    *,
    episodes: str | Path,
    rules: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> E31Inputs:
    """Return selected v3/reference/Rule bundles with exhaustive failure metadata."""

    runtime_root = input_root(episodes, artifact="Runtime output")
    rule_root = input_root(rules, artifact="Rule output")
    runtime_manifest = _manifest(
        runtime_root / "run-manifest.json",
        model=RuntimeRunManifestV2,
        artifact="Runtime manifest",
    )
    rule_manifest = _manifest(
        rule_root / "rule-manifest.json",
        model=RuleRunManifestV2,
        artifact="Rule manifest",
    )
    if (
        rule_manifest["input_runtime_manifest_sha256"]
        != runtime_manifest["manifest_sha256"]
        or rule_manifest["invocation_mode"] != runtime_manifest["invocation_mode"]
    ):
        raise E31InputError(
            "manifest_linkage_invalid",
            "validate",
            "batch",
            "Rule manifest does not match the Runtime manifest",
        )
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
    rule_ids = set(rule_manifest["episode_ids"])
    if not rule_ids.issubset(episode_terminals):
        raise E31InputError(
            "episode_inventory_mismatch",
            "validate",
            "batch",
            "Rule Episodes are not a Runtime Episode subset",
        )
    _inventory(runtime_root, "episodes", set(episode_terminals))
    _inventory(runtime_root, "judge-references", set(episode_terminals))
    _inventory(runtime_root, "failures", set(failure_terminals))
    _inventory(rule_root, "rules", rule_ids)

    propagated_failures = set(rule_manifest["runtime_failure_ids"])
    if not propagated_failures.issubset(failure_terminals):
        raise E31InputError(
            "failure_inventory_mismatch",
            "validate",
            "batch",
            "Rule runtime Failures are not a Runtime Failure subset",
        )
    runtime_failures: dict[str, dict[str, Any]] = {}
    failure_tracks: dict[str, str] = {}
    for failure_id in sorted(propagated_failures):
        terminal = failure_terminals[failure_id]
        document = load_object(
            runtime_root / "failures" / f"{failure_id}.json",
            artifact="Runtime Failure",
        )
        try:
            failure = RuntimeFailureV1.model_validate(document).model_dump(
                mode="json", by_alias=True
            )
        except ValueError as exc:
            raise E31InputError(
                "failure_invalid",
                "validate",
                failure_id,
                "Runtime Failure contract is invalid",
            ) from exc
        if (
            failure["failure_sha256"] != runtime_failure_digest(failure)
            or terminal["artifact_sha256"] != failure["failure_sha256"]
            or rule_manifest["input_runtime_failure_digests"].get(failure_id)
            != failure["failure_sha256"]
            or rule_manifest["runtime_failure_tracks"].get(failure_id)
            != terminal["track"]
        ):
            raise E31InputError(
                "failure_linkage_invalid",
                "validate",
                failure_id,
                "Runtime Failure digest linkage differs",
            )
        runtime_failures[failure_id] = failure
        failure_tracks[failure_id] = terminal["track"]

    bundles: list[E31InputBundle] = []
    for episode_id in sorted(rule_ids):
        episode_document = load_object(
            runtime_root / "episodes" / f"{episode_id}.json",
            artifact="DecisionEpisode v3",
        )
        rule_document = load_object(
            rule_root / "rules" / f"{episode_id}.json", artifact="Rule Result v2"
        )
        reference_document = load_object(
            runtime_root / "judge-references" / f"{episode_id}.json",
            artifact="JudgeReference",
        )
        try:
            episode = DecisionEpisodeV3.model_validate(episode_document).model_dump(
                mode="json", by_alias=True
            )
            rule_result = RuleResultV2.model_validate(rule_document).model_dump(
                mode="json", by_alias=True
            )
            reference = JudgeReferenceV1.model_validate(reference_document).model_dump(
                mode="json", by_alias=True
            )
        except ValueError as exc:
            raise E31InputError(
                "input_contract_invalid",
                "validate",
                episode_id,
                "E3.1 input contract is invalid",
            ) from exc
        episode_digest = decision_episode_digest(episode)
        reference_digest = judge_reference_digest(reference)
        result_digest = rule_result_digest(rule_result)
        terminal = episode_terminals[episode_id]
        if (
            validate_episode(episode, source=f"{episode_id}.json")
            or terminal["artifact_sha256"] != episode_digest
            or rule_result["episode_id"] != episode_id
            or rule_result["episode_sha256"] != episode_digest
            or rule_result["result_sha256"] != result_digest
            or reference["reference_sha256"] != reference_digest
            or episode["judge_reference_sha256"] != reference_digest
            or rule_result["judge_reference_sha256"] != reference_digest
            or rule_manifest["input_episode_digests"].get(episode_id) != episode_digest
            or rule_manifest["input_reference_digests"].get(episode_id)
            != reference_digest
            or rule_manifest["rule_result_digests"].get(episode_id) != result_digest
        ):
            raise E31InputError(
                "input_linkage_invalid",
                "validate",
                episode_id,
                "Episode, JudgeReference, and Rule Result linkage differs",
            )
        for check in rule_result["checks"]:
            if any(
                path.startswith("capture.")
                or not resolve_evidence_path(episode, path)[0]
                for path in check["evidence_paths"]
            ):
                raise E31InputError(
                    "rule_evidence_invalid",
                    "validate",
                    episode_id,
                    "Rule Evidence Path does not resolve in DecisionEpisode v3",
                )
        if episode_ids and episode_id not in episode_ids:
            continue
        if track and episode["track"] != track:
            continue
        bundles.append(
            E31InputBundle(
                episode=episode,
                rule_result=rule_result,
                judge_reference=reference,
            )
        )
    if episode_ids and {item.episode["episode_id"] for item in bundles} != episode_ids:
        raise E31InputError(
            "episode_not_found",
            "filter",
            "batch",
            "one or more Episode IDs were not found in both inputs",
        )
    if not bundles:
        raise E31InputError(
            "selection_empty", "filter", "batch", "Episode selection is empty"
        )
    selected_failures = {
        failure_id: failure
        for failure_id, failure in runtime_failures.items()
        if episode_ids is None
        and (track is None or failure_tracks[failure_id] == track)
    }
    return E31Inputs(
        bundles=tuple(bundles),
        runtime_failures=selected_failures,
        runtime_failure_tracks={
            failure_id: failure_tracks[failure_id] for failure_id in selected_failures
        },
        runtime_manifest=runtime_manifest,
        rule_manifest=rule_manifest,
    )
