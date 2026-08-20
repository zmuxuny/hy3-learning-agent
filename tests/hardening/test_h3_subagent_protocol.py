"""H3 child budget and final-report recovery contracts."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.runtime.subagents as subagent_runtime
import app.tools as tool_package
from app.core.config import settings
from app.core.time import utc_now
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, RunEvent
from app.runtime.checkpoints import make_checkpoint
from app.runtime.state import reconcile_run_after_restart
from app.runtime.tasks import start_tracked_task


class ProcessCrash(BaseException):
    pass


class FakeFunction:
    def __init__(self, name: str, arguments: str = "{}") -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str = "{}") -> None:
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


def model_response(
    content: str,
    *,
    tool_calls: list[FakeToolCall] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content,
            reasoning_content=None,
            tool_calls=tool_calls,
        ))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


class SequenceCompletions:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected extra model call")
        return self.responses.pop(0)


def fake_client(completions: SequenceCompletions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def child_stub(*, age_seconds: int = 0):
    started_at = utc_now() - timedelta(seconds=age_seconds)
    return SimpleNamespace(
        id="budget-child",
        owner_id="local",
        plan_id=None,
        session_id=None,
        started_at=started_at,
        created_at=started_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_reason", "budget", "current_call", "age_seconds", "setting", "limit"),
    (
        ("model_call_limit", {"model_calls": 1}, None, 0, "AGENT_MAX_MODEL_CALLS", 1),
        (
            "tool_call_limit",
            {"tool_calls": 1},
            {"id": "blocked-tool", "name": "profile_get", "arguments": "{}"},
            0,
            "AGENT_MAX_TOOL_CALLS",
            1,
        ),
        ("elapsed_limit", {}, None, 31, "AGENT_MAX_ELAPSED_SECONDS", 30),
        (
            "cost_limit",
            {"estimated_cost_usd": 0.5},
            None,
            0,
            "AGENT_MAX_ESTIMATED_COST_USD",
            0.5,
        ),
    ),
)
async def test_child_enforces_every_shared_budget_limit_before_external_work(
    monkeypatch,
    expected_reason: str,
    budget: dict,
    current_call: dict | None,
    age_seconds: int,
    setting: str,
    limit: int | float,
) -> None:
    monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 12)
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 32)
    monkeypatch.setattr(settings, "AGENT_MAX_ELAPSED_SECONDS", 600)
    monkeypatch.setattr(settings, "AGENT_MAX_ESTIMATED_COST_USD", 0.0)
    monkeypatch.setattr(settings, setting, limit)
    events: list[tuple[str, dict]] = []

    async def capture_event(_run_id, event_type, _summary, payload=None, **_kwargs):
        events.append((event_type, payload or {}))

    monkeypatch.setattr(subagent_runtime, "child_event", capture_event)
    checkpoints: list[dict] = []

    async def save(checkpoint: dict) -> None:
        checkpoints.append(copy.deepcopy(checkpoint))

    class ForbiddenCompletions:
        async def create(self, **_kwargs):
            raise AssertionError("budget-exhausted child called the model")

    report = await subagent_runtime.run_restricted_child(
        client=SimpleNamespace(chat=SimpleNamespace(completions=ForbiddenCompletions())),
        child=child_stub(age_seconds=age_seconds),
        objective="stop at the durable budget boundary",
        context="synthetic context",
        allowlist={"profile_get"} if current_call else set(),
        max_steps=3,
        checkpoint=make_checkpoint(
            kind="subagent",
            phase="tool_ready" if current_call else "awaiting_model",
            step=0,
            messages=[{"role": "user", "content": "budget fixture"}],
            current_tool_call=current_call,
            budget_usage=budget,
        ),
        checkpoint_callback=save,
    )

    assert report == "子 Agent 运行预算已用尽，已安全停止。"
    assert checkpoints[-1]["phase"] == "finalizing"
    assert checkpoints[-1]["final_text"] == report
    assert checkpoints[-1]["budget_usage"]["stopped_reason"] == expected_reason
    assert len(events) == 1
    assert events[0][0] == "run.budget_exceeded"
    assert events[0][1]["reason"] == expected_reason
    assert events[0][1]["budget_usage"]["stopped_reason"] == expected_reason


@pytest.mark.asyncio
async def test_child_synthesis_persists_usage_cost_and_network_budget(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MODEL_INPUT_PRICE_PER_1M", 1.0)
    monkeypatch.setattr(settings, "MODEL_OUTPUT_PRICE_PER_1M", 2.0)

    async def ignore_event(*_args, **_kwargs):
        return None

    async def execute_read(*_args, **_kwargs):
        return {"ok": True, "data": {"items": []}}

    monkeypatch.setattr(subagent_runtime, "child_event", ignore_event)
    monkeypatch.setattr(tool_package, "execute_tool", execute_read)
    completions = SequenceCompletions(
        model_response(
            "先检索。",
            tool_calls=[FakeToolCall("web-call", "web_search")],
            prompt_tokens=10,
            completion_tokens=5,
        ),
        model_response(
            "最终综合报告。",
            prompt_tokens=20,
            completion_tokens=7,
        ),
    )
    checkpoints: list[dict] = []

    async def save(checkpoint: dict) -> None:
        checkpoints.append(copy.deepcopy(checkpoint))

    report = await subagent_runtime.run_restricted_child(
        client=fake_client(completions),
        child=child_stub(),
        objective="collect and synthesize",
        context="synthetic context",
        allowlist={"web_search"},
        max_steps=1,
        checkpoint_callback=save,
    )

    budget = checkpoints[-1]["budget_usage"]
    assert report == "最终综合报告。"
    assert checkpoints[-1]["phase"] == "finalizing"
    assert checkpoints[-1]["final_text"] == report
    assert budget["model_calls"] == 2
    assert budget["tool_calls"] == 1
    assert budget["network_requests"] == 1
    assert budget["prompt_tokens"] == 30
    assert budget["completion_tokens"] == 12
    assert budget["estimated_cost_usd"] == pytest.approx(0.000054)


@pytest.mark.asyncio
async def test_child_runtime_intersects_checkpoint_allowlist_with_read_only_tools(
    monkeypatch,
) -> None:
    events: list[tuple[str, dict]] = []

    async def capture_event(_run_id, event_type, _summary, payload=None, **_kwargs):
        events.append((event_type, payload or {}))

    async def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("write tool reached the child execution boundary")

    monkeypatch.setattr(subagent_runtime, "child_event", capture_event)
    monkeypatch.setattr(tool_package, "execute_tool", forbidden_execute)
    completions = SequenceCompletions(
        model_response(
            "attempt a write",
            tool_calls=[FakeToolCall("write-call", "plan_patch")],
        ),
        model_response("write was rejected; returning a read-only report"),
    )

    report = await subagent_runtime.run_restricted_child(
        client=fake_client(completions),
        child=child_stub(),
        objective="prove the runtime guard",
        context="checkpoint supplied a write tool",
        allowlist={"plan_patch"},
        max_steps=1,
    )

    advertised_names = {
        schema["function"]["name"] for schema in completions.calls[0]["tools"]
    }
    tool_result = next(payload for event_type, payload in events if event_type == "tool.completed")
    assert report == "write was rejected; returning a read-only report"
    assert "plan_patch" not in advertised_names
    assert tool_result["result"]["ok"] is False
    assert "read-only allowlist" in tool_result["result"]["error"]


@pytest.mark.asyncio
async def test_child_finalizing_kill_recovers_without_second_model_call(monkeypatch) -> None:
    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="parent waits for durable report",
            status="running",
        )
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="[audit] freeze one report",
            status="queued",
            checkpoint_schema_version=1,
            checkpoint=make_checkpoint(
                kind="subagent",
                phase="awaiting_model",
                step=0,
                messages=[],
                identity={
                    "role": "audit",
                    "objective": "freeze one report",
                    "context": "synthetic context",
                    "allowlist": [],
                    "max_steps": 1,
                },
            ),
        )
        db.add(child)
        await db.commit()
        parent_id = parent.id
        child_id = child.id

    completions = SequenceCompletions(model_response("first durable report"))
    real_finalize_child = subagent_runtime.finalize_child

    async def crash_before_terminal_commit(*_args, **_kwargs):
        raise ProcessCrash("kill after finalizing checkpoint")

    monkeypatch.setattr(subagent_runtime, "finalize_child", crash_before_terminal_commit)
    with pytest.raises(ProcessCrash):
        await subagent_runtime.execute_durable_child(
            child_id,
            role="audit",
            objective="freeze one report",
            context="synthetic context",
            allowlist=set(),
            max_steps=1,
            client_factory=lambda: fake_client(completions),
        )

    async with AsyncSessionLocal() as db:
        interrupted = await db.get(AgentRun, child_id)
        assert interrupted.status == "running"
        assert interrupted.phase == "finalizing"
        assert interrupted.checkpoint["final_text"] == "first durable report"
        assert interrupted.budget_usage["model_calls"] == 1

    assert await reconcile_run_after_restart(
        AsyncSessionLocal,
        child_id,
        scope_valid=True,
    ) is True
    monkeypatch.setattr(subagent_runtime, "finalize_child", real_finalize_child)
    constructed_clients: list[bool] = []

    def forbidden_client_factory():
        constructed_clients.append(True)
        raise AssertionError("finalizing recovery constructed a model client")

    await subagent_runtime.execute_durable_child(
        child_id,
        role="audit",
        objective="freeze one report",
        context="synthetic context",
        allowlist=set(),
        max_steps=1,
        client_factory=forbidden_client_factory,
    )

    async with AsyncSessionLocal() as db:
        recovered = await db.get(AgentRun, child_id)
        started_events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == child_id,
            RunEvent.event_type == "run.started",
        ))).scalars())
        parent_events = list((await db.execute(select(RunEvent).where(
            RunEvent.run_id == parent_id,
            RunEvent.event_type == "subagent.completed",
        ))).scalars())
    assert constructed_clients == []
    assert completions.calls and len(completions.calls) == 1
    assert recovered.status == "completed"
    assert recovered.output == "first durable report"
    assert recovered.checkpoint is None
    assert recovered.budget_usage["model_calls"] == 1
    assert len(started_events) == 1
    assert len(parent_events) == 1
    assert parent_events[0].payload["report"] == "first durable report"


@pytest.mark.asyncio
async def test_parent_cancel_waits_for_child_worker_cleanup() -> None:
    async with AsyncSessionLocal() as db:
        parent = AgentRun(
            owner_id="local",
            trigger="user_message",
            objective="cancel every child and wait for cleanup",
            status="running",
        )
        db.add(parent)
        await db.flush()
        child = AgentRun(
            owner_id="local",
            parent_run_id=parent.id,
            trigger="subagent",
            objective="child with observable cancellation cleanup",
            status="queued",
        )
        db.add(child)
        await db.commit()
        parent_id = parent.id
        child_id = child.id

    worker_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def child_worker() -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    start_tracked_task(child_id, child_worker())
    await asyncio.wait_for(worker_started.wait(), timeout=3)
    cancel_task = asyncio.create_task(subagent_runtime.cancel_children_for_parent(
        parent_id,
        "parent reached a terminal state",
    ))
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=3)
        await asyncio.sleep(0)
        assert cancel_task.done() is False
    finally:
        release_cleanup.set()

    assert await asyncio.wait_for(cancel_task, timeout=3) == 1
    async with AsyncSessionLocal() as db:
        cancelled = await db.get(AgentRun, child_id)
    assert cancelled.status == "cancelled"
