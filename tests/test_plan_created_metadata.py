import json

import pytest
from sqlalchemy import select

from app.api.agent import decide_plan_proposal
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, PlanProposal, PlanningIntake, Session
from app.schemas import PlanCreate, PlanProposalDecision, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool


def plan_payload(title="创建元数据计划"):
    return {
        "title": title,
        "goal": "验证 created_plan_id",
        "current_level": "初级",
        "weekly_minutes": 300,
        "expected_outcome": "可运行",
        "stages": [{"title": "阶段一", "tasks": [{"title": "任务一"}]}],
    }


@pytest.mark.asyncio
async def test_plan_create_sets_created_plan_id_on_run():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="heartbeat", objective="后台创建计划")
        db.add(run)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="heartbeat")
        raw = json.dumps(plan_payload(), ensure_ascii=False)

        pending = await execute_tool("plan_create", raw, ctx)
        assert pending["ok"] is True and pending["data"]["approval_required"] is True
        ctx.approval_granted = True
        granted = await execute_tool("plan_create", raw, ctx)
        assert granted["ok"] is True

        refreshed = await db.get(AgentRun, run.id)
        assert refreshed.created_plan_id == granted["data"]["plan_id"]


@pytest.mark.asyncio
async def test_proposal_accept_sets_created_plan_id_on_source_run():
    async with AsyncSessionLocal() as db:
        session = Session(
            owner_id="local", title="提案会话"
        )
        db.add(session)
        await db.flush()
        source = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="user_message",
            objective="创建计划",
        )
        db.add(source)
        await db.flush()
        db.add(PlanningIntake(
            session_id=session.id,
            owner_id="local",
            source_run_id=source.id,
            goal="学习并交付",
            confirmed_facts=[{"key": "目标", "value": "交付项目", "source": "user"}],
            open_questions=[],
            readiness="ready",
            readiness_confidence=0.95,
            rationale="信息充分",
        ))
        proposal = PlanProposal(
            owner_id="local",
            session_id=session.id,
            source_run_id=source.id,
            title="创建元数据计划",
            rationale="已确认约束",
            plan_payload=plan_payload(),
            status="pending",
        )
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id

        accepted = await decide_plan_proposal(proposal_id, PlanProposalDecision(accepted=True), db)
        assert accepted.plan_id is not None
        refreshed_source = await db.get(AgentRun, source.id)
        assert refreshed_source.created_plan_id == accepted.plan_id
