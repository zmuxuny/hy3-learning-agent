"""Discriminating offline probes for S06 response handling and failed-turn replay."""

import copy
import json
from types import SimpleNamespace

import pytest
from app.core.config import settings
from app.core.prompt_envelope import request_budget_breakdown
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    Operation,
    OutboxAction,
    RunEvent,
    Session,
    ToolInvocation,
)
from app.runtime.agent import AgentRuntime, _model_request_messages
from app.runtime.checkpoints import make_checkpoint
from sqlalchemy import select


class Call:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def response(calls=None, *, finish_reason="stop", content="已完成", tokens=7):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=calls, reasoning_content=None),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=tokens),
    )


async def start_run(monkeypatch, responses, *, checkpoint=None):
    async def no_enrichment(*_args):
        pass

    monkeypatch.setattr(AgentRuntime, "_after_terminal", no_enrichment)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="S06 offline response probe")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="Offline tool recovery",
            budget_usage=(checkpoint or {}).get("budget_usage"),
            checkpoint_schema_version=1,
            checkpoint=checkpoint or make_checkpoint(
                kind="agent",
                phase="awaiting_model",
                step=0,
                messages=[{"role": "user", "content": "Write the authorized file."}],
            ),
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    requests = []

    class Completions:
        async def create(self, **request):
            requests.append(copy.deepcopy(request))
            return responses.pop(0)

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    await runtime.run(run_id)
    return run_id, requests


def chunk(*, calls=None, content=None, reasoning=None, finish_reason=None, usage=None, delta=True):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=calls) if delta else None,
            finish_reason=finish_reason,
        )],
        usage=usage,
    )


def tool_delta(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_interleaved_stream_preserves_arguments_finish_and_choice_usage(monkeypatch):
    raw = json.dumps({"path": "sample.json", "content": '中文\\路径 "quoted" {}'}, ensure_ascii=False)
    emitted = []
    monkeypatch.setattr("app.runtime.agent.publish_stream_event", lambda _run, event: emitted.append(event))
    usage = SimpleNamespace(prompt_tokens=13, completion_tokens=21)

    async def stream():
        yield chunk(reasoning="synthetic private reasoning")
        yield chunk(calls=[tool_delta(1, call_id="second", name="file_", arguments=raw[:4])])
        yield chunk(calls=[tool_delta(0, call_id="first", name="planning_intake_get", arguments="{")])
        yield chunk(calls=[tool_delta(1, name="write", arguments=raw[4:19])])
        yield chunk(calls=[tool_delta(0, arguments="}")])
        yield chunk(calls=[tool_delta(1, arguments=raw[19:])])
        # Some compatible providers attach usage to the final choice, rather
        # than sending an additional empty-choices usage frame.
        yield chunk(finish_reason="length", usage=usage, delta=False)

    message, recorded_usage = await AgentRuntime()._drain_stream(stream(), SimpleNamespace(id="stream-probe"), 0)
    assert [c.id for c in message.tool_calls] == ["first", "second"]
    assert message.tool_calls[0].function.arguments == "{}"
    assert message.tool_calls[1].function.name == "file_write"
    assert message.tool_calls[1].function.arguments == raw
    assert message.finish_reason == "length"
    assert recorded_usage is usage
    assert message.reasoning_content == "synthetic private reasoning"
    assert "synthetic private reasoning" not in json.dumps(emitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_prefix", [True, False])
@pytest.mark.parametrize("streamed", [True, False])
async def test_truncated_response_never_executes_even_valid_json_then_recovers(monkeypatch, valid_prefix, streamed):
    raw = json.dumps({"path": "must-not-write.txt", "content": "rejected output"})
    if not valid_prefix:
        raw = raw[:-1]
    async def first_stream():
        yield chunk(calls=[tool_delta(0, call_id="rejected", name="file_write", arguments=raw[:12])])
        yield chunk(calls=[tool_delta(0, arguments=raw[12:])])
        yield chunk(finish_reason="length", usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4096))

    first_response = first_stream() if streamed else response(
        [Call("rejected", "file_write", raw)], finish_reason="length", tokens=4096,
    )
    run_id, requests = await start_run(monkeypatch, [
        first_response,
        response(content="上次输出未执行，请分步继续。"),
    ])
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "completed"
        assert run.budget_usage["model_calls"] == 2
        assert run.budget_usage["completion_tokens"] == 4103
        assert not (await db.execute(select(Operation).where(Operation.run_id == run_id))).scalars().all()
        assert not (await db.execute(select(ToolInvocation).where(ToolInvocation.run_id == run_id))).scalars().all()
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        assert sum(e.event_type == "model.response_rejected" for e in events) == 1
        rejected = next(e for e in events if e.event_type == "model.response_rejected")
        assert rejected.payload["tool_calls"][0]["raw_arguments"] == raw
        assert run.checkpoint is None  # the rejection event survives terminal cleanup
        assert not any(e.event_type == "operation.committed" for e in events)
    assert len(requests) == 2
    assert not any(m.get("tool_calls") for m in requests[1]["messages"])
    assert "[Runtime recovery]" in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_repeated_output_truncation_is_bounded_and_usage_survives(monkeypatch):
    # Reaching the step limit on the second rejection must remain a failure,
    # rather than accidentally finalizing the run as a successful budget stop.
    monkeypatch.setattr(settings, "AGENT_MAX_STEPS", 2)
    run_id, requests = await start_run(monkeypatch, [
        response([Call("rejected-1", "file_write", '{"path":')], finish_reason="length"),
        response([Call("rejected-2", "file_write", '{"path":')], finish_reason="length"),
    ])
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.status_reason == "model_output_truncated"
        assert run.budget_usage["model_calls"] == 2
        assert run.budget_usage["completion_tokens"] == 14
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        assert sum(e.event_type == "model.response_rejected" for e in events) == 2
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_setting", "expected_blocker"),
    [("AGENT_MAX_STEPS", "step_limit"), ("AGENT_MAX_MODEL_CALLS", "model_call_limit")],
)
async def test_truncation_on_last_allowed_response_is_failed(monkeypatch, limit_setting, expected_blocker):
    monkeypatch.setattr(settings, limit_setting, 1)
    run_id, requests = await start_run(monkeypatch, [
        response([Call("rejected-last", "file_write", '{"path":')], finish_reason="length"),
    ])
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.status_reason == "model_output_truncated"
        assert run.budget_usage["model_calls"] == 1
        assert run.budget_usage["completion_tokens"] == 7
        assert run.budget_usage["stopped_reason"] == expected_blocker
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        rejected = next(e for e in events if e.event_type == "model.response_rejected")
        assert rejected.payload["repair_blocker"] == expected_blocker
        assert not any(e.event_type == "run.completed" for e in events)
        assert not (await db.execute(select(Operation).where(Operation.run_id == run_id))).scalars().all()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_restart_cannot_bypass_truncation_repair_limit(monkeypatch):
    checkpoint = make_checkpoint(
        kind="agent", phase="awaiting_model", step=2,
        messages=[{"role": "assistant", "content": "partial", "_runtime_response_rejection": "length"}] * 2,
        budget_usage={"model_calls": 2, "prompt_tokens": 22, "completion_tokens": 14},
    )
    run_id, requests = await start_run(monkeypatch, [], checkpoint=checkpoint)
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.status_reason == "model_output_truncated"
        assert run.budget_usage["model_calls"] == 2
    assert requests == []


@pytest.mark.asyncio
async def test_valid_long_arguments_execute_and_replay_unchanged(monkeypatch):
    content = "valid long content\n" * 1500
    raw = json.dumps({"path": "valid-long.txt", "content": content})
    run_id, requests = await start_run(monkeypatch, [
        # Token usage equalling the limit is not itself proof of truncation.
        response([Call("valid-long", "file_write", raw)], finish_reason="tool_calls", tokens=settings.AGENT_OUTPUT_TOKEN_RESERVE),
        response(),
    ])
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "completed"
        operations = (await db.execute(select(Operation).where(Operation.run_id == run_id))).scalars().all()
        assert len(operations) == 1
        action = (await db.execute(select(OutboxAction).where(OutboxAction.run_id == run_id))).scalars().one()
        assert action.payload["content"] == content
    calls = next(m["tool_calls"] for m in requests[1]["messages"] if m.get("tool_calls"))
    assert calls[0]["function"]["arguments"] == raw


@pytest.mark.asyncio
async def test_bad_json_can_be_corrected_into_one_valid_write(monkeypatch):
    malformed = '{"path":"must-not-write.txt","content":"' + "x" * 150000
    corrected = json.dumps({"path": "corrected.txt", "content": "complete authorized content"})
    run_id, requests = await start_run(monkeypatch, [
        response([Call("bad", "file_write", malformed)], finish_reason="tool_calls"),
        response([Call("corrected", "file_write", corrected)], finish_reason="tool_calls"),
        response(),
    ])
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "completed"
        operations = (await db.execute(select(Operation).where(Operation.run_id == run_id))).scalars().all()
        assert len(operations) == 1
        assert operations[0].entity_id == "corrected.txt"
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        failure = next(e for e in events if e.event_type == "model.arguments_rejected")
        assert failure.payload["raw_arguments"] == malformed
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_large_valid_history_still_reports_local_context_limit(monkeypatch):
    raw = json.dumps({"path": "large-valid.txt", "content": "x" * 95000})
    run_id, requests = await start_run(monkeypatch, [
        response([Call("large-valid", "file_write", raw)], finish_reason="tool_calls"),
    ])
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.status_reason == "context_window_exceeded"
        operations = (await db.execute(select(Operation).where(Operation.run_id == run_id))).scalars().all()
        assert len(operations) == 1
        action = (await db.execute(select(OutboxAction).where(OutboxAction.run_id == run_id))).scalars().one()
        assert action.payload["content"] == "x" * 95000
    assert len(requests) == 1


def test_projection_keeps_valid_rounds_and_only_removes_rejected_reasoning():
    malformed = '{"content":"' + "x" * 150000
    valid = json.dumps({"content": "y" * 30000})
    messages = [
        {"role": "assistant", "content": "failed", "reasoning_content": "failed reasoning" * 3000,
         "tool_calls": [Call("invalid", "file_write", malformed).model_dump()]},
        {"role": "tool", "tool_call_id": "invalid", "content": '{"ok":false,"error_code":"invalid_arguments"}'},
        {"role": "assistant", "content": "valid schema error", "reasoning_content": "valid round reasoning",
         "tool_calls": [Call("schema-invalid", "file_write", valid).model_dump()]},
        {"role": "tool", "tool_call_id": "schema-invalid", "content": '{"ok":false,"error_code":"invalid_arguments"}'},
    ]
    before = copy.deepcopy(messages)
    projected, compacted = _model_request_messages(messages)
    assert messages == before
    assert len(compacted) == 1
    assert "reasoning_content" not in projected[0]
    assert projected[2] == messages[2]
    original_breakdown = request_budget_breakdown(messages=messages, tools=[])
    replay_breakdown = request_budget_breakdown(messages=projected, tools=[])
    assert original_breakdown["total_tokens"] > settings.MODEL_CONTEXT_WINDOW
    assert replay_breakdown["total_tokens"] < settings.MODEL_CONTEXT_WINDOW
    assert original_breakdown["prompt_components"]["tool_arguments_tokens"] > 180000
    assert original_breakdown["prompt_components"]["reasoning_tokens"] > 40000
    assert sum(original_breakdown["prompt_components"].values()) == original_breakdown["prompt_tokens"]


def test_mixed_tool_round_keeps_reasoning_and_successful_arguments():
    messages = [
        {"role": "assistant", "content": "mixed", "reasoning_content": "keep valid reasoning",
         "tool_calls": [Call("bad", "file_write", '{"path":').model_dump(),
                        Call("good", "planning_intake_get", "{}").model_dump()]},
        {"role": "tool", "tool_call_id": "bad", "content": '{"ok":false,"error_code":"invalid_arguments"}'},
        {"role": "tool", "tool_call_id": "good", "content": '{"ok":true}'},
    ]
    projected, compacted = _model_request_messages(messages)
    assert len(compacted) == 1
    assert projected[0]["reasoning_content"] == messages[0]["reasoning_content"]
    assert projected[0]["tool_calls"][1] == messages[0]["tool_calls"][1]
