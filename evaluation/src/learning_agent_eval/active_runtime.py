"""Unique active Runtime for Evaluation Protocol Release 1.0.

The private v3 branch exists only for frozen regression fixtures and is not a
public execution entrypoint.
"""

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
from .case_specs import episode_id_for_case, validate_case_spec, validate_case_spec_v2
from .e3_io import current_git_commit
from .e31_runtime import (
    build_real_provider_attestation,
    build_runtime_failure,
    build_runtime_failure_v2,
    build_runtime_manifest_v2,
    build_runtime_manifest_v3,
    build_stub_provider_attestation,
)
from .integrity import artifact_manifest_digest
from .isolation import EvaluationIsolationError, worker_environment
from .models import (
    CaseSuiteManifestV1,
    CaseSuiteManifestV2,
    RuntimeRunManifestV2,
    RuntimeRunManifestV3,
)
from .privacy import privacy_issues
from .release_governance import (
    ReleaseGovernanceError,
    assess_benchmark_release,
    load_benchmark_release,
    load_protocol_release,
    protocol_release_reason_codes,
)
from .runtime_metadata import (
    AGENT_RUNTIME_CONFIG_SHA256,
    DEPENDENCY_LOCK_VERSION,
    ENDPOINT_POLICY_SHA256,
    ENDPOINT_POLICY_VERSION,
    PROJECT_ROOT,
    dependency_environment_reason_codes,
    dependency_lock_sha256,
    git_worktree_clean,
)
from .source_bundles import SOURCE_BUNDLE_VERSION, source_bundle_sha256
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


def _contained_dataset_file(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise RunAgentV3Error(
            "path_outside_dataset", "load", "batch", "manifest paths must be relative"
        )
    candidate = root
    for component in relative_path.parts:
        candidate /= component
        if candidate.is_symlink():
            raise RunAgentV3Error(
                "manifest_file_symlink",
                "load",
                "batch",
                "manifest files must not use symbolic links",
            )
    path = candidate.resolve()
    if path != root and root not in path.parents:
        raise RunAgentV3Error(
            "path_outside_dataset", "load", "batch", "manifest path escapes dataset"
        )
    if path.is_symlink() or not path.is_file():
        raise RunAgentV3Error(
            "manifest_file_missing", "load", "batch", "manifest file is missing"
        )
    return path


@dataclass(frozen=True, slots=True)
class RunAgentV3Summary:
    episode_ids: tuple[str, ...]
    failure_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    invocation_mode: str
    output: Path
    worker_roots: tuple[str, ...]
    formal_evaluation_result: bool
    protocol_eligible: bool = False
    provider_eligible: bool = False
    trusted_benchmark_run: bool = False


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
    dependency_lock_verified: bool,
    model_mode: str,
    error: dict[str, str],
    contract_version: str = "v3",
    evaluation_protocol_release_sha256: str | None = None,
    benchmark_release_id: str | None = None,
    benchmark_release_sha256: str | None = None,
    runtime_run_id: str | None = None,
    runtime_source_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    frozen_time = case["runtime_setup"]["frozen_time"]
    if model_mode == "stub":
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
    else:
        attestation = build_real_provider_attestation(
            [],
            scope="agent_runtime",
            configuration_sha256=AGENT_RUNTIME_CONFIG_SHA256,
            git_commit=git_commit,
            worktree_clean=worktree_clean,
            dependency_lock_verified=dependency_lock_verified,
        )
    builder = build_runtime_failure_v2 if contract_version == "v4" else build_runtime_failure
    active_fields = (
        {
            "evaluation_protocol_release_sha256": evaluation_protocol_release_sha256,
            "benchmark_release_id": benchmark_release_id,
            "benchmark_release_sha256": benchmark_release_sha256,
            "runtime_run_id": runtime_run_id,
            "runtime_source_bundle_version": SOURCE_BUNDLE_VERSION,
            "runtime_source_bundle_sha256": runtime_source_bundle_sha256,
        }
        if contract_version == "v4"
        else {}
    )
    return builder(
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
        **active_fields,
    )


def _validate_manifest(
    document: dict[str, Any], *, contract_version: str = "v3"
) -> dict[str, Any]:
    try:
        model = CaseSuiteManifestV2 if contract_version == "v4" else CaseSuiteManifestV1
        manifest = model.model_validate(document).model_dump(mode="json")
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


def _run_runtime(
    *,
    dataset: str | Path,
    manifest: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
    model_mode: str = "stub",
    allow_real_model: bool = False,
    worker_timeout_seconds: float = 180.0,
    _contract_version: str = "v3",
    budget_ledger: str | Path | None = None,
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
    if _contract_version not in {"v3", "v4"}:
        raise ValueError("unsupported internal evaluation contract")
    active = _contract_version == "v4"
    if active and model_mode == "real":
        from .model_budget import ModelBudget

        if budget_ledger is None:
            raise RunAgentV3Error("budget_required", "prepare", "batch", "real Runtime requires a prepaid budget ledger")
        ledger_path = Path(budget_ledger).absolute()
        if ledger_path == PROJECT_ROOT or PROJECT_ROOT in ledger_path.parents:
            raise RunAgentV3Error("budget_path", "prepare", "batch", "budget ledger must be outside the repository")
        ModelBudget(ledger_path).summary()
    suite = _validate_manifest(
        _load(manifest_path, artifact="CaseSuite manifest"),
        contract_version=_contract_version,
    )
    if active:
        try:
            protocol_release = load_protocol_release()
            benchmark_release = load_benchmark_release(dataset_root, suite)
        except ReleaseGovernanceError as exc:
            raise RunAgentV3Error(
                exc.code,
                "validate",
                "batch",
                "active release governance validation failed",
            ) from exc
    else:
        protocol_release = None
        benchmark_release = None
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
    resource_path = _contained_dataset_file(
        dataset_root, suite["resource_snapshot_file"]
    )
    selected: list[tuple[Path, dict[str, Any], str]] = []
    for relative in suite["case_files"]:
        case_path = _contained_dataset_file(dataset_root, relative)
        case_validator = validate_case_spec_v2 if active else validate_case_spec
        case = case_validator(_load(case_path, artifact="CaseSpec"))
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
    dependency_lock_verified = not dependency_environment_reason_codes()
    if active:
        assert protocol_release is not None
        assert benchmark_release is not None
        filtered = bool(episode_ids or track is not None)
        trust = assess_benchmark_release(
            dataset_root=dataset_root,
            suite=suite,
            release=benchmark_release,
            filtered=filtered,
        )
        protocol_reasons = protocol_release_reason_codes(protocol_release)
        runtime_source_digest = source_bundle_sha256("runtime")
        runtime_run_id = "runtime-run:" + sha256_digest(
            {
                "protocol_release_sha256": protocol_release["release_sha256"],
                "benchmark_release_sha256": benchmark_release["manifest_sha256"],
                "git_commit": commit,
                "invocation_mode": model_mode,
                "requested_episode_ids": sorted(episode_ids or set()),
                "selected_track": track,
            }
        )[:24]
    else:
        trust = None
        protocol_reasons = ()
        runtime_source_digest = None
        runtime_run_id = None
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
                "dependency_lock_verified": dependency_lock_verified,
                "contract_version": _contract_version,
                "budget_ledger": str(Path(budget_ledger).absolute()) if budget_ledger is not None else None,
            }
            if active:
                assert protocol_release is not None
                assert benchmark_release is not None
                request.update(
                    {
                        "evaluation_protocol_release_sha256": protocol_release[
                            "release_sha256"
                        ],
                        "benchmark_release_id": benchmark_release[
                            "benchmark_release_id"
                        ],
                        "benchmark_release_sha256": benchmark_release[
                            "manifest_sha256"
                        ],
                        "runtime_run_id": runtime_run_id,
                        "runtime_source_bundle_version": SOURCE_BUNDLE_VERSION,
                        "runtime_source_bundle_sha256": runtime_source_digest,
                    }
                )
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
                            *(["-m", "learning_agent_eval.active_worker"] if active else [
                                "-c", ("from learning_agent_eval.active_worker import _main; "
                                "raise SystemExit(_main(contract_version='v3'))"),
                            ]),
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
                        dependency_lock_verified=dependency_lock_verified,
                        model_mode=model_mode,
                        error=error,
                        contract_version=_contract_version,
                        evaluation_protocol_release_sha256=(
                            protocol_release["release_sha256"]
                            if protocol_release is not None
                            else None
                        ),
                        benchmark_release_id=(
                            benchmark_release["benchmark_release_id"]
                            if benchmark_release is not None
                            else None
                        ),
                        benchmark_release_sha256=(
                            benchmark_release["manifest_sha256"]
                            if benchmark_release is not None
                            else None
                        ),
                        runtime_run_id=runtime_run_id,
                        runtime_source_bundle_sha256=runtime_source_digest,
                    )
                    (stage / "failures" / f"{failure['failure_id']}.json").write_bytes(
                        canonical_json_bytes(failure)
                    )
                    failure_output_ids.append(failure["failure_id"])
                    artifact_id = failure["failure_id"]
                    artifact_sha256 = failure["failure_sha256"]
                    terminal_kind = "failure"
                    terminal_formal = False
                    terminal_protocol = bool(failure.get("protocol_eligible", True))
                    terminal_provider = bool(failure.get("provider_eligible", False))
                elif completed.returncode != 0:
                    failure = _fallback_failure(
                        case=case,
                        git_commit=commit,
                        worktree_clean=clean,
                        dependency_digest=dependency_digest,
                        dependency_lock_verified=dependency_lock_verified,
                        model_mode=model_mode,
                        error=_worker_error(completed),
                        contract_version=_contract_version,
                        evaluation_protocol_release_sha256=(
                            protocol_release["release_sha256"]
                            if protocol_release is not None
                            else None
                        ),
                        benchmark_release_id=(
                            benchmark_release["benchmark_release_id"]
                            if benchmark_release is not None
                            else None
                        ),
                        benchmark_release_sha256=(
                            benchmark_release["manifest_sha256"]
                            if benchmark_release is not None
                            else None
                        ),
                        runtime_run_id=runtime_run_id,
                        runtime_source_bundle_sha256=runtime_source_digest,
                    )
                    (stage / "failures" / f"{failure['failure_id']}.json").write_bytes(
                        canonical_json_bytes(failure)
                    )
                    failure_output_ids.append(failure["failure_id"])
                    artifact_id = failure["failure_id"]
                    artifact_sha256 = failure["failure_sha256"]
                    terminal_kind = "failure"
                    terminal_formal = False
                    terminal_protocol = bool(failure.get("protocol_eligible", True))
                    terminal_provider = bool(failure.get("provider_eligible", False))
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
                        terminal_formal = False
                        terminal_protocol = bool(
                            failure.get("protocol_eligible", True)
                        )
                        terminal_provider = bool(
                            failure.get("provider_eligible", False)
                        )
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
                                "DecisionEpisode failed validation",
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
                        terminal_formal = False
                        terminal_protocol = bool(
                            episode["provenance"]["protocol_eligible"]
                            if active
                            else episode["completeness"]["status"] == "complete"
                        )
                        terminal_provider = bool(
                            episode["provenance"]["provider_eligible"]
                            if active
                            else episode["environment"]["provider_attestation"]
                            ["attribution_status"]
                            == "eligible"
                        )
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
                        "formal_evaluation_result": terminal_formal,
                        **(
                            {
                                "runtime_run_id": runtime_run_id,
                                "protocol_eligible": terminal_protocol,
                                "provider_eligible": terminal_provider,
                            }
                            if active
                            else {}
                        ),
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

        if active:
            manifest_document = build_runtime_manifest_v3(
                runtime_run_id=str(runtime_run_id),
                dataset_version=suite["dataset_version"],
                evaluation_protocol_release_sha256=protocol_release["release_sha256"],
                benchmark_release_id=benchmark_release["benchmark_release_id"],
                benchmark_release_sha256=benchmark_release["manifest_sha256"],
                case_suite_sha256=suite["case_suite_sha256"],
                benchmark_expected_total_cases=benchmark_release[
                    "expected_total_cases"
                ],
                benchmark_expected_track_counts={
                    name: sum(
                        item["track"] == name for item in benchmark_release["cases"]
                    )
                    for name in (
                        "planning",
                        "intervention",
                        "assessment",
                        "revision",
                    )
                },
                invocation_mode=model_mode,
                terminals=terminals,
                requested_episode_ids=sorted(episode_ids or set()),
                selected_track=track,
                git_commit=commit,
                worktree_clean=clean,
                dependency_lock_version=DEPENDENCY_LOCK_VERSION,
                dependency_lock_sha256=dependency_digest,
                runtime_source_bundle_version=SOURCE_BUNDLE_VERSION,
                runtime_source_bundle_sha256=str(runtime_source_digest),
                protocol_release_verified=not protocol_reasons,
                benchmark_release_trusted=trust.release_registered,
                suite_complete=trust.suite_complete,
                trust_reason_codes=(*trust.reason_codes, *protocol_reasons),
            )
            RuntimeRunManifestV3.model_validate(manifest_document)
        else:
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
                "Runtime output failed final validation",
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
        formal_evaluation_result=manifest_document["formal_evaluation_result"],
        protocol_eligible=bool(manifest_document.get("protocol_eligible", False)),
        provider_eligible=bool(manifest_document.get("provider_eligible", False)),
        trusted_benchmark_run=bool(
            manifest_document.get("trusted_benchmark_run", False)
        ),
    )


def _run_agent_historical_v3(
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
    """Test-owned compatibility seam for frozen DecisionEpisode v3 fixtures."""

    return _run_runtime(
        dataset=dataset,
        manifest=manifest,
        output=output,
        episode_ids=episode_ids,
        track=track,
        model_mode=model_mode,
        allow_real_model=allow_real_model,
        worker_timeout_seconds=worker_timeout_seconds,
        _contract_version="v3",
    )


def run_active_runtime(
    *,
    dataset: str | Path,
    manifest: str | Path,
    output: str | Path,
    episode_ids: set[str] | None = None,
    track: str | None = None,
    model_mode: str = "stub",
    allow_real_model: bool = False,
    worker_timeout_seconds: float = 180.0,
    budget_ledger: str | Path | None = None,
) -> RunAgentV3Summary:
    """Run the sole active CaseSpec v2 to DecisionEpisode v4 chain."""

    return _run_runtime(
        dataset=dataset,
        manifest=manifest,
        output=output,
        episode_ids=episode_ids,
        track=track,
        model_mode=model_mode,
        allow_real_model=allow_real_model,
        worker_timeout_seconds=worker_timeout_seconds,
        _contract_version="v4",
        budget_ledger=budget_ledger,
    )
