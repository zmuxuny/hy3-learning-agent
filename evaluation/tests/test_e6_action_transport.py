"""Explicit model-owned action transport; no action inference or approval bypass."""
import json
from types import SimpleNamespace

import pytest
from learning_agent_eval.action_protocol import (
    TOOL_ACTION_FIELD,
    inspection_only_response,
    parse_response_action_declaration,
    render_action_declaration,
)
from learning_agent_eval.recorder import EvaluationModelRecorder
from learning_agent_eval.scripted_model import ScriptedModelClient


def test_action_arguments_require_valid_explicit_declarations_and_agreement():
    call = {"name": "plan_patch", "canonical_arguments": {"plan_id": 1}}
    assert parse_response_action_declaration("", [call])[0] == "missing"
    call["canonical_arguments"][TOOL_ACTION_FIELD] = ["APPLY_REVERSIBLE_PATCH"]
    assert parse_response_action_declaration("", [call])[:2] == ("valid", ("APPLY_REVERSIBLE_PATCH",))
    assert parse_response_action_declaration(render_action_declaration("", ["NO_OP"]), [call])[0] == "invalid"
    for invalid in [[], ["made_up"], "APPLY_REVERSIBLE_PATCH", ["WAIT", "WAIT"]]:
        call["canonical_arguments"][TOOL_ACTION_FIELD] = invalid
        assert parse_response_action_declaration("", [call])[0] == "invalid"
    assert inspection_only_response("", [{"name": "memory_search"}])
    assert not inspection_only_response("", [{"name": "memory_write"}])


@pytest.mark.asyncio
async def test_native_argument_is_recorded_then_removed_before_product_tool():
    args = {"plan_id": 1, "weekly_minutes": 85, TOOL_ACTION_FIELD: ["APPLY_REVERSIBLE_PATCH"]}
    client = ScriptedModelClient([{"delivery": "nonstream", "assistant_text": "", "tool_calls": [
        {"call_id": "one", "name": "plan_patch", "arguments": args},
    ]}])
    recorder = EvaluationModelRecorder(client, invocation_mode="stub", action_tool_transport=True,
                                      metadata_provider=lambda: SimpleNamespace(depth=0, call_purpose="decision"))
    response = await recorder.chat.completions.create(model="hy3", messages=[], stream=True, tools=[
        {"type": "function", "function": {"name": "plan_patch", "parameters": {"type": "object", "properties": {}, "required": []}}},
    ])
    record = recorder.records[0]
    assert record["function_calls"][0]["canonical_arguments"] == args
    assert json.loads(response.choices[0].message.tool_calls[0].function.arguments) == {"plan_id": 1, "weekly_minutes": 85}
    schema = record["visible_tool_schemas"][0]["function"]["parameters"]
    assert TOOL_ACTION_FIELD in schema["required"]
    assert record["request_config"]["stream"] is False
