"""Configured service routing, offline attribution and request-local pricing."""
import json
from io import BytesIO
from pathlib import Path

import pytest

from learning_agent_eval.active_judge import OpenAICompatibleHy3JudgeProviderV3
from learning_agent_eval.e31_runtime import build_real_provider_attestation
from learning_agent_eval.isolation import worker_environment
from learning_agent_eval.model_budget import ModelBudget
from learning_agent_eval.models import ProviderAttestationV1
from learning_agent_eval.provider_config import ProviderEndpoint
from learning_agent_eval.rubric import JUDGE_CONFIG_SHA256_V3


@pytest.mark.parametrize('base', ['http://example.test/v1', 'https://user:secret@example.test/v1', 'https://example.test/v1?key=hidden', 'https://example.test/v1#fragment'])
def test_provider_rejects_credentials_or_ambiguous_transport(base):
    with pytest.raises(ValueError, match='credential-free HTTPS'):
        ProviderEndpoint(base)


def test_worker_and_judge_share_custom_base_and_record_actual_request(tmp_path, monkeypatch):
    base = 'https://relay.example.test/custom/v1'
    monkeypatch.setenv('OPENAI_API_BASE', base + '/')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-local-key')
    env = worker_environment(project_root=tmp_path, worker_root=tmp_path/'worker', model_mode='real', allow_real_model=True)
    assert env['OPENAI_API_BASE'] == base
    assert env['DATABASE_URL'].endswith('/worker/data/learning_companion.db')
    ledger = tmp_path/'budget.json'
    ModelBudget.create(ledger, limit_micro_cny=1_000_000)
    seen = []
    def transport(request, timeout):
        seen.append(request.full_url)
        return BytesIO(json.dumps({'id':'public-call', 'model':'hy3',
            'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':20,'completion_tokens':2,'total_tokens':22}}).encode())
    monkeypatch.setattr('urllib.request.urlopen', transport)
    provider = OpenAICompatibleHy3JudgeProviderV3(budget_ledger=ledger)
    result = provider.complete({'messages':[], 'response_format':{'type':'json_object'}})
    assert seen == [base+'/chat/completions']
    assert result.status == 'completed'
    assert json.loads(ledger.read_text())['requests'][0]['charged_micro_cny'] == 28


def test_real_attribution_is_replayable_after_endpoint_configuration_changes(monkeypatch):
    monkeypatch.setenv('OPENAI_API_BASE','https://relay.example.test/custom/v1')
    records = [{'call_id':'call-1','request_model':'hy3','response_model':'hy3',
        'provider_request_id':'public-1','requested_at':'2026-09-08T12:00:00Z',
        'responded_at':'2026-09-08T12:00:01Z','response_status':'completed'}]
    d = build_real_provider_attestation(records, scope='semantic_judge',
        configuration_sha256=JUDGE_CONFIG_SHA256_V3, git_commit='a'*40,
        worktree_clean=True, dependency_lock_verified=True)
    assert d['provider_id'] == 'openai-compatible'
    assert d['api_base'] == 'https://relay.example.test/custom/v1'
    assert d['endpoint_origin'] == 'https://relay.example.test'
    monkeypatch.setenv('OPENAI_API_BASE','https://another.example.test/v1')
    assert ProviderAttestationV1.model_validate(d).attribution_status == 'eligible'


def test_reservation_freezes_decimal_rates_before_ledger_rate_change(tmp_path):
    path=tmp_path/'budget.json';budget=ModelBudget.create(path,limit_micro_cny=1_000_000)
    d=json.loads(path.read_text());d.update(input_rate='0.25',output_rate='0.75',pricing_basis='synthetic-rate');path.write_text(json.dumps(d))
    ticket=budget.reserve(scope='test',call_id='test',input_limit=101,output_limit=3)
    d=json.loads(path.read_text());assert d['requests'][0]['charged_micro_cny']==28
    d.update(input_rate=99,output_rate=99);path.write_text(json.dumps(d))
    budget.settle(ticket,{'prompt_tokens':100,'completion_tokens':2,'total_tokens':102})
    row=json.loads(path.read_text())['requests'][0]
    assert row['charged_micro_cny']==27 and row['pricing_basis']=='synthetic-rate'


def test_real_cli_loads_shared_env_only_in_parent(tmp_path, monkeypatch):
    import learning_agent_eval.cli as cli
    import learning_agent_eval.runtime_metadata as metadata
    monkeypatch.setattr(metadata,'PROJECT_ROOT',tmp_path)
    (tmp_path/'manifest.json').write_text('{}')
    (tmp_path/'.env').write_text('OPENAI_API_BASE=https://relay.example.test/v1\nOPENAI_API_KEY=synthetic-local-key\n')
    monkeypatch.setenv('OPENAI_API_BASE','placeholder')
    monkeypatch.setenv('OPENAI_API_KEY','placeholder')
    monkeypatch.delenv('OPENAI_API_BASE')
    monkeypatch.delenv('OPENAI_API_KEY')
    def stop_after_configuration(**kwargs):
        import os
        assert os.environ['OPENAI_API_BASE']=='https://relay.example.test/v1'
        assert os.environ['OPENAI_API_KEY']=='synthetic-local-key'
        raise RuntimeError('configuration_observed')
    monkeypatch.setattr(cli,'run_active_runtime',stop_after_configuration)
    monkeypatch.setattr(cli,'_reject_historical_version',lambda *a,**k:None)
    with pytest.raises(RuntimeError,match='configuration_observed'):
        cli.main(['run-agent','--dataset',str(tmp_path),'--manifest',str(tmp_path/'manifest.json'),'--output',str(tmp_path/'out'),'--model-mode','real','--allow-real-model','--budget-ledger',str(tmp_path/'budget.json')])
