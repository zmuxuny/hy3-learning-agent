"""Offline counterexamples from the independent E6 review."""
from copy import deepcopy
import pytest
from learning_agent_eval.active_judge import _validate_payload_v3, _parse_response
from learning_agent_eval.blinding_v2 import BlindJudgeInputV3
from learning_agent_eval.active_worker import _recorded_failure_reason
from learning_agent_eval.rules import _draft_schedule_checks


def payload():
    return dict(audit_checks=[dict(claim='Output is present',verification='Read public output',consistent=True,evidence_paths=['result.user_visible_output'])],
                dimensions=[dict(dimension_id=f'D{i}',level=2,reason_code='verified',public_summary='Public evidence checked',specific_issue=None,evidence_paths=['result.user_visible_output']) for i in range(1,8)],semantic_issues=[],suggested_hard_gates=[])


@pytest.mark.parametrize('mutation,error',[('missing','response_schema_invalid'),('empty','response_schema_invalid'),('nonexistent','response_evidence_invalid'),('hidden','response_evidence_invalid'),('valid',None)])
def test_active_fact_checks_are_required_and_visible(mutation,error):
    e={'result':{'user_visible_output':'done','private_note':'hidden'}};blind=BlindJudgeInputV3('judge',{'episode':{'result':{'user_visible_output':'done'}}},'0'*64);p=payload()
    if mutation=='missing':p.pop('audit_checks')
    elif mutation=='empty':p['audit_checks']=[]
    elif mutation in ['nonexistent','hidden']:p['audit_checks'][0]['evidence_paths']=['result.nonexistent_fact' if mutation=='nonexistent' else 'result.private_note']
    result,errors=_validate_payload_v3(p,episode=e,blind_input=blind)
    assert errors==((error,) if error else ())
    assert (result is not None)==(error is None)


def test_historical_response_still_accepts_missing_fact_checks():
    p=payload();p.pop('audit_checks');assert _parse_response(p)['audit_checks']==[]


def plan():
    return dict(title='study',goal='learn',expected_outcome='tests',deadline='2026-10-20T00:00:00Z',stages=[dict(title='stage',tasks=[dict(title='task',due_at='2026-10-18T00:00:00Z',review_due_at='2026-10-19T00:00:00Z')])])


def episode(p):
    return {'track':'planning','state_before':{'logical_entities':[{'entity_type':'planning_intake','data':{'session_ref':'session:current','confirmed_facts':[{'key':'deadline','value':'2026-09-20','source':'user'}]}},{'entity_type':'learner','data':{'timezone':'Asia/Shanghai'}},{'entity_type':'session','logical_id':'session:current','data':{}},{'entity_type':'agent_run','logical_id':'agent_run:current','data':{'session_ref':'session:current'}}]},'observable_trace':{'model_calls':[{'run_id':'agent_run:current','returned_tool_calls':[{'name':'plan_proposal_create','canonical_arguments':{'plan':p}}]}]}}


def test_draft_cannot_move_its_own_deadline_past_confirmed_constraint():
    from app.schemas import PlanCreate
    from app.services.plans import confirmed_deadline_issues, plan_completeness_issues
    p=plan();e=episode(p);data=PlanCreate.model_validate(p)
    assert not plan_completeness_issues(data)  # Internally consistent, externally wrong.
    assert confirmed_deadline_issues(data,e['state_before']['logical_entities'][0]['data']['confirmed_facts'],timezone='Asia/Shanghai')
    c=_draft_schedule_checks(e)[0];assert c['status']=='fail'
    assert 'state_before.logical_entities[0].data.confirmed_facts' in c['evidence_paths']
    p['deadline']='2026-09-20T23:59:00+08:00';p['stages'][0]['tasks'][0].update(due_at='2026-09-19T00:00:00+08:00',review_due_at='2026-09-20T23:59:00+08:00')
    assert _draft_schedule_checks(e)[0]['status']=='pass'
    assert not confirmed_deadline_issues(PlanCreate.model_validate(p),e['state_before']['logical_entities'][0]['data']['confirmed_facts'],timezone='Asia/Shanghai')
    p['stages'][0]['tasks'][0]['review_due_at']='2026-09-20T16:01:00Z'
    assert _draft_schedule_checks(e)[0]['status']=='fail'  # Next local day.


@pytest.mark.parametrize('record',[{'record_error':'response_projection_rejected','response_status':'framework_error'},{'response_validation_errors':[{'code':'response_projection_rejected'}],'response_status':'framework_error'}])
def test_recorder_projection_reason_precedes_generic_provider_terminal(record):
    stage,code,summary=_recorded_failure_reason({'status_reason':'model_provider_error'},[record])
    assert (stage,code)==('runtime','runtime.response_projection_rejected')
    assert 'specific cause' in summary
    assert _recorded_failure_reason({},[{'response_status':'provider_error'}])[:2]==('provider','provider.runtime_call_failed')


def test_only_successful_intake_observation_updates_deadline_before_draft():
    p=plan();e=episode(p)
    e['observable_trace']['model_calls'][0]['returned_tool_calls'].insert(0, {'name':'planning_intake_update','call_id':'update','canonical_arguments':{}})
    i={'tool_call_id':'update','tool_name':'planning.intake.update','observation_status':'pending_approval','result':{'session_ref':'session:current','confirmed_facts':[{'key':'deadline','value':'2026-10-20','source':'user'}]}}
    e['observable_trace']['tool_invocations']=[i]
    assert _draft_schedule_checks(e)[0]['status']=='fail'
    i['observation_status']='succeeded'
    assert _draft_schedule_checks(e)[0]['status']=='pass'
