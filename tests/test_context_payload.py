from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.agent import read_run_context
from app.api.settings import read_settings
from app.context.memory import MemoryManager
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, RunEvent
from app.runtime.agent import AgentRuntime


@pytest.mark.asyncio
async def test_context_built_event_carries_memory_ids():
    async with AsyncSessionLocal() as db:
        memory, reused = await MemoryManager(db).propose(
            "local",
            scope="global",
            scope_id=None,
            layer="semantic",
            content="今天学习应遵循用户偏好并通过项目实战",
            source_type="user",
            confidence=0.95,
        )
        assert reused is False
        memory = await MemoryManager(db).confirm("local", memory.id)
        run = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="根据用户偏好通过项目实战安排今天学习",
        )
        db.add(run)
        await db.commit()
        run_id = run.id
        memory_id = memory.id

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
        match = next(item for item in context_event.payload["memory_matches"] if item["id"] == memory_id)
        assert match["scope"] == "global"
        assert match["score_breakdown"]["memory_id"] == memory_id
        assert match["score_breakdown"]["total"] > 0
        snapshot = await read_run_context(run_id, db)
        assert snapshot.id == context_event.payload["snapshot_id"]
        assert f"memory:{memory_id}" in snapshot.markdown


@pytest.mark.asyncio
async def test_settings_expose_model_context_window():
    async with AsyncSessionLocal() as db:
        settings = await read_settings(db)
    assert settings["model_context_window"] > 0
    assert settings["context_token_budget"] > 0
    assert settings["database_file"].endswith("learning_companion.db")
    assert settings["data_counts"]["plans"] >= 0
