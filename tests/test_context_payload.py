from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.settings import read_settings
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, Memory, RunEvent
from app.runtime.agent import AgentRuntime


@pytest.mark.asyncio
async def test_context_built_event_carries_memory_ids():
    async with AsyncSessionLocal() as db:
        db.add(Memory(
            owner_id="local",
            scope="global",
            layer="semantic",
            content="用户偏好通过项目实战学习",
            confidence=0.95,
            status="confirmed",
        ))
        run = AgentRun(owner_id="local", trigger="user_message", objective="今天学什么")
        db.add(run)
        await db.commit()
        run_id = run.id
        memory_id = (await db.execute(select(Memory))).scalars().one().id

    class FinalCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="先复习今天的目标。",
                reasoning_content=None,
                tool_calls=None,
            ))])

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=FinalCompletions()))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        context_event = next(event for event in events if event.event_type == "context.built")
        assert context_event.payload["estimated_tokens"] > 0
        assert memory_id in context_event.payload["memory_ids"]


@pytest.mark.asyncio
async def test_settings_expose_model_context_window():
    settings = await read_settings()
    assert settings["model_context_window"] > 0
    assert settings["context_token_budget"] > 0
