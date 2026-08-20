from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.context import ContextAssembler
from app.core.config import settings
from app.core.prompt_envelope import (
    TOKEN_ESTIMATOR_VERSION,
    PromptEnvelopeExceeded,
    envelope_inputs,
    ensure_request_fits,
    estimate_context_message_tokens,
    estimate_json_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    request_budget_breakdown,
    snapshot_budget_breakdown,
)
from app.db.database import AsyncSessionLocal
from app.models import LearningEvent
from app.runtime.agent import AgentRuntime, ToolFailureGuard
from app.runtime.session_titles import generate_session_title
from app.runtime.subagents import run_restricted_child


@pytest.mark.parametrize(
    "value",
    [
        "a" * 20_000,
        "'.\\/[]{}:," * 2_000,
        "学习上下文" * 2_000,
        "😀🧑🏽‍💻🚀" * 2_000,
        "\x00\n\r\t" * 2_000,
    ],
)
def test_utf8_estimator_is_a_versioned_byte_upper_bound(value: str) -> None:
    assert TOKEN_ESTIMATOR_VERSION == "h5-utf8-byte-framed-upper-bound-v1"
    assert estimate_text_tokens(value) >= len(value.encode("utf-8"))


def test_json_messages_and_tool_schemas_include_serialized_framing() -> None:
    message = {
        "role": "assistant",
        "content": "emoji 😀 and adversarial JSON \\\"{}[]",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "probe", "arguments": '{"value":"\\ud83d\\ude00"}'},
            }
        ],
    }
    schema = {
        "type": "function",
        "function": {
            "name": "probe",
            "description": "x" * 4096,
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
    }

    assert estimate_messages_tokens([message]) > estimate_text_tokens(message["content"])
    assert estimate_json_tokens([schema]) >= len(
        __import__("json").dumps(
            [schema], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


@pytest.mark.asyncio
async def test_root_runtime_rejects_oversized_envelope_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class _Completions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("provider must not be called for an oversized request")

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 4_096)
    monkeypatch.setattr(settings, "AGENT_OUTPUT_TOKEN_RESERVE", 256)
    monkeypatch.setattr(settings, "AGENT_TOOL_RESULT_TOKEN_RESERVE", 256)

    async with AsyncSessionLocal() as db:
        with pytest.raises(PromptEnvelopeExceeded):
            await runtime._call_model(
                db,
                SimpleNamespace(id="oversized-run"),
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "x" * 20_000},
                ],
                ToolFailureGuard(settings.AGENT_TOOL_FAILURE_LIMIT),
                0,
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_session_title_rejects_oversized_envelope_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class _Completions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("provider must not be called for an oversized request")

    objective = "oversized " + ("😀\\\"" * 2_000)
    session = SimpleNamespace(title=objective[:80])
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 4_096)
    monkeypatch.setattr(settings, "AGENT_OUTPUT_TOKEN_RESERVE", 256)
    monkeypatch.setattr(settings, "AGENT_TOOL_RESULT_TOKEN_RESERVE", 256)

    changed = await generate_session_title(
        session,
        objective=objective,
        answer="answer" * 1_000,
        client=client,
    )

    assert changed is False
    assert calls == 0


@pytest.mark.asyncio
async def test_child_and_planning_runtime_reject_oversized_envelope_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class _Completions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("provider must not be called for an oversized child request")

    now = datetime.now(timezone.utc)
    child = SimpleNamespace(
        id="oversized-child",
        owner_id="local",
        plan_id=None,
        session_id=None,
        execution_mode="read_only",
        reply_to_intervention_id=None,
        started_at=now,
        created_at=now,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 4_096)
    monkeypatch.setattr(settings, "AGENT_OUTPUT_TOKEN_RESERVE", 256)
    monkeypatch.setattr(settings, "AGENT_TOOL_RESULT_TOKEN_RESERVE", 256)

    with pytest.raises(PromptEnvelopeExceeded):
        await run_restricted_child(
            client=client,
            child=child,
            objective="planning: inspect the supplied context",
            context="😀\\\"{}[]" * 2_000,
            allowlist=set(),
            max_steps=1,
        )

    assert calls == 0


def test_direct_preflight_counts_tool_schema_and_both_reserves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 12_000)
    monkeypatch.setattr(settings, "AGENT_OUTPUT_TOKEN_RESERVE", 1_000)
    monkeypatch.setattr(settings, "AGENT_TOOL_RESULT_TOKEN_RESERVE", 2_000)
    breakdown = ensure_request_fits(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "probe",
                    "description": "schema" * 100,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert breakdown["tool_schema_tokens"] > 1
    assert breakdown["output_reserve_tokens"] == 1_000
    assert breakdown["tool_result_reserve_tokens"] == 2_000
    assert breakdown["total_tokens"] <= breakdown["model_context_window"]


def test_snapshot_budget_matches_complete_serialized_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 100_000)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_TOKEN_BUDGET", 50_000)
    monkeypatch.setattr(settings, "AGENT_OUTPUT_TOKEN_RESERVE", 1_000)
    monkeypatch.setattr(settings, "AGENT_TOOL_RESULT_TOKEN_RESERVE", 2_000)
    system = 'system with "quotes" and emoji 😀'
    prefix = "Trigger: user_message\nObjective: JSON \\\"quoted\\\"\\path\n\n"
    markdown = "# Agent Context\n- value: \\\\ \\\" 😀\n"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "quoted \\\"schema\\\" 😀",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    inputs = envelope_inputs(system_prompt=system, tools=tools)
    context_tokens = estimate_context_message_tokens(
        system_prompt=system,
        user_content=prefix + markdown,
    )
    snapshot = snapshot_budget_breakdown(inputs, context_tokens=context_tokens)
    request = request_budget_breakdown(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prefix + markdown},
        ],
        tools=tools,
    )

    assert snapshot["total_tokens"] == request["total_tokens"]
    assert snapshot["system_prompt_tokens"] + snapshot["context_tokens"] == request[
        "prompt_tokens"
    ]


@pytest.mark.asyncio
async def test_persisted_snapshot_budget_covers_the_actual_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MODEL_CONTEXT_WINDOW", 100_000)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_TOKEN_BUDGET", 50_000)
    monkeypatch.setattr(settings, "AGENT_OUTPUT_TOKEN_RESERVE", 1_000)
    monkeypatch.setattr(settings, "AGENT_TOOL_RESULT_TOKEN_RESERVE", 2_000)
    system = 'system with "quotes" and emoji 😀'
    prefix = "Trigger: user_message\nObjective: JSON \\\"quoted\\\"\\path\n\n"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "quoted \\\"schema\\\" 😀",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    async with AsyncSessionLocal() as db:
        db.add(
            LearningEvent(
                owner_id="local",
                event_type="prompt.adversarial",
                summary='line one\\line two\n"quoted" 😀',
            )
        )
        await db.commit()
        snapshot = await ContextAssembler(db).build(
            "local",
            objective="adversarial",
            prompt_system=system,
            prompt_tools=tools,
            prompt_prefix=prefix,
        )
        actual = request_budget_breakdown(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prefix + snapshot.markdown},
            ],
            tools=tools,
        )
        await db.commit()

    assert "prompt.adversarial" in snapshot.markdown
    assert snapshot.budget_breakdown["total_tokens"] == actual["total_tokens"]
    assert snapshot.estimated_tokens == snapshot.budget_breakdown["context_tokens"]
