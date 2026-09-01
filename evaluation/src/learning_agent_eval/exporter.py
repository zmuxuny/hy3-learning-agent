"""General DecisionEpisode v2 exporter over verified Runtime facts."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .canonical import canonical_json_bytes, sha256_digest
from .deltas import operation_affected_refs
from .integrity import (
    decision_episode_digest,
    environment_manifest_digest,
    episode_completeness_digest,
)
from .normalizers import NormalizationError, normalize_rfc3339

ACTION_MAPPING_VERSION = "runtime-action-mapping-v1"
ACTION_MAPPING = {
    "plan_proposal": "PROPOSE_PLAN",
    "intervention": "INTERVENE_MESSAGE",
    "assessment_revision": "REVISION_REQUIRED",
    "assessment_accept": "ACCEPT",
    "reversible_patch": "APPLY_REVERSIBLE_PATCH",
    "approval_request": "REQUEST_APPROVAL",
    "wait": "WAIT",
    "no_op": "NO_OP",
}
TOOL_ACTION_MAPPING = {
    "plan.proposal.create": "plan_proposal",
    "notification.send": "intervention",
    "submission.check.accepted": "assessment_accept",
    "submission.check.revision_required": "assessment_revision",
    "plan.patch": "reversible_patch",
}
ACTION_MAPPING_SHA256 = sha256_digest(
    {
        "version": ACTION_MAPPING_VERSION,
        "effect_mapping": ACTION_MAPPING,
        "tool_mapping": TOOL_ACTION_MAPPING,
    }
)


class ExportError(RuntimeError):
    """Verified capture facts cannot be classified without guessing."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _entities(snapshot: dict[str, Any], entity_type: str) -> list[dict[str, Any]]:
    return [
        entity
        for entity in snapshot["logical_entities"]
        if entity["entity_type"] == entity_type
    ]


def _entity_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entity["logical_id"]: entity for entity in snapshot["logical_entities"]}


def _changed_refs(
    delta: dict[str, Any],
    entity_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> list[str]:
    type_by_ref = {
        entity["logical_id"]: entity["entity_type"]
        for entity in [
            *((before or {}).get("logical_entities", [])),
            *after["logical_entities"],
        ]
    }
    return sorted(
        {
            change["entity_ref"]
            for change in delta["changes"]
            if type_by_ref.get(change["entity_ref"]) == entity_type
        }
    )


def _execution_status(status: str) -> str:
    if status in {
        "committed",
        "pending_delivery",
        "needs_reconciliation",
        "retry_pending",
    }:
        return "completed"
    if status in {"rejected", "cancelled"}:
        return "blocked"
    if status in {"running", "pending_approval"}:
        return "not_executed"
    if status == "failed":
        return "failed"
    raise ExportError("export.unsupported_invocation_status")


def _observation_status(
    invocation: dict[str, Any], after: dict[str, Any]
) -> str:
    status = invocation["data"]["status"]
    guard_observations = {
        {
            "guard_rejected": "blocked",
            "deferred_quiet_hours": "deferred",
        }[item["data"]["outcome"]]
        for item in _entities(after, "proactive_decision")
        if item["data"]["invocation_ref"] == invocation["logical_id"]
        and item["data"]["outcome"]
        in {"guard_rejected", "deferred_quiet_hours"}
    }
    if len(guard_observations) > 1:
        raise ExportError("export.ambiguous_guard_observation")
    if guard_observations:
        return next(iter(guard_observations))
    if status == "committed":
        linked_outbox = any(
            item["data"]["invocation_ref"] == invocation["logical_id"]
            for item in _entities(after, "outbox_action")
        )
        return "pending_delivery" if linked_outbox else "succeeded"
    try:
        return {
            "running": "not_executed",
            "pending_approval": "pending_approval",
            "pending_delivery": "pending_delivery",
            "failed": "failed",
            "rejected": "blocked",
            "needs_reconciliation": "needs_reconciliation",
            "retry_pending": "retry_pending",
            "cancelled": "blocked",
        }[status]
    except KeyError as exc:
        raise ExportError("export.unsupported_observation_status") from exc


def _ordered_trace_entities(
    after: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return invocations, Operations, and events in canonical Runtime order."""

    event_entities = sorted(
        _entities(after, "run_event"), key=lambda item: item["data"]["sequence"]
    )
    call_sequence = {
        item["data"]["payload"]["tool_call_id"]: item["data"]["sequence"]
        for item in event_entities
        if item["data"]["event_type"] == "tool.started"
        and isinstance(item["data"]["payload"].get("tool_call_id"), str)
    }
    invocation_entities = sorted(
        _entities(after, "tool_invocation"),
        key=lambda item: (
            call_sequence.get(item["data"]["tool_call_id"], 2**31),
            item["data"]["created_at"],
            item["logical_id"],
        ),
    )
    invocation_order = {
        item["logical_id"]: ordinal
        for ordinal, item in enumerate(invocation_entities, 1)
    }
    operation_entities = sorted(
        _entities(after, "operation"),
        key=lambda item: (
            invocation_order.get(item["data"]["invocation_ref"], 2**31),
            item["data"]["created_at"],
            item["logical_id"],
        ),
    )
    return invocation_entities, operation_entities, event_entities


def _trace(
    *,
    episode_id: str,
    model_records: list[dict[str, Any]],
    after: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    invocation_entities, operation_entities, event_entities = (
        _ordered_trace_entities(after)
    )
    invocation_by_call = {
        item["data"]["tool_call_id"]: item["logical_id"]
        for item in invocation_entities
        if item["data"]["tool_call_id"]
    }
    invocation_to_operations: dict[str, list[str]] = {}
    for operation in operation_entities:
        invocation_ref = operation["data"]["invocation_ref"]
        if invocation_ref is not None:
            invocation_to_operations.setdefault(invocation_ref, []).append(
                operation["logical_id"]
            )
    model_turns: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for record in model_records:
        refs: list[str] = []
        for call in record["function_calls"]:
            try:
                refs.append(invocation_by_call[call["call_id"]])
            except KeyError as exc:
                raise ExportError("export.model_call_unresolved") from exc
        turn_ref = f"turn:{episode_id}:{record['ordinal']:03d}"
        model_turns.append(
            {
                "turn_id": turn_ref,
                "ordinal": record["ordinal"],
                "visible_input_digest": record["visible_input_digest"],
                "assistant_text": record["assistant_text"] or None,
                "tool_call_refs": refs,
            }
        )
        attempts.append(
            {
                "attempt_id": f"attempt:{episode_id}:{record['ordinal']:03d}",
                "ordinal": record["ordinal"],
                "turn_ref": turn_ref,
                "attempted_action": "tool_call" if refs else "respond",
                "invocation_refs": refs,
            }
        )
    invocations: list[dict[str, Any]] = []
    for ordinal, entity in enumerate(invocation_entities, 1):
        data = entity["data"]
        result = data["result_payload"] or {}
        invocations.append(
            {
                "invocation_id": entity["logical_id"],
                "ordinal": ordinal,
                "tool_call_id": data["tool_call_id"],
                "tool_name": data["tool_name"].replace("_", "."),
                "canonical_args": data["canonical_args"] or {},
                "execution_status": _execution_status(data["status"]),
                "observation_status": _observation_status(entity, after),
                "durable_status": data["status"],
                "result": result,
                "result_digest": sha256_digest(result),
                "operation_refs": invocation_to_operations.get(entity["logical_id"], []),
            }
        )
    affected = operation_affected_refs(after)
    operations: list[dict[str, Any]] = []
    for ordinal, entity in enumerate(operation_entities, 1):
        data = entity["data"]
        patch = {
            "forward_patch": data["forward_patch"],
            "inverse_patch": data["inverse_patch"],
        }
        operations.append(
            {
                "operation_id": entity["logical_id"],
                "ordinal": ordinal,
                "invocation_ref": data["invocation_ref"],
                "tool_name": data["tool_name"],
                "status": data["status"],
                "primary_entity_ref": data["primary_entity_ref"],
                "affected_entity_refs": affected[entity["logical_id"]],
                **patch,
                "patch_digest": sha256_digest(patch),
            }
        )
    events = []
    for ordinal, entity in enumerate(event_entities, 1):
        data = entity["data"]
        events.append(
            {
                "event_id": entity["logical_id"],
                "ordinal": ordinal,
                "event_type": data["event_type"],
                "payload": data["payload"],
                "payload_digest": sha256_digest(data["payload"]),
            }
        )
    return (
        {
            "capture_mode": "runtime_recording",
            "model_turns": model_turns,
            "tool_invocations": invocations,
            "run_events": events,
            "operations": operations,
            "guard_decisions": [],
        },
        attempts,
    )


def _effect(
    episode_id: str,
    ordinal: int,
    effect_type: str,
    status: str,
    entity_refs: list[str],
    source_refs: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "effect_id": f"effect:{episode_id}:{ordinal:03d}",
        "ordinal": ordinal,
        "effect_type": effect_type,
        "status": status,
        "entity_refs": entity_refs,
        "source_refs": source_refs,
    }


def _pending_approval_fact(
    after: dict[str, Any], invocation: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a durable approval boundary without inventing a decision entity."""

    invocation_ref = invocation["invocation_id"]
    approvals = [
        item
        for item in _entities(after, "run_approval")
        if item["data"]["invocation_ref"] == invocation_ref
        and item["data"]["decision"] == "pending"
    ]
    if len(approvals) == 1:
        return {
            "policy": "runtime-approval-v1",
            "status": "deferred",
            "reason_code": "approval_required",
            "invocation_ref": invocation_ref,
            "decision_ref": approvals[0]["logical_id"],
        }
    if approvals:
        raise ExportError("export.pending_approval_fact_ambiguous")

    result = invocation["result"]
    proposal_ref = result.get("proposal_ref") if isinstance(result, dict) else None
    proposal = _entity_map(after).get(proposal_ref) if isinstance(proposal_ref, str) else None
    if (
        result.get("approval_required") is True
        and proposal is not None
        and proposal["entity_type"] == "plan_proposal"
        and proposal["data"]["status"] == "pending"
    ):
        return {
            "policy": "plan-adoption-v1",
            "status": "deferred",
            "reason_code": "plan_adoption_required",
            "invocation_ref": invocation_ref,
            "decision_ref": proposal_ref,
        }
    raise ExportError("export.pending_approval_fact_missing")


def _guard_facts(after: dict[str, Any], trace: dict[str, Any]) -> list[dict[str, Any]]:
    outcome_mapping = {
        "success_intervention": ("allowed", "notification_guard_allowed"),
        "deferred_quiet_hours": ("deferred", "notification_guard_quiet_hours"),
        "guard_rejected": ("blocked", "notification_guard_rejected"),
        "success_wait": ("allowed", "proactive_wait_allowed"),
    }
    facts: list[dict[str, Any]] = []
    for entity in sorted(
        _entities(after, "proactive_decision"), key=lambda item: item["ordinal"]
    ):
        outcome = entity["data"]["outcome"]
        if outcome not in outcome_mapping:
            raise ExportError("export.unsupported_guard_outcome")
        status, reason = outcome_mapping[outcome]
        facts.append(
            {
                "policy": entity["data"]["policy_version"],
                "status": status,
                "reason_code": reason,
                "invocation_ref": entity["data"]["invocation_ref"],
                "decision_ref": entity["logical_id"],
            }
        )
    covered = {fact["invocation_ref"] for fact in facts if fact["invocation_ref"]}
    for invocation in trace["tool_invocations"]:
        invocation_ref = invocation["invocation_id"]
        if invocation_ref in covered:
            continue
        if invocation["durable_status"] == "pending_approval":
            facts.append(_pending_approval_fact(after, invocation))
        elif invocation["execution_status"] == "blocked":
            facts.append(
                {
                    "policy": "runtime-tool-guard-v1",
                    "status": "blocked",
                    "reason_code": "tool_guard_blocked",
                    "invocation_ref": invocation_ref,
                    "decision_ref": None,
                }
            )
    if not facts:
        facts.append(
            {
                "policy": "not_applicable",
                "status": "not_evaluated",
                "reason_code": None,
                "invocation_ref": None,
                "decision_ref": None,
            }
        )
    return facts


def _guard_status(facts: list[dict[str, Any]]) -> tuple[str, str | None]:
    statuses = {
        fact["status"] for fact in facts if fact["status"] != "not_evaluated"
    }
    if not statuses:
        return "not_evaluated", None
    if len(statuses) > 1:
        return "mixed", "multiple_guard_outcomes"
    status = next(iter(statuses))
    reasons = sorted(
        {
            str(fact["reason_code"])
            for fact in facts
            if fact["status"] == status and fact["reason_code"] is not None
        }
    )
    return status, reasons[0] if len(reasons) == 1 else "multiple_guard_outcomes"


def _effects_and_action(
    *,
    episode_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any],
    delta: dict[str, Any],
    trace: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, str, str | None]:
    effects: list[dict[str, Any]] = []
    guard_facts = _guard_facts(after, trace)
    guard_status, guard_reason = _guard_status(guard_facts)
    entities = {
        **_entity_map(before or {"logical_entities": []}),
        **_entity_map(after),
    }
    changes_by_entity: dict[str, list[dict[str, Any]]] = {}
    for change in delta["changes"]:
        changes_by_entity.setdefault(change["entity_ref"], []).append(change)

    def sources(refs: list[str]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []

        def add(source_type: str, reference: str | None) -> None:
            if reference is None:
                return
            item = {"source_type": source_type, "ref": reference}
            if item not in result:
                result.append(item)

        for reference in refs:
            for change in changes_by_entity.get(reference, []):
                for operation_ref in change["operation_refs"]:
                    add("operation", operation_ref)
            entity = entities.get(reference)
            if entity is None:
                continue
            data = entity["data"]
            invocation_ref = data.get("invocation_ref")
            if invocation_ref is None and entity["entity_type"] == "outbox_receipt":
                action = entities.get(data.get("outbox_action_ref"))
                invocation_ref = action["data"].get("invocation_ref") if action else None
            add("tool_invocation", invocation_ref)
            add("runtime", data.get("run_ref"))
            add("runtime", data.get("source_run_ref"))
            if not result or result[-1]["ref"] != reference:
                add("entity", reference)
        return result

    def related_refs(seed_refs: list[str]) -> list[str]:
        operation_refs = {
            operation_ref
            for reference in seed_refs
            for change in changes_by_entity.get(reference, [])
            for operation_ref in change["operation_refs"]
        }
        return sorted(
            {
                *seed_refs,
                *(
                    change["entity_ref"]
                    for change in delta["changes"]
                    if operation_refs & set(change["operation_refs"])
                ),
            }
        )

    proposals = _changed_refs(delta, "plan_proposal", before, after)
    if proposals:
        if any(ref not in _entity_map(after) for ref in proposals):
            raise ExportError("export.removed_plan_proposal_unclassified")
        statuses = {
            _entity_map(after)[ref]["data"]["status"] for ref in proposals
        }
        effect_status = "pending" if statuses <= {"pending"} else "applied"
        effects.append(
            _effect(
                episode_id, len(effects) + 1, "plan_proposal", effect_status,
                proposals, sources(proposals),
            )
        )
    intervention_refs = sorted(
        {
            *(_changed_refs(delta, "intervention", before, after)),
            *(_changed_refs(delta, "notification", before, after)),
            *(_changed_refs(delta, "outbox_action", before, after)),
            *(_changed_refs(delta, "outbox_receipt", before, after)),
        }
    )
    if intervention_refs:
        if any(ref not in _entity_map(after) for ref in intervention_refs):
            raise ExportError("export.removed_intervention_effect_unclassified")
        grouped: dict[str | None, list[str]] = {}
        for reference in intervention_refs:
            entity = entities[reference]
            invocation_ref = entity["data"].get("invocation_ref")
            if invocation_ref is None and entity["entity_type"] == "outbox_receipt":
                action = entities.get(entity["data"].get("outbox_action_ref"))
                invocation_ref = action["data"].get("invocation_ref") if action else None
            grouped.setdefault(invocation_ref, []).append(reference)
        guard_by_invocation = {
            fact["invocation_ref"]: fact["status"]
            for fact in guard_facts
            if fact["invocation_ref"] is not None
        }
        for invocation_ref, references in sorted(
            grouped.items(), key=lambda item: item[0] or ""
        ):
            status = guard_by_invocation.get(invocation_ref, "allowed")
            if status != "allowed":
                raise ExportError("export.prevented_intervention_has_side_effect")
            effects.append(
                _effect(
                    episode_id,
                    len(effects) + 1,
                    "intervention",
                    "applied",
                    references,
                    sources(references),
                )
            )
    submission_refs = _changed_refs(delta, "submission", before, after)
    if submission_refs:
        if any(ref not in _entity_map(after) for ref in submission_refs):
            raise ExportError("export.removed_submission_unclassified")
        assessment_refs = related_refs(submission_refs)
        task_refs = {
            entities[reference]["data"].get("task_ref")
            for reference in submission_refs
        }
        for change in delta["changes"]:
            reference = change["entity_ref"]
            entity = entities.get(reference)
            if entity is None or entity["entity_type"] not in {
                "artifact",
                "evidence_observation",
                "learning_event",
            }:
                continue
            data = entity["data"]
            identity = data.get("source_identity")
            source_ref = (
                identity.get("source_ref") if isinstance(identity, dict) else None
            )
            linked_submission = isinstance(source_ref, str) and any(
                source_ref == submission_ref
                or source_ref.startswith(f"{submission_ref}:")
                for submission_ref in submission_refs
            )
            if linked_submission or data.get("task_ref") in task_refs:
                assessment_refs.append(reference)
        assessment_refs = sorted(set(assessment_refs))
        effects.append(
            _effect(
                episode_id, len(effects) + 1, "assessment_verdict", "applied",
                assessment_refs, sources(assessment_refs),
            )
        )
    plan_refs = _changed_refs(delta, "plan", before, after)
    if plan_refs:
        if any(ref not in _entity_map(after) for ref in plan_refs):
            raise ExportError("export.removed_plan_unclassified")
        operation_ref_set = {
            operation_ref
            for reference in plan_refs
            for change in changes_by_entity.get(reference, [])
            for operation_ref in change["operation_refs"]
        }
        operation_refs = [
            item["operation_id"]
            for item in trace["operations"]
            if item["operation_id"] in operation_ref_set
        ]
        if not operation_refs:
            raise ExportError("export.plan_change_without_operation")
        revision_refs = related_refs(plan_refs)
        operation_run_refs = {
            entities[reference]["data"].get("run_ref")
            for reference in operation_refs
        }
        for change in delta["changes"]:
            reference = change["entity_ref"]
            entity = entities.get(reference)
            if entity is None or entity["entity_type"] != "learning_event":
                continue
            data = entity["data"]
            if (
                data.get("plan_ref") in plan_refs
                and data.get("run_ref") in operation_run_refs
            ):
                revision_refs.append(reference)
        revision_refs = sorted(set(revision_refs))
        effects.append(
            _effect(
                episode_id, len(effects) + 1, "reversible_patch", "applied",
                revision_refs,
                [{"source_type": "operation", "ref": ref} for ref in operation_refs],
            )
        )
    pending = [
        item["invocation_id"]
        for item in trace["tool_invocations"]
        if item["durable_status"] == "pending_approval"
    ]
    if pending:
        pending_facts = {
            fact["invocation_ref"]: fact
            for fact in guard_facts
            if fact["invocation_ref"] in pending and fact["status"] == "deferred"
        }
        if set(pending_facts) != set(pending):
            raise ExportError("export.pending_approval_fact_missing")
        approval_refs = [pending_facts[reference]["decision_ref"] for reference in pending]
        if any(reference is None for reference in approval_refs):
            raise ExportError("export.pending_approval_fact_missing")
        effects.append(
            _effect(
                episode_id,
                len(effects) + 1,
                "approval_request",
                "pending",
                approval_refs,
                [
                    *(
                        {"source_type": "tool_invocation", "ref": ref}
                        for ref in pending
                    ),
                    *(
                        {"source_type": "entity", "ref": ref}
                        for ref in approval_refs
                    ),
                ],
            )
        )
    for fact in guard_facts:
        if fact["status"] not in {"blocked", "deferred"}:
            continue
        invocation_ref = fact["invocation_ref"]
        already_represented = any(
            any(
                source["source_type"] == "tool_invocation"
                and source["ref"] == invocation_ref
                for source in effect["source_refs"]
            )
            for effect in effects
        )
        if already_represented:
            continue
        if invocation_ref is not None:
            prevented_sources = [
                {"source_type": "tool_invocation", "ref": invocation_ref}
            ]
        elif fact["decision_ref"] is not None:
            prevented_sources = [
                {"source_type": "entity", "ref": fact["decision_ref"]}
            ]
        else:
            run_entities = _entities(after, "agent_run")
            if len(run_entities) != 1:
                raise ExportError("export.prevented_effect_source_missing")
            prevented_sources = [
                {"source_type": "runtime", "ref": run_entities[0]["logical_id"]}
            ]
        effects.append(
            _effect(
                episode_id,
                len(effects) + 1,
                fact["status"],
                fact["status"],
                [],
                prevented_sources,
            )
        )
    failed_action_key: str | None = None
    failed_invocations = [
        item
        for item in trace["tool_invocations"]
        if item["durable_status"]
        in {"failed", "cancelled", "needs_reconciliation", "retry_pending"}
        and not any(
            any(
                source["source_type"] == "tool_invocation"
                and source["ref"] == item["invocation_id"]
                for source in effect["source_refs"]
            )
            for effect in effects
        )
    ]
    if failed_invocations:
        action_keys: set[str] = set()
        for invocation in failed_invocations:
            tool_name = invocation["tool_name"]
            if tool_name == "submission.check":
                verdict = invocation["result"].get("status")
                lookup = f"{tool_name}.{verdict}" if isinstance(verdict, str) else ""
            else:
                lookup = tool_name
            try:
                action_keys.add(TOOL_ACTION_MAPPING[lookup])
            except KeyError as exc:
                raise ExportError("export.failed_action_unclassified") from exc
        if len(action_keys) != 1:
            raise ExportError("export.failed_action_ambiguous")
        failed_action_key = next(iter(action_keys))
        pending_failure = all(
            item["durable_status"] in {"needs_reconciliation", "retry_pending"}
            for item in failed_invocations
        )
        effects.append(
            _effect(
                episode_id,
                len(effects) + 1,
                "failed",
                "pending" if pending_failure else "failed",
                [],
                [
                    {"source_type": "tool_invocation", "ref": item["invocation_id"]}
                    for item in failed_invocations
                ],
            )
        )
    covered_refs = {
        reference for effect in effects for reference in effect["entity_refs"]
    }
    summarized_elsewhere = {
        "agent_run",
        "constraint",
        "context_snapshot",
        "goal",
        "operation",
        "planning_intake",
        "proactive_decision",
        "resource",
        "run_approval",
        "run_event",
        "session",
        "tool_invocation",
    }
    unclassified = sorted(
        {
            change["entity_ref"]
            for change in delta["changes"]
            if change["entity_ref"] not in covered_refs
            and entities.get(change["entity_ref"], {}).get("entity_type")
            not in summarized_elsewhere
        }
    )
    if unclassified:
        raise ExportError("export.unclassified_state_change")
    primary_types = {effect["effect_type"] for effect in effects}
    if not effects:
        decisions = _entities(after, "proactive_decision")
        is_wait = any(item["data"]["outcome"] == "success_wait" for item in decisions)
        effect_type = "wait" if is_wait else "no_op"
        run_refs = [item["logical_id"] for item in _entities(after, "agent_run")]
        wait_invocations = [
            fact["invocation_ref"]
            for fact in guard_facts
            if fact["reason_code"] == "proactive_wait_allowed"
            and fact["invocation_ref"] is not None
        ]
        effects.append(
            _effect(
                episode_id,
                1,
                effect_type,
                "no_change",
                [],
                (
                    [
                        {"source_type": "tool_invocation", "ref": ref}
                        for ref in wait_invocations
                    ]
                    if wait_invocations
                    else [{"source_type": "runtime", "ref": ref} for ref in run_refs]
                ),
            )
        )
        primary_types = {effect_type}
    action_key: str
    classification_types = primary_types - {"failed"}
    if classification_types in ({"plan_proposal"}, {"plan_proposal", "approval_request"}):
        action_key = "plan_proposal"
    elif classification_types == {"intervention"}:
        action_key = "intervention"
    elif classification_types == {"assessment_verdict"}:
        statuses = {
            _entity_map(after)[ref]["data"]["status"]
            for ref in submission_refs
        }
        if statuses <= {"revision_required"}:
            action_key = "assessment_revision"
        elif statuses <= {"accepted"}:
            action_key = "assessment_accept"
        else:
            raise ExportError("export.unsupported_assessment_status")
    elif classification_types == {"reversible_patch"}:
        action_key = "reversible_patch"
    elif classification_types == {"approval_request"}:
        action_key = "approval_request"
    elif classification_types == {"wait"}:
        action_key = "wait"
    elif classification_types == {"no_op"}:
        action_key = "no_op"
    elif classification_types <= {"intervention", "blocked", "deferred"} and classification_types & {
        "blocked",
        "deferred",
    }:
        tool_names = {item["tool_name"] for item in trace["tool_invocations"]}
        if any(name in {"notification.send", "notification_send"} for name in tool_names):
            action_key = "intervention"
        else:
            raise ExportError("export.blocked_action_unclassified")
    elif not classification_types and primary_types == {"failed"} and failed_action_key is not None:
        action_key = failed_action_key
    else:
        raise ExportError("export.multiple_action_classes")
    if failed_action_key is not None and failed_action_key != action_key:
        raise ExportError("export.failed_action_conflicts_with_effect")
    return effects, ACTION_MAPPING[action_key], guard_status, guard_reason


def _guard_decisions(
    *,
    episode_id: str,
    facts: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    effects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for ordinal, fact in enumerate(facts, 1):
        invocation_ref = fact["invocation_ref"]
        attempted_refs = [
            item["attempt_id"]
            for item in attempts
            if invocation_ref is not None and invocation_ref in item["invocation_refs"]
        ]
        final_refs = [
            effect["effect_id"]
            for effect in effects
            if invocation_ref is not None
            and any(
                source["source_type"] == "tool_invocation"
                and source["ref"] == invocation_ref
                for source in effect["source_refs"]
            )
        ]
        if (
            invocation_ref is None
            and len(facts) == 1
            and fact["status"] != "not_evaluated"
        ):
            final_refs = [effect["effect_id"] for effect in effects]
        decisions.append(
            {
                "guard_id": f"guard:{episode_id}:{ordinal:03d}",
                "ordinal": ordinal,
                "policy": fact["policy"],
                "status": fact["status"],
                "reason_code": fact["reason_code"],
                "invocation_ref": invocation_ref,
                "decision_ref": fact["decision_ref"],
                "attempted_effect_refs": attempted_refs,
                "final_effect_refs": final_refs,
            }
        )
    return decisions


def _environment(
    *,
    fixture: dict[str, Any],
    first_record: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    resource_version: str,
    resource_digest: str,
    git_commit: str,
    invocation_mode: str,
    model_name: str,
    model_temperature: float,
    model_reasoning_effort: str,
    frozen_time: str,
) -> dict[str, Any]:
    proactive_version = str(import_module("app.runtime.proactive").POLICY_VERSION)
    evidence_version = str(
        import_module("app.services.evidence").EVIDENCE_POLICY_VERSION
    )
    environment = {
        "schema_version": "environment-manifest-v1",
        "frozen_time": frozen_time,
        "timezone": fixture["timezone"],
        "model": {
            "provider": "none" if invocation_mode == "stub" else "tencent-tokenhub",
            "name": model_name,
            "invocation_mode": invocation_mode,
            "temperature": model_temperature,
            "reasoning_effort": model_reasoning_effort,
            "max_tokens": 4096,
        },
        "prompt": first_record["system_prompt"],
        "tools": first_record["tools"],
        "policies": {
            "proactive_version": proactive_version,
            "proactive_digest": sha256_digest({"version": proactive_version}),
            "evidence_version": evidence_version,
            "evidence_digest": sha256_digest({"version": evidence_version}),
            "approval_version": "runtime-approval-v1",
            "approval_digest": sha256_digest({"version": "runtime-approval-v1"}),
        },
        "resources": {
            "snapshot_version": resource_version,
            "snapshot_digest": resource_digest,
        },
        "runtime": {
            "git_commit": git_commit,
            "database_mode": "temporary_fixture",
            "fixture_db_sha256": sha256_digest(
                {
                    "state_before": before["snapshot_sha256"],
                    "state_after": after["snapshot_sha256"],
                }
            ),
        },
        "isolation": {
            "production_database_access": False,
            "network_access": "disabled" if invocation_mode == "stub" else "model_provider_only",
            "notification_mode": "fake_outbox",
        },
        "manifest_sha256": "0" * 64,
    }
    environment["manifest_sha256"] = environment_manifest_digest(environment)
    return environment


def _durable_status(
    *,
    after: dict[str, Any],
    trace: dict[str, Any],
    run_status: str,
    guard_status: str,
    effects: list[dict[str, Any]],
) -> str:
    invocation_statuses = {
        item["durable_status"] for item in trace["tool_invocations"]
    }
    outbox_actions = _entities(after, "outbox_action")
    outbox_statuses = {item["data"]["status"] for item in outbox_actions}
    receipt_action_refs = {
        item["data"]["outbox_action_ref"]
        for item in _entities(after, "outbox_receipt")
    }
    applied = any(effect["status"] == "applied" for effect in effects)
    reconciliation = (
        run_status == "needs_reconciliation"
        or "needs_reconciliation" in invocation_statuses
        or "needs_reconciliation" in outbox_statuses
    )
    failed = (
        run_status in {"failed", "cancelled"}
        or bool(invocation_statuses & {"failed", "cancelled"})
        or bool(outbox_statuses & {"failed", "cancelled"})
    )
    pending = bool(
        invocation_statuses & {"running", "pending_approval", "retry_pending"}
        or outbox_statuses & {"queued", "delivering", "retry_pending"}
    )
    pending_delivery = "pending_delivery" in invocation_statuses
    delivery_committed = bool(outbox_actions) and all(
        item["data"]["status"] == "delivered"
        and item["logical_id"] in receipt_action_refs
        for item in outbox_actions
    )
    if guard_status == "mixed" or applied and (reconciliation or failed or pending):
        return "partial"
    if reconciliation:
        return "needs_reconciliation"
    if failed:
        return "failed"
    if guard_status == "blocked":
        return "blocked"
    if guard_status == "deferred":
        return "deferred"
    if pending or (pending_delivery and not delivery_committed):
        return "pending"
    return "committed"


def build_decision_episode_v2(
    *,
    fixture: dict[str, Any],
    oracle: dict[str, Any],
    model_records: list[dict[str, Any]],
    state_before: dict[str, Any],
    state_after: dict[str, Any],
    state_delta: dict[str, Any],
    resource_version: str,
    resource_digest: str,
    git_commit: str,
    invocation_mode: str,
    model_name: str,
    model_temperature: float,
    model_reasoning_effort: str,
    isolation_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build v2 without consulting track, fixture IDs, scripts, or Oracle outcomes."""

    if not model_records:
        raise ExportError("export.model_records_missing")
    try:
        frozen_time = normalize_rfc3339(fixture["frozen_time"])
        trigger = {
            **fixture["trigger"],
            "triggered_at": normalize_rfc3339(fixture["trigger"]["triggered_at"]),
        }
    except NormalizationError as exc:
        raise ExportError(exc.code) from exc
    trace, attempts = _trace(
        episode_id=fixture["episode_id"], model_records=model_records, after=state_after
    )
    effects, action, guard_status, guard_reason = _effects_and_action(
        episode_id=fixture["episode_id"],
        before=state_before,
        after=state_after,
        delta=state_delta,
        trace=trace,
    )
    if not trace["tool_invocations"] and effects[0]["effect_type"] in {
        "wait",
        "no_op",
    }:
        no_tool_attempts = [
            attempt for attempt in attempts if not attempt["invocation_refs"]
        ]
        if no_tool_attempts:
            no_tool_attempts[-1]["attempted_action"] = effects[0]["effect_type"]
    guards = _guard_decisions(
        episode_id=fixture["episode_id"],
        facts=_guard_facts(state_after, trace),
        attempts=attempts,
        effects=effects,
    )
    trace["guard_decisions"] = guards
    run_entities = _entities(state_after, "agent_run")
    if len(run_entities) != 1:
        raise ExportError("export.run_state_missing")
    run = run_entities[0]["data"]
    durable_status = _durable_status(
        after=state_after,
        trace=trace,
        run_status=run["status"],
        guard_status=guard_status,
        effects=effects,
    )
    eligibility = (
        "ineligible_stub"
        if invocation_mode == "stub"
        else "ineligible_engineering"
        if fixture["engineering_only"]
        else "eligible"
    )
    environment = _environment(
        fixture=fixture,
        first_record=model_records[0],
        before=state_before,
        after=state_after,
        resource_version=resource_version,
        resource_digest=resource_digest,
        git_commit=git_commit,
        invocation_mode=invocation_mode,
        model_name=model_name,
        model_temperature=model_temperature,
        model_reasoning_effort=model_reasoning_effort,
        frozen_time=frozen_time,
    )
    evidence_paths = sorted(
        {
            "state_before.snapshot_sha256",
            "state_after.snapshot_sha256",
            "state_delta.delta_sha256",
            "observable_trace.model_turns",
            "observable_trace.run_events",
            "result.layers",
            "isolation_evidence",
            *(
                path
                for requirement in oracle["must_satisfy"]
                for path in requirement["evidence_paths"]
            ),
            *(effect["path"] for effect in oracle["expected_effects"]),
        }
    )
    completeness = {
        "status": "complete" if state_delta["capture_status"] == "complete" else "invalid",
        "error_codes": state_delta["error_codes"],
        "verified_evidence_paths": evidence_paths,
        "completeness_sha256": "0" * 64,
    }
    completeness["completeness_sha256"] = episode_completeness_digest(completeness)
    formal = invocation_mode == "real" and not fixture["engineering_only"]
    episode = {
        "schema_version": "decision-episode-v2",
        "episode_id": fixture["episode_id"],
        "scenario_family_id": fixture["scenario_family_id"],
        "track": fixture["track"],
        "split": fixture["split"],
        "difficulty": fixture["difficulty"],
        "tags": ["e2_runtime_mini", "engineering_only", f"{invocation_mode}_model"],
        "trigger": trigger,
        "state_before": state_before,
        "state_after": state_after,
        "state_delta": state_delta,
        "environment": environment,
        "observable_trace": trace,
        "result": {
            "action_class": action,
            "action_mapping_version": ACTION_MAPPING_VERSION,
            "action_mapping_sha256": ACTION_MAPPING_SHA256,
            "user_visible_output": run["output"] or None,
            "guard": {
                "status": guard_status,
                "reason_code": guard_reason,
                "blocked_effect_refs": [
                    effect["effect_id"]
                    for effect in effects
                    if effect["status"] == "blocked"
                ],
            },
            "layers": {
                "model_attempts": attempts,
                "guard_decision_refs": [item["guard_id"] for item in guards],
                "final_effects": effects,
                "run_status": run["status"],
                "durable_status": durable_status,
                "formal_evaluation_eligibility": eligibility,
            },
        },
        "oracle": oracle,
        "completeness": completeness,
        "isolation_evidence": isolation_evidence,
        "provenance": {
            "source_type": "runtime_export",
            "construction_method": "runtime_recorded",
            "author_role": "evaluation_runtime_recorder",
            "reviewer_role": "evaluation_protocol_reviewer",
            "purpose": "Exercise the isolated production Runtime through the general E2 v2 export contract.",
            "dataset_role": "protocol_mini_fixture",
            "runtime_executed": True,
            "formal_evaluation_result": formal,
            "evaluation_status": (
                "formal_model_evaluation" if formal else "not_a_formal_model_evaluation"
            ),
            "created_at": frozen_time,
            "source_refs": [
                f"fixture:{fixture['episode_id']}",
                f"resource:{resource_version}",
            ],
            "episode_sha256": "0" * 64,
        },
    }
    episode["provenance"]["episode_sha256"] = decision_episode_digest(episode)
    # Force canonical traversal here so exporter errors precede publication.
    canonical_json_bytes(episode)
    return episode
