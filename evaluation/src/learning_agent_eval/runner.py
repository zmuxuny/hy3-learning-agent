"""Pure control plane for isolated Runtime execution and v2 publication."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, sha256_digest
from .isolation import EvaluationIsolationError, worker_environment
from .models import (
    E1CaptureArtifact,
    E1RunManifest,
    E2RunOutputManifest,
    RuntimeMiniFixture,
)
from .privacy import privacy_issues
from .validator import validate_dataset, validate_episode

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class RunAgentError(RuntimeError):
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


@dataclass(frozen=True)
class RunAgentSummary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    invocation_mode: str
    output: Path
    worker_roots: tuple[str, ...]


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunAgentError("document_invalid", "load", "batch", "evaluation document is invalid") from exc
    if not isinstance(value, dict):
        raise RunAgentError("document_invalid", "load", "batch", "evaluation document must be an object")
    return value


def _contained_file(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise RunAgentError("path_outside_dataset", "load", "batch", "manifest paths must be relative")
    candidate = root
    for component in relative_path.parts:
        candidate /= component
        if candidate.is_symlink():
            raise RunAgentError(
                "manifest_file_symlink",
                "load",
                "batch",
                "manifest files must not use symbolic links",
            )
    path = candidate.resolve()
    if path != root and root not in path.parents:
        raise RunAgentError("path_outside_dataset", "load", "batch", "manifest path escapes dataset")
    if path.is_symlink() or not path.is_file():
        raise RunAgentError("manifest_file_missing", "load", "batch", "manifest file is missing")
    return path


def _git_commit(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise RunAgentError("git_commit_unavailable", "prepare", "batch", "current Git commit is unavailable")
    return value


def _worker_error(completed: subprocess.CompletedProcess[str], episode_id: str) -> RunAgentError:
    try:
        document = json.loads(completed.stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        document = {}
    return RunAgentError(
        str(document.get("error_code") or "worker_failed"),
        str(document.get("stage") or "worker"),
        episode_id,
        str(document.get("message") or "isolated evaluation worker failed"),
    )


def run_agent(
    *,
    dataset: str | Path,
    manifest: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
    model_mode: str = "stub",
    allow_real_model: bool = False,
    worker_timeout_seconds: float = 180.0,
) -> RunAgentSummary:
    dataset_argument = Path(dataset).expanduser()
    manifest_argument = Path(manifest).expanduser()
    if dataset_argument.is_symlink():
        raise RunAgentError("dataset_invalid", "load", "batch", "dataset directory is invalid")
    if manifest_argument.is_symlink():
        raise RunAgentError("manifest_invalid", "load", "batch", "run manifest must not be a symbolic link")
    dataset_root = dataset_argument.resolve()
    manifest_path = manifest_argument.resolve()
    output_path = Path(output).resolve()
    if output_path.exists():
        raise RunAgentError("output_exists", "prepare", "batch", "output directory already exists")
    if not dataset_root.is_dir() or dataset_root.is_symlink():
        raise RunAgentError("dataset_invalid", "load", "batch", "dataset directory is invalid")
    dataset_report = validate_dataset(dataset_root)
    if not dataset_report.ok:
        raise RunAgentError("dataset_invalid", "load", "batch", "dataset failed E0 validation")
    if manifest_path != dataset_root and dataset_root not in manifest_path.parents:
        raise RunAgentError("manifest_outside_dataset", "load", "batch", "run manifest must be inside dataset")
    manifest_document = _load(manifest_path)
    try:
        run_manifest = E1RunManifest.model_validate(manifest_document).model_dump(mode="json")
    except ValueError as exc:
        raise RunAgentError("manifest_invalid", "load", "batch", "run manifest contract is invalid") from exc
    if run_manifest["manifest_sha256"] != sha256_digest(
        {key: value for key, value in run_manifest.items() if key != "manifest_sha256"}
    ):
        raise RunAgentError("manifest_digest", "load", "batch", "run manifest digest mismatch")
    if model_mode not in {"stub", "real"}:
        raise RunAgentError("model_mode_invalid", "prepare", "batch", "model mode must be stub or real")
    if model_mode == "real" and not allow_real_model:
        raise RunAgentError("real_model_not_allowed", "prepare", "batch", "real model requires --allow-real-model")

    resource_path = _contained_file(dataset_root, run_manifest["resource_snapshot_file"])
    selected: list[tuple[Path, dict[str, Any]]] = []
    for relative in run_manifest["fixture_files"]:
        fixture_path = _contained_file(dataset_root, relative)
        document = _load(fixture_path)
        try:
            fixture = RuntimeMiniFixture.model_validate(document).model_dump(mode="json")
        except ValueError as exc:
            raise RunAgentError("fixture_invalid", "load", "batch", "runtime fixture contract is invalid") from exc
        if episode_ids and fixture["episode_id"] not in episode_ids:
            continue
        if track and fixture["track"] != track:
            continue
        selected.append((fixture_path, fixture))
    if episode_ids:
        found = {fixture["episode_id"] for _, fixture in selected}
        if found != episode_ids:
            raise RunAgentError("episode_not_found", "filter", "batch", "one or more episode IDs were not found")
    if not selected:
        raise RunAgentError("selection_empty", "filter", "batch", "fixture selection is empty")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e2-stage-", dir=output_path.parent
        )
    )
    worker_roots: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        (stage / "episodes").mkdir()
        (stage / "captures").mkdir()
        commit = _git_commit(PROJECT_ROOT)
        for fixture_path, fixture in selected:
            episode_id = fixture["episode_id"]
            worker_root = Path(tempfile.mkdtemp(prefix=f"learning-agent-e1-{episode_id}-"))
            worker_roots.append(str(worker_root))
            try:
                oracle_path = _contained_file(dataset_root, fixture["oracle_file"])
                published = worker_root / "published"
                request = {
                    "episode_id": episode_id,
                    "project_root": str(PROJECT_ROOT),
                    "worker_root": str(worker_root),
                    "fixture_path": str(fixture_path),
                    "oracle_path": str(oracle_path),
                    "resource_path": str(resource_path),
                    "output_path": str(published),
                    "model_mode": model_mode,
                    "git_commit": commit,
                }
                request_path = worker_root / "request.json"
                request_path.write_bytes(canonical_json_bytes(request))
                try:
                    environment = worker_environment(
                        project_root=PROJECT_ROOT,
                        worker_root=worker_root,
                        model_mode=model_mode,
                        allow_real_model=allow_real_model,
                    )
                except EvaluationIsolationError as exc:
                    raise RunAgentError("real_model_configuration", "prepare", episode_id, str(exc)) from exc
                try:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "learning_agent_eval.worker",
                            "--request",
                            str(request_path),
                        ],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        text=True,
                        capture_output=True,
                        timeout=worker_timeout_seconds,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RunAgentError(
                        "worker_timeout",
                        "worker",
                        episode_id,
                        "isolated worker exceeded its timeout",
                    ) from exc
                if completed.returncode != 0:
                    raise _worker_error(completed, episode_id)
                try:
                    result = json.loads(completed.stdout.strip())
                except json.JSONDecodeError as exc:
                    raise RunAgentError("worker_result_invalid", "worker", episode_id, "worker result is invalid") from exc
                episode = _load(published / "episode.json")
                capture = _load(published / "capture.json")
                if validate_episode(episode, source=f"{episode_id}.json"):
                    raise RunAgentError("episode_invalid", "validate", episode_id, "runtime Episode failed validation")
                try:
                    E1CaptureArtifact.model_validate(capture)
                except ValueError as exc:
                    raise RunAgentError("capture_invalid", "validate", episode_id, "capture artifact is invalid") from exc
                if capture["capture_sha256"] != sha256_digest(
                    {key: value for key, value in capture.items() if key != "capture_sha256"}
                ):
                    raise RunAgentError("capture_digest", "validate", episode_id, "capture digest mismatch")
                if privacy_issues({"episode": episode, "capture": capture}, file=episode_id):
                    raise RunAgentError("privacy_rejected", "validate", episode_id, "runtime artifacts failed privacy checks")
                if (
                    result.get("episode_id") != episode_id
                    or result.get("episode_sha256")
                    != episode["provenance"]["episode_sha256"]
                    or result.get("capture_sha256") != capture["capture_sha256"]
                ):
                    raise RunAgentError(
                        "worker_result_mismatch",
                        "validate",
                        episode_id,
                        "worker result does not match its validated artifacts",
                    )
                (stage / "episodes" / f"{episode_id}.json").write_bytes(
                    (published / "episode.json").read_bytes()
                )
                (stage / "captures" / f"{episode_id}.json").write_bytes(
                    (published / "capture.json").read_bytes()
                )
                results.append(result)
            finally:
                shutil.rmtree(worker_root, ignore_errors=True)

        output_manifest = {
            "schema_version": "e2-run-output-manifest-v1",
            "dataset_version": run_manifest["dataset_version"],
            "episode_schema_version": "decision-episode-v2",
            "invocation_mode": model_mode,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
            "episode_ids": [result["episode_id"] for result in results],
            "episode_digests": {
                result["episode_id"]: result["episode_sha256"] for result in results
            },
            "capture_digests": {
                result["episode_id"]: result["capture_sha256"] for result in results
            },
            "git_commit": commit,
            "manifest_sha256": "0" * 64,
        }
        output_manifest["manifest_sha256"] = sha256_digest(
            {key: value for key, value in output_manifest.items() if key != "manifest_sha256"}
        )
        try:
            E2RunOutputManifest.model_validate(output_manifest)
        except ValueError as exc:
            raise RunAgentError(
                "output_manifest_invalid",
                "publish",
                "batch",
                "runtime output manifest is invalid",
            ) from exc
        (stage / "run-manifest.json").write_bytes(canonical_json_bytes(output_manifest))
        if privacy_issues(output_manifest, file="run-manifest.json"):
            raise RunAgentError("privacy_rejected", "publish", "batch", "output manifest failed privacy checks")
        output_report = validate_dataset(stage)
        if not output_report.ok:
            raise RunAgentError(
                "output_invalid",
                "publish",
                "batch",
                "runtime output failed final validation",
            )
        os.replace(stage, output_path)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return RunAgentSummary(
        episode_ids=tuple(result["episode_id"] for result in results),
        tracks=tuple(fixture["track"] for _, fixture in selected),
        invocation_mode=model_mode,
        output=output_path,
        worker_roots=tuple(worker_roots),
    )
