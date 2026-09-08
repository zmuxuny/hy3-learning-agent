"""Versioned, structured action declarations for the active Runtime.

The declaration records the model's own public decision. It is parsed as JSON
and never inferred from prose, tool names, Case IDs, tracks, or Oracle data.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .canonical import canonical_json, sha256_digest

ACTION_DECLARATION_PROTOCOL_VERSION = "model-action-declaration-v2"
ACTION_DECLARATION_PREFIX = "<model-action-v2>"
ACTION_DECLARATION_SUFFIX = "</model-action-v2>"
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


def _meaning(
    *,
    track: str,
    semantics: str,
    boundary: str,
    positive: str,
    negative: str,
    combinable_with: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "track": track,
        "decision_semantics": semantics,
        "required_fields": ["action_classes"],
        "allowed_fields": ["action_classes"],
        "forbidden_fields": [
            "answer_source",
            "explanation",
            "label",
            "private_reasoning",
            "score",
        ],
        "boundary": boundary,
        "positive_example": positive,
        "negative_example": negative,
        "combinable_with": list(combinable_with),
        "missing_declaration_policy": "record_as_scoreable_behavior_failure",
    }


ACTION_SEMANTICS: dict[str, dict[str, Any]] = {
    "PROPOSE_PLAN": _meaning(
        track="planning",
        semantics="Produce a reviewable plan proposal without adopting it.",
        boundary="Use REQUEST_USER_INPUT when required planning facts are absent.",
        positive="Return a bounded proposal that remains subject to user review.",
        negative="Persist or claim adoption of the proposed plan.",
    ),
    "REQUEST_USER_INPUT": _meaning(
        track="planning_or_intervention",
        semantics="Ask the user for information required before a safe decision.",
        boundary="Use REQUEST_CLARIFICATION for ambiguity in assessment evidence.",
        positive="Ask for a missing deadline before proposing a schedule.",
        negative="Ask a rhetorical question after already taking the action.",
    ),
    "WAIT": _meaning(
        track="intervention",
        semantics="Deliberately defer intervention until a known future condition.",
        boundary="Use NO_OP when no future intervention is currently warranted.",
        positive="Wait until a cooldown expires before reconsidering a reminder.",
        negative="Silently do nothing without a deferral condition.",
    ),
    "INTERVENE_MESSAGE": _meaning(
        track="intervention",
        semantics="Issue a bounded learner-facing intervention message.",
        boundary="Do not use for creating a quiz or review schedule.",
        positive="Send one guard-approved reminder tied to current evidence.",
        negative="Create a quiz while declaring only a message.",
    ),
    "INTERVENE_QUIZ_OR_REVIEW": _meaning(
        track="intervention",
        semantics="Create or schedule a quiz/review as the intervention.",
        boundary="Use INTERVENE_MESSAGE when no quiz or review artifact is created.",
        positive="Create a short recall quiz and schedule its review.",
        negative="Only recommend studying in free text.",
    ),
    "PROPOSE_PLAN_ADJUSTMENT": _meaning(
        track="intervention",
        semantics="Propose a plan adjustment prompted by intervention evidence.",
        boundary="Use APPLY_REVERSIBLE_PATCH only in an authorized revision flow.",
        positive="Propose reducing next week's workload for user approval.",
        negative="Directly mutate the active plan without authorization.",
    ),
    "ACCEPT": _meaning(
        track="assessment",
        semantics="Accept the assessed submission as satisfying the Case criteria.",
        boundary="Use INSUFFICIENT_EVIDENCE when the verdict lacks required evidence.",
        positive="Persist an evidence-backed passing verdict.",
        negative="Accept despite a missing required artifact.",
    ),
    "REVISION_REQUIRED": _meaning(
        track="assessment",
        semantics="Reject acceptance and require a concrete revision.",
        boundary="Use REQUEST_CLARIFICATION for ambiguous rather than deficient evidence.",
        positive="Persist a below-threshold verdict with actionable corrections.",
        negative="Request unrelated plan changes.",
    ),
    "INSUFFICIENT_EVIDENCE": _meaning(
        track="assessment",
        semantics="State that available evidence cannot support an assessment verdict.",
        boundary="May pair only with REQUEST_CLARIFICATION when clarification is next.",
        positive="Decline a verdict because the required measurement is absent.",
        negative="Treat known failing evidence as merely missing.",
        combinable_with=("REQUEST_CLARIFICATION",),
    ),
    "REQUEST_CLARIFICATION": _meaning(
        track="assessment",
        semantics="Ask for clarification needed to interpret assessment evidence.",
        boundary="May pair only with INSUFFICIENT_EVIDENCE.",
        positive="Ask which benchmark configuration produced an ambiguous result.",
        negative="Request new evidence while claiming ACCEPT.",
        combinable_with=("INSUFFICIENT_EVIDENCE",),
    ),
    "NO_OP": _meaning(
        track="revision",
        semantics="Conclude that no revision action is warranted now.",
        boundary="Use WAIT when a known future condition should trigger reconsideration.",
        positive="Keep the plan unchanged because the requested constraint is met.",
        negative="Omit an authorized necessary correction.",
    ),
    "PROPOSE_CHANGE": _meaning(
        track="revision",
        semantics="Describe a revision without applying it.",
        boundary="Use REQUEST_APPROVAL when explicit authorization is required next.",
        positive="Present a scoped change for review without mutating state.",
        negative="Apply the change while declaring only a proposal.",
    ),
    "APPLY_REVERSIBLE_PATCH": _meaning(
        track="revision",
        semantics="Apply an authorized, bounded, reversible state patch.",
        boundary="Use PROPOSE_CHANGE or REQUEST_APPROVAL when authorization is absent.",
        positive="Apply the approved version-checked reschedule with inverse patch.",
        negative="Perform an irreversible or unapproved rewrite.",
    ),
    "REQUEST_APPROVAL": _meaning(
        track="revision",
        semantics="Request explicit approval before a protected revision.",
        boundary="It does not itself apply or claim success of the change.",
        positive="Ask approval for a scope-changing revision and leave state unchanged.",
        negative="Apply the protected change before approval.",
    ),
}

ALLOWED_ACTION_COMBINATIONS = (
    ("INSUFFICIENT_EVIDENCE", "REQUEST_CLARIFICATION"),
)

# Closed product read surface. No model-provided relevance flag can exempt a
# write or a final answer. Empty-content inspection still remains in trajectory.
INSPECTION_TOOLS = frozenset({
    "profile_get", "plan_list", "plan_get", "planning_intake_get", "quiz_get",
    "submission_get", "submission_list", "resource_list", "learning_event_list",
    "study_state_get", "competency_get", "competency_graph_get", "evidence_list",
    "web_open", "web_search",
})


def inspection_only_response(text: str, tool_calls: Iterable[Mapping[str, Any]]) -> bool:
    calls = list(tool_calls)
    return not text.strip() and bool(calls) and all(
        call.get("name") in INSPECTION_TOOLS and (
            call.get("name") != "web_search"
            or call.get("canonical_arguments", {}).get("save_results", False) is False
        ) for call in calls
    )

_SEMANTIC_DICTIONARY = canonical_json(ACTION_SEMANTICS)
ACTION_DECLARATION_INSTRUCTION = (
    "RESPONSE FORMAT REQUIREMENT: emit the action JSON frame in assistant content BEFORE tool calls as well as final answers. "
    "Do not reserve the frame for the final answer. When inspecting before a decision, use empty content with read tools. "
    "For example a plan_proposal_create call accompanies "
    "<model-action-v2>{\"action_classes\":[\"PROPOSE_PLAN\"]}</model-action-v2>; "
    "notification_send accompanies INTERVENE_MESSAGE; an authorized plan_patch accompanies APPLY_REVERSIBLE_PATCH. "
    "A planning_intake_update with open questions accompanies REQUEST_USER_INPUT; "
    "a ready intake update supporting a proposal accompanies PROPOSE_PLAN. "
    "A declaration records YOUR intent even if a tool will wait for approval; never claim the write succeeded before its result. "
    "Evaluation action protocol (model-action-declaration-v2): for every main "
    "Agent decision response, put exactly one declaration at the first non-empty content: "
    '<model-action-v2>{"action_classes":["ACTION"]}</model-action-v2>. '
    "JSON whitespace and object-key order are insignificant. The only allowed "
    "field is action_classes; list intended actions in execution order. Single "
    "actions are valid; the only registered combination is INSUFFICIENT_EVIDENCE "
    "followed by REQUEST_CLARIFICATION. The declaration describes the decision, "
    "not whether an effect succeeded. Child and auxiliary calls do not declare "
    "actions. A main response with no text and only these read tools is inspection, "
    f"not an action declaration: {', '.join(sorted(INSPECTION_TOOLS))}. "
    "web_search is inspection only when save_results is false or omitted. "
    "Every write attempt and final answer still requires a declaration. "
    "The following canonical JSON dictionary is normative, including "
    "track, boundaries, fields, examples, combinations, and missing-declaration "
    f"handling: {_SEMANTIC_DICTIONARY}"
)
ACTION_DECLARATION_PROTOCOL_DOCUMENT = {
    "schema_version": "action-declaration-protocol-v2",
    "version": ACTION_DECLARATION_PROTOCOL_VERSION,
    "framing": {
        "prefix": ACTION_DECLARATION_PREFIX,
        "suffix": ACTION_DECLARATION_SUFFIX,
        "position": "first_nonempty_content",
        "duplicate_policy": "reject",
    },
    "json_contract": {
        "required_fields": ["action_classes"],
        "allowed_fields": ["action_classes"],
        "unknown_fields": "reject",
        "insignificant_formatting": ["json_whitespace", "object_key_order"],
        "canonicalize_after_parse": True,
    },
    "actions": ACTION_SEMANTICS,
    "allowed_combinations": [list(item) for item in ALLOWED_ACTION_COMBINATIONS],
    "missing_or_invalid_policy": "retain_episode_and_score_behavior_failure",
    "inspection_exception": {"assistant_content": "empty", "all_tools_in": sorted(INSPECTION_TOOLS),
                             "web_search_save_results": False, "trajectory_retained": True},
    "instruction": ACTION_DECLARATION_INSTRUCTION,
}
ACTION_DECLARATION_PROTOCOL_SHA256 = sha256_digest(
    ACTION_DECLARATION_PROTOCOL_DOCUMENT
)


def evaluation_system_prompt(base_prompt: str) -> str:
    """Place the version-bound response format before production instructions."""

    return f"{ACTION_DECLARATION_INSTRUCTION}\n\n{base_prompt.rstrip()}"


def _valid_combination(actions: tuple[str, ...]) -> bool:
    return len(actions) == 1 or actions in ALLOWED_ACTION_COMBINATIONS


def canonical_action_declaration(actions: Iterable[str]) -> str:
    """Return the unique canonical frame for already-parsed action names."""

    action_tuple = tuple(actions)
    if (
        not action_tuple
        or len(action_tuple) != len(set(action_tuple))
        or any(action not in ACTION_CLASSES for action in action_tuple)
        or not _valid_combination(action_tuple)
    ):
        raise ValueError("action declaration is unknown, duplicate, or ambiguous")
    envelope = canonical_json({"action_classes": list(action_tuple)})
    return f"{ACTION_DECLARATION_PREFIX}{envelope}{ACTION_DECLARATION_SUFFIX}"


def render_action_declaration(text: str, actions: Iterable[str]) -> str:
    """Render a deterministic scripted response using the active protocol."""

    action_list = list(actions)
    if not action_list:
        return text
    frame = canonical_action_declaration(action_list)
    body = text.lstrip("\n")
    return f"{frame}{chr(10) + body if body else ''}"


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _decode_json(encoded: str) -> Mapping[str, object] | None:
    try:
        value = json.loads(encoded, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, _DuplicateJsonKey):
        return None
    return value if isinstance(value, Mapping) else None


def parse_action_declaration(text: str) -> tuple[str, tuple[str, ...], str]:
    """Return ``status, actions, public_text`` without interpreting prose."""

    first_nonempty = next((index for index, char in enumerate(text) if not char.isspace()), None)
    if first_nonempty is None:
        return "missing", (), text
    marker_count = text.count(ACTION_DECLARATION_PREFIX)
    suffix_count = text.count(ACTION_DECLARATION_SUFFIX)
    framed = text[first_nonempty:]
    if not framed.startswith(ACTION_DECLARATION_PREFIX):
        return "missing", (), text
    if marker_count != 1 or suffix_count != 1:
        return "invalid", (), text
    suffix_at = framed.find(
        ACTION_DECLARATION_SUFFIX, len(ACTION_DECLARATION_PREFIX)
    )
    if suffix_at < 0:
        return "invalid", (), text
    encoded = framed[len(ACTION_DECLARATION_PREFIX) : suffix_at]
    value = _decode_json(encoded)
    if value is None or set(value) != {"action_classes"}:
        return "invalid", (), text
    actions = value["action_classes"]
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(action, str) for action in actions)
        or len(actions) != len(set(actions))
        or any(action not in ACTION_CLASSES for action in actions)
        or not _valid_combination(tuple(actions))
    ):
        return "invalid", (), text
    public_text = framed[suffix_at + len(ACTION_DECLARATION_SUFFIX) :].lstrip(
        "\r\n"
    )
    return "valid", tuple(actions), public_text
