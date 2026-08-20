"""Deterministic accounting for every model request envelope.

Provider tokenizers are not uniformly available for the OpenAI-compatible
models supported by the harness. This module therefore bounds the supplied
UTF-8 payload with one token per byte. A byte-level BPE token cannot encode less
than one byte, so this may reserve substantially more space than a provider
tokenizer but cannot undercount the supplied text. Canonical JSON and an
explicit chat-template framing reserve are included; this is a versioned safety
policy, not a claim that every provider uses the same hidden template. Output
and one bounded tool result are reserved before a provider call is allowed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.core.config import settings


TOKEN_ESTIMATOR_VERSION = "h5-utf8-byte-framed-upper-bound-v1"
PROMPT_ENVELOPE_VERSION = "h5-prompt-envelope-v1"
_MESSAGE_FRAMING_TOKEN_RESERVE = 32
_TOOL_SCHEMA_FRAMING_TOKEN_RESERVE = 32


class PromptEnvelopeExceeded(RuntimeError):
    """The complete request cannot fit the configured model context window."""

    def __init__(self, breakdown: Mapping[str, Any]):
        self.breakdown = dict(breakdown)
        super().__init__(
            "model request envelope exceeds the configured context window "
            f"({self.breakdown.get('total_tokens')}/"
            f"{self.breakdown.get('model_context_window')} tokens)"
        )


def estimate_text_tokens(value: str) -> int:
    """Return a stable upper bound for any byte-level provider tokenizer."""

    if not value:
        return 0
    return max(1, len(value.encode("utf-8")))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def estimate_json_tokens(value: Any) -> int:
    return estimate_text_tokens(_canonical_json(value))


def estimate_messages_tokens(messages: Iterable[Mapping[str, Any]]) -> int:
    """Count message content, structured calls, roles, and protocol framing."""

    serialized = [dict(message) for message in messages]
    # The canonical JSON bytes bound every supplied role/content/tool-call
    # value including quotes, slashes, control characters and Unicode. Reserve
    # additional per-message framing for provider chat templates which are not
    # represented in the public request JSON.
    return max(
        1,
        estimate_json_tokens(serialized)
        + _MESSAGE_FRAMING_TOKEN_RESERVE * (len(serialized) + 1),
    )


def estimate_tool_schema_tokens(tools: Sequence[Mapping[str, Any]] | None) -> int:
    if not tools:
        return 1
    return max(
        1,
        estimate_json_tokens(list(tools)) + _TOOL_SCHEMA_FRAMING_TOKEN_RESERVE,
    )


@dataclass(frozen=True)
class EnvelopeInputs:
    model_context_window: int
    context_token_budget: int
    system_prompt_tokens: int
    tool_schema_tokens: int
    output_reserve_tokens: int
    tool_result_reserve_tokens: int

    @property
    def fixed_tokens(self) -> int:
        return (
            self.system_prompt_tokens
            + self.tool_schema_tokens
            + self.output_reserve_tokens
            + self.tool_result_reserve_tokens
        )

    @property
    def effective_context_budget(self) -> int:
        return max(
            0,
            min(
                self.context_token_budget,
                self.model_context_window - self.fixed_tokens,
            ),
        )


def envelope_inputs(
    *,
    system_prompt: str,
    tools: Sequence[Mapping[str, Any]] | None,
) -> EnvelopeInputs:
    return EnvelopeInputs(
        model_context_window=settings.MODEL_CONTEXT_WINDOW,
        context_token_budget=settings.AGENT_CONTEXT_TOKEN_BUDGET,
        system_prompt_tokens=estimate_messages_tokens(
            [{"role": "system", "content": system_prompt}]
        ),
        tool_schema_tokens=estimate_tool_schema_tokens(tools),
        output_reserve_tokens=settings.AGENT_OUTPUT_TOKEN_RESERVE,
        tool_result_reserve_tokens=settings.AGENT_TOOL_RESULT_TOKEN_RESERVE,
    )


def estimate_context_message_tokens(
    *,
    system_prompt: str,
    user_content: str,
) -> int:
    """Return the exact safe-envelope delta added by the user Context message."""

    system_tokens = estimate_messages_tokens(
        [{"role": "system", "content": system_prompt}]
    )
    complete_tokens = estimate_messages_tokens(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
    )
    return max(1, complete_tokens - system_tokens)


def snapshot_budget_breakdown(
    inputs: EnvelopeInputs,
    *,
    context_tokens: int,
) -> dict[str, Any]:
    total_tokens = inputs.fixed_tokens + context_tokens
    return {
        "model_context_window": inputs.model_context_window,
        "context_token_budget": inputs.context_token_budget,
        "system_prompt_tokens": inputs.system_prompt_tokens,
        "tool_schema_tokens": inputs.tool_schema_tokens,
        "context_tokens": context_tokens,
        "output_reserve_tokens": inputs.output_reserve_tokens,
        "tool_result_reserve_tokens": inputs.tool_result_reserve_tokens,
        "total_tokens": total_tokens,
        "effective_context_budget": inputs.effective_context_budget,
        "envelope_version": PROMPT_ENVELOPE_VERSION,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "config_sources": {
            "model_context_window": "MODEL_CONTEXT_WINDOW",
            "context_token_budget": "AGENT_CONTEXT_TOKEN_BUDGET",
            "output_reserve_tokens": "AGENT_OUTPUT_TOKEN_RESERVE",
            "tool_result_reserve_tokens": "AGENT_TOOL_RESULT_TOKEN_RESERVE",
        },
    }


def request_budget_breakdown(
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Account the exact serialized request immediately before provider I/O."""

    prompt_tokens = estimate_messages_tokens(messages)
    tool_schema_tokens = estimate_tool_schema_tokens(tools)
    total_tokens = (
        prompt_tokens
        + tool_schema_tokens
        + settings.AGENT_OUTPUT_TOKEN_RESERVE
        + settings.AGENT_TOOL_RESULT_TOKEN_RESERVE
    )
    return {
        "model_context_window": settings.MODEL_CONTEXT_WINDOW,
        "prompt_tokens": prompt_tokens,
        "tool_schema_tokens": tool_schema_tokens,
        "output_reserve_tokens": settings.AGENT_OUTPUT_TOKEN_RESERVE,
        "tool_result_reserve_tokens": settings.AGENT_TOOL_RESULT_TOKEN_RESERVE,
        "total_tokens": total_tokens,
        "envelope_version": PROMPT_ENVELOPE_VERSION,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
    }


def ensure_request_fits(
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    breakdown = request_budget_breakdown(messages=messages, tools=tools)
    if breakdown["total_tokens"] > breakdown["model_context_window"]:
        raise PromptEnvelopeExceeded(breakdown)
    return breakdown
