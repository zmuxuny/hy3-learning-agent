import asyncio
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.database import AsyncSessionLocal
from app.db.uow import run_short_transaction
from app.models import (
    AgentRun,
    EvidenceObservation,
    LearningEvent,
    Notification,
    Plan,
    ProactiveDecision,
    ReviewSchedule,
    Stage,
    Task,
    UserProfile,
)
from app.notifications.email import EmailReplyPoller
from app.runtime.agent import AgentRuntime
from app.runtime.proactive import SUCCESS_OUTCOMES, capture_proactive_candidate
from app.runtime.state import NONTERMINAL_RUN_STATUSES, ensure_root_scope_available
from app.runtime.tasks import start_tracked_task
from app.services.evidence import build_plan_evidence_state


class ProactiveScheduler:
    def __init__(self):
        self._loop_task: asyncio.Task | None = None
        self._run_tasks: set[asyncio.Task] = set()
        self._started_at: datetime | None = None
        self._last_cycle_at: datetime | None = None
        self._next_cycle_at: datetime | None = None
        self._last_decision = "waiting_for_first_cycle"
        self._trigger_lock = asyncio.Lock()

    def start(self) -> None:
        if settings.ENABLE_SCHEDULER and self._loop_task is None:
            self._started_at = utc_now()
            self._next_cycle_at = self._started_at + timedelta(seconds=settings.AGENT_HEARTBEAT_SECONDS)
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
            self._next_cycle_at = None
        active_tasks = list(self._run_tasks)
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        self._run_tasks.clear()

    async def trigger_now(
        self,
        trigger: str = "heartbeat",
        *,
        plan_id: int | None = None,
        objective: str | None = None,
        candidate: dict | None = None,
    ) -> AgentRun:
        async def create_run(db) -> AgentRun:
            await ensure_root_scope_available(
                db,
                owner_id=settings.DEFAULT_OWNER_ID,
                plan_id=plan_id,
                session_id=None,
            )
            run = AgentRun(
                owner_id=settings.DEFAULT_OWNER_ID,
                trigger=trigger,
                objective=(
                    objective or "检查进行中的计划、近期学习事件、到期复习、通知历史和学习偏好。"
                    "使用工具收集证据，再决定保持安静、提醒、抽查或执行可撤销的低风险调整。用简体中文汇报。"
                ),
                model=settings.MODEL_NAME,
                plan_id=plan_id,
            )
            if candidate is not None:
                capture_proactive_candidate(run, candidate)
            db.add(run)
            return run

        # This is a bounded, DB-only claim.  It is safe to replay after a
        # SQLite busy rollback and it never wraps model/network work.
        async with self._trigger_lock:
            run = await run_short_transaction(AsyncSessionLocal, create_run)
            run_id = run.id
        task = start_tracked_task(run_id, AgentRuntime().run(run_id))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return run

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.AGENT_HEARTBEAT_SECONDS)
            self._last_cycle_at = utc_now()
            self._next_cycle_at = self._last_cycle_at + timedelta(seconds=settings.AGENT_HEARTBEAT_SECONDS)
            try:
                await self._poll_email_replies()
                if await self._paused_by_user():
                    self._last_decision = "paused_by_user"
                    continue
                candidate = await self._next_candidate()
                if candidate:
                    await self.trigger_now(
                        "heartbeat",
                        plan_id=candidate["plan_id"],
                        objective=candidate["objective"],
                        candidate=candidate,
                    )
                    self._last_decision = candidate["reason"]
                else:
                    self._last_decision = "quiet_no_intervention_needed"
            except RuntimeError:
                self._last_decision = "heartbeat_already_running"
                continue
            except Exception:
                self._last_decision = "cycle_error"
                continue

    async def _paused_by_user(self) -> bool:
        async with AsyncSessionLocal() as db:
            profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
            return bool(profile and profile.proactive_paused)

    async def describe(self) -> dict:
        async with AsyncSessionLocal() as db:
            latest = (await db.execute(
                select(AgentRun).where(
                    AgentRun.owner_id == settings.DEFAULT_OWNER_ID,
                    AgentRun.trigger.in_(["heartbeat", "manual_heartbeat"]),
                    AgentRun.parent_run_id.is_(None),
                ).order_by(AgentRun.created_at.desc()).limit(1)
            )).scalars().one_or_none()
        return {
            "enabled": settings.ENABLE_SCHEDULER,
            "scope": "global",
            "interval_seconds": settings.AGENT_HEARTBEAT_SECONDS,
            "progress_checkin_hours": settings.AGENT_PROGRESS_CHECKIN_HOURS,
            "candidate_cooldown_minutes": settings.AGENT_CANDIDATE_COOLDOWN_MINUTES,
            "started_at": canonical_utc(self._started_at),
            "last_cycle_at": canonical_utc(self._last_cycle_at),
            "next_cycle_at": canonical_utc(self._next_cycle_at),
            "last_decision": self._last_decision,
            "paused": await self._paused_by_user(),
            "active": bool(latest and latest.status in NONTERMINAL_RUN_STATUSES),
            "last_run": ({
                "id": latest.id,
                "trigger": latest.trigger,
                "status": latest.status,
                "plan_id": latest.plan_id,
                "created_at": canonical_utc(latest.created_at),
                "completed_at": canonical_utc(latest.completed_at),
            } if latest else None),
        }

    async def _next_candidate(self) -> dict | None:
        now = utc_now()
        async with AsyncSessionLocal() as db:
            recently_checked_plan_ids: set[int] = set()
            if settings.AGENT_CANDIDATE_COOLDOWN_MINUTES > 0:
                cooldown_start = now - timedelta(minutes=settings.AGENT_CANDIDATE_COOLDOWN_MINUTES)
                successful_decisions = list(
                    (
                        await db.execute(
                            select(ProactiveDecision).where(
                                ProactiveDecision.owner_id == settings.DEFAULT_OWNER_ID,
                                ProactiveDecision.status == "terminal",
                                ProactiveDecision.outcome.in_(SUCCESS_OUTCOMES),
                                ProactiveDecision.plan_id.is_not(None),
                            )
                        )
                    ).scalars()
                )
                recently_checked_plan_ids = {
                    decision.plan_id
                    for decision in successful_decisions
                    if decision.plan_id is not None
                    and (
                        (
                            decision.next_eligible_at is not None
                            and coerce_legacy_utc(decision.next_eligible_at) > now
                        )
                        or (
                            decision.next_eligible_at is None
                            and decision.decided_at is not None
                            and coerce_legacy_utc(decision.decided_at) >= cooldown_start
                        )
                    )
                }

            due_reviews = list((await db.execute(
                select(ReviewSchedule)
                .join(Plan, Plan.id == ReviewSchedule.plan_id)
                .where(
                    ReviewSchedule.owner_id == settings.DEFAULT_OWNER_ID,
                    ReviewSchedule.status == "scheduled",
                    ReviewSchedule.due_at <= now,
                    Plan.status == "active",
                    ReviewSchedule.plan_id.notin_(recently_checked_plan_ids),
                )
                .order_by(ReviewSchedule.due_at)
                .limit(20)
            )).scalars())
            for due_review in due_reviews:
                candidate = {
                    "plan_id": due_review.plan_id,
                    "reason": "due_review",
                    "candidate_key": f"review:{due_review.id}",
                    "candidate_kind": "due_review",
                    "candidate_payload": {
                        "plan_id": due_review.plan_id,
                        "review_id": due_review.id,
                        "task_id": due_review.task_id,
                        "due_at": canonical_utc(due_review.due_at),
                    },
                    "detected_at": now,
                    "objective": f"复习安排 {due_review.id} 已到期，关联任务 {due_review.task_id}。检查提交证据与近期事件，再决定创建合适的抽查或有用提醒；用简体中文汇报。",
                }
                candidate.update(await _evidence_source_envelope(db, due_review.plan_id))
                if await _candidate_is_eligible(db, candidate["candidate_key"], now):
                    return candidate

            due_tasks = list((await db.execute(
                select(Task)
                .join(Stage).join(Plan)
                .where(
                    Plan.owner_id == settings.DEFAULT_OWNER_ID,
                    Plan.status == "active",
                    Task.status.in_(["pending", "active", "blocked"]),
                    Task.due_at.is_not(None),
                    Task.due_at <= now + timedelta(hours=24),
                    Plan.id.notin_(recently_checked_plan_ids),
                )
                .order_by(Task.due_at)
                .options(selectinload(Task.stage))
                .limit(20)
            )).scalars())
            for due_task in due_tasks:
                candidate = {
                    "plan_id": due_task.stage.plan_id,
                    "reason": "task_due_within_24h",
                    "candidate_key": f"task:{due_task.id}:due",
                    "candidate_kind": "task_due_within_24h",
                    "candidate_payload": {
                        "plan_id": due_task.stage.plan_id,
                        "task_id": due_task.id,
                        "due_at": canonical_utc(due_task.due_at),
                    },
                    "detected_at": now,
                    "objective": f"任务 {due_task.id} 将在 24 小时内截止或已经逾期。检查当前证据、近期提醒和学习活动；只有确实有帮助时才介入，并用简体中文汇报。",
                }
                candidate.update(await _evidence_source_envelope(db, due_task.stage.plan_id))
                if await _candidate_is_eligible(db, candidate["candidate_key"], now):
                    return candidate

            plans = list((await db.execute(
                select(Plan).where(
                    Plan.owner_id == settings.DEFAULT_OWNER_ID,
                    Plan.status == "active",
                ).order_by(Plan.updated_at)
            )).scalars())
            for plan in plans:
                if plan.id in recently_checked_plan_ids:
                    continue
                activity_events = list((await db.execute(
                    select(LearningEvent).where(
                        LearningEvent.owner_id == settings.DEFAULT_OWNER_ID,
                        LearningEvent.plan_id == plan.id,
                        LearningEvent.event_type == "email.reply.received",
                        LearningEvent.invalidated_at.is_(None),
                    ).order_by(LearningEvent.occurred_at.desc()).limit(20)
                )).scalars())
                last_event = next((event for event in activity_events if _is_learning_activity(event)), None)
                checkin_before = now - timedelta(hours=settings.AGENT_PROGRESS_CHECKIN_HOURS)
                source_envelope = await _evidence_source_envelope(db, plan.id)
                projected_activity_at = source_envelope.pop("last_evidence_occurred_at")
                fallback_activity_at = coerce_legacy_utc(
                    last_event.occurred_at if last_event else plan.created_at
                )
                last_activity_at = max(
                    value
                    for value in (projected_activity_at, fallback_activity_at)
                    if value is not None
                )
                recent_notification = (await db.execute(
                    select(Notification.id).where(
                        Notification.owner_id == settings.DEFAULT_OWNER_ID,
                        Notification.plan_id == plan.id,
                        Notification.channel == "in_app",
                        Notification.sent_at >= checkin_before,
                    ).limit(1)
                )).scalar_one_or_none()
                if last_activity_at < checkin_before and recent_notification is None:
                    candidate = {
                        "plan_id": plan.id,
                        "reason": "progress_checkin_due",
                        "candidate_key": f"plan:{plan.id}:progress_checkin",
                        "candidate_kind": "progress_checkin_due",
                        "candidate_payload": {
                            "plan_id": plan.id,
                            "last_activity_at": canonical_utc(last_activity_at),
                            "checkin_before": canonical_utc(checkin_before),
                        },
                        "detected_at": now,
                        "objective": (
                            f"计划 {plan.id} 已超过 {settings.AGENT_PROGRESS_CHECKIN_HOURS} 小时没有新的学习证据。"
                            "读取当前任务、近期提交和通知历史；如果没有更新，主动发一条简短站内询问，"
                            "请学习者说明进度、阻塞或是否需要调整。若已有充分证据表明无需打扰，则保持安静。"
                        ),
                    }
                    candidate.update(source_envelope)
                    if await _candidate_is_eligible(db, candidate["candidate_key"], now):
                        return candidate
        return None

    async def _poll_email_replies(self) -> None:
        async with AsyncSessionLocal() as db:
            run_ids = await EmailReplyPoller().poll(db, settings.DEFAULT_OWNER_ID)
        for run_id in run_ids:
            task = start_tracked_task(run_id, AgentRuntime().run(run_id))
            self._run_tasks.add(task)
            task.add_done_callback(self._run_tasks.discard)


def _is_learning_activity(event: LearningEvent) -> bool:
    """Admit only independently verified, non-Evidence learner activity.

    Submission/quiz/task events are projections of Evidence producers and must
    never outlive an append-only Evidence invalidation. Generic mail engagement
    is not proof of learning either; ingress must explicitly classify a reply
    before it can delay a progress check-in.
    """

    if event.event_type != "email.reply.received":
        return False
    payload = event.payload or {}
    return payload.get("verified_non_evidence_learning_activity") is True


async def _candidate_is_eligible(db, candidate_key: str, now: datetime) -> bool:
    latest = (
        await db.execute(
            select(ProactiveDecision)
            .where(
                ProactiveDecision.owner_id == settings.DEFAULT_OWNER_ID,
                ProactiveDecision.candidate_key == candidate_key,
                ProactiveDecision.status == "terminal",
            )
            .order_by(ProactiveDecision.decided_at.desc())
            .limit(1)
        )
    ).scalars().one_or_none()
    if latest is None or latest.next_eligible_at is None:
        return True
    return coerce_legacy_utc(latest.next_eligible_at) <= now


async def _evidence_source_envelope(db, plan_id: int) -> dict:
    """Describe the effective Evidence projection used by candidate selection."""
    projection = await build_plan_evidence_state(db, settings.DEFAULT_OWNER_ID, plan_id)
    occurred_values = [
        datetime.fromisoformat(item["last_observed_at"])
        for item in projection.get("by_task", [])
        if item.get("last_observed_at")
    ]
    unscoped_ids = list(projection.get("unscoped_observation_ids") or [])
    if unscoped_ids:
        occurred_values.extend(
            list(
                (
                    await db.execute(
                        select(EvidenceObservation.occurred_at).where(
                            EvidenceObservation.id.in_(unscoped_ids)
                        )
                    )
                ).scalars()
            )
        )
    watermark = await db.scalar(
        select(EvidenceObservation.id)
        .where(
            EvidenceObservation.owner_id == settings.DEFAULT_OWNER_ID,
            EvidenceObservation.plan_id == plan_id,
        )
        .order_by(EvidenceObservation.id.desc())
        .limit(1)
    )
    return {
        "source_watermark": watermark,
        "source_projection_digest": projection.get("digest"),
        "last_evidence_occurred_at": (
            max(coerce_legacy_utc(value) for value in occurred_values)
            if occurred_values
            else None
        ),
    }


proactive_scheduler = ProactiveScheduler()
