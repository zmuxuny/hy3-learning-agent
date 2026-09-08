"""Post-formal input regressions: lossless encoding and typed local rejection."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from learning_agent_eval.active_judge import (
    JudgeInputLimitExceeded,
    OpenAICompatibleHy3JudgeProviderV3,
    build_provider_request_v3,
    evaluate_active_judges,
)
from learning_agent_eval.blinding_v2 import BlindJudgeInputV3
from learning_agent_eval.canonical import canonical_json_bytes
from learning_agent_eval.judge_request_projection import pack_shared_values, unpack_shared_values
from learning_agent_eval.validator import validate_dataset
from test_e5_judge_failures import judge_inputs  # Reuse one isolated, scripted Runtime fixture.


def test_medium_repeated_public_values_are_shared_without_losing_literals():
    repeated = {'description': 'x' * 300, 'example': {'$shared': 'literal-user-value'}}
    original = {'calls': [repeated] * 8, 'literal': {'$literal': [['x', 'y']]}}
    packed = pack_shared_values(original)
    assert unpack_shared_values(packed) == original
    assert len(canonical_json_bytes(packed)) < len(canonical_json_bytes(original)) / 2


def test_shared_schema_preserves_exact_legal_evidence_path_set():
    episode = {'result': {'action_class': 'WAIT'}, 'observable_trace': {
        'model_calls': [{'assistant_text': 'wait', 'returned_tool_calls': []}],
        'tool_invocations': [], 'guard_decisions': [],
    }}
    request = build_provider_request_v3(BlindJudgeInputV3(judge_id='test', document={'episode': episode}, sha256='a' * 64))
    schema = request['response_format']['json_schema']['schema']
    offered = json.loads(request['messages'][1]['content'])['evidence_path_catalog']
    assert schema['$defs']['EpisodeEvidencePath'] == {'type': 'string', 'enum': offered}
    assert set(offered) == {'result.action_class', 'observable_trace.model_calls[0].assistant_text',
                            'observable_trace.model_calls[0].returned_tool_calls'}
    for definition in schema['$defs'].values():
        paths = definition.get('properties', {}).get('evidence_paths')
        if paths:
            assert paths['items'] == {'$ref': '#/$defs/EpisodeEvidencePath'}
    assert unpack_shared_values(json.loads(request['messages'][1]['content'])) == {'episode': episode}


def test_oversize_request_is_rejected_before_budget_reservation_or_http(monkeypatch):
    provider = object.__new__(OpenAICompatibleHy3JudgeProviderV3)
    provider.calls = 0
    provider.budget = SimpleNamespace(reserve=Mock(side_effect=AssertionError('must not reserve')))
    network = Mock(side_effect=AssertionError('must not call network'))
    monkeypatch.setattr('urllib.request.urlopen', network)
    with pytest.raises(JudgeInputLimitExceeded, match='judge_input_limit_exceeded'):
        provider.complete({'messages': [{'role': 'user', 'content': 'x' * 196608}], 'response_format': {}})
    assert provider.calls == 0
    provider.budget.reserve.assert_not_called()
    network.assert_not_called()


def test_input_rejection_keeps_distinct_unscored_artifact_without_paid_repair(tmp_path, judge_inputs):
    provider = SimpleNamespace(mode='real', complete=Mock(side_effect=JudgeInputLimitExceeded('judge_input_limit_exceeded')))
    output = tmp_path / 'judges'
    evaluate_active_judges(episodes=judge_inputs['runtime'], rules=judge_inputs['rules'], output=output,
                          judge_mode='real', allow_real_judge=True, provider=provider)
    result = json.loads((output / 'judge-results' / f"{judge_inputs['episode_id']}.json").read_text())
    attempts = [json.loads(line) for line in (tmp_path / 'judges-attempts.jsonl').read_text().splitlines()]
    assert result['status'] == 'judge_error' and result['error_code'] == 'judge_input_limit_exceeded'
    assert result['dimensions'] == [] and provider.complete.call_count == len(attempts) == 1
    assert attempts[0]['budget_ticket'] is None and attempts[0]['provider_attempted'] is False
    assert attempts[0]['validation_error_codes'] == ['judge_input_limit_exceeded']
    assert validate_dataset(output).ok
