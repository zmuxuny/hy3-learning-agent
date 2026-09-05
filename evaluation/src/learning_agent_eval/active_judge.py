"""Unique active blind Judge for Evaluation Protocol Release 1.0."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from .blinding_v2 import (
    BlindJudgeInputV2,
    BlindJudgeInputV3,
    BlindProjectionV2Error,
    build_blind_judge_input_v2,
    build_blind_judge_input_v3,
    path_visible_to_judge_v2,
    path_visible_to_judge_v3,
)
from .canonical import canonical_json, canonical_json_bytes
from .e3_io import current_git_commit
from .e31_io import E31InputError, load_e31_inputs, load_e311_inputs, load_object
from .e31_runtime import (
    build_real_provider_attestation,
    build_stub_provider_attestation,
)
from .integrity import (
    artifact_manifest_digest,
    judge_result_digest,
    provider_attestation_digest,
)
from .judge_request_projection import evidence_catalog, pack_shared_values
from .model_budget import ModelBudgetExceeded
from .models import (
    JudgeResponsePayloadV1,
    JudgeResultV2,
    JudgeResultV3,
    JudgeRunManifestV2,
    JudgeRunManifestV3,
)
from .normalizers import utc_timestamp
from .privacy import privacy_issues
from .rubric import (
    JUDGE_CONFIG_DOCUMENT_V2,
    JUDGE_CONFIG_DOCUMENT_V3,
    JUDGE_CONFIG_SHA256_V2,
    JUDGE_CONFIG_SHA256_V3,
    JUDGE_CONFIG_VERSION_V2,
    JUDGE_CONFIG_VERSION_V3,
    JUDGE_INSTRUCTIONS_V2,
    JUDGE_INSTRUCTIONS_V3,
    JUDGE_PROMPT_SHA256_V2,
    JUDGE_PROMPT_SHA256_V3,
    JUDGE_PROMPT_VERSION_V2,
    JUDGE_PROMPT_VERSION_V3,
    JUDGE_VERSION_V2,
    JUDGE_VERSION_V3,
    REPAIR_LIMIT,
    RUBRIC_SHA256,
    RUBRIC_VERSION,
    TRACK_ANCHOR_SHA256,
    TRACK_ANCHOR_VERSION,
)
from .runtime_metadata import (
    DEPENDENCY_LOCK_VERSION,
    ENDPOINT_POLICY_SHA256,
    ENDPOINT_POLICY_VERSION,
    HY3_ENDPOINT_ORIGIN,
    HY3_MODEL,
    dependency_environment_reason_codes,
    dependency_lock_sha256,
    git_worktree_clean,
)
from .source_bundles import SOURCE_BUNDLE_VERSION, source_bundle_sha256
from .validator import resolve_evidence_path, validate_dataset

HY3_COMPLETIONS_URL = f"{HY3_ENDPOINT_ORIGIN}/v1/chat/completions"
JUDGE_SOURCE_BUNDLE_SHA256_V3 = source_bundle_sha256("judge")


class JudgeEvaluationV2Error(E31InputError):
    """A stable, public-safe Judge v2 error."""


@dataclass(frozen=True, slots=True)
class JudgeProviderReplyV2:
    content: object
    request_model: str
    response_model: str | None
    provider_request_id: str | None
    requested_at: str
    responded_at: str | None
    status: Literal["completed", "provider_error", "framework_error"]
    finish_reason: str | None = None
    budget_ticket: int | None = None


class JudgeProviderV2(Protocol):
    mode: Literal["stub", "real"]

    def complete(self, request: Mapping[str, Any]) -> JudgeProviderReplyV2: ...


class FixedResponseJudgeProviderV2:
    """Replay explicit caller responses; it contains no scoring heuristic."""

    mode: Literal["stub"] = "stub"
    provider_model = "fixed-judge-response-v2"

    def __init__(self, responses: Sequence[object], *, frozen_time: str):
        if not responses:
            raise ValueError("at least one fixed Judge response is required")
        self._responses = tuple(deepcopy(item) for item in responses)
        self._frozen_time = frozen_time
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    @classmethod
    def from_file(
        cls, path: str | Path, *, frozen_time: str
    ) -> FixedResponseJudgeProviderV2:
        try:
            document = load_object(
                Path(path).resolve(), artifact="fixed Judge response"
            )
        except E31InputError as exc:
            raise JudgeEvaluationV2Error(
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
            or document.get("schema_version") != "fixed-judge-responses-v2"
            or document.get("judge_mode") != "stub"
            or document.get("formal_evaluation_result") is not False
            or document.get("evaluation_status") != "not_a_formal_model_evaluation"
            or not isinstance(document.get("responses"), list)
            or not document["responses"]
        ):
            raise JudgeEvaluationV2Error(
                "stub_response_invalid",
                "prepare",
                "batch",
                "fixed Judge response document is invalid",
            )
        return cls(document["responses"], frozen_time=frozen_time)

    def complete(self, request: Mapping[str, Any]) -> JudgeProviderReplyV2:
        self.requests.append(deepcopy(dict(request)))
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return JudgeProviderReplyV2(
            content=deepcopy(self._responses[index]),
            request_model=self.provider_model,
            response_model=self.provider_model,
            provider_request_id=f"fixed-response-{self.calls:03d}",
            requested_at=self._frozen_time,
            responded_at=self._frozen_time,
            status="completed",
        )


class FixedResponseJudgeProviderV3(FixedResponseJudgeProviderV2):
    """Replay caller-supplied active responses without scoring heuristics."""

    provider_model = "fixed-judge-response-v3"

    @classmethod
    def from_file(
        cls, path: str | Path, *, frozen_time: str
    ) -> FixedResponseJudgeProviderV3:
        try:
            document = load_object(
                Path(path).resolve(), artifact="fixed Judge response"
            )
        except E31InputError as exc:
            raise JudgeEvaluationV2Error(
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
            or document.get("schema_version") != "fixed-judge-responses-v3"
            or document.get("judge_mode") != "stub"
            or document.get("formal_evaluation_result") is not False
            or document.get("evaluation_status")
            != "not_a_formal_model_evaluation"
            or not isinstance(document.get("responses"), list)
            or not document["responses"]
        ):
            raise JudgeEvaluationV2Error(
                "stub_response_invalid",
                "prepare",
                "batch",
                "fixed Judge response document is invalid",
            )
        return cls(document["responses"], frozen_time=frozen_time)


class OpenAICompatibleHy3JudgeProviderV2:
    """Fixed-endpoint real Hy3 seam with response attribution metadata."""

    mode: Literal["real"] = "real"
    config_document = JUDGE_CONFIG_DOCUMENT_V2

    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise JudgeEvaluationV2Error(
                "judge_credentials_missing",
                "prepare",
                "batch",
                "real Judge requires caller OPENAI_API_KEY",
            )
        self._api_key = api_key

    def complete(self, request: Mapping[str, Any]) -> JudgeProviderReplyV2:
        requested_at = utc_timestamp(datetime.now(timezone.utc))
        body = {
            "model": HY3_MODEL,
            "messages": request["messages"],
            "temperature": self.config_document["temperature"],
            "reasoning_effort": self.config_document["reasoning_effort"],
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
            response_model = str(envelope["model"])
            request_id = str(envelope["id"])
        except (
            OSError,
            UnicodeError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            urllib.error.URLError,
        ):
            return JudgeProviderReplyV2(
                content=None,
                request_model=HY3_MODEL,
                response_model=None,
                provider_request_id=None,
                requested_at=requested_at,
                responded_at=utc_timestamp(datetime.now(timezone.utc)),
                status="provider_error",
            )
        return JudgeProviderReplyV2(
            content=content,
            request_model=HY3_MODEL,
            response_model=response_model,
            provider_request_id=request_id,
            requested_at=requested_at,
            responded_at=utc_timestamp(datetime.now(timezone.utc)),
            status="completed",
        )


class OpenAICompatibleHy3JudgeProviderV3(OpenAICompatibleHy3JudgeProviderV2):
    """Active fixed Hy3 seam; attribution is audited by the v3 config digest."""

    config_document = JUDGE_CONFIG_DOCUMENT_V3

    def __init__(self, *, budget_ledger: str | Path, scope: str = "semantic_judge") -> None:
        super().__init__()
        from .model_budget import ModelBudget

        self.budget = ModelBudget(budget_ledger)
        self.budget.summary()
        self.scope = scope
        self.calls = 0

    def complete(self, request: Mapping[str, Any]) -> JudgeProviderReplyV2:
        from .model_budget import INPUT_LIMIT

        requested_at = utc_timestamp(datetime.now(timezone.utc))
        body = {
            "model": HY3_MODEL, "messages": request["messages"],
            "temperature": self.config_document["temperature"],
            "reasoning_effort": self.config_document["reasoning_effort"],
            "response_format": request["response_format"],
            "max_tokens": self.config_document["max_tokens"], "n": 1,
        }
        wire = canonical_json_bytes(body)
        input_bound = len(wire) + 2048
        if input_bound > INPUT_LIMIT:
            raise ValueError("judge_input_limit_exceeded")
        self.calls += 1
        ticket = self.budget.reserve(
            scope=self.scope, call_id=request.get("_budget_call_id", f"judge-call:{self.calls:04d}"),
            input_limit=input_bound, output_limit=body["max_tokens"],
        )
        usage = None
        content = response_model = request_id = finish_reason = None
        status = "provider_error"
        try:
            wire_request = urllib.request.Request(
                HY3_COMPLETIONS_URL, data=wire,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            # urllib has no automatic request retries. Each repair re-enters here.
            with urllib.request.urlopen(wire_request, timeout=120) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            if not isinstance(envelope, dict):
                raise TypeError("provider_envelope_invalid")
            candidate_usage = envelope.get("usage")
            if isinstance(candidate_usage, dict):
                usage = {key: candidate_usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            choice = envelope["choices"][0]
            content = choice["message"]["content"]
            response_model, request_id = str(envelope["model"]), str(envelope["id"])
            finish_reason = choice.get("finish_reason")
            status = "completed"
        except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError):
            pass
        finally:
            self.budget.settle(ticket, usage)
            self.budget.record_outcome(ticket, outcome="complete" if status == "completed" else "provider_error")
        return JudgeProviderReplyV2(
            content=content, request_model=HY3_MODEL, response_model=response_model,
            provider_request_id=request_id, requested_at=requested_at,
            responded_at=utc_timestamp(datetime.now(timezone.utc)), status=status,
            finish_reason=finish_reason, budget_ticket=ticket,
        )


@dataclass(frozen=True, slots=True)
class JudgeEvaluationV2Summary:
    episode_ids: tuple[str, ...]
    tracks: tuple[str, ...]
    runtime_failure_ids: tuple[str, ...]
    output: Path
    statuses: tuple[str, ...]
    invalid_episode_ids: tuple[str, ...]
    judge_error_episode_ids: tuple[str, ...]
    repair_attempted_episode_ids: tuple[str, ...]
    formal_evaluation_result: bool
    protocol_eligible: bool = False
    provider_eligible: bool = False
    trusted_benchmark_run: bool = False


def build_provider_request_v2(
    blind_input: BlindJudgeInputV2, *, repair_error_codes: Sequence[str] = ()
) -> dict[str, Any]:
    """Build an ephemeral provider request; expanded prompt text is not published."""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": JUDGE_INSTRUCTIONS_V2},
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
        "schema_version": "judge-provider-request-v2",
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


def build_provider_request_v3(
    blind_input: BlindJudgeInputV3, *, repair_error_codes: Sequence[str] = (),
    invalid_evidence_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Build an ephemeral active request; expanded prompt text is never published."""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": JUDGE_INSTRUCTIONS_V3},
        {"role": "user", "content": canonical_json({
            **pack_shared_values(blind_input.document),
            "evidence_path_catalog": evidence_catalog(blind_input.document["episode"]),
        })},
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
                        "invalid_evidence_paths": list(invalid_evidence_paths),
                        "evidence_instruction": "Replace invalid paths with exact entries from evidence_path_catalog.",
                    }
                ),
            }
        )
    return {
        "schema_version": "judge-provider-request-v3",
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


def _parse_response(value: object) -> dict[str, Any]:
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


def _all_evidence_paths(payload: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for dimension in payload["dimensions"]:
        paths.extend(dimension["evidence_paths"])
    for issue in payload["semantic_issues"]:
        paths.extend(issue["evidence_paths"])
    for suggestion in payload["suggested_hard_gates"]:
        paths.extend(suggestion["evidence_paths"])
    return paths


def _validate_payload(
    value: object,
    *,
    episode: Mapping[str, Any],
    blind_input: BlindJudgeInputV2,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    try:
        payload = _parse_response(value)
    except (TypeError, ValueError) as exc:
        return None, (str(exc),)
    errors: set[str] = set()
    for path in _all_evidence_paths(payload):
        if (
            path.startswith("capture.")
            or not resolve_evidence_path(episode, path)[0]
            or not path_visible_to_judge_v2(blind_input, path)
        ):
            errors.add("response_evidence_invalid")
    if privacy_issues(payload, file="judge-provider-response"):
        errors.add("response_privacy_invalid")
    return (payload if not errors else None), tuple(sorted(errors))


def _validate_payload_v3(
    value: object,
    *,
    episode: Mapping[str, Any],
    blind_input: BlindJudgeInputV3,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    try:
        payload = _parse_response(value)
    except (TypeError, ValueError) as exc:
        return None, (str(exc),)
    errors: set[str] = set()
    for path in _all_evidence_paths(payload):
        if (
            path.startswith("capture.")
            or not resolve_evidence_path(episode, path)[0]
            or not path_visible_to_judge_v3(blind_input, path)
        ):
            errors.add("response_evidence_invalid")
    if privacy_issues(payload, file="judge-provider-response-v3"):
        errors.add("response_privacy_invalid")
    return (payload if not errors else None), tuple(sorted(errors))


def _provider_attestation(
    *,
    mode: str,
    replies: Sequence[JudgeProviderReplyV2],
    frozen_time: str,
    git_commit: str,
    worktree_clean: bool,
    dependency_digest: str,
    dependency_lock_verified: bool,
    configuration_sha256: str = JUDGE_CONFIG_SHA256_V2,
) -> dict[str, Any]:
    records = [
        {
            "call_id": f"judge-provider-call:{index:03d}",
            "request_model": reply.request_model,
            "response_model": reply.response_model,
            "provider_request_id": reply.provider_request_id,
            "response_status": reply.status,
            "requested_at": reply.requested_at,
            "responded_at": reply.responded_at,
        }
        for index, reply in enumerate(replies, 1)
    ]
    if mode == "stub":
        return build_stub_provider_attestation(
            records,
            scope="semantic_judge",
            configured_model="fixed-judge-response-v1",
            frozen_time=frozen_time,
            git_commit=git_commit,
            dependency_lock_version=DEPENDENCY_LOCK_VERSION,
            dependency_lock_sha256=dependency_digest,
            endpoint_policy_version=ENDPOINT_POLICY_VERSION,
            endpoint_policy_sha256=ENDPOINT_POLICY_SHA256,
            worktree_clean=worktree_clean,
        )
    return build_real_provider_attestation(
        records,
        scope="semantic_judge",
        configuration_sha256=configuration_sha256,
        git_commit=git_commit,
        worktree_clean=worktree_clean,
        dependency_lock_verified=dependency_lock_verified,
    )


def _result_base(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
    blind_input: BlindJudgeInputV2,
    judge_mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": "judge-result-v2",
        "judge_version": JUDGE_VERSION_V2,
        "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "judge_reference_sha256": reference["reference_sha256"],
        "track": episode["track"],
        "judge_prompt_version": JUDGE_PROMPT_VERSION_V2,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V2,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "blind_input_sha256": blind_input.sha256,
        "judge_mode": judge_mode,
    }


def validate_judge_result_v2(
    result: Mapping[str, Any],
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
    blind_input: BlindJudgeInputV2 | None = None,
) -> tuple[str, ...]:
    """Recompute v3 linkage, Evidence Paths, attribution, and formal state."""

    errors: set[str] = set()
    try:
        validated = JudgeResultV2.model_validate(result).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError:
        return ("judge_contract_invalid",)
    if validated["result_sha256"] != judge_result_digest(validated):
        errors.add("judge_digest_mismatch")
    expected = {
        "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "judge_reference_sha256": reference["reference_sha256"],
        "track": episode["track"],
        "judge_version": JUDGE_VERSION_V2,
        "judge_prompt_version": JUDGE_PROMPT_VERSION_V2,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V2,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
    }
    if any(validated[key] != value for key, value in expected.items()):
        errors.add("judge_input_linkage_invalid")
    if blind_input is None:
        try:
            blind_input = build_blind_judge_input_v2(episode, rule_result, reference)
        except BlindProjectionV2Error:
            return tuple(sorted(errors | {"blind_input_invalid"}))
    if validated["blind_input_sha256"] != blind_input.sha256:
        errors.add("blind_input_digest_mismatch")
    if validated["provider_attestation"]["attestation_sha256"] != (
        provider_attestation_digest(validated["provider_attestation"])
    ):
        errors.add("judge_provider_attestation_invalid")
    if validated["status"] == "complete":
        for path in _all_evidence_paths(validated):
            if (
                path.startswith("capture.")
                or not resolve_evidence_path(episode, path)[0]
                or not path_visible_to_judge_v2(blind_input, path)
            ):
                errors.add("judge_evidence_invalid")
    expected_formal = bool(
        validated["status"] == "complete"
        and validated["judge_mode"] == "real"
        and validated["provider_attestation"]["attribution_status"] == "eligible"
        and episode["provenance"]["formal_evaluation_result"]
        and rule_result["formal_evaluation_result"]
    )
    if validated["formal_evaluation_result"] != expected_formal or validated[
        "evaluation_status"
    ] != (
        "formal_model_evaluation"
        if expected_formal
        else "not_a_formal_model_evaluation"
    ):
        errors.add("judge_formal_state_invalid")
    if privacy_issues(validated, file="judge-result-v2"):
        errors.add("judge_privacy_invalid")
    return tuple(sorted(errors))


def _evaluate_one(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
    judge_mode: str,
    provider: JudgeProviderV2,
    git_commit: str,
    worktree_clean: bool,
    dependency_digest: str,
    dependency_lock_verified: bool = True,
) -> tuple[dict[str, Any], bool]:
    try:
        blind_input = build_blind_judge_input_v2(episode, rule_result, reference)
    except BlindProjectionV2Error as exc:
        raise JudgeEvaluationV2Error(
            "blind_input_invalid",
            "blind",
            str(episode["episode_id"]),
            "DecisionEpisode v3 could not produce a safe blind Judge input",
        ) from exc
    result = _result_base(
        episode=episode,
        rule_result=rule_result,
        reference=reference,
        blind_input=blind_input,
        judge_mode=judge_mode,
    )
    replies: list[JudgeProviderReplyV2] = []
    repaired = False
    if rule_result["status"] == "invalid_input":
        payload = None
        status = "invalid_input"
        error_code = "rule_invalid_input"
    else:
        payload = None
        error_codes: tuple[str, ...] = ()
        for attempt in range(REPAIR_LIMIT + 1):
            request = build_provider_request_v2(
                blind_input,
                repair_error_codes=error_codes if attempt else (),
            )
            try:
                reply = provider.complete(request)
            except Exception:  # noqa: BLE001 - raw provider exceptions are discarded
                error_codes = ("provider_error",)
                break
            replies.append(reply)
            if reply.status != "completed":
                error_codes = ("provider_error",)
                break
            payload, error_codes = _validate_payload(
                reply.content, episode=episode, blind_input=blind_input
            )
            if payload is not None:
                repaired = attempt == 1
                break
        if payload is None:
            status = "judge_error"
            error_code = (
                "judge_provider_error"
                if error_codes == ("provider_error",)
                else "judge_response_invalid"
            )
        else:
            status = "complete"
            error_code = None
    attestation = _provider_attestation(
        mode=judge_mode,
        replies=replies,
        frozen_time=episode["environment"]["frozen_time"],
        git_commit=git_commit,
        worktree_clean=worktree_clean,
        dependency_digest=dependency_digest,
        dependency_lock_verified=dependency_lock_verified,
    )
    formal = bool(
        status == "complete"
        and judge_mode == "real"
        and attestation["attribution_status"] == "eligible"
        and episode["provenance"]["formal_evaluation_result"]
        and rule_result["formal_evaluation_result"]
    )
    if payload is None:
        result.update(
            {
                "dimensions": [],
                "semantic_issues": [],
                "suggested_hard_gates": [],
            }
        )
    else:
        result.update(payload)
    result.update(
        {
            "status": status,
            "provider_attestation": attestation,
            "formal_evaluation_result": formal,
            "evaluation_status": (
                "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
            ),
            "error_code": error_code,
            "result_sha256": "0" * 64,
        }
    )
    result["result_sha256"] = judge_result_digest(result)
    errors = validate_judge_result_v2(
        result,
        episode=episode,
        rule_result=rule_result,
        reference=reference,
        blind_input=blind_input,
    )
    if errors:
        raise JudgeEvaluationV2Error(
            "judge_result_invalid",
            "validate",
            str(episode["episode_id"]),
            "Judge Result v2 failed contract or evidence validation",
        )
    return result, repaired


def _evaluate_judges_historical_v2(
    *,
    episodes: str | Path,
    rules: str | Path,
    output: str | Path,
    judge_mode: str,
    allow_real_judge: bool = False,
    stub_response: str | Path | None = None,
    provider: JudgeProviderV2 | None = None,
    episode_ids: set[str] | None = None,
    track: str | None = None,
) -> JudgeEvaluationV2Summary:
    """Evaluate a selected v3 batch and atomically publish Judge Results v2."""

    output_path = Path(output).resolve()
    if output_path.exists():
        raise JudgeEvaluationV2Error(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    if judge_mode not in {"stub", "real"}:
        raise JudgeEvaluationV2Error(
            "judge_mode_invalid", "prepare", "batch", "Judge mode is invalid"
        )
    if judge_mode == "real" and not allow_real_judge:
        raise JudgeEvaluationV2Error(
            "real_judge_not_allowed",
            "prepare",
            "batch",
            "real Judge requires --allow-real-judge",
        )
    try:
        inputs = load_e31_inputs(
            episodes=episodes,
            rules=rules,
            episode_ids=episode_ids,
            track=track,
        )
    except E31InputError as exc:
        raise JudgeEvaluationV2Error(
            exc.code, exc.stage, exc.artifact_id, exc.public_message
        ) from exc
    frozen_time = inputs.bundles[0].episode["environment"]["frozen_time"]
    if provider is None:
        if judge_mode == "stub":
            if stub_response is None:
                raise JudgeEvaluationV2Error(
                    "stub_response_required",
                    "prepare",
                    "batch",
                    "stub Judge requires a caller-supplied fixed response",
                )
            provider = FixedResponseJudgeProviderV2.from_file(
                stub_response, frozen_time=frozen_time
            )
        else:
            provider = OpenAICompatibleHy3JudgeProviderV2()
    if provider.mode != judge_mode:
        raise JudgeEvaluationV2Error(
            "judge_provider_mode_mismatch",
            "prepare",
            "batch",
            "Judge Provider mode differs from the selected mode",
        )
    commit = current_git_commit()
    try:
        clean = git_worktree_clean()
    except RuntimeError as exc:
        raise JudgeEvaluationV2Error(
            "git_status_unavailable",
            "prepare",
            "batch",
            "current Git worktree status is unavailable",
        ) from exc
    dependency_digest = dependency_lock_sha256()
    dependency_lock_verified = not dependency_environment_reason_codes()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e31-judge-stage-", dir=output_path.parent
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
                reference=bundle.judge_reference,
                judge_mode=judge_mode,
                provider=provider,
                git_commit=commit,
                worktree_clean=clean,
                dependency_digest=dependency_digest,
                dependency_lock_verified=dependency_lock_verified,
            )
            episode_id = result["episode_id"]
            (stage / "judge-results" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(result)
            )
            results.append(result)
            if repaired:
                repaired_ids.append(episode_id)
        formal = not inputs.runtime_failures and all(
            result["formal_evaluation_result"] for result in results
        )
        failure_ids = sorted(inputs.runtime_failures)
        manifest = {
            "schema_version": "judge-run-manifest-v2",
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
            "judge_mode": judge_mode,
            "input_runtime_manifest_sha256": inputs.runtime_manifest["manifest_sha256"],
            "input_rule_manifest_sha256": inputs.rule_manifest["manifest_sha256"],
            "requested_episode_ids": sorted(episode_ids or set()),
            "selected_track": track,
            "episode_ids": [result["episode_id"] for result in results],
            "runtime_failure_ids": failure_ids,
            "input_episode_digests": {
                result["episode_id"]: result["episode_sha256"] for result in results
            },
            "input_rule_result_digests": {
                result["episode_id"]: result["rule_result_sha256"] for result in results
            },
            "input_reference_digests": {
                result["episode_id"]: result["judge_reference_sha256"]
                for result in results
            },
            "input_runtime_failure_digests": {
                failure_id: inputs.runtime_failures[failure_id]["failure_sha256"]
                for failure_id in failure_ids
            },
            "runtime_failure_tracks": {
                failure_id: inputs.runtime_failure_tracks[failure_id]
                for failure_id in failure_ids
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
            "formal_evaluation_result": formal,
            "evaluation_status": (
                "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
            ),
            "git_commit": commit,
            "manifest_sha256": "0" * 64,
        }
        manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
        try:
            JudgeRunManifestV2.model_validate(manifest)
        except ValidationError as exc:
            raise JudgeEvaluationV2Error(
                "manifest_invalid",
                "publish",
                "batch",
                "Judge Run Manifest v2 is invalid",
            ) from exc
        if privacy_issues({"results": results, "manifest": manifest}, file="judge-v2"):
            raise JudgeEvaluationV2Error(
                "privacy_rejected",
                "publish",
                "batch",
                "Judge artifacts failed privacy validation",
            )
        (stage / "run-manifest.json").write_bytes(canonical_json_bytes(manifest))
        report = validate_dataset(stage)
        if not report.ok:
            raise JudgeEvaluationV2Error(
                "output_invalid",
                "publish",
                "batch",
                "Judge output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return JudgeEvaluationV2Summary(
        episode_ids=tuple(result["episode_id"] for result in results),
        tracks=tuple(bundle.episode["track"] for bundle in inputs.bundles),
        runtime_failure_ids=tuple(sorted(inputs.runtime_failures)),
        output=output_path,
        statuses=tuple(result["status"] for result in results),
        invalid_episode_ids=tuple(
            result["episode_id"]
            for result in results
            if result["status"] == "invalid_input"
        ),
        judge_error_episode_ids=tuple(
            result["episode_id"]
            for result in results
            if result["status"] == "judge_error"
        ),
        repair_attempted_episode_ids=tuple(repaired_ids),
        formal_evaluation_result=formal,
    )


def _result_base_v3(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
    blind_input: BlindJudgeInputV3,
    rule_manifest: Mapping[str, Any],
    judge_mode: str,
    selection_mode: str,
    worktree_clean: bool,
) -> dict[str, Any]:
    return {
        "schema_version": "judge-result-v3",
        "judge_version": JUDGE_VERSION_V3,
        "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "judge_reference_sha256": reference["reference_sha256"],
        "track": episode["track"],
        "judge_prompt_version": JUDGE_PROMPT_VERSION_V3,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V3,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "blind_input_sha256": blind_input.sha256,
        "judge_mode": judge_mode,
        "evaluation_protocol_release_id": rule_manifest[
            "evaluation_protocol_release_id"
        ],
        "evaluation_protocol_release_sha256": rule_manifest[
            "evaluation_protocol_release_sha256"
        ],
        "benchmark_release_id": rule_manifest["benchmark_release_id"],
        "benchmark_release_sha256": rule_manifest["benchmark_release_sha256"],
        "runtime_run_id": rule_manifest["runtime_run_id"],
        "judge_source_bundle_version": SOURCE_BUNDLE_VERSION,
        "judge_source_bundle_sha256": JUDGE_SOURCE_BUNDLE_SHA256_V3,
        "input_rule_manifest_sha256": rule_manifest["manifest_sha256"],
        "input_rule_manifest_formal_evaluation_result": rule_manifest[
            "formal_evaluation_result"
        ],
        "input_episode_formal_evaluation_result": episode["provenance"][
            "formal_evaluation_result"
        ],
        "input_rule_result_formal_evaluation_result": rule_result[
            "formal_evaluation_result"
        ],
        "input_rule_manifest_trusted_benchmark_run": rule_manifest[
            "trusted_benchmark_run"
        ],
        "selection_mode": selection_mode,
        "worktree_clean": bool(worktree_clean),
    }


def validate_judge_result_v3(
    result: Mapping[str, Any],
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
    rule_manifest: Mapping[str, Any],
    blind_input: BlindJudgeInputV3 | None = None,
) -> tuple[str, ...]:
    """Recompute active linkage, evidence, attribution, and monotonic formality."""

    errors: set[str] = set()
    try:
        validated = JudgeResultV3.model_validate(result).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError:
        return ("judge_contract_invalid",)
    if validated["result_sha256"] != judge_result_digest(validated):
        errors.add("judge_digest_mismatch")
    expected = {
        "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"],
        "rule_result_sha256": rule_result["result_sha256"],
        "judge_reference_sha256": reference["reference_sha256"],
        "track": episode["track"],
        "judge_version": JUDGE_VERSION_V3,
        "judge_prompt_version": JUDGE_PROMPT_VERSION_V3,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V3,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "input_rule_manifest_sha256": rule_manifest["manifest_sha256"],
        "input_rule_manifest_formal_evaluation_result": rule_manifest[
            "formal_evaluation_result"
        ],
        "input_episode_formal_evaluation_result": episode["provenance"][
            "formal_evaluation_result"
        ],
        "input_rule_result_formal_evaluation_result": rule_result[
            "formal_evaluation_result"
        ],
        "input_rule_manifest_trusted_benchmark_run": rule_manifest[
            "trusted_benchmark_run"
        ],
        "evaluation_protocol_release_id": rule_manifest[
            "evaluation_protocol_release_id"
        ],
        "evaluation_protocol_release_sha256": rule_manifest[
            "evaluation_protocol_release_sha256"
        ],
        "benchmark_release_id": rule_manifest["benchmark_release_id"],
        "benchmark_release_sha256": rule_manifest["benchmark_release_sha256"],
        "runtime_run_id": rule_manifest["runtime_run_id"],
        "judge_source_bundle_version": SOURCE_BUNDLE_VERSION,
        "judge_source_bundle_sha256": JUDGE_SOURCE_BUNDLE_SHA256_V3,
    }
    if any(validated[key] != value for key, value in expected.items()):
        errors.add("judge_input_linkage_invalid")
    if blind_input is None:
        try:
            blind_input = build_blind_judge_input_v3(
                episode, rule_result, reference
            )
        except BlindProjectionV2Error:
            return tuple(sorted(errors | {"blind_input_invalid"}))
    if validated["blind_input_sha256"] != blind_input.sha256:
        errors.add("blind_input_digest_mismatch")
    if validated["provider_attestation"]["attestation_sha256"] != (
        provider_attestation_digest(validated["provider_attestation"])
    ):
        errors.add("judge_provider_attestation_invalid")
    if validated["status"] == "complete":
        for path in _all_evidence_paths(validated):
            if (
                path.startswith("capture.")
                or not resolve_evidence_path(episode, path)[0]
                or not path_visible_to_judge_v3(blind_input, path)
            ):
                errors.add("judge_evidence_invalid")
    expected_trust = bool(
        validated["judge_mode"] == "real"
        and validated["provider_attestation"]["attribution_status"] == "eligible"
        and rule_manifest["trusted_benchmark_run"]
        and validated["selection_mode"] == "inherited"
        and validated["worktree_clean"]
        and validated["protocol_eligible"]
        and validated["provider_eligible"]
    )
    if (
        validated["trusted_benchmark_run"] != expected_trust
        or validated["formal_evaluation_result"] is not False
        or validated["evaluation_status"] != "not_a_formal_model_evaluation"
    ):
        errors.add("judge_trust_state_invalid")
    if privacy_issues(validated, file="judge-result-v3"):
        errors.add("judge_privacy_invalid")
    return tuple(sorted(errors))


def _evaluate_one_v3(
    *,
    episode: Mapping[str, Any],
    rule_result: Mapping[str, Any],
    reference: Mapping[str, Any],
    rule_manifest: Mapping[str, Any],
    judge_mode: str,
    provider: JudgeProviderV2,
    selection_mode: str,
    git_commit: str,
    worktree_clean: bool,
    dependency_digest: str,
    dependency_lock_verified: bool = True,
    attempt_log_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    try:
        blind_input = build_blind_judge_input_v3(episode, rule_result, reference)
    except BlindProjectionV2Error as exc:
        raise JudgeEvaluationV2Error(
            "blind_input_invalid",
            "blind",
            str(episode["episode_id"]),
            "DecisionEpisode v4 could not produce a safe blind Judge input",
        ) from exc
    result = _result_base_v3(
        episode=episode,
        rule_result=rule_result,
        reference=reference,
        blind_input=blind_input,
        rule_manifest=rule_manifest,
        judge_mode=judge_mode,
        selection_mode=selection_mode,
        worktree_clean=worktree_clean,
    )
    replies: list[JudgeProviderReplyV2] = []
    repaired = False
    if rule_result["status"] == "invalid_input":
        payload = None
        status = "invalid_input"
        error_code = "rule_invalid_input"
    else:
        payload = None
        error_codes: tuple[str, ...] = ()
        invalid_paths: list[str] = []
        for attempt in range(REPAIR_LIMIT + 1):
            repaired = attempt == 1
            request = build_provider_request_v3(
                blind_input,
                repair_error_codes=error_codes if attempt else (),
                invalid_evidence_paths=invalid_paths if attempt else (),
            )
            request["_budget_call_id"] = f"{episode['episode_id']}:attempt:{attempt + 1}"
            try:
                reply = provider.complete(request)
            except ModelBudgetExceeded:
                error_codes = ("judge_budget_exhausted",)
                _record_judge_attempt(attempt_log_path, episode, blind_input, attempt, None, error_codes)
                break
            except ValueError:
                error_codes = ("judge_request_rejected",)
                _record_judge_attempt(attempt_log_path, episode, blind_input, attempt, None, error_codes)
                break
            except Exception:  # noqa: BLE001 - raw provider exceptions are discarded
                error_codes = ("provider_error",)
                break
            replies.append(reply)
            if reply.status != "completed":
                error_codes = ("provider_error",)
                _record_judge_attempt(attempt_log_path, episode, blind_input, attempt, reply, error_codes)
                break
            if reply.finish_reason == "length":
                payload, error_codes = None, ("response_output_truncated",)
            else:
                payload, error_codes = _validate_payload_v3(
                    reply.content, episode=episode, blind_input=blind_input
                )
            invalid_paths = []
            if "response_evidence_invalid" in error_codes and "response_privacy_invalid" not in error_codes:
                invalid_paths = sorted({p for p in _all_evidence_paths(_parse_response(reply.content))
                                        if not resolve_evidence_path(episode, p)[0]
                                        or not path_visible_to_judge_v3(blind_input, p)})
            _record_judge_attempt(attempt_log_path, episode, blind_input, attempt, reply, error_codes, invalid_paths)
            if reply.budget_ticket is not None and isinstance(provider, OpenAICompatibleHy3JudgeProviderV3):
                provider.budget.record_outcome(
                    reply.budget_ticket,
                    outcome=("complete" if payload is not None else
                             "output_truncated" if reply.finish_reason == "length" else "response_invalid"),
                    error_codes=error_codes,
                )
            if payload is not None:
                repaired = attempt == 1
                break
        if payload is None:
            status = "judge_error"
            error_code = (
                "judge_provider_error"
                if error_codes == ("provider_error",)
                else error_codes[0] if error_codes in {("judge_budget_exhausted",), ("judge_request_rejected",)}
                else "judge_response_invalid"
            )
        else:
            status = "complete"
            error_code = None
    attestation = _provider_attestation(
        mode=judge_mode,
        replies=replies,
        frozen_time=episode["environment"]["frozen_time"],
        git_commit=git_commit,
        worktree_clean=worktree_clean,
        dependency_digest=dependency_digest,
        dependency_lock_verified=dependency_lock_verified,
        configuration_sha256=JUDGE_CONFIG_SHA256_V3,
    )
    protocol_eligible = bool(
        rule_manifest["protocol_eligible"]
        and worktree_clean
        and JUDGE_SOURCE_BUNDLE_SHA256_V3 == source_bundle_sha256("judge")
    )
    provider_eligible = bool(
        judge_mode == "real"
        and attestation["attribution_status"] == "eligible"
        and rule_manifest["provider_eligible"]
    )
    trusted = bool(
        provider_eligible
        and protocol_eligible
        and rule_manifest["trusted_benchmark_run"]
        and selection_mode == "inherited"
        and worktree_clean
    )
    result.update(
        {
            "dimensions": [] if payload is None else payload["dimensions"],
            "semantic_issues": [] if payload is None else payload["semantic_issues"],
            "suggested_hard_gates": (
                [] if payload is None else payload["suggested_hard_gates"]
            ),
            "status": status,
            "provider_attestation": attestation,
            "protocol_eligible": protocol_eligible,
            "provider_eligible": provider_eligible,
            "trusted_benchmark_run": trusted,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
            "error_code": error_code,
            "result_sha256": "0" * 64,
        }
    )
    result["result_sha256"] = judge_result_digest(result)
    errors = validate_judge_result_v3(
        result,
        episode=episode,
        rule_result=rule_result,
        reference=reference,
        rule_manifest=rule_manifest,
        blind_input=blind_input,
    )
    if errors:
        raise JudgeEvaluationV2Error(
            "judge_result_invalid",
            "validate",
            str(episode["episode_id"]),
            "Judge Result v3 failed contract or evidence validation",
        )
    return result, repaired


def _record_judge_attempt(path, episode, blind_input, attempt, reply, errors, invalid_paths=()):
    """Keep rejected public payloads outside frozen result contracts, even on failure."""
    if path is None:
        return
    from .canonical import sha256_digest
    from .recorder import public_projection

    content = None if reply is None else reply.content
    raw_text = content if isinstance(content, str) else None
    malformed = False
    try:
        if isinstance(content, str):
            content = json.loads(content)
        content = public_projection(content)
        sha256_digest(content)  # Also rejects non-finite JSON numbers.
    except (TypeError, ValueError):
        malformed = True
        content = None
    withheld = malformed or bool(privacy_issues(content, file="judge-attempt"))
    row = {
        "record_version": "judge-attempt-evidence-v1", "episode_id": episode["episode_id"],
        "episode_sha256": episode["provenance"]["episode_sha256"], "blind_input_sha256": blind_input.sha256,
        "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V3, "configuration_sha256": JUDGE_CONFIG_SHA256_V3,
        "attempt": attempt + 1, "provider_attempted": reply is not None,
        "budget_ticket": None if reply is None else reply.budget_ticket,
        "finish_reason": None if reply is None else reply.finish_reason,
        "provider_status": None if reply is None else reply.status,
        "validation_error_codes": list(errors), "invalid_evidence_paths": [] if withheld else list(invalid_paths),
        "public_response": None if withheld else content,
        "response_withheld_for_privacy": withheld, "public_response_sha256": sha256_digest(content),
        "response_projection_status": "unparseable_json" if malformed else "privacy_withheld" if withheld else "public",
        "raw_response_text_sha256": None if raw_text is None else hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "raw_response_text_bytes": None if raw_text is None else len(raw_text.encode("utf-8")),
    }
    row["record_sha256"] = sha256_digest(row)
    with path.open("ab") as handle:
        handle.write(canonical_json_bytes(row) + b"\n")


def evaluate_active_judges(
    *,
    episodes: str | Path,
    rules: str | Path,
    output: str | Path,
    judge_mode: str,
    allow_real_judge: bool = False,
    stub_response: str | Path | None = None,
    provider: JudgeProviderV2 | None = None,
    episode_ids: set[str] | None = None,
    track: str | None = None,
    budget_ledger: str | Path | None = None,
) -> JudgeEvaluationV2Summary:
    """Evaluate the active v4 chain and preserve failure-only partitions."""

    output_path = Path(output).resolve()
    if output_path.exists():
        raise JudgeEvaluationV2Error(
            "output_exists", "prepare", "batch", "output directory already exists"
        )
    if judge_mode not in {"stub", "real"}:
        raise JudgeEvaluationV2Error(
            "judge_mode_invalid", "prepare", "batch", "Judge mode is invalid"
        )
    if judge_mode == "real" and not allow_real_judge:
        raise JudgeEvaluationV2Error(
            "real_judge_not_allowed",
            "prepare",
            "batch",
            "real Judge requires --allow-real-judge",
        )
    try:
        inputs = load_e311_inputs(
            episodes=episodes,
            rules=rules,
            episode_ids=episode_ids,
            track=track,
        )
    except E31InputError as exc:
        raise JudgeEvaluationV2Error(
            exc.code, exc.stage, exc.artifact_id, exc.public_message
        ) from exc
    selection_mode = (
        "adhoc_filter"
        if episode_ids is not None
        or track is not None
        or inputs.rule_manifest["selection_mode"] == "adhoc_filter"
        else "inherited"
    )
    if inputs.bundles:
        frozen_time = inputs.bundles[0].episode["environment"]["frozen_time"]
        if provider is None:
            if judge_mode == "stub":
                if stub_response is None:
                    raise JudgeEvaluationV2Error(
                        "stub_response_required",
                        "prepare",
                        "batch",
                        "stub Judge requires a caller-supplied fixed response",
                    )
                provider = FixedResponseJudgeProviderV3.from_file(
                    stub_response, frozen_time=frozen_time
                )
            else:
                if budget_ledger is None:
                    raise JudgeEvaluationV2Error(
                        "judge_budget_required", "prepare", "batch",
                        "real Judge requires the existing shared budget ledger",
                    )
                provider = OpenAICompatibleHy3JudgeProviderV3(budget_ledger=budget_ledger, scope=str(output_path.name))
        if provider.mode != judge_mode:
            raise JudgeEvaluationV2Error(
                "judge_provider_mode_mismatch",
                "prepare",
                "batch",
                "Judge Provider mode differs from the selected mode",
            )
    commit = current_git_commit()
    try:
        clean = git_worktree_clean()
    except RuntimeError as exc:
        raise JudgeEvaluationV2Error(
            "git_status_unavailable",
            "prepare",
            "batch",
            "current Git worktree status is unavailable",
        ) from exc
    dependency_digest = dependency_lock_sha256()
    dependency_lock_verified = not dependency_environment_reason_codes()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_path.name}.e311-judge-stage-", dir=output_path.parent
        )
    )
    results: list[dict[str, Any]] = []
    repaired_ids: list[str] = []
    try:
        (stage / "judge-results").mkdir()
        for bundle in inputs.bundles:
            assert provider is not None
            result, repaired = _evaluate_one_v3(
                episode=bundle.episode,
                rule_result=bundle.rule_result,
                reference=bundle.judge_reference,
                rule_manifest=inputs.rule_manifest,
                judge_mode=judge_mode,
                provider=provider,
                selection_mode=selection_mode,
                git_commit=commit,
                worktree_clean=clean,
                dependency_digest=dependency_digest,
                dependency_lock_verified=dependency_lock_verified,
                attempt_log_path=output_path.parent / f"{output_path.name}-attempts.jsonl",
            )
            episode_id = result["episode_id"]
            (stage / "judge-results" / f"{episode_id}.json").write_bytes(
                canonical_json_bytes(result)
            )
            results.append(result)
            if repaired:
                repaired_ids.append(episode_id)
        failure_ids = sorted(inputs.runtime_failures)
        protocol_eligible = bool(
            inputs.rule_manifest["protocol_eligible"]
            and clean
            and all(result["protocol_eligible"] for result in results)
        )
        provider_eligible = bool(
            inputs.rule_manifest["provider_eligible"]
            and (not results or all(result["provider_eligible"] for result in results))
        )
        trusted = bool(
            inputs.rule_manifest["trusted_benchmark_run"]
            and selection_mode == "inherited"
            and protocol_eligible
            and provider_eligible
            and all(result["trusted_benchmark_run"] for result in results)
        )
        formal = False
        manifest = {
            "schema_version": "judge-run-manifest-v3",
            "runtime_run_id": inputs.rule_manifest["runtime_run_id"],
            "evaluation_protocol_release_id": inputs.rule_manifest[
                "evaluation_protocol_release_id"
            ],
            "evaluation_protocol_release_sha256": inputs.rule_manifest[
                "evaluation_protocol_release_sha256"
            ],
            "benchmark_release_id": inputs.rule_manifest["benchmark_release_id"],
            "benchmark_release_sha256": inputs.rule_manifest[
                "benchmark_release_sha256"
            ],
            "case_suite_sha256": inputs.rule_manifest["case_suite_sha256"],
            "input_episode_schema_version": "decision-episode-v4",
            "input_rule_schema_version": "rule-result-v3",
            "input_reference_schema_version": "judge-reference-v2",
            "input_failure_schema_version": "runtime-failure-v2",
            "judge_version": JUDGE_VERSION_V3,
            "judge_config_version": JUDGE_CONFIG_VERSION_V3,
            "judge_config_sha256": JUDGE_CONFIG_SHA256_V3,
            "judge_prompt_version": JUDGE_PROMPT_VERSION_V3,
            "judge_prompt_sha256": JUDGE_PROMPT_SHA256_V3,
            "rubric_version": RUBRIC_VERSION,
            "rubric_sha256": RUBRIC_SHA256,
            "track_anchor_version": TRACK_ANCHOR_VERSION,
            "track_anchor_sha256": TRACK_ANCHOR_SHA256,
            "repair_limit": REPAIR_LIMIT,
            "judge_mode": judge_mode,
            "judge_source_bundle_version": SOURCE_BUNDLE_VERSION,
            "judge_source_bundle_sha256": JUDGE_SOURCE_BUNDLE_SHA256_V3,
            "input_runtime_manifest_sha256": inputs.runtime_manifest[
                "manifest_sha256"
            ],
            "input_rule_manifest_sha256": inputs.rule_manifest["manifest_sha256"],
            "input_rule_manifest_formal_evaluation_result": inputs.rule_manifest[
                "formal_evaluation_result"
            ],
            "input_rule_manifest_trusted_benchmark_run": inputs.rule_manifest[
                "trusted_benchmark_run"
            ],
            "selection_mode": selection_mode,
            "requested_episode_ids": sorted(episode_ids or set()),
            "selected_track": track,
            "episode_ids": [result["episode_id"] for result in results],
            "runtime_failure_ids": failure_ids,
            "input_episode_digests": {
                result["episode_id"]: result["episode_sha256"] for result in results
            },
            "input_rule_result_digests": {
                result["episode_id"]: result["rule_result_sha256"]
                for result in results
            },
            "input_reference_digests": {
                result["episode_id"]: result["judge_reference_sha256"]
                for result in results
            },
            "input_runtime_failure_digests": {
                failure_id: inputs.runtime_failures[failure_id]["failure_sha256"]
                for failure_id in failure_ids
            },
            "episode_tracks": {
                result["episode_id"]: result["track"] for result in results
            },
            "runtime_failure_tracks": {
                failure_id: inputs.runtime_failure_tracks[failure_id]
                for failure_id in failure_ids
            },
            "blind_input_digests": {
                result["episode_id"]: result["blind_input_sha256"]
                for result in results
            },
            "judge_result_digests": {
                result["episode_id"]: result["result_sha256"] for result in results
            },
            "result_statuses": {
                result["episode_id"]: result["status"] for result in results
            },
            "result_formal_evaluation_states": {
                result["episode_id"]: result["formal_evaluation_result"]
                for result in results
            },
            "result_trusted_benchmark_states": {
                result["episode_id"]: result["trusted_benchmark_run"]
                for result in results
            },
            "protocol_eligible": protocol_eligible,
            "provider_eligible": provider_eligible,
            "trusted_benchmark_run": trusted,
            "formal_evaluation_result": formal,
            "evaluation_status": "not_a_formal_model_evaluation",
            "worktree_clean": clean,
            "git_commit": commit,
            "manifest_sha256": "0" * 64,
        }
        manifest["manifest_sha256"] = artifact_manifest_digest(manifest)
        try:
            JudgeRunManifestV3.model_validate(manifest)
        except ValidationError as exc:
            raise JudgeEvaluationV2Error(
                "manifest_invalid",
                "publish",
                "batch",
                "Judge Run Manifest v3 is invalid",
            ) from exc
        if privacy_issues({"results": results, "manifest": manifest}, file="judge-v3"):
            raise JudgeEvaluationV2Error(
                "privacy_rejected",
                "publish",
                "batch",
                "Judge artifacts failed privacy validation",
            )
        (stage / "run-manifest.json").write_bytes(canonical_json_bytes(manifest))
        report = validate_dataset(stage)
        if not report.ok:
            raise JudgeEvaluationV2Error(
                "output_invalid",
                "publish",
                "batch",
                "Judge output failed final validation",
            )
        os.replace(stage, output_path)
    except BaseException:
        if judge_mode == "real":
            # Paid responses remain inspectable even if later publication fails.
            incomplete = output_path.parent / f"{output_path.name}-incomplete-{stage.name.rsplit('-', 1)[-1]}"
            os.replace(stage, incomplete)
        else:
            shutil.rmtree(stage, ignore_errors=True)
        raise

    return JudgeEvaluationV2Summary(
        episode_ids=tuple(result["episode_id"] for result in results),
        tracks=tuple(
            [bundle.episode["track"] for bundle in inputs.bundles]
            + [inputs.runtime_failure_tracks[item] for item in failure_ids]
        ),
        runtime_failure_ids=tuple(failure_ids),
        output=output_path,
        statuses=tuple(result["status"] for result in results),
        invalid_episode_ids=tuple(
            result["episode_id"]
            for result in results
            if result["status"] == "invalid_input"
        ),
        judge_error_episode_ids=tuple(
            result["episode_id"]
            for result in results
            if result["status"] == "judge_error"
        ),
        repair_attempted_episode_ids=tuple(repaired_ids),
        formal_evaluation_result=formal,
        protocol_eligible=protocol_eligible,
        provider_eligible=provider_eligible,
        trusted_benchmark_run=trusted,
    )
