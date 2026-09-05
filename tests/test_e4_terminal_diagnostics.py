"""Real Runtime replay distinguishes bad tool JSON from a local context limit."""

from types import SimpleNamespace

import pytest
from app.core.config import settings
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
    observed = []
    orig = AgentRuntime._fail

    async def fail(self, db, run, lease, exc):
        observed.append(
            {"type": type(exc).__name__, "breakdown": getattr(exc, "breakdown", None)}
        )
        return await orig(self, db, run, lease, exc)

    monkeypatch.setattr(AgentRuntime, "_fail", fail)

    async def no_enrichment(*args):
        pass

    monkeypatch.setattr(AgentRuntime, "_after_terminal", no_enrichment)

    class Completions:
        def __init__(self):
            self.calls = 0

        async def create(self, **request):
            self.calls += 1
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
        if payload_bytes == 10:
            assert observed == []
        else:
            assert len(observed) == 1
            assert observed[0]["type"] == "PromptEnvelopeExceeded"
            assert observed[0]["breakdown"]["total_tokens"] > settings.MODEL_CONTEXT_WINDOW
        assert run.status == ("completed" if payload_bytes == 10 else "failed")
        assert run.status_reason == (
            None if payload_bytes == 10 else "context_window_exceeded"
        )
        assert client.calls == (2 if payload_bytes == 10 else 1)
        errors = [
            e
            for e in events
            if e.event_type == "tool.completed"
            and (e.payload or {}).get("result", {}).get("error_code")
            == "invalid_arguments"
        ]
        assert len(errors) == 2
        assert not any(e.event_type == "operation.committed" for e in events)
