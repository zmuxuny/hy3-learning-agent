import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, LearningEvent, Notification, Plan, ReviewSchedule, Stage, Task
from app.notifications.email import EmailReplyPoller
from app.runtime.agent import AgentRuntime


class ProactiveScheduler:
    def __init__(self):
        self._loop_task: asyncio.Task | None = None
        self._run_tasks: set[asyncio.Task] = set()
        self._started_at: datetime | None = None
        self._last_cycle_at: datetime | None = None
        self._next_cycle_at: datetime | None = None
        self._last_decision = "waiting_for_first_cycle"

    def start(self) -> None:
        if settings.ENABLE_SCHEDULER and self._loop_task is None:
            self._started_at = datetime.now(timezone.utc)
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
        async with AsyncSessionLocal() as db:
            active = await db.execute(
                select(AgentRun.id).where(
                    AgentRun.owner_id == settings.DEFAULT_OWNER_ID,
                    AgentRun.trigger.in_(["heartbeat", "manual_heartbeat"]),
                    AgentRun.status.in_(["queued", "running"]),
                ).limit(1)
            )
            if active.scalar_one_or_none():
                raise RuntimeError("A heartbeat run is already active")
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
            await db.commit()
            await db.refresh(run)
            run_id = run.id
        task = asyncio.create_task(AgentRuntime().run(run_id))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return run

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.AGENT_HEARTBEAT_SECONDS)
            self._last_cycle_at = datetime.now(timezone.utc)
            self._next_cycle_at = self._last_cycle_at + timedelta(seconds=settings.AGENT_HEARTBEAT_SECONDS)
            try:
                await self._poll_email_replies()
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
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "next_cycle_at": self._next_cycle_at.isoformat() if self._next_cycle_at else None,
            "last_decision": self._last_decision,
            "active": bool(latest and latest.status in {"queued", "running"}),
            "last_run": ({
                "id": latest.id,
                "trigger": latest.trigger,
                "status": latest.status,
                "plan_id": latest.plan_id,
                "created_at": latest.created_at.isoformat(),
                "completed_at": latest.completed_at.isoformat() if latest.completed_at else None,
            } if latest else None),
        }

    async def _next_candidate(self) -> dict | None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            due_review = (await db.execute(
                select(ReviewSchedule)
                .join(Plan, Plan.id == ReviewSchedule.plan_id)
                .where(
                    ReviewSchedule.owner_id == settings.DEFAULT_OWNER_ID,
                    ReviewSchedule.status == "scheduled",
                    ReviewSchedule.due_at <= now,
                    Plan.status == "active",
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

            plan = (await db.execute(
                select(Plan).where(
                    Plan.owner_id == settings.DEFAULT_OWNER_ID,
                    Plan.status == "active",
                ).order_by(Plan.updated_at).limit(1)
            )).scalars().one_or_none()
            if plan:
                last_event = (await db.execute(
                    select(LearningEvent).where(
                        LearningEvent.owner_id == settings.DEFAULT_OWNER_ID,
                        LearningEvent.plan_id == plan.id,
                    ).order_by(LearningEvent.created_at.desc()).limit(1)
                )).scalars().one_or_none()
                checkin_before = now - timedelta(hours=settings.AGENT_PROGRESS_CHECKIN_HOURS)
                last_activity_at = _aware(last_event.created_at) if last_event else _aware(plan.created_at)
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
            task = asyncio.create_task(AgentRuntime().run(run_id))
            self._run_tasks.add(task)
            task.add_done_callback(self._run_tasks.discard)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


proactive_scheduler = ProactiveScheduler()
