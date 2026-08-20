from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.db.uow import flush as flush_uow
from app.models import AgentRun, Intervention


class InterventionStateError(RuntimeError):
    """A reply target cannot make the requested durable state transition."""


async def accept_intervention_reply(
    db: AsyncSession,
    intervention: Intervention | None,
) -> Intervention | None:
    """Record acceptance of a typed reply in the caller's short UoW."""

    if intervention is None:
        return None
    if intervention.state == "active":
        intervention.state = "replied"
        intervention.outcome = "reply_received"
        await flush_uow(db)
        return intervention
    if intervention.state == "replied":
        # Email ingestion predates the normalized outcome, while all new
        # in-app/queue replies use reply_received. Both represent the same
        # non-terminal state and are safe to resume after a restart.
        if intervention.outcome == "":
            intervention.outcome = "reply_received"
            await flush_uow(db)
        elif intervention.outcome != "reply_received":
            raise InterventionStateError("Intervention reply acceptance conflicts with durable state")
        return intervention
    raise InterventionStateError("Intervention reply target is no longer active")


async def finalize_run_intervention(
    db: AsyncSession,
    run: AgentRun,
) -> Intervention | None:
    """Close a root reply target atomically with its durable Run terminal state."""

    intervention_id = run.reply_to_intervention_id
    if intervention_id is None or run.parent_run_id is not None:
        return None
    if run.status not in {"completed", "failed", "cancelled"}:
        raise InterventionStateError("Intervention outcome requires a terminal Run")
    intervention = await db.get(Intervention, intervention_id)
    if (
        intervention is None
        or intervention.owner_id != run.owner_id
        or intervention.session_id != run.session_id
        or intervention.plan_id != run.plan_id
    ):
        raise InterventionStateError("Run Intervention reply scope mismatch")

    if run.status == "cancelled":
        desired_state = "cancelled"
        desired_outcome = "run_cancelled"
    else:
        desired_state = "resolved"
        desired_outcome = "run_completed" if run.status == "completed" else "run_failed"

    if intervention.state in {"resolved", "cancelled"}:
        if intervention.state != desired_state or intervention.outcome != desired_outcome:
            raise InterventionStateError("Terminal Intervention outcome conflicts with Run replay")
        return intervention
    if intervention.state not in {"active", "replied"}:
        raise InterventionStateError("Intervention reply target cannot be finalized")

    intervention.state = desired_state
    intervention.outcome = desired_outcome
    intervention.resolved_at = utc_now() if desired_state == "resolved" else None
    await flush_uow(db)
    return intervention
