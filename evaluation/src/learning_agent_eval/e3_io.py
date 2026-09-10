"""Shared, fail-closed E3 input loading without production imports or subprocesses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .integrity import (
    artifact_manifest_digest,
    decision_episode_digest,
    rule_result_digest,
)
from .models import (
    DecisionEpisodeV2,
    E2RunOutputManifest,
    RuleResultV1,
    RuleRunManifestV1,
)
from .validator import resolve_evidence_path, validate_dataset, validate_episode

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class E3InputError(RuntimeError):
    """A safely renderable E3 control-plane failure."""

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
class E3InputBundle:
    episode: dict[str, Any]
    rule_result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class E3Inputs:
    bundles: tuple[E3InputBundle, ...]
    runtime_manifest: dict[str, Any]
    rule_manifest: dict[str, Any]


def load_object(path: Path, *, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise E3InputError(
            "document_invalid",
            "load",
            "batch",
            f"{artifact} document is invalid",
        ) from exc
    if not isinstance(value, dict):
        raise E3InputError(
            "document_invalid",
            "load",
            "batch",
            f"{artifact} document must be an object",
        )
    return value


def _root(value: str | Path, *, artifact: str) -> Path:
    argument = Path(value).expanduser()
    if argument.is_symlink():
        raise E3InputError(
            "input_invalid",
            "load",
            "batch",
            f"{artifact} directory must not be a symbolic link",
        )
    root = argument.resolve()
    if not root.is_dir():
        raise E3InputError(
            "input_invalid", "load", "batch", f"{artifact} directory is invalid"
        )
    return root


def _manifest(
    path: Path,
    *,
    artifact: str,
    model: type[E2RunOutputManifest | RuleRunManifestV1],
) -> dict[str, Any]:
    document = load_object(path, artifact=artifact)
    try:
        value = model.model_validate(document).model_dump(mode="json", by_alias=True)
    except ValueError as exc:
        raise E3InputError(
            "manifest_invalid", "load", "batch", f"{artifact} manifest is invalid"
        ) from exc
    if value["manifest_sha256"] != artifact_manifest_digest(value):
        raise E3InputError(
            "manifest_digest",
            "load",
            "batch",
            f"{artifact} manifest digest mismatch",
        )
    return value


def _closed_json_inventory(
    root: Path, directory: str, expected_ids: set[str]
) -> list[Path]:
    target = root / directory
    files = sorted(target.glob("*.json")) if target.is_dir() else []
    if {path.stem for path in files} != expected_ids:
        raise E3InputError(
            "artifact_inventory_mismatch",
            "load",
            "batch",
            f"{directory} inventory does not match its manifest",
        )
    return files


def load_e3_inputs(
    *,
    episodes: str | Path,
    rules: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> E3Inputs:
    """Validate v2 Runtime and Rule outputs, then return a selected digest-joined set."""

    episode_root = _root(episodes, artifact="Runtime output")
    rule_root = _root(rules, artifact="Rule output")
    for root, artifact in (
        (episode_root, "Runtime output"),
        (rule_root, "Rule output"),
    ):
        report = validate_dataset(root)
        if not report.ok:
            raise E3InputError(
                "input_invalid",
                "validate",
                "batch",
                f"{artifact} failed validation",
            )
    runtime_manifest = _manifest(
        episode_root / "run-manifest.json",
        artifact="Runtime output",
        model=E2RunOutputManifest,
    )
    rule_manifest = _manifest(
        rule_root / "rule-manifest.json",
        artifact="Rule output",
        model=RuleRunManifestV1,
    )
    runtime_ids = set(runtime_manifest["episode_ids"])
    rule_ids = set(rule_manifest["episode_ids"])
    _closed_json_inventory(episode_root, "episodes", runtime_ids)
    _closed_json_inventory(rule_root, "rules", rule_ids)
    if not rule_ids.issubset(runtime_ids):
        raise E3InputError(
            "episode_inventory_mismatch",
            "validate",
            "batch",
            "Rule Episodes are not a subset of the Runtime output",
        )
    bundles: list[E3InputBundle] = []
    for episode_id in sorted(rule_ids):
        episode = load_object(
            episode_root / "episodes" / f"{episode_id}.json",
            artifact="DecisionEpisode v2",
        )
        if episode_ids and episode_id not in episode_ids:
            continue
        if track and episode.get("track") != track:
            continue
        if episode.get("schema_version") != "decision-episode-v2":
            raise E3InputError(
                "episode_version_invalid",
                "validate",
                episode_id,
                "E3 accepts DecisionEpisode v2 only",
            )
        episode_issues = validate_episode(episode, source=f"{episode_id}.json")
        if episode_issues:
            raise E3InputError(
                "episode_invalid",
                "validate",
                episode_id,
                "DecisionEpisode v2 failed validation",
            )
        try:
            episode = DecisionEpisodeV2.model_validate(episode).model_dump(
                mode="json", by_alias=True
            )
        except ValueError as exc:
            raise E3InputError(
                "episode_invalid",
                "validate",
                episode_id,
                "DecisionEpisode v2 contract is invalid",
            ) from exc
        episode_digest = decision_episode_digest(episode)
        if (
            episode_digest != episode["provenance"]["episode_sha256"]
            or runtime_manifest["episode_digests"].get(episode_id) != episode_digest
            or rule_manifest["input_episode_digests"].get(episode_id) != episode_digest
        ):
            raise E3InputError(
                "episode_digest_mismatch",
                "validate",
                episode_id,
                "DecisionEpisode digest linkage is invalid",
            )
        rule_result = load_object(
            rule_root / "rules" / f"{episode_id}.json", artifact="Rule Result"
        )
        try:
            rule_result = RuleResultV1.model_validate(rule_result).model_dump(
                mode="json", by_alias=True
            )
        except ValueError as exc:
            raise E3InputError(
                "rule_result_invalid",
                "validate",
                episode_id,
                "Rule Result contract is invalid",
            ) from exc
        result_digest = rule_result_digest(rule_result)
        if (
            rule_result["episode_id"] != episode_id
            or rule_result["episode_sha256"] != episode_digest
            or result_digest != rule_result["result_sha256"]
            or rule_manifest["rule_result_digests"].get(episode_id) != result_digest
            or rule_result["evaluator_version"] != rule_manifest["evaluator_version"]
            or rule_result["rule_pack_version"] != rule_manifest["rule_pack_version"]
            or rule_result["rule_pack_sha256"] != rule_manifest["rule_pack_sha256"]
        ):
            raise E3InputError(
                "rule_digest_mismatch",
                "validate",
                episode_id,
                "Rule Result does not match DecisionEpisode v2 and its manifest",
            )
        for check in rule_result["checks"]:
            for evidence_path in check["evidence_paths"]:
                if (
                    evidence_path.startswith("capture.")
                    or not resolve_evidence_path(episode, evidence_path)[0]
                ):
                    raise E3InputError(
                        "rule_evidence_invalid",
                        "validate",
                        episode_id,
                        "Rule Evidence Path does not resolve in DecisionEpisode v2",
                    )
        bundles.append(E3InputBundle(episode=episode, rule_result=rule_result))
    if episode_ids:
        found = {bundle.episode["episode_id"] for bundle in bundles}
        if found != episode_ids:
            raise E3InputError(
                "episode_not_found",
                "filter",
                "batch",
                "one or more Episode IDs were not found in both inputs",
            )
    if not bundles:
        raise E3InputError(
            "selection_empty", "filter", "batch", "Episode selection is empty"
        )
    return E3Inputs(
        bundles=tuple(bundles),
        runtime_manifest=runtime_manifest,
        rule_manifest=rule_manifest,
    )


def current_git_commit() -> str:
    """Read the current Git object without invoking a subprocess."""

    try:
        dot_git = PROJECT_ROOT / ".git"
        if dot_git.is_file():
            line = dot_git.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir: "):
                raise ValueError("invalid gitdir")
            git_dir = (dot_git.parent / line.removeprefix("gitdir: ")).resolve()
        else:
            git_dir = dot_git
        common_file = git_dir / "commondir"
        common_dir = (
            (git_dir / common_file.read_text(encoding="utf-8").strip()).resolve()
            if common_file.is_file() else git_dir
        )
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head.removeprefix("ref: ")
            ref_path = git_dir / ref
            if not ref_path.is_file():
                ref_path = common_dir / ref
            if ref_path.is_file():
                head = ref_path.read_text(encoding="utf-8").strip()
            else:
                packed = common_dir / "packed-refs"
                matches = [
                    line.split(" ", 1)[0]
                    for line in packed.read_text(encoding="utf-8").splitlines()
                    if line.endswith(f" {ref}")
                ]
                head = matches[0] if len(matches) == 1 else ""
    except (OSError, UnicodeError, ValueError) as exc:
        raise E3InputError(
            "git_commit_unavailable",
            "prepare",
            "batch",
            "current Git commit is unavailable",
        ) from exc
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise E3InputError(
            "git_commit_unavailable",
            "prepare",
            "batch",
            "current Git commit is unavailable",
        )
    return head
