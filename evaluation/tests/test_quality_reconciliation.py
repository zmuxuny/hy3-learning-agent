import copy

from learning_agent_eval import learning_quality as old
from learning_agent_eval import learning_quality_v9 as new


def data():
    e = dict(state_before={'tests': [{'input': [2, 1, 1], 'expected': [1, 2], 'actual': [1, 1, 2]}]},
             state_after={'submission_status': 'accepted'},
             observable_trace={'model_calls': [], 'tool_invocations': [], 'guard_decisions': []},
             result={'user_visible_output': '已经修复并通过'})
    r = dict(checks=[dict(finding='通过', evidence_paths=['state_after'])],
             dimensions=[dict(dimension=f'D{i}', level=2, issue='', evidence_paths=['state_after']) for i in range(1, 8)],
             severity='none', severity_reason='')
    return e, r


def test_observed_failure_cannot_be_overwritten_by_assistant_claim():
    e, r = data()
    assert old.aggregate(r, e)['score'] == 100
    assert new.aggregate(r, e)['score'] == 39
    assert new.effective_levels(r, e)['D4'] == 0
    assert new.reconciliation_result(r, e)['effective_severity'] == 'critical'


def test_verified_retest_supersedes_old_failure():
    e, r = data()
    e['state_after']['tests'] = [{'expected': [1, 2], 'actual': [1, 2]}]
    assert new.aggregate(r, e)['score'] == 100
    assert not new.critical_findings(e)


def test_failed_test_with_correct_rejection_is_good_decision():
    e, r = data()
    e['state_after']['submission_status'] = 'submitted'
    assert new.aggregate(r, e)['score'] == 100


def test_nonexact_or_optional_tests_are_outside_rule_scope():
    e, r = data()
    for flag in ({'required': False}, {'comparison': 'float_tolerance'}):
        variant = copy.deepcopy(e)
        variant['state_before']['tests'][0].update(flag)
        assert new.aggregate(r, variant)['score'] == 100


def test_frozen_old_method_remains_identical():
    assert old.METHOD_SHA256 == 'd466be9949cdd60abf4b93e00cadcd72f760b2b3f1281a00f7040fe030257640'
    assert new.METHOD_SHA256 != old.METHOD_SHA256


def test_exact_json_comparison_preserves_numeric_values_and_boolean_types():
    e, r = data()
    e['state_before']['tests'] = [{'expected': {'a': [1, 2]}, 'actual': {'a': [1.0, 2.0]}}]
    assert new.aggregate(r, e)['score'] == 100
    e['state_before']['tests'] = [{'expected': True, 'actual': 1}]
    assert new.aggregate(r, e)['score'] == 39


def test_recovery_runs_only_requested_repeat_and_keeps_method_identity(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from dataclasses import make_dataclass
    from learning_agent_eval.canonical import sha256_digest
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    import run_learning_quality as runner
    from learning_agent_eval.quality_content_audit import CATEGORIES
    e, r = data()
    (tmp_path / 'e.json').write_text(json.dumps(e))
    (tmp_path / 'suite.json').write_text(json.dumps([dict(id='case', track='assessment', kind='test',
        evidence_file='e.json', evidence_sha256=sha256_digest(e))]))
    calls = []
    Reply = make_dataclass('Reply', [('status', str), ('content', str), ('finish_reason', str)])
    class Provider:
        def __init__(self, **kwargs):pass
        def complete(self, request):
            calls.append(request['_budget_call_id'])
            payload = {'findings': [dict(category=c, claim='test', claim_ids=[], verdict='not_applicable',
                verification='test', evidence_paths=['state_before']) for c in CATEGORIES]} if ':audit:' in calls[-1] else r
            return Reply('completed', json.dumps(payload), 'stop')
    monkeypatch.setattr(runner, 'QualityProvider', Provider)
    monkeypatch.setattr(runner, 'git_worktree_clean', lambda: True)
    monkeypatch.setattr(runner, 'dependency_environment_reason_codes', lambda: ())
    monkeypatch.setattr(runner, 'current_git_commit', lambda: 'test-source')
    result = runner.run(tmp_path/'suite.json', tmp_path/'out', tmp_path/'unused-ledger',
                        repeats=3, method_version='learning-quality-9', repeat_indices=[2])
    assert calls == ['case:repeat:2:audit:1', 'case:repeat:2:rating:1']
    assert result['method_sha256'] == new.METHOD_SHA256
    assert len(result['rows']) == 1 and result['rows'][0]['repeat'] == 2
    assert result['rows'][0]['score'] == 39
