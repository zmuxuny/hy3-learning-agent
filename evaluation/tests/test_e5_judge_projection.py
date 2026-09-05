from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.judge_request_projection import (
    pack_shared_values,
    unpack_shared_values,
)


def test_repeated_schema_and_message_values_round_trip_without_evidence_loss():
    schema = {"description": "学习材料" * 900, "properties": {"title": {"type": "string"}}}
    message = {"role": "system", "content": "Long public instruction. " * 500}
    original = {"calls": [{"tools": [schema], "messages": [message, {"content": str(i)}]} for i in range(4)]}
    packed = pack_shared_values(original)
    assert unpack_shared_values(packed) == original
    assert len(canonical_json_bytes(packed)) < len(canonical_json_bytes(original)) / 2
    assert pack_shared_values(original) == packed


def test_encoding_markers_in_business_data_are_escaped_and_preserved():
    original = {"literal": {"$shared": "v1"}, "another": {"$literal": [["business", "value"]]}, "long": ["x" * 1000] * 2}
    assert unpack_shared_values(pack_shared_values(original)) == original
