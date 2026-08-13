import json

import pytest
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.api.operations import undo_operation
from app.models import AgentRun, Competency, CompetencyEdge, Plan
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool


def _plan() -> PlanCreate:
    return PlanCreate(
        title="技能图测试计划",
        goal="理解 Python 异步基础",
        current_level="入门",
        expected_outcome="可以解释并完成一个小练习",
        stages=[StageCreate(title="基础", tasks=[TaskCreate(title="解释事件循环")])],
    )


@pytest.mark.asyncio
async def test_competency_graph_requires_explicit_nodes_and_rejects_cycles():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", _plan())
        task = plan.stages[0].tasks[0]
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="建立技能图")
        db.add(run)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id)

        first = await execute_tool("competency_create", json.dumps({
            "key": "python.async.event_loop",
            "title": "理解事件循环",
            "description": "解释事件循环如何调度协程",
            "competency_type": "concept",
            "scope": "plan",
            "plan_id": plan.id,
        }), ctx)
        second = await execute_tool("competency_create", json.dumps({
            "key": "python.async.coroutines",
            "title": "使用协程",
            "description": "编写并运行简单协程",
            "competency_type": "skill",
            "scope": "plan",
            "plan_id": plan.id,
        }), ctx)
        assert first["ok"] is True
        assert second["ok"] is True
        c1 = first["data"]["competency_id"]
        c2 = second["data"]["competency_id"]

        link_plan = await execute_tool("competency_link", json.dumps({
            "competency_id": c1,
            "plan_id": plan.id,
            "relation": "targets",
            "target_stage": "demonstrated",
        }), ctx)
        link_task = await execute_tool("competency_link", json.dumps({
            "competency_id": c1,
            "task_id": task.id,
            "relation": "assesses",
            "target_stage": "demonstrated",
        }), ctx)
        assert link_plan["ok"] is True
        assert link_task["ok"] is True

        edge = await execute_tool("competency_edge", json.dumps({
            "source_id": c1,
            "target_id": c2,
            "relation": "prerequisite",
        }), ctx)
        cycle = await execute_tool("competency_edge", json.dumps({
            "source_id": c2,
            "target_id": c1,
            "relation": "prerequisite",
        }), ctx)
        assert edge["ok"] is True
        assert cycle["ok"] is False
        assert "cycle" in cycle["error"]

        graph = await execute_tool("competency_graph_get", json.dumps({"plan_id": plan.id}), ctx)
        assert graph["ok"] is True
        assert {item["id"] for item in graph["data"]["competencies"]} == {c1, c2}
        assert len(graph["data"]["task_links"]) == 1
        assert len(graph["data"]["edges"]) == 1

        stored = list((await db.execute(select(Competency).where(Competency.plan_id == plan.id))).scalars())
        stored_edges = list((await db.execute(select(CompetencyEdge).where(CompetencyEdge.owner_id == "local"))).scalars())
        assert len(stored) == 2
        assert len(stored_edges) == 1

        transient = await execute_tool("competency_create", json.dumps({
            "key": "python.async.transient",
            "title": "临时技能节点",
            "scope": "plan",
            "plan_id": plan.id,
        }), ctx)
        assert transient["ok"] is True
        undone = await undo_operation(transient["data"]["operation_id"], db)
        assert undone.status == "undone"
        assert await db.get(Competency, transient["data"]["competency_id"]) is None


@pytest.mark.asyncio
async def test_background_competency_write_requires_approval():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", _plan())
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="heartbeat", objective="维护技能图")
        db.add(run)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="heartbeat", plan_id=plan.id)
        result = await execute_tool("competency_create", json.dumps({
            "key": "python.async",
            "title": "异步编程",
            "scope": "plan",
            "plan_id": plan.id,
        }), ctx)
        assert result["ok"] is True
        assert result["data"]["approval_required"] is True
        assert result["data"]["blocking"] is True
