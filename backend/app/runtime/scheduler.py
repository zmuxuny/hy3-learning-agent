import asyncio
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.database import AsyncSessionLocal
from app.db.uow import run_short_transaction
from app.models import AgentRun, LearningEvent, Notification, Plan, ReviewSchedule, Stage, Task, UserProfile
from app.notifications.email import EmailReplyPoller
from app.runtime.agent import AgentRuntime
from app.runtime.state import NONTERMINAL_RUN_STATUSES, ensure_root_scope_available
from app.runtime.tasks import start_tracked_task


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

    async def trigger_now(self, trigger: str = "heartbeat", *, plan_id: int | None = None, objective: str | None = None) -> AgentRun:
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
                    await self.trigger_now("heartbeat", plan_id=candidate["plan_id"], objective=candidate["objective"])
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
                recently_checked_plan_ids = set((await db.execute(
                    select(AgentRun.plan_id).where(
                        AgentRun.owner_id == settings.DEFAULT_OWNER_ID,
                        AgentRun.trigger.in_(["heartbeat", "manual_heartbeat"]),
                        AgentRun.parent_run_id.is_(None),
                        AgentRun.plan_id.is_not(None),
                        AgentRun.created_at >= cooldown_start,
                    ).distinct()
                )).scalars())

            due_review = (await db.execute(
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
                .limit(1)
            )).scalars().one_or_none()
            if due_review:
                return {
                    "plan_id": due_review.plan_id,
                    "reason": "due_review",
                    "objective": f"复习安排 {due_review.id} 已到期，关联任务 {due_review.task_id}。检查提交证据与近期事件，再决定创建合适的抽查或有用提醒；用简体中文汇报。",
                }

            due_task = (await db.execute(
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
                .limit(1)
            )).scalars().one_or_none()
            if due_task:
                return {
                    "plan_id": due_task.stage.plan_id,
                    "reason": "task_due_within_24h",
                    "objective": f"任务 {due_task.id} 将在 24 小时内截止或已经逾期。检查当前证据、近期提醒和学习活动；只有确实有帮助时才介入，并用简体中文汇报。",
                }

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
                        LearningEvent.event_type.in_([
                            "submission.created",
                            "submission.checked",
                            "quiz.graded",
                            "task.updated",
                            "email.reply.received",
                        ]),
                    ).order_by(LearningEvent.created_at.desc()).limit(20)
                )).scalars())
                last_event = next((event for event in activity_events if _is_learning_activity(event)), None)
                checkin_before = now - timedelta(hours=settings.AGENT_PROGRESS_CHECKIN_HOURS)
                last_activity_at = coerce_legacy_utc(
                    last_event.created_at if last_event else plan.created_at
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
                    return {
                        "plan_id": plan.id,
                        "reason": "progress_checkin_due",
                        "objective": (
                            f"计划 {plan.id} 已超过 {settings.AGENT_PROGRESS_CHECKIN_HOURS} 小时没有新的学习证据。"
                            "读取当前任务、近期提交和通知历史；如果没有更新，主动发一条简短站内询问，"
                            "请学习者说明进度、阻塞或是否需要调整。若已有充分证据表明无需打扰，则保持安静。"
                        ),
                    }
        return None

    async def _poll_email_replies(self) -> None:
        async with AsyncSessionLocal() as db:
            run_ids = await EmailReplyPoller().poll(db, settings.DEFAULT_OWNER_ID)
        for run_id in run_ids:
            task = start_tracked_task(run_id, AgentRuntime().run(run_id))
            self._run_tasks.add(task)
            task.add_done_callback(self._run_tasks.discard)


def _is_learning_activity(event: LearningEvent) -> bool:
    """Separate learner progress from Agent housekeeping and plan metadata edits."""
    if event.event_type != "task.updated":
        return True
    payload = event.payload or {}
    after = payload.get("after") or {}
    return after.get("status") == "completed" or bool(payload.get("evidence"))


proactive_scheduler = ProactiveScheduler()
