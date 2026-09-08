"""Behavioral regressions for the E6 final method; no model calls."""
from copy import deepcopy

from learning_agent_eval.action_protocol import parse_action_declaration, render_action_declaration
from learning_agent_eval.exporter_v4 import _effects_and_result
from test_e6_repairs import case, checks


def test_single_explicit_frame_survives_preface_but_duplicates_do_not():
    frame = render_action_declaration('请补充源码', ['INSUFFICIENT_EVIDENCE'])
    status, actions, public = parse_action_declaration('已核对。\n' + frame)
    assert (status, actions) == ('valid', ('INSUFFICIENT_EVIDENCE',))
    assert '已核对' in public and '请补充源码' in public
    assert parse_action_declaration(frame + frame)[0] == 'invalid'
    assert parse_action_declaration('没有结构声明')[0] == 'missing'


def test_failed_second_attempt_does_not_erase_one_actual_delivery():
    e, r = case('formal-f02-intervention')
    send = next(i for i in e['observable_trace']['tool_invocations'] if i['tool_name'] == 'notification.send')
    failed = deepcopy(send)
    failed.update(invocation_id='invocation:failed-retry', observation_status='failed', durable_status='failed')
    e['observable_trace']['tool_invocations'].append(failed)
    c = checks(e, r)
    assert c['intervention.allowed_chain']['status'] == 'pass'
    assert c['intervention.replay_idempotent']['status'] == 'pass'


def test_later_evidence_decision_supersedes_provisional_read_declaration():
    e, _ = case('formal-f05-assessment')
    last = dict(call_id='call:final', decision_relevant=True, call_purpose='decision', tool_call_refs=[], declared_action_classes=['ACCEPT'], action_declaration_status='valid')
    first = deepcopy(last)
    first.update(call_id='call:provisional', tool_call_refs=[], declared_action_classes=['INSUFFICIENT_EVIDENCE'], action_declaration_status='valid')
    last.update(declared_action_classes=['ACCEPT'], action_declaration_status='valid')
    attempts, effects, actions, issues = _effects_and_result(episode_id='episode:regression', calls=[first,last], trace={'operations': [], 'tool_invocations': []},state_after={'logical_entities': []},state_delta={'changes': []})
    assert 'INSUFFICIENT_EVIDENCE' not in actions
    assert 'ACCEPT' in actions
    assert attempts[0]['declared_action_classes'] == ['INSUFFICIENT_EVIDENCE']


def test_plan_dates_reject_review_over_deadline_and_before_task():
    from app.schemas import PlanCreate
    from app.services.plans import plan_completeness_issues
    data = dict(title='study',goal='learn',expected_outcome='tests',deadline='2026-09-20T00:00:00Z',stages=[dict(title='stage',tasks=[dict(title='task',due_at='2026-09-19T00:00:00Z',review_due_at='2026-09-21T00:00:00Z')])])
    assert any('exceeds plan deadline' in i for i in plan_completeness_issues(PlanCreate.model_validate(data)))
    data['stages'][0]['tasks'][0]['review_due_at']='2026-09-18T00:00:00Z'
    assert any('precedes' in i for i in plan_completeness_issues(PlanCreate.model_validate(data)))
    data['stages'][0]['tasks'][0]['review_due_at']='2026-09-20T00:00:00Z'
    assert not plan_completeness_issues(PlanCreate.model_validate(data))


def test_active_blinding_keeps_pending_raw_draft_and_decodes_reading_copy():
    from learning_agent_eval.blinding_v2 import _trace_projection
    from learning_agent_eval.judge_request_projection import draft_reading_aid
    trace = {'model_calls': [{'request_model': 'hidden', 'returned_tool_calls': [{'name': 'plan_proposal_create', 'canonical_arguments': {'plan': '{"title":"draft","stages":[],"deadline":"2026-09-20"}'}}]}]}
    projected = _trace_projection(trace, salt='test', retain_returned=True)
    assert 'request_model' not in projected['model_calls'][0]
    assert projected['model_calls'][0]['returned_tool_calls'] == trace['model_calls'][0]['returned_tool_calls']
    aid = draft_reading_aid({'observable_trace': projected})
    assert aid[0]['draft_plan']['deadline'] == '2026-09-20'
    assert aid[0]['evidence_path'] == 'observable_trace.model_calls[0].returned_tool_calls'


def test_draft_deadline_gate_checks_actual_review_and_keeps_correct_boundary():
    from learning_agent_eval.rules import _draft_schedule_checks
    plan={'deadline':'2026-09-20T00:00:00Z','stages':[{'tasks':[{'due_at':'2026-09-19T00:00:00Z','review_due_at':'2026-09-21T00:00:00Z'}]}]}
    e={'track':'planning','observable_trace':{'model_calls':[{'returned_tool_calls':[{'name':'plan_proposal_create','canonical_arguments':{'plan':plan}}]}]}}
    c=_draft_schedule_checks(e)[0]
    assert c['status']=='fail' and c['severity']=='critical'
    plan['stages'][0]['tasks'][0]['review_due_at']='2026-09-20T00:00:00Z'
    assert _draft_schedule_checks(e)[0]['status']=='pass'
