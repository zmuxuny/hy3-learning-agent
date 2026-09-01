"""Public, deterministic completeness results for E2 Episodes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import CanonicalizationError, sha256_digest
from .integrity import integrity_result_digest
from .validator import resolve_evidence_path, validate_episode


def _stage(code: str) -> str:
    prefix = code.split(".", 1)[0]
    return {
        "schema": "schema",
        "privacy": "privacy",
        "canonical": "canonical",
        "digest": "digest",
        "snapshot": "snapshot",
        "delta": "state_delta",
        "reference": "references",
        "trace": "trace",
        "operation": "operation",
        "guard": "guard",
        "oracle": "oracle",
        "environment": "environment",
        "provenance": "provenance",
        "completeness": "completeness",
        "result": "result",
    }.get(prefix, "validation")


def _input_digest(episode: Mapping[str, Any]) -> str:
    provenance = episode.get("provenance")
    if isinstance(provenance, Mapping):
        digest = provenance.get("episode_sha256")
        if isinstance(digest, str) and len(digest) == 64:
            return digest
    return sha256_digest(
        {
            "schema_version": episode.get("schema_version"),
            "episode_id": episode.get("episode_id"),
            "invalid_input": True,
        }
    )


def check_episode_integrity(episode: Mapping[str, Any]) -> dict[str, Any]:
    """Return safe issue metadata; rejected values never enter the result."""

    episode_id = episode.get("episode_id")
    public_episode_id = (
        episode_id
        if isinstance(episode_id, str) and episode_id
        else "invalid-episode"
    )
    try:
        issues = validate_episode(episode, source=public_episode_id)
    except (CanonicalizationError, KeyError, TypeError, ValueError):
        issues = ()
        internal_error = True
    else:
        internal_error = False
    errors: list[dict[str, Any]] = []
    for issue in issues:
        candidate = issue.path.removeprefix("$.")
        evidence_path = None
        if candidate != "$" and resolve_evidence_path(episode, candidate)[0]:
            evidence_path = candidate
        errors.append(
            {
                "error_code": issue.code,
                "stage": _stage(issue.code),
                "episode_id": public_episode_id,
                "message": issue.message,
                "evidence_path": evidence_path,
            }
        )
    if internal_error:
        errors.append(
            {
                "error_code": "integrity.invalid_structure",
                "stage": "validation",
                "episode_id": public_episode_id,
                "message": "Episode structure cannot be validated safely.",
                "evidence_path": None,
            }
        )
    errors.sort(
        key=lambda item: (
            item["stage"],
            item["error_code"],
            item["evidence_path"] or "",
        )
    )
    result = {
        "schema_version": "integrity-result-v1",
        "episode_id": public_episode_id,
        "episode_sha256": _input_digest(episode),
        "status": "invalid" if errors else "valid",
        "errors": errors,
        "result_sha256": "0" * 64,
    }
    result["result_sha256"] = integrity_result_digest(result)
    return result
