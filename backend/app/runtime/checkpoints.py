"""Versioned durable checkpoint envelopes for root and child Agent Runs."""

from __future__ import annotations

from typing import Any


CHECKPOINT_SCHEMA_VERSION = 1


def normalize_checkpoint(
    value: dict[str, Any] | None,
    *,
    kind: str = "agent",
) -> dict[str, Any] | None:
    """Return the canonical H3 envelope, accepting pre-H3 test/DB payloads."""

    if not value:
        return None
    source = dict(value)
    legacy_calls = list(source.get("pending_tool_calls") or [])
    current = source.get("current_tool_call")
    remaining = list(source.get("remaining_tool_calls") or legacy_calls)
    phase = str(source.get("phase") or "")
    if not phase:
        phase = "tool_ready" if current is not None or remaining else "awaiting_model"
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "kind": str(source.get("kind") or kind),
        "phase": phase,
        "step": int(source.get("step") or 0),
        "messages": list(source.get("messages") or []),
        "current_tool_call": current,
        "remaining_tool_calls": remaining,
        "current_invocation_id": source.get("current_invocation_id"),
        "context_snapshot_id": source.get("context_snapshot_id"),
        "cards": list(source.get("cards") or []),
        "budget_usage": dict(source.get("budget_usage") or {}),
        "state_version": int(source.get("state_version") or 1),
    }
    for key in (
        "action_key",
        "assignment_index",
        "role",
        "objective",
        "context",
        "allowlist",
        "max_steps",
        "tool_calls_used",
        "granted_tool_call_ids",
        "retry_count",
        "retry_not_before",
        "final_text",
        "final_message_key",
    ):
        if key in source:
            checkpoint[key] = source[key]
    return checkpoint


def make_checkpoint(
    *,
    kind: str,
    phase: str,
    step: int,
    messages: list[dict[str, Any]],
    current_tool_call: dict[str, Any] | None = None,
    remaining_tool_calls: list[dict[str, Any]] | None = None,
    current_invocation_id: int | None = None,
    context_snapshot_id: int | None = None,
    cards: list[dict[str, Any]] | None = None,
    budget_usage: dict[str, Any] | None = None,
    state_version: int = 1,
    identity: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "kind": kind,
        "phase": phase,
        "step": int(step),
        "messages": list(messages),
        "current_tool_call": current_tool_call,
        "remaining_tool_calls": list(remaining_tool_calls or []),
        "current_invocation_id": current_invocation_id,
        "context_snapshot_id": context_snapshot_id,
        "cards": list(cards or []),
        "budget_usage": dict(budget_usage or {}),
        "state_version": max(1, int(state_version)),
    }
    if identity:
        checkpoint.update(identity)
    checkpoint.update(extra)
    return checkpoint


def pending_calls(checkpoint: dict[str, Any] | None) -> list[dict[str, Any]]:
    normalized = normalize_checkpoint(checkpoint)
    if normalized is None:
        return []
    current = normalized.get("current_tool_call")
    return ([dict(current)] if isinstance(current, dict) else []) + [
        dict(item) for item in normalized.get("remaining_tool_calls") or []
    ]
