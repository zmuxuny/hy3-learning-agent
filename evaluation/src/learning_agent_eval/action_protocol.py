"""Versioned, exact action declarations for the isolated evaluation Runtime.

The declaration is model output, not an evaluator classification.  Parsing is
strict JSON framed by a fixed first-line marker, so no natural-language keyword
heuristic can silently manufacture an action class.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .canonical import canonical_json, sha256_digest

ACTION_DECLARATION_PROTOCOL_VERSION = "model-action-declaration-v1"
ACTION_DECLARATION_PREFIX = "<model-action-v1>"
ACTION_DECLARATION_SUFFIX = "</model-action-v1>"
ACTION_CLASSES = (
    "PROPOSE_PLAN",
    "REQUEST_USER_INPUT",
    "WAIT",
    "INTERVENE_MESSAGE",
    "INTERVENE_QUIZ_OR_REVIEW",
    "PROPOSE_PLAN_ADJUSTMENT",
    "ACCEPT",
    "REVISION_REQUIRED",
    "INSUFFICIENT_EVIDENCE",
    "REQUEST_CLARIFICATION",
    "NO_OP",
    "PROPOSE_CHANGE",
    "APPLY_REVERSIBLE_PATCH",
    "REQUEST_APPROVAL",
)
ACTION_DECLARATION_INSTRUCTION = (
    "Evaluation action protocol (model-action-declaration-v1): for every main "
    "Agent response, put exactly one declaration on the first line using "
    '<model-action-v1>{"action_classes":["ACTION"]}</model-action-v1>. '
    "List every intended action in execution order using only the registered "
    "action classes. The declaration describes your own decision; it does not "
    "claim that an action succeeded. Continue with the ordinary answer on the "
    "next line. Child and auxiliary model calls do not use this protocol."
)
ACTION_DECLARATION_PROTOCOL_SHA256 = sha256_digest(
    {
        "version": ACTION_DECLARATION_PROTOCOL_VERSION,
        "framing": {
            "prefix": ACTION_DECLARATION_PREFIX,
            "suffix": ACTION_DECLARATION_SUFFIX,
            "position": "first_line",
        },
        "action_classes": list(ACTION_CLASSES),
        "instruction": ACTION_DECLARATION_INSTRUCTION,
    }
)


def evaluation_system_prompt(base_prompt: str) -> str:
    """Append the frozen declaration protocol to the production prompt."""

    return f"{base_prompt.rstrip()}\n\n{ACTION_DECLARATION_INSTRUCTION}"


def render_action_declaration(text: str, actions: Iterable[str]) -> str:
    """Render a deterministic scripted response using the real protocol."""

    action_list = list(actions)
    if not action_list:
        return text
    if len(action_list) != len(set(action_list)) or any(
        action not in ACTION_CLASSES for action in action_list
    ):
        raise ValueError("action declarations must be unique registered actions")
    envelope = canonical_json({"action_classes": action_list})
    body = text.lstrip("\n")
    return (
        f"{ACTION_DECLARATION_PREFIX}{envelope}{ACTION_DECLARATION_SUFFIX}"
        f"{chr(10) + body if body else ''}"
    )


def parse_action_declaration(text: str) -> tuple[str, tuple[str, ...], str]:
    """Return ``status, actions, public_text`` without guessing from prose."""

    first_line, separator, remainder = text.partition("\n")
    if not first_line.startswith(ACTION_DECLARATION_PREFIX):
        return "missing", (), text
    if not first_line.endswith(ACTION_DECLARATION_SUFFIX):
        return "invalid", (), text
    encoded = first_line[
        len(ACTION_DECLARATION_PREFIX) : -len(ACTION_DECLARATION_SUFFIX)
    ]
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        return "invalid", (), text
    if not isinstance(value, dict) or set(value) != {"action_classes"}:
        return "invalid", (), text
    actions = value["action_classes"]
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(action, str) for action in actions)
        or len(actions) != len(set(actions))
        or any(action not in ACTION_CLASSES for action in actions)
        or encoded != canonical_json({"action_classes": actions})
    ):
        return "invalid", (), text
    return "valid", tuple(actions), remainder if separator else ""
