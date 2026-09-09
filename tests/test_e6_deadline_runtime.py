"""Real Runtime approval lifecycle with synthetic model responses, no paid calls."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.api.agent import decide_run_approval, decide_plan_proposal
from app.db.database import AsyncSessionLocal
from app.models import Session, AgentRun, PlanningIntake, RunApproval, ToolInvocation, PlanProposal, Plan, RunEvent
from app.runtime.agent import AgentRuntime
from app.schemas import RunApprovalRequest, PlanProposalDecision
from test_run_recovery import FakeToolCall


@pytest.mark.parametrize('change',['extend','remove'])
@pytest.mark.parametrize('approve',[True,False])
async def test_deadline_approval_pause_decide_resume_preserves_session_and_proposal_constraints(monkeypatch,tmp_path,change,approve):
    import app.api.agent as api
    monkeypatch.setattr(api,'_wake_runtime',lambda *_args,**_kwargs:None)
    async with AsyncSessionLocal() as db:
        for sid,deadline in [('current','2026-09-10'),('other','2026-10-30')]:
            db.add(Session(id=sid,owner_id='local',title=sid));await db.flush()
            db.add(PlanningIntake(session_id=sid,owner_id='local',goal='Learn',confirmed_facts=[{'key':'deadline','value':deadline,'source':'user'}],open_questions=[],readiness='ready',readiness_confidence=1,rationale='Synthetic confirmed facts'))
        run=AgentRun(owner_id='local',session_id='current',trigger='user_message',objective='Review a proposed deadline change',model='hy3');db.add(run);await db.commit();rid=run.id
    class Completions:
        def __init__(self):self.requests=[]
        async def create(self,**kwargs):
            self.requests.append(kwargs);step=len(self.requests)
            if step==1:
                args={'goal':'Learn','confirmed_facts':[] if change=='remove' else [{'key':'deadline','value':'2026-09-30','source':'user'}],'open_questions':[],'readiness':'ready','readiness_confidence':1,'rationale':'Propose a deadline change for user approval'}
                calls=[FakeToolCall('deadline-update','planning_intake_update',json.dumps(args))]
            elif step==2:
                args={'plan':{'title':'Synthetic plan','goal':'Learn','expected_outcome':'Tested exercise','deadline':'2026-09-20T00:00:00+08:00','stages':[{'title':'Stage','tasks':[{'title':'Exercise','due_at':'2026-09-18T00:00:00+08:00','review_due_at':'2026-09-19T00:00:00+08:00','is_core':True,'evidence_required':True}]}]},'rationale':'Draft for review'}
                calls=[FakeToolCall('proposal','plan_proposal_create',json.dumps(args))]
            else:calls=None
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Review the recorded tool result.',reasoning_content=None,tool_calls=calls))])
    model=Completions();runtime=AgentRuntime();runtime.client=SimpleNamespace(chat=SimpleNamespace(completions=model))
    await runtime.run(rid)
    async with AsyncSessionLocal() as db:
        run=await db.get(AgentRun,rid);assert run.status=='waiting_approval'
        assert run.pending_approval['tool_call']['name']=='planning_intake_update'
        assert (await db.get(PlanningIntake,'current')).confirmed_facts[0]['value']=='2026-09-10'
        assert (await db.get(PlanningIntake,'other')).confirmed_facts[0]['value']=='2026-10-30'
        assert not (await db.execute(select(PlanProposal))).scalars().all()
        paused={'status':run.status,'current_deadline':'2026-09-10','other_deadline':'2026-10-30','proposal_count':0}
        await decide_run_approval(rid,RunApprovalRequest(approved=approve),db)
    await runtime.run(rid,resume=True)
    async with AsyncSessionLocal() as db:
        run=await db.get(AgentRun,rid);assert run.status=='completed';assert run.pending_approval is None
        facts=(await db.get(PlanningIntake,'current')).confirmed_facts
        assert facts==([] if approve and change=='remove' else [{'key':'deadline','value':'2026-09-30' if approve else '2026-09-10','source':'user'}])
        assert (await db.get(PlanningIntake,'other')).confirmed_facts==[{'key':'deadline','value':'2026-10-30','source':'user'}]
        proposals=(await db.execute(select(PlanProposal))).scalars().all();assert len(proposals)==int(approve)
        if approve:
            assert proposals[0].session_id=='current'
            await decide_plan_proposal(proposals[0].id,PlanProposalDecision(accepted=True),db)
        plans=(await db.execute(select(Plan))).scalars().all();assert len(plans)==int(approve)
        invocations=(await db.execute(select(ToolInvocation).where(ToolInvocation.run_id==rid))).scalars().all()
        approvals=(await db.execute(select(RunApproval).where(RunApproval.run_id==rid))).scalars().all()
        events=(await db.execute(select(RunEvent).where(RunEvent.run_id==rid).order_by(RunEvent.sequence))).scalars().all()
        assert any(e.event_type=='approval.resolved' and e.payload['decision']==('approve' if approve else 'reject') for e in events)
        evidence={'change':change,'approved':approve,'model_mode':'synthetic_responses_real_runtime','paused':paused,'final_status':run.status,'current_facts':facts,'other_deadline':'2026-10-30','proposals':len(proposals),'adopted_plans':len(plans),'invocations':[{'tool':i.tool_name,'status':i.status,'result':i.result_payload} for i in invocations],'approval_count':len(approvals),'events':[{'type':e.event_type,'summary':e.summary,'payload':e.payload} for e in events]}
        (tmp_path/f'deadline-runtime-{change}-{approve}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
