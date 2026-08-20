"""Durable proactive-candidate and decision lifecycle helpers.

The helpers only mutate the caller's ``AsyncSession`` and flush when an id is
needed.  They never commit and never perform provider or network work, so a
Guard outcome, its Intervention, and its delivery rows can share one UoW.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import coerce_legacy_utc, utc_now
from app.db.uow import flush as flush_uow
from app.models import AgentRun, ProactiveDecision


POLICY_VERSION = "proactive-decision-v1"
RETRY_BACKOFF = timedelta(minutes=5)
TERMINAL_OUTCOMES = {
    "success_wait",
    "success_intervention",
    "deferred_quiet_hours",
    "guard_rejected",
    "model_failed",
    "runtime_failed",
    "cancelled",
    "legacy_unverified",
}
SUCCESS_OUTCOMES = {"success_wait", "success_intervention"}


class ProactiveDecisionConflictError(ValueError):
    """A Run/Decision identity was replayed with different immutable input."""


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_proactive_candidate(
    run: AgentRun,
    candidate: dict[str, Any],
    *,
    detected_at: datetime | None = None,
) -> AgentRun:
    """Copy an immutable scheduler candidate envelope onto its root Run."""
    candidate_key = str(candidate["candidate_key"])
    candidate_kind = str(candidate.get("candidate_kind") or candidate["reason"])
    payload = dict(candidate.get("candidate_payload") or {})
    run.proactive_candidate_state = "captured"
    run.proactive_candidate_key = candidate_key
    run.proactive_candidate_kind = candidate_kind
    run.proactive_candidate_payload = payload
    run.proactive_candidate_digest = canonical_digest(
        {"key": candidate_key, "kind": candidate_kind, "payload": payload}
    )
    run.proactive_source_watermark = candidate.get("source_watermark")
    run.proactive_source_projection_digest = candidate.get("source_projection_digest")
    run.proactive_detected_at = detected_at or candidate.get("detected_at") or utc_now()
    return run


async def create_proactive_decision(
    db: AsyncSession,
    run: AgentRun,
    *,
    source_invocation_id: int | None = None,
    policy_version: str = POLICY_VERSION,
    policy_payload: dict[str, Any] | None = None,
) -> ProactiveDecision | None:
    """Create/retrieve the building Decision for a captured proactive root Run."""
    if run.proactive_candidate_state != "captured":
        return None
    existing = (
        await db.execute(
            select(ProactiveDecision).where(ProactiveDecision.source_run_id == run.id)
        )
    ).scalars().one_or_none()
    if existing is not None:
        _assert_decision_identity(
            existing,
            run,
            source_invocation_id=source_invocation_id,
            policy_version=policy_version,
            policy_payload=policy_payload,
        )
        return existing
    policy_payload = dict(policy_payload or {})
    decision = ProactiveDecision(
        owner_id=run.owner_id,
        plan_id=run.plan_id,
        source_run_id=run.id,
        source_invocation_id=source_invocation_id,
        candidate_key=run.proactive_candidate_key,
        candidate_kind=run.proactive_candidate_kind,
        candidate_payload=dict(run.proactive_candidate_payload or {}),
        candidate_digest=run.proactive_candidate_digest,
        source_watermark=run.proactive_source_watermark,
        source_projection_digest=run.proactive_source_projection_digest,
        policy_version=policy_version,
        policy_digest=canonical_digest(policy_payload),
        status="building",
        decision_payload={},
        decision_digest=canonical_digest({}),
    )
    try:
        async with db.begin_nested():
            db.add(decision)
            await flush_uow(db)
        return decision
    except IntegrityError:
        existing = (
            await db.execute(
                select(ProactiveDecision).where(
                    ProactiveDecision.source_run_id == run.id
                )
            )
        ).scalars().one_or_none()
        if existing is None:
            raise
        _assert_decision_identity(
            existing,
            run,
            source_invocation_id=source_invocation_id,
            policy_version=policy_version,
            policy_payload=policy_payload,
        )
        return existing


async def finalize_proactive_decision(
    db: AsyncSession,
    run: AgentRun,
    *,
    outcome: str,
    reason_code: str,
    next_eligible_at: datetime | None = None,
    decision_payload: dict[str, Any] | None = None,
    source_invocation_id: int | None = None,
    decided_at: datetime | None = None,
) -> ProactiveDecision | None:
    """Finalize one captured Run exactly once inside the caller's UoW.

    Runtime hooks should call this for ``success_wait``, ``model_failed``,
    ``runtime_failed`` and ``cancelled``. ``NotificationService`` calls it for
    ``success_intervention`` and Guard rejections.
    """
    if outcome not in TERMINAL_OUTCOMES:
        raise ValueError(f"unsupported proactive decision outcome: {outcome}")
    decision = await create_proactive_decision(
        db,
        run,
        source_invocation_id=source_invocation_id,
    )
    if decision is None:
        return decision
    if decision.status == "terminal":
        replay_decided_at = decision.decided_at or decided_at or utc_now()
        replay_next_eligible_at = next_eligible_at
        if replay_next_eligible_at is None and outcome in {
            "guard_rejected",
            "model_failed",
            "runtime_failed",
        }:
            replay_next_eligible_at = replay_decided_at + RETRY_BACKOFF
        payload = dict(decision_payload or {})
        desired_digest = canonical_digest(
            {
                "next_eligible_at": replay_next_eligible_at,
                "outcome": outcome,
                "payload": payload,
                "reason_code": reason_code,
            }
        )
        _assert_terminal_replay(
            decision,
            outcome=outcome,
            reason_code=reason_code,
            next_eligible_at=replay_next_eligible_at,
            decision_payload=payload,
            decision_digest=desired_digest,
        )
        return decision
    decided_at = decided_at or utc_now()
    if next_eligible_at is None and outcome in {
        "guard_rejected",
        "model_failed",
        "runtime_failed",
    }:
        next_eligible_at = decided_at + RETRY_BACKOFF
    payload = dict(decision_payload or {})
    decision_digest = canonical_digest(
        {
            "next_eligible_at": next_eligible_at,
            "outcome": outcome,
            "payload": payload,
            "reason_code": reason_code,
        }
    )
    result = await db.execute(
        update(ProactiveDecision)
        .where(
            ProactiveDecision.id == decision.id,
            ProactiveDecision.status == "building",
        )
        .values(
            status="terminal",
            outcome=outcome,
            reason_code=reason_code,
            next_eligible_at=next_eligible_at,
            decision_payload=payload,
            decision_digest=decision_digest,
            decided_at=decided_at,
        )
    )
    await db.refresh(decision)
    if result.rowcount != 1:
        _assert_terminal_replay(
            decision,
            outcome=outcome,
            reason_code=reason_code,
            next_eligible_at=next_eligible_at,
            decision_payload=payload,
            decision_digest=decision_digest,
        )
    return decision


def next_quiet_hours_end(
    now: datetime,
    quiet_hours: dict[str, Any],
    *,
    timezone_name: str,
) -> datetime | None:
    """Return the exact next quiet-window end in UTC, or None when outside it."""
    local_now = coerce_legacy_utc(now).astimezone(ZoneInfo(timezone_name))
    try:
        start = datetime.strptime(str(quiet_hours.get("start", "23:00")), "%H:%M").time()
        end = datetime.strptime(str(quiet_hours.get("end", "08:00")), "%H:%M").time()
    except ValueError:
        return None
    current = local_now.time().replace(tzinfo=None)
    if start == end:
        return None
    if start < end:
        if not start <= current < end:
            return None
        end_date = local_now.date()
    else:
        if current >= start:
            end_date = (local_now + timedelta(days=1)).date()
        elif current < end:
            end_date = local_now.date()
        else:
            return None
    local_end = datetime.combine(end_date, end, tzinfo=ZoneInfo(timezone_name))
    return local_end.astimezone(timezone.utc)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return coerce_legacy_utc(value).isoformat()
    return str(value)


def _assert_decision_identity(
    decision: ProactiveDecision,
    run: AgentRun,
    *,
    source_invocation_id: int | None,
    policy_version: str,
    policy_payload: dict[str, Any] | None,
) -> None:
    identity = {
        "owner_id": run.owner_id,
        "plan_id": run.plan_id,
        "source_run_id": run.id,
        "source_invocation_id": source_invocation_id,
        "candidate_key": run.proactive_candidate_key,
        "candidate_kind": run.proactive_candidate_kind,
        "candidate_payload": dict(run.proactive_candidate_payload or {}),
        "candidate_digest": run.proactive_candidate_digest,
        "source_watermark": run.proactive_source_watermark,
        "source_projection_digest": run.proactive_source_projection_digest,
        "policy_version": policy_version,
        "policy_digest": canonical_digest(policy_payload or {}),
    }
    conflicts = [
        field
        for field, expected in identity.items()
        if getattr(decision, field) != expected
    ]
    if conflicts:
        raise ProactiveDecisionConflictError(
            "proactive decision immutable identity conflict: "
            + ",".join(sorted(conflicts))
        )


def _assert_terminal_replay(
    decision: ProactiveDecision,
    *,
    outcome: str,
    reason_code: str,
    next_eligible_at: datetime | None,
    decision_payload: dict[str, Any],
    decision_digest: str,
) -> None:
    same_next = (
        decision.next_eligible_at is None and next_eligible_at is None
    ) or (
        decision.next_eligible_at is not None
        and next_eligible_at is not None
        and coerce_legacy_utc(decision.next_eligible_at)
        == coerce_legacy_utc(next_eligible_at)
    )
    if not (
        decision.status == "terminal"
        and decision.outcome == outcome
        and decision.reason_code == reason_code
        and same_next
        and dict(decision.decision_payload or {}) == decision_payload
        and decision.decision_digest == decision_digest
    ):
        raise ProactiveDecisionConflictError(
            "terminal proactive decision replay does not match durable outcome"
        )
