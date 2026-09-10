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
