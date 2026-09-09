"""Exercise real intake/proposal handlers in the isolated test database."""
import pytest
from app.db.database import AsyncSessionLocal
from app.models import Session, AgentRun, PlanningIntake, PlanProposal
from app.tools.base import ToolContext
from app.tools.planning import PlanningIntakeUpdateArgs, planning_intake_update, PlanProposalCreateArgs, plan_proposal_create
from sqlalchemy import select


@pytest.mark.asyncio
async def test_deadline_change_or_removal_requires_specific_approval_and_proposal_honors_it():
    async with AsyncSessionLocal() as db:
        session=Session(owner_id='local',title='deadline');db.add(session);await db.flush()
        run=AgentRun(owner_id='local',session_id=session.id,trigger='user_message',objective='plan');db.add(run);await db.flush()
        ctx=ToolContext(db=db,owner_id='local',run_id=run.id,trigger='user_message',session_id=session.id)
        def args(value):return PlanningIntakeUpdateArgs(goal='learn',confirmed_facts=[] if value is None else [{'key':'deadline','value':value,'source':'user'}],readiness='ready',readiness_confidence=1,rationale='user stated constraint')
        await planning_intake_update(ctx,args('2026-09-20'))
        for value in ['2026-10-20',None]:
            response=await planning_intake_update(ctx,args(value));assert response['approval_required']
            assert (await db.get(PlanningIntake,session.id)).confirmed_facts[0]['value']=='2026-09-20'
        proposal=PlanProposalCreateArgs(plan={'title':'learn','goal':'learn','expected_outcome':'tests','deadline':'2026-10-20T00:00:00Z','stages':[{'title':'stage','tasks':[{'title':'task','due_at':'2026-10-18T00:00:00Z','review_due_at':'2026-10-19T00:00:00Z'}]}]},rationale='draft')
        response=await plan_proposal_create(ctx,proposal);assert 'user-confirmed deadline' in response['error']
        assert not (await db.execute(select(PlanProposal))).scalars().all()
        ctx.approval_granted=True
        await planning_intake_update(ctx,args('2026-10-20'))
        response=await plan_proposal_create(ctx,proposal);assert 'error' not in response

        # A proposal can become stale after an explicitly approved constraint change.
        from app.api.agent import decide_plan_proposal
        from app.schemas import PlanProposalDecision
        from app.models import Plan
        from fastapi import HTTPException
        proposal_id=response['proposal_id']
        await planning_intake_update(ctx,args('2026-09-20'))
        with pytest.raises(HTTPException, match='user-confirmed deadline') as error:
            await decide_plan_proposal(proposal_id,PlanProposalDecision(accepted=True),db)
        assert error.value.status_code==409
        assert not (await db.execute(select(Plan))).scalars().all()
        assert (await db.get(PlanProposal,proposal_id)).status=='pending'
