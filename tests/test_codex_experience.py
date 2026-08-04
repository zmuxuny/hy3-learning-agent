import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.agent import (
    _start_runtime,
    decide_run_approval,
    delete_queued_message,
    enqueue_message,
    list_queue,
    send_queued_message,
    steer_run,
    update_queued_message,
)
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage, QueuedMessage, RunEvent, RunSteerMessage, Session
from app.runtime.agent import AgentRuntime
from app.runtime.events import subscribe_stream, unsubscribe_stream
from app.schemas import (
    QueuedMessageCreate,
    QueuedMessageUpdate,
    RunApprovalRequest,
    RunSteerCreate,
)


def stream_chunk(content=None, reasoning=None, tool_calls=None, usage=None):
    delta = {
        "content": content,
        "reasoning_content": reasoning,
        "tool_calls": tool_calls,
    }
    if usage is None:
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(**delta))],
            usage=None,
        )
    return SimpleNamespace(choices=None, usage=usage)


def tool_call_chunk(call_id, name, arguments):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            reasoning_content=None,
            tool_calls=[SimpleNamespace(
                index=0,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )],
        ))],
        usage=None,
    )


class StreamingCompletions:
    """First call streams a tool call, second call streams a final answer."""

    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append({
            **kwargs,
            "messages": [dict(message) for message in kwargs["messages"]],
        })

        async def first_stream():
            yield tool_call_chunk("call-1", "planning_intake_get", "{}")
            yield stream_chunk(usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5))

        async def second_stream():
            for piece in ["完成", "了", "流式验证"]:
                yield stream_chunk(content=piece)
            yield stream_chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=9))

        return first_stream() if len(self.calls) == 1 else second_stream()


@pytest.mark.asyncio
async def test_streaming_emits_deltas_and_persists_final_message():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="流式测试")
        db.add(run)
        await db.commit()
        run_id = run.id

    deltas = []
    event_queue = await subscribe_stream(run_id)

    async def collect():
        while True:
            deltas.append(await event_queue.get())

    collector = asyncio.create_task(collect())
    try:
        runtime = AgentRuntime()
        runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=StreamingCompletions()))
        await runtime.run(run_id)
        await asyncio.sleep(0.05)
    finally:
        collector.cancel()
        unsubscribe_stream(run_id, event_queue)

    async with AsyncSessionLocal() as db:
        completed = await db.get(AgentRun, run_id)
        assert completed.status == "completed"
        assert completed.output == "完成了流式验证"
        assert completed.budget_usage["prompt_tokens"] == 30
        assert completed.budget_usage["completion_tokens"] == 14
        message = (await db.execute(
            select(ChatMessage).where(ChatMessage.run_id == run_id, ChatMessage.role == "assistant")
        )).scalars().one()
        assert message.content == "完成了流式验证"

    delta_payloads = [item["payload"] for item in deltas if item["type"] == "assistant.delta"]
    assert delta_payloads
    assert delta_payloads[-1]["text"] == "完成了流式验证"


@pytest.mark.asyncio
async def test_steer_injects_message_into_running_run(monkeypatch):
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="转向测试会话")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="读取计划后教我",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async def noop_start(_run_id, **_kwargs):
        return None

    monkeypatch.setattr("app.api.agent._start_runtime", noop_start)

    async with AsyncSessionLocal() as db:
        steered = await steer_run(run_id, RunSteerCreate(content="先不要读计划，先讲概念"), db)
        assert steered.status == "queued"

    completions = StreamingCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    runtime_calls = [call for call in completions.calls if "tools" in call]
    assert len(runtime_calls) == 2
    second_messages = runtime_calls[1]["messages"]
    steer_messages = [m for m in second_messages if m["role"] == "user" and str(m.get("content", "")).startswith("[中途转向]")]
    assert steer_messages and steer_messages[0]["content"].endswith("先不要读计划，先讲概念")

    async with AsyncSessionLocal() as db:
        applied = (await db.execute(
            select(RunSteerMessage).where(RunSteerMessage.run_id == run_id)
        )).scalars().all()
        assert applied and applied[0].applied_at is not None
        steer_events = list((await db.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "steer.received")
        )).scalars())
        assert steer_events
        chat = (await db.execute(
            select(ChatMessage).where(ChatMessage.run_id == run_id, ChatMessage.role == "user")
        )).scalars().all()
        assert any(item.message_metadata.get("ui_kind") == "steer" for item in chat)


@pytest.mark.asyncio
async def test_queue_crud_and_send(monkeypatch):
    monkeypatch.setattr("app.api.agent._start_runtime", lambda _run_id, **_kwargs: None)

    async with AsyncSessionLocal() as db:
        first = await enqueue_message(QueuedMessageCreate(objective="第一条排队"), db)
        second = await enqueue_message(QueuedMessageCreate(objective="第二条排队"), db)
        assert first.position == 0
        assert second.position == 1

        rows = await list_queue(db=db)
        assert [row.objective for row in rows] == ["第一条排队", "第二条排队"]

        moved = await update_queued_message(first.id, QueuedMessageUpdate(position=1), db)
        assert moved.position == 1
        rows = await list_queue(db=db)
        assert [row.objective for row in rows] == ["第二条排队", "第一条排队"]

        edited = await update_queued_message(second.id, QueuedMessageUpdate(objective="改过的第二条"), db)
        assert edited.objective == "改过的第二条"

        await delete_queued_message(first.id, db)
        rows = await list_queue(db=db)
        assert [row.objective for row in rows] == ["改过的第二条"]

        sent = await send_queued_message(second.id, db)
        assert sent.objective == "改过的第二条"
        remaining = (await db.execute(select(QueuedMessage))).scalars().all()
        assert remaining == []


class AnswerApprovalCompletions:
    def __init__(self):
        self.calls = []
        self.final_text = "明白了，我会先讲概念。"

    async def create(self, **kwargs):
        self.calls.append({
            **kwargs,
            "messages": [dict(message) for message in kwargs["messages"]],
        })

        async def first_stream():
            yield tool_call_chunk("call-plan", "plan_create", json.dumps({
                "title": "审批回答计划",
                "goal": "验证回答",
                "current_level": "初级",
                "weekly_minutes": 300,
                "expected_outcome": "可运行",
                "stages": [{"title": "阶段一", "tasks": [{"title": "任务一"}]}],
            }, ensure_ascii=False))
            yield stream_chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4))

        async def second_stream():
            yield stream_chunk(content=self.final_text)
            yield stream_chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=8))

        return first_stream() if len(self.calls) == 1 else second_stream()


@pytest.mark.asyncio
async def test_approval_answer_is_fed_back_to_model(monkeypatch):
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="heartbeat", objective="先问再执行")
        db.add(run)
        await db.commit()
        run_id = run.id

    async def noop_start(_run_id, **_kwargs):
        return None

    monkeypatch.setattr("app.api.agent._start_runtime", noop_start)

    completions = AnswerApprovalCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        paused = await db.get(AgentRun, run_id)
        assert paused.status == "waiting_approval"

    async with AsyncSessionLocal() as db:
        await decide_run_approval(
            run_id,
            RunApprovalRequest(approved=False, answer="先别建计划，我想先看大纲"),
            db,
        )

    await runtime.run(run_id, resume=True, approval_decision="reject")

    assert len(completions.calls) == 2
    second_messages = completions.calls[1]["messages"]
    tool_result = second_messages[-1]
    assert tool_result["role"] == "tool"
    payload = json.loads(tool_result["content"])
    assert payload["approval"] == "answered"
    assert payload["answer"] == "先别建计划，我想先看大纲"
