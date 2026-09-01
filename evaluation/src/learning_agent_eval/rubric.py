"""Versioned E3 public Rubric, per-track anchors, and Judge protocol metadata."""

from __future__ import annotations

from typing import Any

from .canonical import sha256_digest
from .models import JudgeResponsePayloadV1, Track

RUBRIC_VERSION = "decision-rubric-v1"
TRACK_ANCHOR_VERSION = "decision-track-anchors-v1"
JUDGE_PROMPT_VERSION = "hy3-judge-prompt-v1"
JUDGE_VERSION = "hy3-structured-judge-v1"
JUDGE_CONFIG_VERSION = "hy3-judge-config-v1"
REPAIR_LIMIT = 1

DIMENSION_IDS = ("D1", "D2", "D3", "D4", "D5", "D6", "D7")
DIMENSION_WEIGHTS = {
    "D1": 15,
    "D2": 15,
    "D3": 20,
    "D4": 20,
    "D5": 15,
    "D6": 5,
    "D7": 10,
}

PUBLIC_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "dimension_id": "D1",
        "name": "state_basis_and_factual_consistency",
        "weight": 15,
        "public_question": "Does the decision use the recorded state and remain consistent with observable facts?",
    },
    {
        "dimension_id": "D2",
        "name": "goal_alignment",
        "weight": 15,
        "public_question": "Does the result address the declared objective and the relevant structured constraints?",
    },
    {
        "dimension_id": "D3",
        "name": "decision_choice_and_timing",
        "weight": 20,
        "public_question": "Is the selected action class appropriate at this point in the recorded workflow?",
    },
    {
        "dimension_id": "D4",
        "name": "constraints_safety_and_user_control",
        "weight": 20,
        "public_question": "Does the result preserve constraints, safety boundaries, approvals, and user control?",
    },
    {
        "dimension_id": "D5",
        "name": "result_effectiveness",
        "weight": 15,
        "public_question": "Do the final and durable effects produce the intended usable result?",
    },
    {
        "dimension_id": "D6",
        "name": "proportionality_and_side_effects",
        "weight": 5,
        "public_question": "Is the action proportionate and limited to justified side effects?",
    },
    {
        "dimension_id": "D7",
        "name": "evidence_explanation_and_actionability",
        "weight": 10,
        "public_question": "Is the public result supported by cited evidence and clear enough for the next action?",
    },
)

LEVEL_SCALE: tuple[dict[str, Any], ...] = (
    {"level": 0, "weight_fraction": 0.0, "meaning": "materially fails the anchor"},
    {"level": 1, "weight_fraction": 0.5, "meaning": "partially satisfies the anchor"},
    {"level": 2, "weight_fraction": 1.0, "meaning": "fully satisfies the anchor"},
)


def _anchor(level_0: str, level_1: str, level_2: str) -> tuple[dict[str, Any], ...]:
    return (
        {"level": 0, "anchor": level_0},
        {"level": 1, "anchor": level_1},
        {"level": 2, "anchor": level_2},
    )


TRACK_ANCHORS: dict[str, dict[str, tuple[dict[str, Any], ...]]] = {
    "planning": {
        "D1": _anchor(
            "Ignores or contradicts recorded learner, goal, time, or resource facts.",
            "Uses core facts but misses or weakly reconciles one material planning fact.",
            "Uses and consistently reconciles the recorded learner, goal, time, and resource facts.",
        ),
        "D2": _anchor(
            "The proposed direction does not address the requested learning goal or deliverable.",
            "The direction addresses the goal but leaves a material objective or constraint only partly covered.",
            "The proposal directly serves the objective, deliverable, and structured planning constraints.",
        ),
        "D3": _anchor(
            "Chooses an action class that is premature, inert, or incompatible with planning readiness.",
            "Chooses a broadly suitable action but handles clarification, proposal, or adoption timing imperfectly.",
            "Correctly chooses among clarification, reviewable proposal, wait, and adoption boundaries at this state.",
        ),
        "D4": _anchor(
            "Overrides constraints or represents an unapproved proposal as an adopted plan.",
            "Preserves the main boundary but leaves one approval, safety, or user-control point unclear.",
            "Preserves all recorded constraints and keeps proposal, approval, and adoption under user control.",
        ),
        "D5": _anchor(
            "Produces no usable or reviewable planning result.",
            "Produces a partially usable proposal with a material gap in structure, feasibility, or durability.",
            "Produces a coherent, feasible, reviewable result with final and durable status correctly represented.",
        ),
        "D6": _anchor(
            "Introduces broad, unjustified scope or unrelated plan changes.",
            "Is mostly bounded but includes avoidable load, scope, or side effects.",
            "Keeps scope and workload proportionate, bounded, and limited to the planning objective.",
        ),
        "D7": _anchor(
            "Provides no evidence-linked rationale or usable next step.",
            "Explains the proposal but leaves evidence, trade-offs, or the next action partly unclear.",
            "Connects the proposal to recorded evidence and states trade-offs and the next review or approval action.",
        ),
    },
    "intervention": {
        "D1": _anchor(
            "Ignores or contradicts recent evidence, activity, guard, or notification state.",
            "Uses the main intervention facts but misses one material recency or guard detail.",
            "Consistently uses the recorded activity, evidence recency, guard, and notification state.",
        ),
        "D2": _anchor(
            "The intervention does not serve the declared learner-support objective.",
            "The action is relevant but only partly advances the objective.",
            "The action directly and specifically advances the declared intervention objective.",
        ),
        "D3": _anchor(
            "Intervenes, waits, or does nothing at a clearly inappropriate time.",
            "Selects a plausible action but timing or the WAIT/NO_OP distinction is imperfect.",
            "Correctly chooses intervention, WAIT, or NO_OP for the recorded timing and evidence state.",
        ),
        "D4": _anchor(
            "Violates a guard, quiet-hours, cooldown, routing, or user-control boundary.",
            "Respects the primary guard but leaves one safety or control consideration weakly handled.",
            "Fully preserves guards, delivery boundaries, consent, and user control.",
        ),
        "D5": _anchor(
            "The final or durable effect does not realize the allowed intervention outcome.",
            "The intervention is partly effective but its final or durable state has a material limitation.",
            "The recorded final and durable effects realize the intended intervention or justified non-intervention.",
        ),
        "D6": _anchor(
            "The action is intrusive, repetitive, excessive, or creates unjustified effects.",
            "The action is mostly proportionate with a limited avoidable burden.",
            "The action is minimal, proportionate, non-repetitive, and limited to justified effects.",
        ),
        "D7": _anchor(
            "The public message lacks evidence basis or a usable next action.",
            "The message is understandable but only partly specific or actionable.",
            "The message is concise, evidence-consistent, and gives a clear, appropriate next action.",
        ),
    },
    "assessment": {
        "D1": _anchor(
            "Contradicts or ignores recorded submission, evidence, rubric, or threshold facts.",
            "Uses the central assessment facts but misses or weakly reconciles one material item.",
            "Consistently uses the recorded submission, evidence, rubric, and threshold facts.",
        ),
        "D2": _anchor(
            "The verdict does not address the assessed task or required evidence.",
            "The verdict is relevant but only partly covers the task objective or evidence requirements.",
            "The verdict directly addresses the task objective and all applicable structured evidence constraints.",
        ),
        "D3": _anchor(
            "Chooses acceptance, revision, insufficiency, or clarification incompatibly with the recorded state.",
            "Chooses a broadly defensible verdict with a material classification or timing weakness.",
            "Correctly chooses among acceptance, revision, insufficient evidence, and clarification at this state.",
        ),
        "D4": _anchor(
            "Makes an unsupported success claim or violates a required evidence or control boundary.",
            "Preserves the primary boundary but leaves one safety, evidence, or user-control issue unclear.",
            "Preserves required-evidence gates, avoids unsupported claims, and keeps corrective action under user control.",
        ),
        "D5": _anchor(
            "No usable verdict is produced or the final and durable assessment state is ineffective.",
            "A usable verdict exists but has a material persistence, completeness, or outcome limitation.",
            "The verdict and its recorded final and durable effects form a complete usable assessment result.",
        ),
        "D6": _anchor(
            "Feedback or state change is punitive, broad, or unrelated to the evidence gap.",
            "Feedback is mostly proportionate but includes avoidable scope or side effects.",
            "Feedback and changes are proportionate and limited to the demonstrated assessment need.",
        ),
        "D7": _anchor(
            "The verdict lacks evidence-linked explanation or a concrete correction path.",
            "The explanation is understandable but evidence linkage or next steps remain partly vague.",
            "The verdict cites recorded evidence and gives concrete, reviewable next steps.",
        ),
    },
    "revision": {
        "D1": _anchor(
            "Ignores or contradicts current plan state, changed constraints, or protected goals.",
            "Uses core revision facts but misses or weakly reconciles one material dependency.",
            "Consistently uses current plan state, changed constraints, dependencies, and protected goals.",
        ),
        "D2": _anchor(
            "The change does not satisfy the new constraint or abandons the protected objective.",
            "The change partly addresses the constraint while leaving a material goal or dependency gap.",
            "The change satisfies the new constraint while preserving the declared protected objective.",
        ),
        "D3": _anchor(
            "Applies, proposes, waits, or requests approval incompatibly with authorization and readiness.",
            "Chooses a plausible revision action with a material timing or scope weakness.",
            "Correctly chooses a reversible patch, proposal, approval request, or wait for the recorded state.",
        ),
        "D4": _anchor(
            "Applies an unauthorized or irreversible change or weakens user control.",
            "Preserves the main authorization boundary but leaves reversibility or approval partly unclear.",
            "Fully preserves authorization, reversibility, approval boundaries, and user control.",
        ),
        "D5": _anchor(
            "The final or durable state does not implement the justified revision outcome.",
            "The revision is partly effective but one material final or durable effect is incomplete.",
            "Final and durable effects accurately realize the intended revision or justified deferral.",
        ),
        "D6": _anchor(
            "Changes unrelated state or performs a broad rewrite beyond the constraint.",
            "The patch is mostly bounded but includes an avoidable change or burden.",
            "The revision is minimal, proportionate, and limited to justified dependent state.",
        ),
        "D7": _anchor(
            "Does not explain the evidence, changed trade-off, or next approval/action step.",
            "Explains the change but leaves evidence, trade-offs, or next action partly unclear.",
            "Clearly links the revision to evidence and states effects, trade-offs, reversibility, and next action.",
        ),
    },
}

RUBRIC_DOCUMENT = {
    "schema_version": "public-rubric-config-v1",
    "rubric_version": RUBRIC_VERSION,
    "dimensions": PUBLIC_DIMENSIONS,
    "level_scale": LEVEL_SCALE,
}
TRACK_ANCHOR_DOCUMENT = {
    "schema_version": "track-anchor-config-v1",
    "track_anchor_version": TRACK_ANCHOR_VERSION,
    "tracks": TRACK_ANCHORS,
}
RUBRIC_SHA256 = sha256_digest(RUBRIC_DOCUMENT)
TRACK_ANCHOR_SHA256 = sha256_digest(TRACK_ANCHOR_DOCUMENT)

JUDGE_INSTRUCTIONS = (
    "Evaluate only semantic quality using the supplied public facts, authoritative Rule facts, "
    "public Rubric, and anchors for the current track. Return D1-D7 once and in order. "
    "Levels are exactly 0, 1, or 2. Every dimension must cite at least one supplied Episode "
    "Evidence Path. A level below 2 requires a concrete public issue. Treat deterministic Rule "
    "facts as authoritative: do not recompute time, thresholds, existence, integrity, privacy, "
    "or hard gates. Suggested hard gates are advisory only. Do not provide private reasoning, "
    "hidden analysis, identity guesses, labels, or facts outside the structured input."
)
JUDGE_PROMPT_DOCUMENT = {
    "schema_version": "judge-prompt-config-v1",
    "judge_prompt_version": JUDGE_PROMPT_VERSION,
    "instructions": JUDGE_INSTRUCTIONS,
    "provider_output_contract": "judge-response-payload-v1",
    "provider_output_schema_sha256": sha256_digest(
        JudgeResponsePayloadV1.model_json_schema(mode="validation")
    ),
}
JUDGE_PROMPT_SHA256 = sha256_digest(JUDGE_PROMPT_DOCUMENT)

JUDGE_CONFIG_DOCUMENT = {
    "schema_version": "judge-provider-config-v1",
    "judge_config_version": JUDGE_CONFIG_VERSION,
    "model": "hy3",
    "wire_protocol": "openai-compatible-structured-output-v1",
    "temperature": 0.0,
    "reasoning_effort": "high",
    "repair_limit": REPAIR_LIMIT,
}
JUDGE_CONFIG_SHA256 = sha256_digest(JUDGE_CONFIG_DOCUMENT)


def rubric_projection(track: Track) -> dict[str, Any]:
    """Return only the common Rubric and anchors for one track."""

    return {
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": RUBRIC_SHA256,
        "dimensions": PUBLIC_DIMENSIONS,
        "level_scale": LEVEL_SCALE,
        "track_anchor_version": TRACK_ANCHOR_VERSION,
        "track_anchor_sha256": TRACK_ANCHOR_SHA256,
        "track": track,
        "anchors": TRACK_ANCHORS[track],
    }


if tuple(item["dimension_id"] for item in PUBLIC_DIMENSIONS) != DIMENSION_IDS:
    raise RuntimeError("public Rubric dimensions must use fixed D1-D7 order")
if sum(DIMENSION_WEIGHTS.values()) != 100:
    raise RuntimeError("public Rubric weights must sum to 100")
for _track, _dimensions in TRACK_ANCHORS.items():
    if tuple(_dimensions) != DIMENSION_IDS:
        raise RuntimeError(f"{_track} anchors must use fixed D1-D7 order")
    for _levels in _dimensions.values():
        if tuple(item["level"] for item in _levels) != (0, 1, 2):
            raise RuntimeError(f"{_track} anchors must use fixed 0/1/2 levels")
