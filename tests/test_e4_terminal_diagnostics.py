"""The original E4 envelope counterexample now recovers without replaying bad JSON."""

from types import SimpleNamespace

import pytest
from app.core.config import settings
from app.core.prompt_envelope import PromptEnvelopeExceeded, ensure_request_fits
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, RunEvent, Session
from app.runtime.agent import AgentRuntime
from app.runtime.checkpoints import make_checkpoint
from sqlalchemy import select


class Call:
    def __init__(self, name, raw, i):
        self.id = f"malformed-{i}"
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=raw)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_bytes", [10, 150000])
async def test_malformed_arguments_do_not_hide_context_limit(
    monkeypatch, payload_bytes
):
    monkeypatch.setattr(settings, "AGENT_RUN_MAX_RETRIES", 0)
    monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 8)
    async def no_enrichment(*args):
        pass

    monkeypatch.setattr(AgentRuntime, "_after_terminal", no_enrichment)

    class Completions:
        def __init__(self):
            self.calls = 0
            self.requests = []
            self.checkpoint_messages = []

        async def create(self, **request):
            self.calls += 1
            self.requests.append(request)
            if self.calls == 2:
                async with AsyncSessionLocal() as db:
                    stored = await db.get(AgentRun, run_id)
                    self.checkpoint_messages = stored.checkpoint["messages"]
            calls = (
                [
                    Call(
                        "planning_intake_update", '{"goal":"' + "x" * payload_bytes, 1
                    ),
                    Call("plan_proposal_create", '{"plan":', 2),
                ]
                if self.calls == 1
                else None
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="Invalid input is retained."
                            if calls
                            else "Please clarify.",
                            tool_calls=calls,
                            reasoning_content=None,
                        )
                    )
                ],
                usage=None,
            )

    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Offline terminal probe")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="Offline terminal probe",
            checkpoint_schema_version=1,
            checkpoint=make_checkpoint(
                kind="agent",
                phase="awaiting_model",
                step=0,
                messages=[{"role": "user", "content": "Inspect malformed input."}],
                budget_usage={"model_calls": 0, "tool_calls": 0, "elapsed_ms": 0},
            ),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    client = Completions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=client))
    await runtime.run(run_id)
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        events = (
            (
                await db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        assert run.status == "completed"
        assert run.status_reason is None
        assert client.calls == 2
        original = client.checkpoint_messages
        if payload_bytes == 150000:
            # This is the historical mechanism's independent counterexample:
            # the durable original still exceeds the conservative envelope.
            with pytest.raises(PromptEnvelopeExceeded):
                ensure_request_fits(messages=original, tools=client.requests[1]["tools"])
        replayed = client.requests[1]["messages"]
        ensure_request_fits(messages=replayed, tools=client.requests[1]["tools"])
        replayed_calls = next(m["tool_calls"] for m in replayed if m.get("tool_calls"))
        assert all("_runtime_rejected_arguments" in c["function"]["arguments"] for c in replayed_calls)
        original_calls = next(m["tool_calls"] for m in original if m.get("tool_calls"))
        assert original_calls[0]["function"]["arguments"] == '{"goal":"' + "x" * payload_bytes
        failures = [e for e in events if e.event_type == "model.arguments_rejected"]
        assert len(failures) == 2
        assert failures[0].payload["raw_arguments"] == original_calls[0]["function"]["arguments"]
        errors = [
            e
            for e in events
            if e.event_type == "tool.completed"
            and (e.payload or {}).get("result", {}).get("error_code")
            == "invalid_arguments"
        ]
        assert len(errors) == 2
        assert not any(e.event_type == "operation.committed" for e in events)
