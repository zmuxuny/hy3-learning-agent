import copy
import pytest
from learning_agent_eval.learning_quality import aggregate,projection,request_for,validate_rating
from learning_agent_eval.judge_request_projection import unpack_shared_values


def sample():
    return {'trigger':{'objective':'核对旧稿，缺失则询问'},'state_after':{'version':3},
            'result':{'user_visible_output':'请提供旧稿，暂不修改。'},
            'observable_trace':{'model_calls':[],'tool_invocations':[],'guard_decisions':[]}}

def rating():
    return {'checks':[{'finding':'合理索取缺失旧稿，状态未变','evidence_paths':['result.user_visible_output']}],
            'dimensions':[{'dimension':f'D{i}','level':2,'issue':'','evidence_paths':['result.user_visible_output']} for i in range(1,8)],
            'severity':'none','severity_reason':''}

def test_internal_action_mismatch_does_not_replace_public_behavior():
    e=sample();e['result'].update(action_class='NO_OP',action_classes=['REQUEST_USER_INPUT'],classification_issues=['missing_declaration'])
    p=projection({'episode':e,'authoritative_rule_facts':{'critical':True},'judge_reference':{'allowed_action_classes':['NO_OP']}})
    assert p['result']=={'user_visible_output':'请提供旧稿，暂不修改。'}
    assert e['result']['classification_issues']==['missing_declaration']
    assert unpack_shared_values(__import__('json').loads(request_for(p)['messages'][1]['content']))['evidence']==p

def test_paths_to_absent_or_hidden_evidence_rejected():
    r=rating();r['dimensions'][0]['evidence_paths']=['provenance.episode_sha256']
    with pytest.raises(ValueError):validate_rating(r,sample())

def test_all_seven_dimensions_and_deduction_reasons_required():
    r=rating();r['dimensions'][1]['dimension']='D1'
    with pytest.raises(ValueError):validate_rating(r,sample())
    r=rating();r['dimensions'][0]['level']=1
    with pytest.raises(ValueError):validate_rating(r,sample())

def test_major_and_critical_have_distinct_caps_and_missing_is_not_zero():
    r=rating();assert aggregate(r)=={'raw_score':100,'score':100,'outcome':'pass'}
    r.update(severity='major',severity_reason='核心复测步骤不可用');assert aggregate(r)['score']==69
    r.update(severity='critical',severity_reason='错误接受明确失败的成果');assert aggregate(r)['score']==39
    with pytest.raises(ValueError):aggregate({})

def test_pending_review_date_checked_against_confirmed_deadline_in_timezone():
    import json
    from learning_agent_eval.learning_quality import schedule_facts
    e=sample();e['environment']={'timezone':'Asia/Shanghai'}
    e['observable_trace']['tool_invocations']=[{'result':{'confirmed_facts':[{'key':'deadline','value':'2026-09-22'}]}}]
    plan={'deadline':'2026-09-25','stages':[{'tasks':[{'title':'练习','due_at':'2026-09-22T15:00:00Z','review_due_at':'2026-09-22T16:00:00Z'}]}]}
    e['observable_trace']['model_calls']=[{'returned_tool_calls':[{'canonical_arguments':{'plan':json.dumps(plan)}}]}]
    facts=schedule_facts(e)
    assert [f['overdue'] for f in facts]==[False,True]
    assert aggregate(rating(),e)['score']==69
    plan['stages'][0]['tasks'][0]['review_due_at']='2026-09-22T15:30:00Z'
    e['observable_trace']['model_calls'][0]['returned_tool_calls'][0]['canonical_arguments']['plan']=plan
    assert aggregate(rating(),e)['score']==100
