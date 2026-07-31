import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, LearningEvent, Plan, ReviewSchedule, Stage, Task
from app.notifications.email import EmailReplyPoller
from app.runtime.agent import AgentRuntime


class ProactiveScheduler:
    def __init__(self):
        self._loop_task: asyncio.Task | None = None
        self._run_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if settings.ENABLE_SCHEDULER and self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for task in list(self._run_tasks):
            task.cancel()

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
            try:
                await self._poll_email_replies()
                candidate = await self._next_candidate()
                if candidate:
                    await self.trigger_now("heartbeat", plan_id=candidate["plan_id"], objective=candidate["objective"])
            except RuntimeError:
                continue

    async def _next_candidate(self) -> dict | None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            due_review = (await db.execute(
                select(ReviewSchedule)
                .where(ReviewSchedule.owner_id == settings.DEFAULT_OWNER_ID, ReviewSchedule.status == "scheduled", ReviewSchedule.due_at <= now)
                .order_by(ReviewSchedule.due_at)
                .limit(1)
            )).scalars().one_or_none()
            if due_review:
                return {
                    "plan_id": due_review.plan_id,
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
                    "objective": f"任务 {due_task.id} 将在 24 小时内截止或已经逾期。检查当前证据、近期提醒和学习活动；只有确实有帮助时才介入，并用简体中文汇报。",
                }

            last_event = (await db.execute(
                select(LearningEvent).where(LearningEvent.owner_id == settings.DEFAULT_OWNER_ID)
                .order_by(LearningEvent.created_at.desc()).limit(1)
            )).scalars().one_or_none()
            if last_event and last_event.created_at and _aware(last_event.created_at) < now - timedelta(days=3):
                plan = (await db.execute(
                    select(Plan).where(Plan.owner_id == settings.DEFAULT_OWNER_ID, Plan.status == "active").order_by(Plan.updated_at).limit(1)
                )).scalars().one_or_none()
                if plan:
                    return {
                        "plan_id": plan.id,
                        "objective": "已经超过三天没有学习活动。检查计划与通知历史；只有 Guard 允许且确有必要时，发送一条具体的重新开始建议。用简体中文汇报。",
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
