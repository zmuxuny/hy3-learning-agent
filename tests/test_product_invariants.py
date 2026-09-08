import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.api.agent import decide_plan_proposal, rename_session
from app.api.operations import undo_operation
from app.api.plans import create_plan as create_plan_endpoint, set_plan_archived
from app.context import ContextAssembler
from app.core.config import PROJECT_ROOT, settings
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    ChatMessage,
    LearningEvent,
    Notification,
    Plan,
    PlanProposal,
    PlanningIntake,
    QueuedMessage,
    Quiz,
    ReviewSchedule,
    Session,
    Task,
    TaskSubmission,
    ToolInvocation,
    UserProfile,
)
from app.notifications.conversation import open_notification_in_conversation
from app.notifications.service import NotificationService
from app.runtime.proactive import capture_proactive_candidate, finalize_proactive_decision
from app.runtime.scheduler import proactive_scheduler
from app.schemas import (
    PlanArchiveUpdate,
    PlanCreate,
    PlanProposalDecision,
    SessionUpdate,
    StageCreate,
    TaskCreate,
)
from app.services import plans as plan_service
from app.services.queue import dispatch_next_queued_message
from app.tools import ToolContext, execute_tool


def plan_payload(title: str) -> PlanCreate:
    return PlanCreate(
        title=title,
        goal="完成一个可验证作品",
        current_level="入门",
        weekly_minutes=180,
        expected_outcome="提交作品",
        stages=[StageCreate(
            title="阶段一",
            tasks=[TaskCreate(title="完成练习", review_due_at=datetime.now(timezone.utc) + timedelta(days=7))],
        )],
    )


@pytest.mark.asyncio
async def test_sqlite_enforces_declared_foreign_keys():
    async with AsyncSessionLocal() as db:
        assert int((await db.execute(text("PRAGMA foreign_keys"))).scalar_one()) == 1


def test_core_tasks_need_evidence_but_optional_tasks_do_not():
    plan = PlanCreate.model_validate({
        "title": "边界练习", "goal": "实现FIFO", "expected_outcome": "断言输出",
        "stages": [{"title": "练习", "tasks": [
            {"title": "阅读", "is_core": False},
            {"title": "实现", "is_core": True},
        ]}],
    })
    assert plan_service.plan_completeness_issues(plan) == ["stage 1 task 2: core tasks require evidence_required=true"]
    plan.stages[0].tasks[1].evidence_required = True
    assert plan_service.plan_completeness_issues(plan) == []


def test_formal_plan_completeness_rejects_empty_structure():
    issues = plan_service.plan_completeness_issues(PlanCreate(title="只有标题"))
    assert issues == [
        "goal is required",
        "expected_outcome is required",
        "at least one stage is required",
    ]


@pytest.mark.asyncio
async def test_every_public_plan_creation_path_enforces_formal_completeness():
    incomplete = PlanCreate(title="只有标题")
    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as api_error:
            await create_plan_endpoint(incomplete, db)
        assert api_error.value.status_code == 422

        run = AgentRun(owner_id="local", trigger="user_message", objective="创建计划")
        db.add(run)
        await db.commit()
        tool_result = await execute_tool(
            "plan_create",
            incomplete.model_dump_json(),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
        assert tool_result["ok"] is False
        assert "incomplete" in tool_result["error"]


@pytest.mark.asyncio
async def test_archived_planning_session_cannot_materialize_a_pending_proposal():
    async with AsyncSessionLocal() as db:
        session = Session(
            owner_id="local",
            title="已归档的规划对话",
            archived_at=datetime.now(timezone.utc),
        )
        db.add(session)
        await db.flush()
        db.add(PlanningIntake(
            session_id=session.id,
            owner_id="local",
            goal="学习 Python",
            readiness="ready",
        ))
        proposal = PlanProposal(
            owner_id="local",
            session_id=session.id,
            title="Python 计划",
            plan_payload=plan_payload("Python 计划").model_dump(mode="json"),
            status="pending",
        )
        db.add(proposal)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await decide_plan_proposal(
                proposal.id,
                PlanProposalDecision(accepted=True),
                db,
            )
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_opening_one_notification_marks_the_whole_delivery_group_read():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="Intervention delivery thread")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="heartbeat",
            objective="提醒",
            status="completed",
            phase="terminal",
        )
        db.add(run)
        await db.flush()
        sent = await NotificationService(db).send(
            owner_id="local",
            run_id=run.id,
            session_id=session.id,
            trigger="manual_heartbeat",
            title="同一提醒",
            body="正文",
            plan_id=None,
            channels=["email"],
        )
        await db.commit()
        deliveries = list(
            (
                await db.execute(
                    select(Notification)
                    .where(Notification.intervention_id == sent["intervention_id"])
                    .order_by(Notification.id)
                )
            ).scalars()
        )
        assert [delivery.channel for delivery in deliveries] == ["in_app", "email"]
        assert len({delivery.intervention_id for delivery in deliveries}) == 1
        in_app = next(delivery for delivery in deliveries if delivery.channel == "in_app")
        email = next(delivery for delivery in deliveries if delivery.channel == "email")
        await open_notification_in_conversation(db, email)
        await db.commit()
        await db.refresh(in_app)
        await db.refresh(email)
        assert in_app.read_at is not None
        assert email.read_at is not None
        assert in_app.session_id == email.session_id


@pytest.mark.asyncio
async def test_archiving_cannot_hide_a_waiting_approval_run():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("审批中的计划"))
        session = Session(owner_id="local", plan_id=plan.id, title="审批中的会话")
        db.add(session)
        await db.flush()
        db.add(AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="user_message",
            objective="修改目标",
            status="waiting_approval",
            pending_approval={"reason": "需要确认"},
        ))
        await db.commit()

        with pytest.raises(HTTPException) as session_error:
            await rename_session(session.id, SessionUpdate(archived=True), db)
        assert session_error.value.status_code == 409

        with pytest.raises(HTTPException) as plan_error:
            await set_plan_archived(plan.id, PlanArchiveUpdate(archived=True), db)
        assert plan_error.value.status_code == 409


@pytest.mark.asyncio
async def test_archived_plan_is_read_only_across_public_write_paths():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("只读归档计划"))
        task = plan.stages[0].tasks[0]
        plan.status = "archived"
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="继续修改")
        db.add(run)
        await db.commit()

        with pytest.raises(HTTPException) as direct_error:
            await plan_service.update_task(db, "local", task.id, {"status": "active"})
        assert direct_error.value.status_code == 409

        ctx = ToolContext(
            db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id,
        )
        attempts = [
            ("plan_patch", {"plan_id": plan.id, "title": "不能改名", "reason": "归档后修改标题"}),
            ("task_create", {"stage_id": plan.stages[0].id, "title": "不能新增任务"}),
            ("task_patch", {"task_id": task.id, "changes": {"status": "active"}, "reason": "继续任务"}),
            ("submission_create", {"task_id": task.id, "content": "归档后提交"}),
            ("review_schedule", {"plan_id": plan.id, "task_id": task.id, "due_at": datetime.now(timezone.utc).isoformat()}),
            ("quiz_create", {"plan_id": plan.id, "task_id": task.id, "prompt": "归档后测验"}),
            ("calendar_create", {"plan_id": plan.id, "task_id": task.id, "title": "归档后日历", "starts_at": datetime.now(timezone.utc).isoformat()}),
        ]
        for tool_name, arguments in attempts:
            result = await execute_tool(tool_name, json.dumps(arguments), ctx)
            assert result["ok"] is False, tool_name
            assert "Restore the plan" in result["error"], tool_name


@pytest.mark.asyncio
async def test_archiving_cannot_discard_unhandled_session_queue():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("有待处理消息"))
        session = Session(owner_id="local", plan_id=plan.id, title="还有队列")
        db.add(session)
        await db.flush()
        db.add(QueuedMessage(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            objective="尚未执行的用户要求",
            position=0,
        ))
        await db.commit()

        with pytest.raises(HTTPException) as session_error:
            await rename_session(session.id, SessionUpdate(archived=True), db)
        assert session_error.value.status_code == 409
        assert "queued messages" in session_error.value.detail

        with pytest.raises(HTTPException) as plan_error:
            await set_plan_archived(plan.id, PlanArchiveUpdate(archived=True), db)
        assert plan_error.value.status_code == 409
        assert "queued messages" in plan_error.value.detail

        assert await db.scalar(select(QueuedMessage.id).where(QueuedMessage.session_id == session.id))


@pytest.mark.asyncio
async def test_queue_dispatch_is_a_real_continuous_session_turn():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="连续对话")
        db.add(session)
        await db.flush()
        queued = QueuedMessage(
            owner_id="local",
            session_id=session.id,
            objective="排队后的问题",
            position=0,
        )
        db.add(queued)
        await db.commit()

        run = await dispatch_next_queued_message(db, owner_id="local", session_id=session.id)
        assert run is not None
        assert run.session_id == session.id
        message = (await db.execute(
            select(ChatMessage).where(ChatMessage.run_id == run.id)
        )).scalars().one()
        assert (message.role, message.content) == ("user", "排队后的问题")
        assert list((await db.execute(select(QueuedMessage))).scalars()) == []


@pytest.mark.asyncio
async def test_scheduler_ignores_housekeeping_and_throttles_recent_plan_checks(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_PROGRESS_CHECKIN_HOURS", 1)
    monkeypatch.setattr(settings, "AGENT_CANDIDATE_COOLDOWN_MINUTES", 180)
    stale_at = datetime.now(timezone.utc) - timedelta(hours=3)
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("没有真实进展"))
        plan.created_at = stale_at
        events = list((await db.execute(
            select(LearningEvent)
        )).scalars())
        for event in events:
            event.created_at = stale_at
        db.add(LearningEvent(
            owner_id="local",
            plan_id=plan.id,
            event_type="plan.updated",
            summary="Agent 调整了计划标题",
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        plan_id = plan.id

    candidate = await proactive_scheduler._next_candidate()
    assert candidate and candidate["plan_id"] == plan_id
    assert candidate["reason"] == "progress_checkin_due"

    async with AsyncSessionLocal() as db:
        run = AgentRun(
            owner_id="local",
            plan_id=plan_id,
            trigger="heartbeat",
            objective="刚检查过",
            status="completed",
            phase="terminal",
        )
        capture_proactive_candidate(run, candidate)
        db.add(run)
        await db.flush()
        await finalize_proactive_decision(
            db,
            run,
            outcome="success_wait",
            reason_code="recent_plan_check_completed",
            decision_payload={"plan_id": plan_id, "intervention_needed": False},
        )
        await db.commit()
    assert await proactive_scheduler._next_candidate() is None


@pytest.mark.asyncio
async def test_daily_limit_counts_one_intervention_not_each_delivery(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_NOTIFICATION_COOLDOWN_MINUTES", 0)
    async with AsyncSessionLocal() as db:
        profile = await db.get(UserProfile, "local")
        profile.quiet_hours = {"start": "00:00", "end": "00:00"}
        profile.daily_notification_limit = 2
        session = Session(owner_id="local", title="Daily Intervention count")
        db.add(session)
        await db.flush()
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            trigger="heartbeat",
            objective="检查",
            status="completed",
            phase="terminal",
        )
        db.add(run)
        await db.flush()
        sent = await NotificationService(db).send(
            owner_id="local",
            run_id=run.id,
            session_id=session.id,
            trigger="manual_heartbeat",
            title="同一提醒",
            body="正文",
            plan_id=None,
            channels=["email"],
        )
        now = datetime.now(timezone.utc)
        deliveries = list(
            (
                await db.execute(
                    select(Notification).where(
                        Notification.intervention_id == sent["intervention_id"]
                    )
                )
            ).scalars()
        )
        assert len(deliveries) == 2
        assert len({delivery.intervention_id for delivery in deliveries}) == 1
        for delivery in deliveries:
            delivery.status = "sent"
            delivery.sent_at = now
        await db.commit()
        allowed, reason = await NotificationService(db)._guard("local", "heartbeat", None)
    assert allowed is True
    assert reason == "allowed"


@pytest.mark.asyncio
async def test_assessment_attempts_are_immutable_and_review_has_a_terminal_action():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("考核审计"))
        task = plan.stages[0].tasks[0]
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="验收")
        db.add(run)
        await db.flush()
        quiz = Quiz(owner_id="local", plan_id=plan.id, task_id=task.id, prompt="解释概念", status="passed")
        submission = TaskSubmission(owner_id="local", plan_id=plan.id, task_id=task.id, status="accepted")
        review = ReviewSchedule(owner_id="local", plan_id=plan.id, task_id=task.id, due_at=datetime.now(timezone.utc))
        db.add_all([quiz, submission, review])
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id)

        regrade = await execute_tool("quiz_grade", json.dumps({
            "quiz_id": quiz.id, "answer": "再次回答", "score": 100, "feedback": "重复",
        }), ctx)
        assert regrade["ok"] is False
        recheck = await execute_tool("submission_check", json.dumps({
            "submission_id": submission.id, "score": 100, "feedback": "重复",
        }), ctx)
        assert recheck["ok"] is False

        resolved = await execute_tool("review_resolve", json.dumps({
            "review_id": review.id, "action": "complete", "reason": "本轮复习和考核已经完成",
        }), ctx)
        assert resolved["ok"] is True
        assert resolved["data"]["status"] == "completed"
        await undo_operation(resolved["data"]["operation_id"], db)
        await db.refresh(review)
        assert review.status == "scheduled"
        audit_events = list((await db.execute(
            select(LearningEvent).where(LearningEvent.event_type == "operation.undone")
        )).scalars())
        assert audit_events and audit_events[-1].payload["operation_id"] == resolved["data"]["operation_id"]


@pytest.mark.asyncio
async def test_undo_created_task_keeps_audit_without_dangling_task_reference():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("撤销任务审计"))
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="添加任务")
        db.add(run)
        await db.commit()
        result = await execute_tool("task_create", json.dumps({
            "stage_id": plan.stages[0].id,
            "title": "临时补充任务",
        }), ToolContext(
            db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id,
        ))
        assert result["ok"] is True
        task_id = result["data"]["task_id"]

        await undo_operation(result["data"]["operation_id"], db)
        assert await db.get(Task, task_id) is None
        audit = (await db.execute(
            select(LearningEvent).where(LearningEvent.event_type == "operation.undone")
        )).scalars().one()
        assert audit.plan_id == plan.id
        assert audit.task_id is None
        assert audit.payload["entity_id"] == str(task_id)


@pytest.mark.asyncio
async def test_global_context_is_an_index_and_markdown_projection_excludes_session_transcript(
    isolated_runtime_root,
):
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("无关计划"))
        session = Session(owner_id="local", title="全局对话")
        db.add(session)
        await db.flush()
        db.add_all([
            ChatMessage(session_id=session.id, role="user", content="这是只属于本会话的句子"),
            ReviewSchedule(owner_id="local", plan_id=plan.id, due_at=datetime.now(timezone.utc)),
        ])
        run = AgentRun(owner_id="local", session_id=session.id, trigger="user_message", objective="看看全局")
        db.add(run)
        await db.commit()

        snapshot = await ContextAssembler(db).build(
            "local", session_id=session.id, run_id=run.id, objective=run.objective,
        )
        await db.commit()
        assert "## Plan index" in snapshot.markdown
        assert "## Actionable learning state" not in snapshot.markdown
        assert "这是只属于本会话的句子" in snapshot.markdown

        context_root = isolated_runtime_root / "data" / "context"
        canonical = (context_root / "global.md").read_text(encoding="utf-8")
        exact = (context_root / "runs" / f"{run.id}.md").read_text(encoding="utf-8")
        assert "这是只属于本会话的句子" not in canonical
        assert "这是只属于本会话的句子" in exact


@pytest.mark.asyncio
async def test_uncertain_write_is_not_automatically_replayed():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("幂等保护"))
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="修改")
        db.add(run)
        await db.flush()
        arguments = json.dumps({
            "task_id": plan.stages[0].tasks[0].id,
            "changes": {"status": "active"},
            "reason": "开始学习",
        })
        from app.tools.registry import _idempotency_key

        key = _idempotency_key(run.id, "task_patch", arguments, "same-call")
        db.add(ToolInvocation(
            owner_id="local",
            run_id=run.id,
            idempotency_key=key,
            tool_name="task_patch",
            args_hash="uncertain",
            status="running",
        ))
        await db.commit()
        result = await execute_tool(
            "task_patch",
            arguments,
            ToolContext(
                db=db, owner_id="local", run_id=run.id, trigger="user_message",
                plan_id=plan.id, tool_call_id="same-call",
            ),
        )
        assert result["ok"] is False
        assert result["uncertain_outcome"] is True
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.stages[0].tasks[0].status == "pending"
