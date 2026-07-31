import asyncio
from contextlib import suppress

from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun
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

    async def trigger_now(self, trigger: str = "heartbeat") -> AgentRun:
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
                    "Inspect active plans, recent learning events, due reviews, notification history, and learner preferences. "
                    "Use tools to gather evidence. Decide whether to stay silent, remind, quiz, or make a reversible low-risk adjustment."
                ),
                model=settings.MODEL_NAME,
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
                await self.trigger_now()
            except RuntimeError:
                continue


proactive_scheduler = ProactiveScheduler()
