"""E1-only deterministic projection from one temporary production database."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from .canonical import sha256_digest
from .integrity import decision_episode_digest, environment_manifest_digest


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _normalize(value: Any, identities: dict[str, str], *, key: str = "") -> Any:
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, dict):
        return {
            str(child_key): _normalize(child, identities, key=str(child_key))
            for child_key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if str(child_key) not in {"claim_token", "reply_token", "endpoint", "keys"}
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(child, identities, key=key) for child in value]
    if isinstance(value, str):
        return identities.get(value, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _trace_references(value: Any) -> Any:
    """Name nested identity uses as refs, reserving E0 ``*_id`` for declarations."""

    declared_id_keys = {
        "trigger_id",
        "logical_id",
        "turn_id",
        "invocation_id",
        "event_id",
        "operation_id",
        "change_id",
    }
    if isinstance(value, dict):
        return {
            (f"{key.removesuffix('_id')}_ref" if key in declared_id_keys else key): _trace_references(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_trace_references(child) for child in value]
    return value


async def collect_database_projection(
    session_factory: Any,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """Read only the synthetic rows required by the four E1 Mini tracks."""

    models = import_module("app.models")
    select = import_module("sqlalchemy").select
    episode_id = fixture["episode_id"]
    run_id = fixture["run_id"]
    async with session_factory() as db:
        run = await db.get(models.AgentRun, run_id)
        if run is None:
            raise RuntimeError("runtime run row is missing")
        invocations = list(
            (
                await db.execute(
                    select(models.ToolInvocation)
                    .where(models.ToolInvocation.run_id == run_id)
                    .order_by(models.ToolInvocation.id)
                )
            ).scalars()
        )
        events = list(
            (
                await db.execute(
                    select(models.RunEvent)
                    .where(models.RunEvent.run_id == run_id)
                    .order_by(models.RunEvent.sequence)
                )
            ).scalars()
        )
        operations = list(
            (
                await db.execute(
                    select(models.Operation)
                    .where(models.Operation.run_id == run_id)
                    .order_by(models.Operation.created_at, models.Operation.id)
                )
            ).scalars()
        )
        notifications = list(
            (
                await db.execute(
                    select(models.Notification)
                    .where(models.Notification.run_id == run_id)
                    .order_by(models.Notification.id)
                )
            ).scalars()
        )
        outbox = list(
            (
                await db.execute(
                    select(models.OutboxAction)
                    .where(models.OutboxAction.run_id == run_id)
                    .order_by(models.OutboxAction.created_at, models.OutboxAction.id)
                )
            ).scalars()
        )
        outbox_ids = [item.id for item in outbox]
        receipts = list(
            (
                await db.execute(
                    select(models.OutboxReceipt)
                    .where(models.OutboxReceipt.outbox_action_id.in_(outbox_ids))
                    .order_by(models.OutboxReceipt.id)
                )
            ).scalars()
        ) if outbox_ids else []
        interventions = list(
            (
                await db.execute(
                    select(models.Intervention)
                    .where(models.Intervention.source_run_id == run_id)
                    .order_by(models.Intervention.created_at, models.Intervention.id)
                )
            ).scalars()
        )
        decisions = list(
            (
                await db.execute(
                    select(models.ProactiveDecision)
                    .where(models.ProactiveDecision.source_run_id == run_id)
                    .order_by(models.ProactiveDecision.created_at, models.ProactiveDecision.id)
                )
            ).scalars()
        )
        proposals = list(
            (
                await db.execute(
                    select(models.PlanProposal)
                    .where(models.PlanProposal.source_run_id == run_id)
                    .order_by(models.PlanProposal.created_at, models.PlanProposal.id)
                )
            ).scalars()
        )
        submissions = list(
            (
                await db.execute(
                    select(models.TaskSubmission)
                    .where(models.TaskSubmission.owner_id == fixture["owner_id"])
                    .order_by(models.TaskSubmission.id)
                )
            ).scalars()
        )
        plans = list(
            (
                await db.execute(
                    select(models.Plan)
                    .where(models.Plan.owner_id == fixture["owner_id"])
                    .order_by(models.Plan.id)
                )
            ).scalars()
        )

    identities: dict[str, str] = {}
    for ordinal, item in enumerate(invocations, 1):
        identities[str(item.id)] = f"invocation:{episode_id}:{ordinal:03d}"
    for ordinal, item in enumerate(operations, 1):
        identities[item.id] = f"operation:{episode_id}:{ordinal:03d}"
    for ordinal, item in enumerate(outbox, 1):
        identities[item.id] = f"outbox:{episode_id}:{ordinal:03d}"
    for ordinal, item in enumerate(interventions, 1):
        identities[item.id] = f"intervention:{episode_id}:{ordinal:03d}"
        identities[item.session_id] = f"session:{episode_id}:intervention:{ordinal:03d}"
    for ordinal, item in enumerate(decisions, 1):
        identities[item.id] = f"decision:{episode_id}:{ordinal:03d}"
    for ordinal, item in enumerate(proposals, 1):
        identities[item.id] = f"proposal:{episode_id}:{ordinal:03d}"

    projection = {
        "run": {
            "run_id": run_id,
            "status": run.status,
            "phase": run.phase,
            "trigger": run.trigger,
            "output": run.output,
            "started_at": _timestamp(run.started_at),
            "completed_at": _timestamp(run.completed_at),
        },
        "tool_invocations": [
            {
                "logical_id": f"invocation:{episode_id}:{ordinal:03d}",
                "tool_call_id": item.tool_call_id,
                "tool_name": item.tool_name,
                "canonical_args": _normalize(item.canonical_args or {}, identities),
                "status": item.status,
                "result": _normalize(item.result_payload or {}, identities),
                "completed_at": _timestamp(item.completed_at),
            }
            for ordinal, item in enumerate(invocations, 1)
        ],
        "run_events": [
            {
                "logical_id": f"event:{episode_id}:{item.sequence:03d}",
                "sequence": item.sequence,
                "event_type": item.event_type,
                "summary": item.summary,
                "payload": _normalize(item.payload or {}, identities),
                "created_at": _timestamp(item.created_at),
            }
            for item in events
        ],
        "operations": [
            {
                "logical_id": f"operation:{episode_id}:{ordinal:03d}",
                "tool_name": item.tool_name,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "forward_patch": _normalize(item.forward_patch or {}, identities),
                "inverse_patch": _normalize(item.inverse_patch or {}, identities),
                "status": item.status,
                "created_at": _timestamp(item.created_at),
            }
            for ordinal, item in enumerate(operations, 1)
        ],
        "notifications": [
            {
                "logical_id": f"notification:{episode_id}:{ordinal:03d}",
                "channel": item.channel,
                "status": item.status,
                "title": item.title,
                "body": item.body,
                "sent_at": _timestamp(item.sent_at),
            }
            for ordinal, item in enumerate(notifications, 1)
        ],
        "outbox_actions": [
            {
                "logical_id": f"outbox:{episode_id}:{ordinal:03d}",
                "action_key": item.action_key,
                "request_digest": item.request_digest,
                "destination": item.destination,
                "status": item.status,
                "attempt": item.attempt,
                "completed_at": _timestamp(item.completed_at),
            }
            for ordinal, item in enumerate(outbox, 1)
        ],
        "outbox_receipts": [
            {
                "logical_id": f"receipt:{episode_id}:{ordinal:03d}",
                "outbox_action_ref": identities.get(item.outbox_action_id, item.outbox_action_id),
                "action_key": item.action_key,
                "status": item.status,
                "provider_id": item.provider_id,
                "response": _normalize(item.response or {}, identities),
                "accepted_at": _timestamp(item.accepted_at),
            }
            for ordinal, item in enumerate(receipts, 1)
        ],
        "interventions": [
            {
                "logical_id": f"intervention:{episode_id}:{ordinal:03d}",
                "state": item.state,
                "reason_code": item.reason_code,
                "title": item.title,
                "body": item.body,
                "created_at": _timestamp(item.created_at),
            }
            for ordinal, item in enumerate(interventions, 1)
        ],
        "proactive_decisions": [
            {
                "logical_id": f"decision:{episode_id}:{ordinal:03d}",
                "status": item.status,
                "outcome": item.outcome,
                "reason_code": item.reason_code,
                "decided_at": _timestamp(item.decided_at),
            }
            for ordinal, item in enumerate(decisions, 1)
        ],
        "plan_proposals": [
            {
                "logical_id": f"proposal:{episode_id}:{ordinal:03d}",
                "title": item.title,
                "status": item.status,
                "plan_payload": _normalize(item.plan_payload or {}, identities),
                "created_at": _timestamp(item.created_at),
            }
            for ordinal, item in enumerate(proposals, 1)
        ],
        "plans": [
            {
                "plan_id": item.id,
                "title": item.title,
                "status": item.status,
                "weekly_minutes": item.weekly_minutes,
                "version": item.version,
            }
            for item in plans
        ],
        "submissions": [
            {
                "submission_id": item.id,
                "status": item.status,
                "score": item.score,
                "feedback": item.feedback,
                "checked_at": _timestamp(item.checked_at),
            }
            for item in submissions
        ],
    }
    return projection


def _tool_trace(projection: dict[str, Any]) -> list[dict[str, Any]]:
    trace = []
    for ordinal, item in enumerate(projection["tool_invocations"], 1):
        durable_status = item["status"]
        if durable_status in {"committed", "pending_delivery", "pending_approval"}:
            status = "succeeded"
        elif durable_status in {"rejected", "cancelled"}:
            status = "blocked"
        else:
            status = "failed"
        result = {
            "durable_status": durable_status,
            "data": _trace_references(item["result"]),
        }
        trace.append(
            {
                "invocation_id": item["logical_id"],
                "ordinal": ordinal,
                "tool_name": item["tool_name"],
                "canonical_args": item["canonical_args"],
                "status": status,
                "result": result,
                "result_digest": sha256_digest(result),
            }
        )
    return trace


def _event_trace(projection: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": item["logical_id"],
            "ordinal": ordinal,
            "event_type": item["event_type"],
            "payload": _trace_references(item["payload"]),
            "payload_digest": sha256_digest(_trace_references(item["payload"])),
        }
        for ordinal, item in enumerate(projection["run_events"], 1)
    ]


def _operation_trace(
    fixture: dict[str, Any],
    projection: dict[str, Any],
) -> list[dict[str, Any]]:
    entity_by_type = {
        item["entity_type"]: item["logical_id"]
        for item in fixture["state_before"]["logical_entities"]
    }
    aliases = {"submission": "submission", "plan": "plan"}
    trace = []
    for ordinal, item in enumerate(projection["operations"], 1):
        entity_ref = entity_by_type.get(aliases.get(item["entity_type"], item["entity_type"]))
        if entity_ref is None:
            raise RuntimeError("E1 Mini operation has no state-before logical entity")
        patch = {
            "forward_patch": _trace_references(item["forward_patch"]),
            "inverse_patch": _trace_references(item["inverse_patch"]),
        }
        trace.append(
            {
                "operation_id": item["logical_id"],
                "ordinal": ordinal,
                "tool_name": item["tool_name"],
                "entity_ref": entity_ref,
                **patch,
                "patch_digest": sha256_digest(patch),
            }
        )
    return trace


def _actual_result(
    fixture: dict[str, Any],
    projection: dict[str, Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    track = fixture["track"]
    guard = {"status": "not_evaluated", "reason_code": None, "blocked_effect_refs": []}
    changes: list[dict[str, Any]] = []
    if track == "planning":
        if not projection["plan_proposals"]:
            raise RuntimeError("planning fixture produced no plan proposal")
        action = "PROPOSE_PLAN"
    elif track == "intervention":
        decisions = projection["proactive_decisions"]
        if not decisions or decisions[0]["outcome"] != "success_intervention":
            raise RuntimeError("intervention fixture did not pass the production guard")
        if not projection["outbox_receipts"]:
            raise RuntimeError("intervention fixture produced no durable receipt")
        action = "INTERVENE_MESSAGE"
        guard = {
            "status": "allowed",
            "reason_code": "notification_guard_allowed",
            "blocked_effect_refs": [],
        }
    elif track == "assessment":
        submissions = projection["submissions"]
        if not submissions:
            raise RuntimeError("assessment fixture has no submission")
        action = "REVISION_REQUIRED" if submissions[0]["status"] == "revision_required" else "ACCEPT"
        if not operations:
            raise RuntimeError("assessment fixture produced no operation")
        changes.append(
            {
                "change_id": f"change:{fixture['episode_id']}:submission-status",
                "path": "state_before.logical_entities[2].data.status",
                "before": "submitted",
                "after": submissions[0]["status"],
                "operation_ref": operations[0]["operation_id"],
            }
        )
    else:
        plans = projection["plans"]
        if not plans or plans[0]["weekly_minutes"] == fixture["seed"]["weekly_minutes"]:
            raise RuntimeError("revision fixture did not change the persisted plan")
        if not operations:
            raise RuntimeError("revision fixture produced no reversible operation")
        action = "APPLY_REVERSIBLE_PATCH"
        changes.append(
            {
                "change_id": f"change:{fixture['episode_id']}:weekly-minutes",
                "path": "state_before.logical_entities[0].data.weekly_minutes",
                "before": fixture["seed"]["weekly_minutes"],
                "after": plans[0]["weekly_minutes"],
                "operation_ref": operations[0]["operation_id"],
            }
        )
    return {
        "action_class": action,
        "user_visible_output": projection["run"]["output"] or None,
        "state_changes": changes,
        "guard": guard,
    }


def build_decision_episode(
    *,
    fixture: dict[str, Any],
    oracle: dict[str, Any],
    model_records: list[dict[str, Any]],
    database_projection: dict[str, Any],
    database_projection_sha256: str,
    resource_version: str,
    resource_digest: str,
    git_commit: str,
    invocation_mode: str,
    model_name: str,
    model_temperature: float,
    model_reasoning_effort: str,
) -> dict[str, Any]:
    invocations = _tool_trace(database_projection)
    invocation_by_call = {
        item["tool_call_id"]: item["logical_id"]
        for item in database_projection["tool_invocations"]
        if item["tool_call_id"]
    }
    model_turns = []
    for record in model_records:
        model_turns.append(
            {
                "turn_id": f"turn:{fixture['episode_id']}:{record['ordinal']:03d}",
                "ordinal": record["ordinal"],
                "visible_input_digest": record["visible_input_digest"],
                "assistant_text": record["assistant_text"] or None,
                "tool_call_refs": [
                    invocation_by_call[call["call_id"]]
                    for call in record["function_calls"]
                    if call["call_id"] in invocation_by_call
                ],
            }
        )
    operations = _operation_trace(fixture, database_projection)
    first_record = model_records[0]
    proactive_version = str(
        import_module("app.runtime.proactive").POLICY_VERSION
    )
    evidence_version = str(
        import_module("app.services.evidence").EVIDENCE_POLICY_VERSION
    )
    environment = {
        "schema_version": "environment-manifest-v1",
        "frozen_time": fixture["frozen_time"],
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
            "fixture_db_sha256": database_projection_sha256,
        },
        "isolation": {
            "production_database_access": False,
            "network_access": "disabled" if invocation_mode == "stub" else "model_provider_only",
            "notification_mode": "fake_outbox",
        },
        "manifest_sha256": "0" * 64,
    }
    environment["manifest_sha256"] = environment_manifest_digest(environment)
    episode = {
        "schema_version": "decision-episode-v1",
        "episode_id": fixture["episode_id"],
        "scenario_family_id": fixture["scenario_family_id"],
        "track": fixture["track"],
        "split": fixture["split"],
        "difficulty": fixture["difficulty"],
        "tags": ["e1_runtime_mini", "engineering_only", f"{invocation_mode}_model"],
        "trigger": fixture["trigger"],
        "state_before": fixture["state_before"],
        "environment": environment,
        "observable_trace": {
            "capture_mode": "runtime_recording",
            "model_turns": model_turns,
            "tool_invocations": invocations,
            "run_events": _event_trace(database_projection),
            "operations": operations,
        },
        "result": _actual_result(fixture, database_projection, operations),
        "oracle": oracle,
        "provenance": {
            "source_type": "runtime_export",
            "construction_method": "runtime_recorded",
            "author_role": "evaluation_runtime_recorder",
            "reviewer_role": "evaluation_protocol_reviewer",
            "purpose": "Exercise the E1 isolated production Runtime chain with a deterministic engineering fixture.",
            "dataset_role": "protocol_mini_fixture",
            "runtime_executed": True,
            "formal_evaluation_result": False,
            "evaluation_status": "not_a_formal_model_evaluation",
            "created_at": fixture["frozen_time"],
            "source_refs": [
                f"fixture:{fixture['episode_id']}",
                f"resource:{resource_version}",
            ],
            "episode_sha256": "0" * 64,
        },
    }
    episode["provenance"]["episode_sha256"] = decision_episode_digest(episode)
    return episode
