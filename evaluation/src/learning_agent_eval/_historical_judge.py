"""Frozen E3 Judge implementation for test-owned historical reproduction."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from .blinding import (
    BlindJudgeInput,
    BlindProjectionError,
    build_blind_judge_input,
    contains_quality_label,
    path_visible_to_judge,
)
from .canonical import canonical_json, canonical_json_bytes
from .e3_io import E3InputError, current_git_commit, load_e3_inputs, load_object
from .integrity import artifact_manifest_digest, judge_result_digest
from .models import JudgeResponsePayloadV1, JudgeResultV1, JudgeRunManifestV1
from .privacy import privacy_issues
from .rubric import (
    JUDGE_CONFIG_DOCUMENT,
    JUDGE_CONFIG_SHA256,
    JUDGE_CONFIG_VERSION,
    JUDGE_INSTRUCTIONS,
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

HY3_COMPLETIONS_URL = "https://tokenhub.tencentmaas.com/v1/chat/completions"


class JudgeEvaluationError(E3InputError):
    """A safely renderable Judge control-plane failure."""


class JudgeProvider(Protocol):
    """Injectable seam that receives only the strict blind Judge request."""

    mode: Literal["stub", "real"]

    def complete(self, request: Mapping[str, Any]) -> object: ...


class FixedResponseJudgeProvider:
    """Replay caller-supplied structured responses without semantic heuristics."""

    mode: Literal["stub"] = "stub"

    def __init__(self, responses: Sequence[object]):
        if not responses:
            raise ValueError("at least one fixed Judge response is required")
        self._responses = tuple(deepcopy(item) for item in responses)
        self.calls = 0

    @classmethod
    def from_file(cls, path: str | Path) -> FixedResponseJudgeProvider:
        try:
            document = load_object(
                Path(path).resolve(), artifact="fixed Judge response"
            )
        except E3InputError as exc:
            raise JudgeEvaluationError(
                "stub_response_invalid",
                "prepare",
                "batch",
                "fixed Judge response document is invalid",
            ) from exc
        if (
            set(document)
            != {
                "schema_version",
                "judge_mode",
                "formal_evaluation_result",
                "evaluation_status",
                "responses",
            }
            or document.get("schema_version") != "fixed-judge-responses-v1"
            or document.get("judge_mode") != "stub"
            or document.get("formal_evaluation_result") is not False
            or document.get("evaluation_status") != "not_a_formal_model_evaluation"
            or not isinstance(document.get("responses"), list)
            or not document["responses"]
        ):
            raise JudgeEvaluationError(
                "stub_response_invalid",
                "prepare",
                "batch",
                "fixed Judge response document is invalid",
            )
        return cls(document["responses"])

    def complete(self, request: Mapping[str, Any]) -> object:
        del request
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return deepcopy(self._responses[index])


class OpenAICompatibleHy3JudgeProvider:
    """Minimal real Hy3 seam; credentials stay in caller environment only."""

    mode: Literal["real"] = "real"

    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise JudgeEvaluationError(
                "judge_credentials_missing",
                "prepare",
                "batch",
                "real Judge requires caller OPENAI_API_KEY",
            )
        self._api_key = api_key

    def complete(self, request: Mapping[str, Any]) -> object:
        body = {
            "model": JUDGE_CONFIG_DOCUMENT["model"],
            "messages": request["messages"],
            "temperature": JUDGE_CONFIG_DOCUMENT["temperature"],
            "reasoning_effort": JUDGE_CONFIG_DOCUMENT["reasoning_effort"],
            "response_format": request["response_format"],
        }
        wire_request = urllib.request.Request(
            HY3_COMPLETIONS_URL,
            data=canonical_json_bytes(body),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(wire_request, timeout=120) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
        except (
            OSError,
            UnicodeError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            urllib.error.URLError,
        ) as exc:
            raise RuntimeError("judge_provider_error") from exc
        return content


@dataclass(frozen=True, slots=True)
class JudgeEvaluationSummary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    output: Path
    statuses: tuple[str, ...]
    invalid_episode_ids: tuple[str, ...]
    judge_error_episode_ids: tuple[str, ...]
    repair_attempted_episode_ids: tuple[str, ...]
    formal_evaluation_result: bool


def build_provider_request(
    blind_input: BlindJudgeInput, *, repair_error_codes: Sequence[str] = ()
) -> dict[str, Any]:
    """Build a provider request; expanded prompt content is never an artifact."""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": JUDGE_INSTRUCTIONS},
        {"role": "user", "content": canonical_json(blind_input.document)},
    ]
    if repair_error_codes:
        messages.append(
            {
                "role": "user",
                "content": canonical_json(
                    {
                        "schema_version": "judge-repair-request-v1",
                        "instruction": "Return one corrected complete payload only.",
                        "validation_error_codes": sorted(set(repair_error_codes)),
                    }
                ),
            }
        )
    return {
        "schema_version": "judge-provider-request-v1",
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "judge_response_payload_v1",
                "strict": True,
                "schema": JudgeResponsePayloadV1.model_json_schema(mode="validation"),
            },
        },
    }


def _parse_provider_response(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("response_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise TypeError("response_json_invalid")
    try:
        return JudgeResponsePayloadV1.model_validate(value).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError as exc:
        raise ValueError("response_schema_invalid") from exc


def _all_judge_paths(payload: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for dimension in payload["dimensions"]:
        paths.extend(dimension["evidence_paths"])
    for issue in payload["semantic_issues"]:
        paths.extend(issue["evidence_paths"])
    for suggestion in payload["suggested_hard_gates"]:
        paths.extend(suggestion["evidence_paths"])
    return paths


def _validate_provider_payload(
    value: object,
    *,
    episode: Mapping[str, Any],
    blind_input: BlindJudgeInput,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    try:
        payload = _parse_provider_response(value)
    except (TypeError, ValueError) as exc:
        return None, (str(exc),)
    error_codes: set[str] = set()
    for evidence_path in _all_judge_paths(payload):
        original_resolves = resolve_evidence_path(episode, evidence_path)[0]
        if (
            evidence_path.startswith("capture.")
            or not original_resolves
            or not path_visible_to_judge(blind_input, evidence_path)
        ):
            error_codes.add("response_evidence_invalid")
    if privacy_issues(payload, file="judge-provider-response"):
        error_codes.add("response_privacy_invalid")
    if contains_quality_label(payload):
        error_codes.add("response_label_invalid")
    return (payload if not error_codes else None), tuple(sorted(error_codes))


def _formal_state(
    *, episode: Mapping[str, Any], rule_result: Mapping[str, Any], judge_mode: str
) -> tuple[bool, str]:
    formal = bool(
        judge_mode == "real"
        and episode["provenance"]["formal_evaluation_result"]
        and rule_result["formal_evaluation_result"]
        and episode["result"]["layers"]["formal_evaluation_eligibility"] == "eligible"
    )
    return formal, (
        "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
    )


def _result_base(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    blind_input: BlindJudgeInput,
    judge_mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": "judge-result-v1",
        "judge_version": JUDGE_VERSION,
        "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "track": episode["track"],
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "blind_input_sha256": blind_input.sha256,
        "judge_mode": judge_mode,
    }


def validate_judge_result(
    result: Mapping[str, Any],
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    blind_input: BlindJudgeInput | None = None,
) -> tuple[str, ...]:
    """Validate a Judge Artifact against its exact v2 Episode and Rule Result."""

    errors: set[str] = set()
    try:
        validated = JudgeResultV1.model_validate(result).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError:
        return ("judge_contract_invalid",)
    if validated["result_sha256"] != judge_result_digest(validated):
        errors.add("judge_digest_mismatch")
    expected_metadata = {
        "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "track": episode["track"],
        "judge_version": JUDGE_VERSION,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
    }
    if any(validated[key] != value for key, value in expected_metadata.items()):
        errors.add("judge_input_linkage_invalid")
    if blind_input is None:
        try:
            blind_input = build_blind_judge_input(episode, rule_result)
        except BlindProjectionError:
            return tuple(sorted(errors | {"blind_input_invalid"}))
    if validated["blind_input_sha256"] != blind_input.sha256:
        errors.add("blind_input_digest_mismatch")
    if validated["status"] == "complete":
        for path in _all_judge_paths(validated):
            if (
                path.startswith("capture.")
                or not resolve_evidence_path(episode, path)[0]
                or not path_visible_to_judge(blind_input, path)
            ):
                errors.add("judge_evidence_invalid")
    formal, evaluation_status = _formal_state(
        episode=episode,
        rule_result=rule_result,
        judge_mode=validated["judge_mode"],
    )
    if validated["status"] != "complete":
        formal, evaluation_status = False, "not_a_formal_model_evaluation"
    if (
        validated["formal_evaluation_result"] != formal
        or validated["evaluation_status"] != evaluation_status
    ):
        errors.add("judge_formal_state_invalid")
    if privacy_issues(validated, file="judge-result"):
        errors.add("judge_privacy_invalid")
    if contains_quality_label(
        {
            "dimensions": validated["dimensions"],
            "semantic_issues": validated["semantic_issues"],
            "suggested_hard_gates": validated["suggested_hard_gates"],
        }
    ):
        errors.add("judge_label_invalid")
    return tuple(sorted(errors))


def _evaluate_one(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    judge_mode: str,
    provider: JudgeProvider,
) -> tuple[dict[str, Any], bool]:
    try:
        blind_input = build_blind_judge_input(episode, rule_result)
    except BlindProjectionError as exc:
        raise JudgeEvaluationError(
            "blind_input_invalid",
            "blind",
            str(episode["episode_id"]),
            "DecisionEpisode could not produce a safe blind Judge input",
        ) from exc
    result = _result_base(
        episode=episode,
        rule_result=rule_result,
        blind_input=blind_input,
        judge_mode=judge_mode,
    )
    repaired = False
    if rule_result["status"] == "invalid_input":
        result.update(
            {
                "dimensions": [],
                "semantic_issues": [],
                "suggested_hard_gates": [],
                "status": "invalid_input",
                "formal_evaluation_result": False,
                "evaluation_status": "not_a_formal_model_evaluation",
                "error_code": "rule_invalid_input",
                "result_sha256": "0" * 64,
            }
        )
    else:
        payload: dict[str, Any] | None = None
        error_codes: tuple[str, ...] = ()
        for attempt in range(REPAIR_LIMIT + 1):
            request = build_provider_request(
                blind_input,
                repair_error_codes=error_codes if attempt else (),
            )
            try:
                response = provider.complete(request)
            except Exception:  # noqa: BLE001 - provider exceptions are never published
                error_codes = ("provider_error",)
                break
            payload, error_codes = _validate_provider_payload(
                response,
                episode=episode,
                blind_input=blind_input,
            )
            if payload is not None:
                repaired = attempt == 1
                break
        if payload is None:
            result.update(
                {
                    "dimensions": [],
                    "semantic_issues": [],
                    "suggested_hard_gates": [],
                    "status": "judge_error",
                    "formal_evaluation_result": False,
                    "evaluation_status": "not_a_formal_model_evaluation",
                    "error_code": (
                        "judge_provider_error"
                        if error_codes == ("provider_error",)
                        else "judge_response_invalid"
                    ),
                    "result_sha256": "0" * 64,
                }
            )
        else:
            formal, evaluation_status = _formal_state(
                episode=episode,
                rule_result=rule_result,
                judge_mode=judge_mode,
            )
            result.update(payload)
            result.update(
                {
                    "status": "complete",
                    "formal_evaluation_result": formal,
                    "evaluation_status": evaluation_status,
                    "error_code": None,
                    "result_sha256": "0" * 64,
                }
            )
    result["result_sha256"] = judge_result_digest(result)
    errors = validate_judge_result(
        result,
        episode=episode,
        rule_result=rule_result,
        blind_input=blind_input,
    )
    if errors:
        raise JudgeEvaluationError(
            "judge_result_invalid",
            "validate",
            str(episode["episode_id"]),
            "Judge Result failed contract or evidence validation",
        )
    return result, repaired


def _evaluate_judges_historical_v1(
    *,
    episodes: str | Path,
    rules: str | Path,
    output: str | Path,
    judge_mode: str,
    allow_real_judge: bool = False,
    stub_response: str | Path | None = None,
    provider: JudgeProvider | None = None,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> JudgeEvaluationSummary:
    """Evaluate a selected digest-joined batch and atomically publish E3 Judge output."""

    output_path = Path(output).resolve()
    if output_path.exists():
        raise JudgeEvaluationError(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    if judge_mode not in {"stub", "real"}:
        raise JudgeEvaluationError(
            "judge_mode_invalid", "prepare", "batch", "Judge mode is invalid"
        )
    if judge_mode == "real" and not allow_real_judge:
        raise JudgeEvaluationError(
            "real_judge_not_allowed",
            "prepare",
            "batch",
            "real Judge requires --allow-real-judge",
        )
    if provider is None:
        if judge_mode == "stub":
            if stub_response is None:
                raise JudgeEvaluationError(
                    "stub_response_required",
                    "prepare",
                    "batch",
                    "stub Judge requires a caller-supplied fixed response",
                )
            provider = FixedResponseJudgeProvider.from_file(stub_response)
        else:
            provider = OpenAICompatibleHy3JudgeProvider()
    if provider.mode != judge_mode:
        raise JudgeEvaluationError(
            "judge_provider_mode_mismatch",
            "prepare",
            "batch",
            "Judge Provider mode does not match the requested Judge mode",
        )
    try:
        inputs = load_e3_inputs(
            episodes=episodes,
            rules=rules,
            episode_ids=episode_ids,
            track=track,
        )
    except E3InputError as exc:
        raise JudgeEvaluationError(
            exc.code, exc.stage, exc.episode_id, exc.public_message
        ) from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e3-judge-stage-", dir=output_path.parent
        )
    )
    results: list[dict[str, Any]] = []
    repaired_ids: list[str] = []
    try:
        (stage / "judge-results").mkdir()
        for bundle in inputs.bundles:
            result, repaired = _evaluate_one(
                episode=bundle.episode,
                rule_result=bundle.rule_result,
                judge_mode=judge_mode,
                provider=provider,
            )
            episode_id = result["episode_id"]
            (stage / "judge-results" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(result)
            )
            results.append(result)
            if repaired:
                repaired_ids.append(episode_id)
        formal = bool(results) and all(
            result["formal_evaluation_result"] for result in results
        )
        manifest = {
            "schema_version": "judge-run-manifest-v1",
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
            "judge_mode": judge_mode,
            "formal_evaluation_result": formal,
            "evaluation_status": (
                "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
            ),
            "requested_episode_ids": sorted(episode_ids or set()),
            "selected_track": track,
            "episode_ids": [result["episode_id"] for result in results],
            "input_episode_digests": {
                result["episode_id"]: result["episode_sha256"] for result in results
            },
            "input_rule_result_digests": {
                result["episode_id"]: result["rule_result_sha256"] for result in results
            },
            "blind_input_digests": {
                result["episode_id"]: result["blind_input_sha256"] for result in results
            },
            "judge_result_digests": {
                result["episode_id"]: result["result_sha256"] for result in results
            },
            "result_statuses": {
                result["episode_id"]: result["status"] for result in results
            },
            "git_commit": current_git_commit(),
            "manifest_sha256": "0" * 64,
        }
        manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
        try:
            JudgeRunManifestV1.model_validate(manifest)
        except ValidationError as exc:
            raise JudgeEvaluationError(
                "manifest_invalid",
                "publish",
                "batch",
                "Judge Run Manifest is invalid",
            ) from exc
        if privacy_issues(
            {"results": results, "manifest": manifest}, file="judge-output"
        ):
            raise JudgeEvaluationError(
                "privacy_rejected",
                "publish",
                "batch",
                "Judge artifacts failed privacy validation",
            )
        (stage / "run-manifest.json").write_bytes(canonical_json_bytes(manifest))
        output_report = validate_dataset(stage)
        if not output_report.ok:
            raise JudgeEvaluationError(
                "output_invalid",
                "publish",
                "batch",
                "Judge output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    invalid_ids = tuple(
        result["episode_id"]
        for result in results
        if result["status"] == "invalid_input"
    )
    error_ids = tuple(
        result["episode_id"] for result in results if result["status"] == "judge_error"
    )
    return JudgeEvaluationSummary(
        episode_ids=tuple(result["episode_id"] for result in results),
        tracks=tuple(bundle.episode["track"] for bundle in inputs.bundles),
        output=output_path,
        statuses=tuple(result["status"] for result in results),
        invalid_episode_ids=invalid_ids,
        judge_error_episode_ids=error_ids,
        repair_attempted_episode_ids=tuple(repaired_ids),
        formal_evaluation_result=formal,
    )
