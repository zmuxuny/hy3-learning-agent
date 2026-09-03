"""Atomic CaseSpec runner for the active DecisionEpisode v3 path."""

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

from .canonical import canonical_json_bytes
from .case_specs import episode_id_for_case, validate_case_spec
from .e3_io import current_git_commit
from .e31_runtime import (
    build_runtime_failure,
    build_runtime_manifest_v2,
    build_stub_provider_attestation,
)
from .integrity import artifact_manifest_digest
from .isolation import EvaluationIsolationError, worker_environment
from .models import CaseSuiteManifestV1, RuntimeRunManifestV2
from .privacy import privacy_issues
from .runner import _contained_file
from .runtime_metadata import (
    DEPENDENCY_LOCK_VERSION,
    ENDPOINT_POLICY_SHA256,
    ENDPOINT_POLICY_VERSION,
    PROJECT_ROOT,
    dependency_lock_sha256,
    git_worktree_clean,
)
from .validator import validate_dataset, validate_episode


class RunAgentV3Error(RuntimeError):
    """A stable, public-safe v3 control-plane error."""

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
class RunAgentV3Summary:
    episode_ids: tuple[str, ...]
    failure_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    invocation_mode: str
    output: Path
    worker_roots: tuple[str, ...]
    formal_evaluation_result: bool


def _load(path: Path, *, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunAgentV3Error(
            "document_invalid", "load", "batch", f"{artifact} is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise RunAgentV3Error(
            "document_invalid", "load", "batch", f"{artifact} must be an object"
        )
    return value


def _worker_error(completed: subprocess.CompletedProcess[str]) -> dict[str, str]:
    try:
        value = json.loads(completed.stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        value = {}
    return {
        "error_code": str(value.get("error_code") or "worker.framework_error"),
        "stage": str(value.get("stage") or "worker"),
        "message": str(value.get("message") or "isolated evaluation worker failed"),
    }


def _failure_class(stage: str) -> str:
    if stage == "isolation":
        return "isolation_violation"
    if stage == "provider":
        return "provider_error"
    if stage == "load":
        return "fixture_error"
    return "framework_error"


def _fallback_failure(
    *,
    case: dict[str, Any],
    git_commit: str,
    worktree_clean: bool,
    dependency_digest: str,
    error: dict[str, str],
) -> dict[str, Any]:
    frozen_time = case["runtime_setup"]["frozen_time"]
    attestation = build_stub_provider_attestation(
        [],
        scope="agent_runtime",
        configured_model="e31-scripted-model",
        frozen_time=frozen_time,
        git_commit=git_commit,
        dependency_lock_version=DEPENDENCY_LOCK_VERSION,
        dependency_lock_sha256=dependency_digest,
        endpoint_policy_version=ENDPOINT_POLICY_VERSION,
        endpoint_policy_sha256=ENDPOINT_POLICY_SHA256,
        worktree_clean=worktree_clean,
    )
    return build_runtime_failure(
        case_id=case["case_id"],
        case_spec_sha256=case["case_spec_sha256"],
        stage=error["stage"],
        failure_class=_failure_class(error["stage"]),
        reason_code=error["error_code"],
        public_summary=error["message"],
        model_calls=[],
        provider_attestation=attestation,
        isolation_evidence=None,
        started_at=frozen_time,
        failed_at=frozen_time,
    )


def _validate_manifest(document: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest = CaseSuiteManifestV1.model_validate(document).model_dump(mode="json")
    except ValueError as exc:
        raise RunAgentV3Error(
            "manifest_invalid",
            "load",
            "batch",
            "CaseSuite manifest contract is invalid",
        ) from exc
    if manifest["manifest_sha256"] != artifact_manifest_digest(manifest):
        raise RunAgentV3Error(
            "manifest_digest",
            "load",
            "batch",
            "CaseSuite manifest digest does not match",
        )
    return manifest


def run_agent_v3(
    *,
    dataset: str | Path,
    manifest: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
    model_mode: str = "stub",
    allow_real_model: bool = False,
    worker_timeout_seconds: float = 180.0,
) -> RunAgentV3Summary:
    """Run selected CaseSpecs independently and preserve every terminal artifact."""

    dataset_argument = Path(dataset).expanduser()
    manifest_argument = Path(manifest).expanduser()
    if dataset_argument.is_symlink() or manifest_argument.is_symlink():
        raise RunAgentV3Error(
            "input_symlink", "load", "batch", "evaluation inputs cannot be symlinks"
        )
    dataset_root = dataset_argument.resolve()
    manifest_path = manifest_argument.resolve()
    output_path = Path(output).resolve()
    if output_path.exists():
        raise RunAgentV3Error(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    if not dataset_root.is_dir():
        raise RunAgentV3Error(
            "dataset_invalid", "load", "batch", "CaseSpec dataset is invalid"
        )
    if manifest_path != dataset_root and dataset_root not in manifest_path.parents:
        raise RunAgentV3Error(
            "manifest_outside_dataset",
            "load",
            "batch",
            "CaseSuite manifest must be inside its dataset",
        )
    dataset_report = validate_dataset(dataset_root)
    if not dataset_report.ok:
        raise RunAgentV3Error(
            "dataset_invalid", "validate", "batch", "CaseSpec dataset failed validation"
        )
    suite = _validate_manifest(_load(manifest_path, artifact="CaseSuite manifest"))
    if model_mode not in {"stub", "real"}:
        raise RunAgentV3Error(
            "model_mode_invalid", "prepare", "batch", "model mode must be stub or real"
        )
    if model_mode == "real" and not allow_real_model:
        raise RunAgentV3Error(
            "real_model_not_allowed",
            "prepare",
            "batch",
            "real model execution requires explicit opt-in",
        )
    resource_path = _contained_file(dataset_root, suite["resource_snapshot_file"])
    selected: list[tuple[Path, dict[str, Any], str]] = []
    for relative in suite["case_files"]:
        case_path = _contained_file(dataset_root, relative)
        case = validate_case_spec(_load(case_path, artifact="CaseSpec"))
        episode_id = episode_id_for_case(case)
        if episode_ids and episode_id not in episode_ids:
            continue
        if track and case["track"] != track:
            continue
        if case["runtime_setup"]["invocation_mode"] != model_mode:
            raise RunAgentV3Error(
                "case_mode_mismatch",
                "filter",
                episode_id,
                "selected CaseSpec invocation mode differs from model mode",
            )
        selected.append((case_path, case, episode_id))
    if episode_ids and {item[2] for item in selected} != episode_ids:
        raise RunAgentV3Error(
            "episode_not_found",
            "filter",
            "batch",
            "one or more Episode IDs were not found",
        )
    if not selected:
        raise RunAgentV3Error(
            "selection_empty", "filter", "batch", "CaseSpec selection is empty"
        )
    selected.sort(key=lambda item: item[1]["case_id"])

    commit = current_git_commit()
    try:
        clean = git_worktree_clean()
    except RuntimeError as exc:
        raise RunAgentV3Error(
            "git_status_unavailable",
            "prepare",
            "batch",
            "current Git worktree status is unavailable",
        ) from exc
    dependency_digest = dependency_lock_sha256()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e31-runtime-stage-", dir=output_path.parent
        )
    )
    worker_roots: list[str] = []
    terminals: list[dict[str, Any]] = []
    tracks: list[str] = []
    episode_output_ids: list[str] = []
    failure_output_ids: list[str] = []
    try:
        (stage / "episodes").mkdir()
        (stage / "judge-references").mkdir()
        (stage / "failures").mkdir()
        for case_path, case, episode_id in selected:
            tracks.append(case["track"])
            worker_root = Path(
                tempfile.mkdtemp(prefix=f"learning-agent-e31-{episode_id}-")
            )
            worker_roots.append(str(worker_root))
            published = worker_root / "published"
            request = {
                "case_id": case["case_id"],
                "project_root": str(PROJECT_ROOT),
                "worker_root": str(worker_root),
                "case_path": str(case_path),
                "resource_path": str(resource_path),
                "output_path": str(published),
                "model_mode": model_mode,
                "git_commit": commit,
                "worktree_clean": clean,
                "dependency_lock_version": DEPENDENCY_LOCK_VERSION,
                "dependency_lock_sha256": dependency_digest,
            }
            (worker_root / "request.json").write_bytes(canonical_json_bytes(request))
            try:
                environment = worker_environment(
                    project_root=PROJECT_ROOT,
                    worker_root=worker_root,
                    model_mode=model_mode,
                    allow_real_model=allow_real_model,
                )
                try:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "learning_agent_eval.worker_v3",
                            "--request",
                            str(worker_root / "request.json"),
                        ],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        text=True,
                        capture_output=True,
                        timeout=worker_timeout_seconds,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    completed = None
                if completed is None:
                    error = {
                        "error_code": "worker.timeout",
                        "stage": "worker",
                        "message": "isolated evaluation worker exceeded its timeout",
                    }
                    failure = _fallback_failure(
                        case=case,
                        git_commit=commit,
                        worktree_clean=clean,
                        dependency_digest=dependency_digest,
                        error=error,
                    )
                    (stage / "failures" / f"{failure['failure_id']}.json").write_bytes(
                        canonical_json_bytes(failure)
                    )
                    failure_output_ids.append(failure["failure_id"])
                    artifact_id = failure["failure_id"]
                    artifact_sha256 = failure["failure_sha256"]
                    terminal_kind = "failure"
                elif completed.returncode != 0:
                    failure = _fallback_failure(
                        case=case,
                        git_commit=commit,
                        worktree_clean=clean,
                        dependency_digest=dependency_digest,
                        error=_worker_error(completed),
                    )
                    (stage / "failures" / f"{failure['failure_id']}.json").write_bytes(
                        canonical_json_bytes(failure)
                    )
                    failure_output_ids.append(failure["failure_id"])
                    artifact_id = failure["failure_id"]
                    artifact_sha256 = failure["failure_sha256"]
                    terminal_kind = "failure"
                else:
                    try:
                        result = json.loads(completed.stdout.strip())
                    except json.JSONDecodeError as exc:
                        raise RunAgentV3Error(
                            "worker_result_invalid",
                            "worker",
                            episode_id,
                            "isolated worker result is invalid",
                        ) from exc
                    if result.get("status") == "failure":
                        failure_path = published / "failure.json"
                        failure = _load(failure_path, artifact="Runtime Failure")
                        artifact_id = failure["failure_id"]
                        artifact_sha256 = failure["failure_sha256"]
                        (stage / "failures" / f"{artifact_id}.json").write_bytes(
                            failure_path.read_bytes()
                        )
                        failure_output_ids.append(artifact_id)
                        terminal_kind = "failure"
                    elif result.get("status") == "episode":
                        episode = _load(published / "episode.json", artifact="Episode")
                        reference = _load(
                            published / "judge-reference.json",
                            artifact="JudgeReference",
                        )
                        if validate_episode(episode, source=f"{episode_id}.json"):
                            raise RunAgentV3Error(
                                "episode_invalid",
                                "validate",
                                episode_id,
                                "DecisionEpisode v3 failed validation",
                            )
                        if (
                            result.get("artifact_id") != episode_id
                            or result.get("artifact_sha256")
                            != episode["provenance"]["episode_sha256"]
                            or result.get("reference_sha256")
                            != reference["reference_sha256"]
                        ):
                            raise RunAgentV3Error(
                                "worker_result_mismatch",
                                "validate",
                                episode_id,
                                "worker result does not match published artifacts",
                            )
                        (stage / "episodes" / f"{episode_id}.json").write_bytes(
                            (published / "episode.json").read_bytes()
                        )
                        (stage / "judge-references" / f"{episode_id}.json").write_bytes(
                            (published / "judge-reference.json").read_bytes()
                        )
                        episode_output_ids.append(episode_id)
                        artifact_id = episode_id
                        artifact_sha256 = episode["provenance"]["episode_sha256"]
                        terminal_kind = "episode"
                    else:
                        raise RunAgentV3Error(
                            "worker_result_invalid",
                            "worker",
                            episode_id,
                            "isolated worker returned an unknown terminal kind",
                        )
                terminals.append(
                    {
                        "case_id": case["case_id"],
                        "case_spec_sha256": case["case_spec_sha256"],
                        "track": case["track"],
                        "terminal_kind": terminal_kind,
                        "artifact_id": artifact_id,
                        "artifact_sha256": artifact_sha256,
                    }
                )
            except EvaluationIsolationError as exc:
                raise RunAgentV3Error(
                    "real_model_configuration",
                    "prepare",
                    episode_id,
                    str(exc),
                ) from exc
            finally:
                shutil.rmtree(worker_root, ignore_errors=True)

        manifest_document = build_runtime_manifest_v2(
            dataset_version=suite["dataset_version"],
            invocation_mode=model_mode,
            terminals=terminals,
            git_commit=commit,
            dependency_lock_version=DEPENDENCY_LOCK_VERSION,
            dependency_lock_sha256=dependency_digest,
        )
        RuntimeRunManifestV2.model_validate(manifest_document)
        if privacy_issues(manifest_document, file="run-manifest.json"):
            raise RunAgentV3Error(
                "privacy_rejected",
                "publish",
                "batch",
                "Runtime manifest failed privacy checks",
            )
        (stage / "run-manifest.json").write_bytes(
            canonical_json_bytes(manifest_document)
        )
        report = validate_dataset(stage)
        if not report.ok:
            raise RunAgentV3Error(
                "output_invalid",
                "publish",
                "batch",
                "v3 Runtime output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return RunAgentV3Summary(
        episode_ids=tuple(sorted(episode_output_ids)),
        failure_ids=tuple(sorted(failure_output_ids)),
        tracks=tuple(tracks),
        invocation_mode=model_mode,
        output=output_path,
        worker_roots=tuple(worker_roots),
        formal_evaluation_result=False,
    )
