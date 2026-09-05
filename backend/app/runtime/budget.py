"""Shared durable budget accounting for root and child Agent Runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from app.core.config import settings
from app.core.time import coerce_legacy_utc, utc_now


def default_budget() -> dict[str, int | float | str]:
    return {
        "model_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls": 0,
        "network_requests": 0,
        "elapsed_ms": 0,
        "approval_wait_ms": 0,
        "estimated_cost_usd": 0.0,
        "stopped_reason": "",
    }


def normalize_budget(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {**default_budget(), **dict(value or {})}


def refresh_elapsed(
    budget: dict[str, Any],
    started_at: datetime | None,
    *,
    ended_at: datetime | None = None,
) -> None:
    if started_at is None:
        return
    budget["elapsed_ms"] = max(
        int(budget.get("elapsed_ms") or 0),
        0,
        int(
            (
                coerce_legacy_utc(ended_at or utc_now())
                - coerce_legacy_utc(started_at)
            ).total_seconds()
            * 1000
        ) - max(0, int(budget.get("approval_wait_ms") or 0)),
    )


def budget_reason(
    budget: dict[str, Any],
    *,
    started_at: datetime | None,
    tool_call_limit: int | None = None,
) -> str | None:
    """Check execution time and usage, excluding durable user approval waits."""

    refresh_elapsed(budget, started_at)
    if (
        settings.AGENT_MAX_ELAPSED_SECONDS
        and budget["elapsed_ms"] >= settings.AGENT_MAX_ELAPSED_SECONDS * 1000
    ):
        return "elapsed_limit"
    if budget["model_calls"] >= settings.AGENT_MAX_MODEL_CALLS:
        return "model_call_limit"
    if budget["tool_calls"] >= (tool_call_limit or settings.AGENT_MAX_TOOL_CALLS):
        return "tool_call_limit"
    if (
        settings.AGENT_MAX_ESTIMATED_COST_USD > 0
        and budget["estimated_cost_usd"] >= settings.AGENT_MAX_ESTIMATED_COST_USD
    ):
        return "cost_limit"
    return None


def reserve_model_call(budget: dict[str, Any]) -> None:
    budget["model_calls"] = int(budget.get("model_calls") or 0) + 1


def reserve_tool_call(budget: dict[str, Any], tool_name: str) -> None:
    budget["tool_calls"] = int(budget.get("tool_calls") or 0) + 1
    if tool_name in {"web_search", "web_open"}:
        budget["network_requests"] = int(budget.get("network_requests") or 0) + 1


def record_model_usage(budget: dict[str, Any], usage: Any | None) -> None:
    if usage is None:
        return
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    budget["prompt_tokens"] = int(budget.get("prompt_tokens") or 0) + prompt_tokens
    budget["completion_tokens"] = (
        int(budget.get("completion_tokens") or 0) + completion_tokens
    )
    budget["estimated_cost_usd"] = float(
        budget.get("estimated_cost_usd") or 0.0
    ) + (
        prompt_tokens / 1_000_000 * settings.MODEL_INPUT_PRICE_PER_1M
        + completion_tokens / 1_000_000 * settings.MODEL_OUTPUT_PRICE_PER_1M
    )
