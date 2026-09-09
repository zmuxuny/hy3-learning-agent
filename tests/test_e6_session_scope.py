"""Use real Collector snapshots to exercise multi-session deadline attribution."""
from copy import deepcopy
import pytest
from app.db.database import AsyncSessionLocal
from app.models import Session, AgentRun, PlanningIntake
from learning_agent_eval.normalizers import StableIdentityRegistry
from learning_agent_eval.snapshots import collect_state_snapshot
from learning_agent_eval.rules import _draft_schedule_checks


@pytest.fixture
async def scoped_episode(tmp_path):
    async with AsyncSessionLocal() as db:
        db.add_all([Session(id='current-session',owner_id='local',title='Current'),Session(id='other-session',owner_id='local',title='Other')])
        await db.flush()
        db.add(AgentRun(id='current-run',owner_id='local',session_id='current-session',trigger='user_message',objective='Plan within deadline'))
        await db.flush()
        db.add(AgentRun(id='other-run',parent_run_id='current-run',owner_id='local',session_id='other-session',trigger='subagent',objective='Separate session'))
        for session_id,deadline in [('current-session','2026-09-20'),('other-session','2026-09-10')]:
            db.add(PlanningIntake(session_id=session_id,owner_id='local',goal='Learn',confirmed_facts=[{'key':'deadline','value':deadline,'source':'user'}],open_questions=[],readiness='ready',readiness_confidence=1,rationale='Synthetic confirmed requirement'))
        await db.commit()
    fixture={'episode_id':'session-scope-probe','owner_id':'local','run_id':'current-run','state_before':{'logical_entities':[],'context':{'public_summary':'Synthetic scope probe','source_refs':[],'context_sha256':'0'*64}}}
    registry=StableIdentityRegistry(episode_id='session-scope-probe')
    capture=await collect_state_snapshot(AsyncSessionLocal,fixture,registry=registry,captured_at='2026-09-09T02:00:00Z',resource_version='scope-probe',resource_digest='a'*64,phase='before')
    sessions={name:registry.resolve('session',name+'-session') for name in ['current','other']}
    runs={name:registry.resolve('agent_run',name+'-run') for name in ['current','other']}
    plan={'deadline':'2026-09-20T00:00:00+08:00','stages':[{'tasks':[{'due_at':'2026-09-18T00:00:00+08:00','review_due_at':'2026-09-19T00:00:00+08:00'}]}]}
    e={'schema_version':'decision-episode-v4','track':'planning','state_before':capture.document,'state_after':deepcopy(capture.document),'observable_trace':{'model_calls':[{'run_id':runs['current'],'returned_tool_calls':[{'name':'plan_proposal_create','call_id':'draft','canonical_arguments':{'plan':plan}}]}],'tool_invocations':[]}}
    assert len([x for x in e['state_before']['logical_entities'] if x['entity_type']=='planning_intake'])==2
    import json
    (tmp_path/'collector-scope-episode.json').write_text(json.dumps(e,ensure_ascii=False,indent=2))
    return e,sessions,runs


def set_deadline(e,session,value):
    next(x for x in e['state_before']['logical_entities'] if x['entity_type']=='planning_intake' and x['data']['session_ref']==session)['data']['confirmed_facts'][0]['value']=value


@pytest.mark.parametrize('current,other,expected',[('2026-09-20','2026-09-10','pass'),('2026-09-10','2026-09-30','fail')])
async def test_other_session_cannot_cause_false_positive_or_false_negative(scoped_episode,current,other,expected):
    e,sessions,_=scoped_episode;set_deadline(e,sessions['current'],current);set_deadline(e,sessions['other'],other)
    for state in ['state_before','state_after']:
        assert _draft_schedule_checks(e)[0]['status']==expected, _draft_schedule_checks(e)[0]
        e[state]['logical_entities'].reverse()
        assert _draft_schedule_checks(e)[0]['status']==expected, _draft_schedule_checks(e)[0]
    check=_draft_schedule_checks(e)[0]
    for path in check['evidence_paths']:
        if path.endswith('.data.confirmed_facts'):
            index=int(path.split('logical_entities[')[1].split(']')[0])
            assert e['state_before']['logical_entities'][index]['data']['session_ref']==sessions['current']


@pytest.mark.parametrize('caller,result_session,expected',[('other','other','fail'),('current','other','invalid_input'),('current','current','pass')])
async def test_prior_successful_observation_requires_own_run_and_session(scoped_episode,caller,result_session,expected):
    e,sessions,runs=scoped_episode;set_deadline(e,sessions['current'],'2026-09-10')
    e['observable_trace']['model_calls'].insert(0,{'run_id':runs[caller],'returned_tool_calls':[{'name':'planning_intake_get','call_id':'get'}]})
    e['observable_trace']['tool_invocations']=[{'tool_call_id':'get','tool_name':'planning.intake.get','observation_status':'succeeded','result':{'session_ref':sessions[result_session],'confirmed_facts':[{'key':'deadline','value':'2026-09-30','source':'user'}]}}]
    assert _draft_schedule_checks(e)[0]['status']==expected, _draft_schedule_checks(e)[0]


@pytest.mark.parametrize('missing',['run','session','intake','duplicate_intake','conflicting_run'])
async def test_missing_or_ambiguous_scope_is_evidence_failure(scoped_episode,missing):
    e,sessions,runs=scoped_episode
    if missing=='run':e['observable_trace']['model_calls'][0].pop('run_id')
    elif missing=='session':
        for state in ['state_before','state_after']:
            next(x for x in e[state]['logical_entities'] if x['logical_id']==runs['current'])['data']['session_ref']=None
    elif missing=='intake':e['state_before']['logical_entities']=[x for x in e['state_before']['logical_entities'] if not (x['entity_type']=='planning_intake' and x['data']['session_ref']==sessions['current'])]
    elif missing=='duplicate_intake':
        x=deepcopy(next(x for x in e['state_before']['logical_entities'] if x['entity_type']=='planning_intake' and x['data']['session_ref']==sessions['current']));x['logical_id']='planning_intake:ambiguous';e['state_before']['logical_entities'].append(x)
    else:next(x for x in e['state_after']['logical_entities'] if x['logical_id']==runs['current'])['data']['session_ref']=sessions['other']
    check=_draft_schedule_checks(e)[0];assert check['status']=='invalid_input';assert check['reason_code']=='planning_intake_evidence_insufficient'


async def test_successful_observation_without_origin_run_is_not_trusted(scoped_episode):
    e,sessions,_=scoped_episode
    e['observable_trace']['model_calls'].insert(0,{'returned_tool_calls':[{'name':'planning_intake_get','call_id':'get'}]})
    e['observable_trace']['tool_invocations']=[{'tool_call_id':'get','tool_name':'planning.intake.get','observation_status':'succeeded','result':{'session_ref':sessions['current'],'confirmed_facts':[]}}]
    assert _draft_schedule_checks(e)[0]['status']=='invalid_input'


async def test_observation_array_order_does_not_change_last_causal_update(scoped_episode):
    e,sessions,runs=scoped_episode
    calls=[{'run_id':runs['current'],'returned_tool_calls':[{'name':'planning_intake_update','call_id':cid}]} for cid in ['early','late']]
    e['observable_trace']['model_calls']=calls+e['observable_trace']['model_calls']
    observations=[{'tool_call_id':cid,'tool_name':'planning.intake.update','observation_status':'succeeded','result':{'session_ref':sessions['current'],'confirmed_facts':[{'key':'deadline','value':date,'source':'user'}]}} for cid,date in [('early','2026-09-30'),('late','2026-09-10')]]
    e['observable_trace']['tool_invocations']=observations
    assert _draft_schedule_checks(e)[0]['status']=='fail'
    observations.reverse()
    assert _draft_schedule_checks(e)[0]['status']=='fail'
