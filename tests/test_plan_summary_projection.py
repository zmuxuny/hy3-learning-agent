"""Same-Run plan reads must not mix current facts with a stale memory summary."""

import json

import app.context.memory as memory_module
import pytest
from app.context.memory import MemoryManager
from app.db.database import AsyncSessionLocal, engine
from app.db.uow import commit as commit_uow
from app.models import AgentRun, Plan
from app.schemas import PlanCreate, StageCreate, TaskCreate
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool
from sqlalchemy import event, select


@pytest.mark.asyncio
async def test_same_run_plan_and_task_patches_return_current_read_only_summary(monkeypatch):
    monkeypatch.setattr(memory_module, "get_embedding_provider", lambda: None)
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", PlanCreate(
            title="异步执行练习", goal="掌握恢复机制", expected_outcome="可验证的示例",
            weekly_minutes=180,
            stages=[StageCreate(title="实践", tasks=[
                TaskCreate(title="阅读示例"),
                TaskCreate(title="搭建环境"),
                TaskCreate(title="验证恢复"),
            ])],
        ))
        plan_id = plan.id
        task_ids = [task.id for task in plan.stages[0].tasks]
        version = plan.version
        run = AgentRun(owner_id="local", plan_id=plan_id, trigger="user_message", objective="调整并检查计划")
        db.add(run)
        await commit_uow(db)
        run_id = run.id

        async def call(name, arguments, call_id):
            result = await execute_tool(name, json.dumps(arguments), ToolContext(
                db=db, owner_id="local", run_id=run_id, trigger="user_message",
                plan_id=plan_id, tool_call_id=call_id,
            ))
            assert result["ok"], result
            return result["data"]

        async def read_plan(call_id):
            statements = []

            def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(statement.lstrip().split(None, 1)[0].upper())

            event.listen(engine.sync_engine, "before_cursor_execute", observe)
            try:
                data = await call("plan_get", {"plan_id": plan_id}, call_id)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", observe)
            assert statements and set(statements) == {"SELECT"}
            assert not db.new and not db.dirty and not db.deleted
            return data

        manager = MemoryManager(db)
        await manager.maintain("local")
        await commit_uow(db)
        initial = await read_plan("read-cached-plan")
        assert initial["memory_summary"] == f"进度 0/3；当前任务：阅读示例；阻塞：无；计划版本 {version}。"
        cached = await db.scalar(select(Plan.memory_summary).where(Plan.id == plan_id))
        assert cached == initial["memory_summary"]

        await call("task_patch", {
            "task_id": task_ids[0], "changes": {"status": "completed"},
            "reason": "已阅读并确认示例", "expected_plan_version": version,
        }, "complete-reading")
        after_task = await read_plan("read-after-task")
        assert after_task["version"] == version + 1
        assert after_task["memory_summary"] == f"进度 1/3；当前任务：搭建环境；阻塞：无；计划版本 {version + 1}。"

        await call("task_patch", {
            "task_id": task_ids[1], "changes": {"title": "等待测试环境", "status": "blocked"},
            "reason": "环境暂时不可用", "expected_plan_version": version + 1,
        }, "record-blocker")
        await call("task_patch", {
            "task_id": task_ids[2], "changes": {"title": "验证重试逻辑", "status": "active"},
            "reason": "先验证独立的重试逻辑", "expected_plan_version": version + 2,
        }, "start-retry-task")
        await call("plan_patch", {
            "plan_id": plan_id, "weekly_minutes": 420, "reason": "增加本周实践时间",
            "expected_version": version + 3,
        }, "change-plan-time")
        current = await read_plan("read-after-plan")
        expected = f"进度 1/3；当前任务：验证重试逻辑；阻塞：等待测试环境；计划版本 {version + 4}。"
        assert current["version"] == version + 4 and current["weekly_minutes"] == 420
        assert current["memory_summary"] == expected
        assert [task["status"] for task in current["stages"][0]["tasks"]] == ["completed", "blocked", "active"]
        assert await db.scalar(select(Plan.memory_summary).where(Plan.id == plan_id)) == cached

        # Explicit maintenance later persists the same projection without a
        # semantic difference between the read tool and the background cache.
        maintained = await manager.maintain("local")
        await commit_uow(db)
        assert maintained["plans_refreshed"] == 1
        assert await db.scalar(select(Plan.memory_summary).where(Plan.id == plan_id)) == expected
