"""Stable E3.1 runtime metadata without importing product configuration."""

from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import sha256_digest
from .provider_config import ProviderEndpoint

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEPENDENCY_LOCK_VERSION = "evaluation-runtime-lock-v1"
DEPENDENCY_LOCK_FILE = "evaluation/runtime-requirements.lock"
ENDPOINT_POLICY_VERSION = "hy3-configured-endpoint-policy-v2"
HY3_ENDPOINT_ID = "tencent-tokenhub-primary"  # Historical default, not actual run attribution.
HY3_ENDPOINT_ORIGIN = "https://tokenhub.tencentmaas.com"
HY3_API_BASE = f"{HY3_ENDPOINT_ORIGIN}/v1"
HY3_MODEL = "hy3"
ENDPOINT_POLICY_SHA256 = sha256_digest({
    "version": ENDPOINT_POLICY_VERSION,
    "transport": "openai-compatible-credential-free-https",
    "configuration": "OPENAI_API_BASE; immutable api_base recorded per attestation",
    "request_model": HY3_MODEL,
})
AGENT_RUNTIME_CONFIG_SHA256 = sha256_digest(
    {
        "scope": "agent_runtime",
        "endpoint_policy_sha256": ENDPOINT_POLICY_SHA256,
        "model": HY3_MODEL,
        "temperature": "0.9",
        "reasoning_effort": "high",
        "max_tokens": 16000,
        "n": 1,
        "sdk_max_retries": 0,
        "cost_policy": "hy3-prepaid-budget-v1",
        "max_steps": 8,
        "max_model_calls": 8,
        "max_tool_calls": 16,
    }
)
def dependency_lock_sha256(project_root: Path = PROJECT_ROOT) -> str:
    """Hash the exact committed dependency closure."""

    return hashlib.sha256(
        (project_root / DEPENDENCY_LOCK_FILE).read_bytes()
    ).hexdigest()


def dependency_environment_reason_codes(
    project_root: Path = PROJECT_ROOT,
) -> tuple[str, ...]:
    """Compare installed distributions with every exact entry in the lock."""

    try:
        lines = (
            (project_root / DEPENDENCY_LOCK_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        return ("provider.dependency_lock_missing",)
    reasons: set[str] = set()
    entries = [
        line.strip() for line in lines if line.strip() and not line.startswith("#")
    ]
    if not entries:
        return ("provider.dependency_lock_empty",)
    if entries != sorted(entries, key=lambda item: item.partition("==")[0].casefold()):
        reasons.add("provider.dependency_lock_unsorted")
    for entry in entries:
        name, separator, expected = entry.partition("==")
        if not separator or not name or not expected:
            reasons.add("provider.dependency_lock_invalid")
            continue
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            reasons.add("provider.dependency_missing")
            continue
        if actual != expected:
            reasons.add("provider.dependency_version_mismatch")
    return tuple(sorted(reasons))


def provider_attribution_reason_codes(
    attestation: Mapping[str, Any],
) -> tuple[str, ...]:
    """Recompute why a real Hy3 attribution is not formally eligible."""

    if attestation.get("invocation_mode") != "real":
        return ()
    reasons: set[str] = set()
    try:
        lock_digest = dependency_lock_sha256()
    except OSError:
        lock_digest = None
        reasons.add("provider.dependency_lock_missing")
    try:
        endpoint = ProviderEndpoint(attestation.get("api_base") or "")
    except ValueError:
        return ("provider.endpoint_configuration_invalid",)
    expected_fields = {
        "api_base": endpoint.api_base,
        "provider_id": endpoint.provider_id,
        "endpoint_policy_version": ENDPOINT_POLICY_VERSION,
        "endpoint_policy_sha256": ENDPOINT_POLICY_SHA256,
        "endpoint_id": endpoint.endpoint_id,
        "endpoint_origin": endpoint.origin,
        "configured_model": HY3_MODEL,
        "dependency_lock_version": DEPENDENCY_LOCK_VERSION,
        "dependency_lock_sha256": lock_digest,
    }
    if any(attestation.get(key) != value for key, value in expected_fields.items()):
        reasons.add("provider.configuration_not_allowlisted")
    scope = attestation.get("scope")
    if scope == "agent_runtime":
        expected_configuration = AGENT_RUNTIME_CONFIG_SHA256
    else:
        from .rubric import JUDGE_CONFIG_SHA256_V2, JUDGE_CONFIG_SHA256_V3

        expected_configuration = {JUDGE_CONFIG_SHA256_V2, JUDGE_CONFIG_SHA256_V3}
    actual_configuration = attestation.get("configuration_sha256")
    if (
        actual_configuration not in expected_configuration
        if isinstance(expected_configuration, set)
        else actual_configuration != expected_configuration
    ):
        reasons.add("provider.configuration_digest_mismatch")
    if not attestation.get("worktree_clean"):
        reasons.add("provider.worktree_dirty")
    if not attestation.get("dependency_lock_verified"):
        reasons.add("provider.dependency_lock_unverified")
    calls = attestation.get("calls")
    if not isinstance(calls, list) or not calls:
        reasons.add("provider.calls_missing")
        calls = []
    for call in calls:
        if not isinstance(call, Mapping) or call.get("request_model") != HY3_MODEL:
            reasons.add("provider.request_model_mismatch")
            continue
        if call.get("status") != "completed":
            reasons.add("provider.call_incomplete")
        if call.get("response_model") != HY3_MODEL:
            reasons.add("provider.response_model_mismatch")
        if not call.get("provider_request_id"):
            reasons.add("provider.request_id_missing")
    return tuple(sorted(reasons))


def git_worktree_clean(project_root: Path = PROJECT_ROOT) -> bool:
    """Return the auditable Git cleanliness bit or fail instead of guessing."""

    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git_status_unavailable")
    return not completed.stdout
