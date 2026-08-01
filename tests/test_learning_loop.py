import copy
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from app.api.agent import (
    create_run,
    decide_plan_proposal,
    edit_user_message,
    handoff_session,
    list_sessions,
    read_planning_state,
    read_session_messages,
    rename_session,
    submit_planning_answers,
)
from app.api.plans import read_plan_resources, set_plan_archived
from app.api.notifications import archive_read_notifications, read_notifications, set_notification_archived
from app.api.workspace import upload_workspace_file
from app.db.database import AsyncSessionLocal
from app.api.operations import undo_operation
from app.context.memory import MemoryManager
from app.models import (
    AgentRun,
    ChatMessage,
    ChatMessageRevision,
    LearningEvent,
    LearningResource,
    Memory,
    Notification,
    Plan,
    PlanProposal,
    PlanningIntake,
    RunEvent,
    Session,
    SessionPlanLink,
    TaskSubmission,
)
from app.runtime.agent import AgentRuntime, ToolFailureGuard
from app.main import reconcile_interrupted_runs
from app.runtime.scheduler import proactive_scheduler
from app.schemas import (
    AgentRunCreate,
    MessageEdit,
    NotificationArchiveUpdate,
    PlanArchiveUpdate,
    PlanCreate,
    PlanningAnswer,
    PlanningAnswersSubmit,
    PlanProposalDecision,
    SessionHandoffCreate,
    SessionUpdate,
    StageCreate,
    TaskCreate,
    TaskUpdate,
)
from app.services import plans as plan_service
from app.tools import ToolContext, execute_tool
from app.tools.registry import tool_contracts
from app.search.security import fetch_with_safe_redirects
from app.search.providers import DuckDuckGoSearchProvider


def plan_payload(title: str = "Python async mastery") -> PlanCreate:
    return PlanCreate(
        title=title,
        goal="Build and explain a robust asyncio service",
        current_level="Can write basic Python",
        weekly_minutes=300,
        expected_outcome="A tested async mini-project",
        stages=[
            StageCreate(
                title="Async foundations",
                tasks=[
                    TaskCreate(title="Read event-loop guide", estimated_minutes=30),
                    TaskCreate(
                        title="Implement a concurrent crawler",
                        is_core=True,
                        evidence_required=True,
                        estimated_minutes=90,
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_startup_reconciles_interrupted_runs_without_losing_trace():
    async with AsyncSessionLocal() as db:
        interrupted = AgentRun(
            owner_id="local", trigger="user_message", objective="unfinished", status="running",
        )
        completed = AgentRun(
            owner_id="local", trigger="user_message", objective="done", status="completed",
        )
        db.add_all([interrupted, completed])
        await db.commit()
        interrupted_id = interrupted.id
        completed_id = completed.id

    assert await reconcile_interrupted_runs() == 1

    async with AsyncSessionLocal() as db:
        interrupted = await db.get(AgentRun, interrupted_id)
        completed = await db.get(AgentRun, completed_id)
        assert interrupted.status == "failed"
        assert interrupted.completed_at is not None
        assert completed.status == "completed"
        event = (await db.execute(select(RunEvent).where(
            RunEvent.run_id == interrupted_id,
            RunEvent.event_type == "run.failed",
        ))).scalars().one()
        assert event.payload["code"] == "process_interrupted"


@pytest.mark.asyncio
async def test_session_focus_cannot_be_rebound():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", plan_id=None, title="Global conversation")
        db.add(session)
        await db.commit()
        await db.refresh(session)

        with pytest.raises(HTTPException) as error:
            await create_run(
                AgentRunCreate(objective="Switch focus silently", session_id=session.id, plan_id=999),
                db,
            )

        assert error.value.status_code == 409
        assert error.value.detail == "Session focus does not match requested plan"


@pytest.mark.asyncio
async def test_planning_card_answers_are_a_structured_same_session_interaction(monkeypatch):
    import app.api.agent as agent_api

    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="计划共创")
        db.add(session)
        await db.flush()
        db.add(PlanningIntake(
            session_id=session.id,
            owner_id="local",
            goal="学习并完成作品",
            open_questions=[{
                "id": "weekly_time",
                "prompt": "每周能投入多少时间？",
                "why": "决定计划密度",
                "options": ["3 小时", "5 小时"],
                "allow_custom": True,
            }],
            readiness="collecting",
            readiness_confidence=0.45,
            rationale="还缺少时间约束",
        ))
        await db.commit()

        run = await submit_planning_answers(
            session.id,
            PlanningAnswersSubmit(answers=[PlanningAnswer(question_id="weekly_time", answer="5 小时")]),
            db,
        )
        assert run.session_id == session.id
        intake = await db.get(PlanningIntake, session.id)
        assert intake.open_questions == []
        assert "重新判断" in intake.rationale
        message = (await db.execute(select(ChatMessage).where(ChatMessage.run_id == run.id))).scalars().one()
        assert message.message_metadata["ui_kind"] == "planning_answers"
        assert message.message_metadata["answer_count"] == 1
        assert "5 小时" in message.content


@pytest.mark.asyncio
async def test_scheduler_can_request_progress_without_task_deadlines(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_PROGRESS_CHECKIN_HOURS", 1)
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("主动跟进计划"))
        stale_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
        plan.created_at = stale_at
        event = (await db.execute(select(LearningEvent).where(LearningEvent.plan_id == plan.id))).scalars().one()
        event.created_at = stale_at
        await db.commit()
        plan_id = plan.id

    candidate = await proactive_scheduler._next_candidate()
    assert candidate["plan_id"] == plan_id
    assert candidate["reason"] == "progress_checkin_due"
    assert "主动发一条简短站内询问" in candidate["objective"]


@pytest.mark.asyncio
async def test_core_evidence_progress_and_undo():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload())
        normal, core = plan.stages[0].tasks

        run = AgentRun(owner_id="local", trigger="user_message", objective="Update my progress")
        db.add(run)
        await db.commit()

        result = await execute_tool(
            "task_patch",
            json.dumps({
                "task_id": normal.id,
                "changes": json.dumps({"status": "completed"}),
                "reason": "User finished it",
            }),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
        assert result["ok"] is True
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 0.5

        with pytest.raises(HTTPException) as error:
            await plan_service.update_task(db, "local", core.id, TaskUpdate(status="completed"))
        assert error.value.status_code == 409

        await plan_service.update_task(
            db,
            "local",
            core.id,
            TaskUpdate(status="completed", evidence=[{"kind": "repository", "value": "local/demo"}]),
        )
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.progress == 1.0
        assert refreshed.stages[0].status == "completed"

        operation = await undo_operation(result["data"]["operation_id"], db)
        assert operation.status == "undone"
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.stages[0].tasks[0].status == "pending"
        assert refreshed.progress == 0.5


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class FakeCompletions:
    def __init__(self):
        self.calls = []
        self.responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="我先检查当前计划。",
                    reasoning_content="private planning tokens",
                    tool_calls=[FakeToolCall("call-1", "plan_list", "{}")],
                ))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="现在发送一条可见提醒。",
                    reasoning_content="private tool selection tokens",
                    tool_calls=[FakeToolCall(
                        "call-2",
                        "notification_send",
                        json.dumps({
                            "title": "该开始异步练习了",
                            "body": "先完成 25 分钟的事件循环练习。",
                            "channels": ["in_app", "browser"],
                        }, ensure_ascii=False),
                    )],
                ))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="我检查了计划，并把本次练习提醒放进了收件箱。",
                    reasoning_content="private final tokens",
                    tool_calls=None,
                ))]
            ),
        ]

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_harness_runs_tools_and_keeps_reasoning_private():
    async with AsyncSessionLocal() as db:
        await plan_service.create_plan(db, "local", plan_payload("Harness demo"))
        run = AgentRun(owner_id="local", trigger="user_message", objective="监督我开始今天的学习")
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = FakeCompletions()
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        assert run.status == "completed"
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        event_types = [event.event_type for event in events]
        assert event_types.count("tool.started") == 2
        assert "run.completed" in event_types
        assert all("private" not in event.summary for event in events)
        notifications = list((await db.execute(select(Notification))).scalars())
        assert {item.channel for item in notifications} == {"in_app", "browser"}
        messages = await read_session_messages(run.session_id, db)
        assert [(message.role, message.content) for message in messages] == [
            ("user", "监督我开始今天的学习"),
            ("assistant", "我检查了计划，并把本次练习提醒放进了收件箱。"),
        ]
        assert all(message.run_id == run_id for message in messages)

    assert completions.calls[0]["extra_body"] == {"reasoning_effort": "high"}
    assert "The supplied tool schemas are the complete set" in completions.calls[0]["messages"][0]["content"]
    tool_names = {tool["function"]["name"] for tool in completions.calls[0]["tools"]}
    assert len(tool_names) == 37
    contracts = tool_contracts()
    assert len(contracts) == 37
    assert all(contract["input_schema"] and contract["output_schema"] for contract in contracts)
    assert {
        "profile_get",
        "plan_list",
        "plan_get",
        "plan_create",
        "task_patch",
        "review_schedule",
        "quiz_create",
        "quiz_get",
        "quiz_grade",
        "memory_propose",
        "notification_send",
        "plan_patch",
        "submission_create",
        "submission_check",
        "memory_search",
        "memory_maintain",
        "web_search",
        "web_open",
        "resource_save",
        "file_read",
        "file_write",
        "code_execute",
        "calendar_list",
        "calendar_create",
        "planning_intake_get",
        "planning_intake_update",
        "planning_delegate",
        "plan_proposal_create",
        "study_state_get",
    }.issubset(tool_names)
    assert "Coursera, edX, Hugging Face Learn" in completions.calls[0]["messages"][0]["content"]
    assert "what should I do now" in completions.calls[0]["messages"][0]["content"]
    replayed_messages = completions.calls[1]["messages"]
    assert any(message.get("reasoning_content") == "private planning tokens" for message in replayed_messages)


@pytest.mark.asyncio
async def test_complete_learning_submission_calendar_and_workspace_loop():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("Demo loop"))
        task = plan.stages[0].tasks[1]
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="完成学习闭环")
        db.add(run)
        await db.commit()
        ctx = ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id)

        patched = await execute_tool(
            "plan_patch",
            json.dumps({"plan_id": plan.id, "weekly_minutes": 420, "reason": "用户增加了本周学习时间"}),
            ctx,
        )
        assert patched["ok"] is True
        assert patched["data"]["undo_available"] is True

        written = await execute_tool(
            "file_write",
            json.dumps({"path": "demo/answer.py", "content": "print(sum(range(5)))", "overwrite": True}),
            ctx,
        )
        assert written["ok"] is True
        read = await execute_tool("file_read", json.dumps({"path": "demo/answer.py"}), ctx)
        assert read["data"]["content"] == "print(sum(range(5)))"
        executed = await execute_tool(
            "code_execute",
            json.dumps({"language": "python", "code": read["data"]["content"]}),
            ctx,
        )
        assert executed["data"]["exit_code"] == 0
        assert executed["data"]["stdout"].strip() == "10"

        submitted = await execute_tool(
            "submission_create",
            json.dumps({
                "task_id": task.id,
                "submission_type": "code",
                "content": "实现并验证并发抓取器的最小版本",
                "artifacts": [{"path": "demo/answer.py", "exit_code": 0}],
            }, ensure_ascii=False),
            ctx,
        )
        assert submitted["ok"] is True
        checked = await execute_tool(
            "submission_check",
            json.dumps({
                "submission_id": submitted["data"]["submission_id"],
                "score": 88,
                "feedback": "代码可以运行，证据满足本阶段要求。",
                "checks": [{"name": "python execution", "passed": True, "stdout": "10"}],
            }, ensure_ascii=False),
            ctx,
        )
        assert checked["data"]["status"] == "accepted"
        assert checked["data"]["task_status"] == "completed"

        calendar = await execute_tool(
            "calendar_create",
            json.dumps({
                "title": "异步编程复习",
                "starts_at": "2030-01-02T19:00:00+08:00",
                "ends_at": "2030-01-02T19:45:00+08:00",
                "plan_id": plan.id,
                "task_id": task.id,
            }, ensure_ascii=False),
            ctx,
        )
        assert calendar["ok"] is True
        listed = await execute_tool("calendar_list", json.dumps({"plan_id": plan.id}), ctx)
        assert listed["data"]["events"][0]["title"] == "异步编程复习"

        submission = await db.get(TaskSubmission, submitted["data"]["submission_id"])
        assert submission.score == 88
        refreshed = await plan_service.get_plan(db, "local", plan.id)
        assert refreshed.stages[0].tasks[1].status == "completed"


@pytest.mark.asyncio
async def test_layered_memory_retrieval_lifecycle_and_session_compression():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("Memory plan"))
        db.add_all([
            Memory(
                owner_id="local", scope="global", layer="semantic", status="confirmed",
                content="用户更喜欢通过编写 Python 项目学习异步编程", confidence=0.95,
            ),
            Memory(
                owner_id="local", scope="plan", scope_id=str(plan.id), layer="episodic", status="confirmed",
                content="并发抓取器任务的主要阻塞是超时处理", confidence=0.9,
            ),
            Memory(
                owner_id="local", scope="global", layer="short_term", status="confirmed",
                content="这条短期信息已经过期", expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
        ])
        session = Session(owner_id="local", plan_id=plan.id, title="Long session")
        db.add(session)
        await db.flush()
        db.add(Memory(
            owner_id="local", scope="session", scope_id=session.id, layer="episodic", status="confirmed",
            content="本次会话约定先处理连接超时，再优化并发数", confidence=0.98,
        ))
        for index in range(28):
            db.add(ChatMessage(session_id=session.id, role="user" if index % 2 == 0 else "assistant", content=f"第 {index} 轮：讨论并发抓取器和超时处理"))
        await db.commit()

        manager = MemoryManager(db)
        found = await manager.retrieve(
            "local", plan_id=plan.id, session_id=session.id, query="抓取器连接超时怎么办", limit=5,
        )
        assert any(item.scope == "session" for item in found)
        assert found[0].scope == "plan"
        without_session = await manager.retrieve("local", plan_id=plan.id, query="连接超时", limit=10)
        assert all(item.scope != "session" for item in without_session)
        maintained = await manager.maintain("local")
        assert maintained["expired"] == 1
        compressed = await manager.compress_session(session, client=None)
        await db.commit()
        assert compressed is True
        assert "历史对话压缩" in session.summary

        result = await db.execute(select(ChatMessage).where(ChatMessage.session_id == session.id))
        messages = list(result.scalars())
        assert len(messages) == 28
        assert sum(bool(item.message_metadata.get("included_in_summary")) for item in messages) == 12


@pytest.mark.asyncio
async def test_workspace_upload_enters_agent_file_scope():
    upload = UploadFile(filename="solution.py", file=io.BytesIO(b"print('ready')\n"))
    result = await upload_workspace_file(upload)
    assert result["path"].startswith("uploads/solution-")

    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="Inspect upload")
        db.add(run)
        await db.commit()
        read = await execute_tool(
            "file_read",
            json.dumps({"path": result["path"]}),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message"),
        )
        assert read["ok"] is True
        assert read["data"]["content"] == "print('ready')\n"


@pytest.mark.asyncio
async def test_session_navigation_and_manual_rename_are_session_based():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", plan_id=None, title="第一条很长的用户消息")
        db.add(session)
        await db.flush()
        first = AgentRun(owner_id="local", session_id=session.id, trigger="user_message", objective="第一问", status="completed")
        second = AgentRun(owner_id="local", session_id=session.id, trigger="user_message", objective="第二问", status="completed")
        db.add_all([first, second])
        await db.flush()
        db.add_all([
            ChatMessage(session_id=session.id, run_id=first.id, role="user", content="第一问"),
            ChatMessage(session_id=session.id, run_id=first.id, role="assistant", content="第一答"),
            ChatMessage(session_id=session.id, run_id=second.id, role="user", content="第二问"),
            ChatMessage(session_id=session.id, run_id=second.id, role="assistant", content="第二答"),
        ])
        await db.commit()

        rows = await list_sessions(30, db)
        row = next(item for item in rows if item["id"] == session.id)
        assert row["run_count"] == 2
        assert row["message_count"] == 4
        assert row["last_message"] == "第二答"

        renamed = await rename_session(session.id, SessionUpdate(title="Transformer 实战计划"), db)
        assert renamed["title"] == "Transformer 实战计划"


@pytest.mark.asyncio
async def test_sessions_and_plans_can_be_archived_and_restored():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("Archive lifecycle"))
        session = Session(owner_id="local", plan_id=plan.id, title="可归档对话")
        db.add(session)
        await db.commit()
        await db.refresh(session)

        archived_session = await rename_session(session.id, SessionUpdate(archived=True), db)
        assert archived_session["archived_at"] is not None
        assert all(row["id"] != session.id for row in await list_sessions(30, db))
        assert any(row["id"] == session.id for row in await list_sessions(30, db, archived=True))

        restored_session = await rename_session(session.id, SessionUpdate(archived=False), db)
        assert restored_session["archived_at"] is None

        archived_plan = await set_plan_archived(plan.id, PlanArchiveUpdate(archived=True), db)
        assert archived_plan.status == "archived"
        assert all(item.id != plan.id for item in await plan_service.list_plans(db, "local"))
        assert any(item.id == plan.id for item in await plan_service.list_plans(db, "local", archived=True))

        restored_plan = await set_plan_archived(plan.id, PlanArchiveUpdate(archived=False), db)
        assert restored_plan.status == "active"


@pytest.mark.asyncio
async def test_notifications_can_be_archived_in_bulk_and_restored():
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        read_message = Notification(
            owner_id="local",
            channel="in_app",
            title="已读提醒",
            body="可以批量归档",
            status="sent",
            read_at=now,
        )
        unread_message = Notification(
            owner_id="local",
            channel="in_app",
            title="未读提醒",
            body="不能被批量误归档",
            status="sent",
        )
        already_archived = Notification(
            owner_id="local",
            channel="in_app",
            title="历史提醒",
            body="保留在归档列表",
            status="sent",
            read_at=now,
            archived_at=now,
        )
        db.add_all([read_message, unread_message, already_archived])
        await db.commit()
        await db.refresh(read_message)
        await db.refresh(unread_message)

        result = await archive_read_notifications(db)
        assert result["archived"] == 1
        assert [item.id for item in await read_notifications(False, False, db)] == [unread_message.id]
        archived = await read_notifications(False, True, db)
        assert {item.id for item in archived} == {read_message.id, already_archived.id}

        restored = await set_notification_archived(
            read_message.id,
            NotificationArchiveUpdate(archived=False),
            db,
        )
        assert restored.archived_at is None
        assert {item.id for item in await read_notifications(False, False, db)} == {
            read_message.id,
            unread_message.id,
        }


@pytest.mark.asyncio
async def test_plan_handoff_preserves_provenance_and_context_boundaries():
    from app.context import ContextAssembler

    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("Context handoff"))
        source = Session(owner_id="local", title="讨论学习方向")
        db.add(source)
        await db.flush()
        db.add_all([
            ChatMessage(session_id=source.id, role="user", content="我想系统学习 asyncio"),
            ChatMessage(session_id=source.id, role="assistant", content="我们已经创建了一份计划。"),
        ])
        await db.commit()

        child = await handoff_session(source.id, SessionHandoffCreate(plan_id=plan.id), db)
        assert child["parent_session_id"] == source.id
        assert child["plan_id"] == plan.id
        assert "讨论学习方向" in child["handoff_summary"]

        links = list((await db.execute(select(SessionPlanLink).where(
            SessionPlanLink.session_id.in_([source.id, child["id"]])
        ))).scalars())
        assert {(link.session_id, link.relation_type) for link in links} == {
            (source.id, "discussed"),
            (child["id"], "focused"),
        }

        global_snapshot = await ContextAssembler(db).build("local", session_id=source.id, objective="继续计划")
        assert "## Plan index" in global_snapshot.markdown
        assert f"plan:{plan.id}" in global_snapshot.markdown
        assert "relation=discussed" in global_snapshot.markdown
        assert "## Saved learning resources" not in global_snapshot.markdown

        plan_snapshot = await ContextAssembler(db).build(
            "local", plan_id=plan.id, session_id=child["id"], objective="继续"
        )
        assert "Handoff from parent session" in plan_snapshot.markdown
        assert "讨论学习方向" in plan_snapshot.markdown


@pytest.mark.asyncio
async def test_email_reply_returns_to_notification_session(monkeypatch):
    from app.notifications.email import EmailReplyPoller

    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("Email continuity"))
        session = Session(owner_id="local", plan_id=plan.id, title="邮件连续对话")
        db.add(session)
        await db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            channel="email",
            title="今天继续吗",
            body="完成第一个任务",
            status="sent",
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)

        monkeypatch.setattr(EmailReplyPoller, "configured", property(lambda self: True))
        monkeypatch.setattr(EmailReplyPoller, "_fetch_unseen", lambda self: [{
            "reply_token": notification.reply_token,
            "subject": "Re: 今天继续吗",
            "body": "我已经完成了，请检查。",
        }])
        run_ids = await EmailReplyPoller().poll(db, "local")

        run = await db.get(AgentRun, run_ids[0])
        assert run.session_id == session.id
        message = (await db.execute(select(ChatMessage).where(ChatMessage.run_id == run.id))).scalars().one()
        assert message.content == "我已经完成了，请检查。"
        assert message.message_metadata["channel"] == "email"


@pytest.mark.asyncio
async def test_email_diagnostics_exercise_smtp_and_imap(monkeypatch):
    import app.notifications.diagnostics as diagnostics

    for name, value in {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USERNAME": "learner@example.com",
        "SMTP_PASSWORD": "app-password",
        "SMTP_TO": "learner@example.com",
        "IMAP_HOST": "imap.example.com",
        "IMAP_USERNAME": "learner@example.com",
        "IMAP_PASSWORD": "app-password",
        "ENABLE_EMAIL_REPLY_POLLING": True,
    }.items():
        monkeypatch.setattr(diagnostics.settings, name, value)

    smtp_calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            smtp_calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def starttls(self):
            smtp_calls.append(("starttls",))

        def login(self, username, password):
            smtp_calls.append(("login", username, password))

        def send_message(self, message):
            smtp_calls.append(("send", message["To"]))

    class FakeSMTPSSL(FakeSMTP):
        def __init__(self, host, port, timeout):
            smtp_calls.append(("ssl_connect", host, port, timeout))

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            self.host = host

        def login(self, username, password):
            return "OK", []

        def select(self, folder, readonly=False):
            return "OK", [b"7"]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(diagnostics.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(diagnostics.smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(diagnostics.imaplib, "IMAP4_SSL", FakeIMAP)

    smtp_result = await diagnostics.test_smtp(send_message=True)
    imap_result = await diagnostics.test_imap()
    assert smtp_result["message_sent"] is True
    assert ("send", "learner@example.com") in smtp_calls
    assert imap_result["message_count"] == 7

    monkeypatch.setattr(diagnostics.settings, "SMTP_USE_SSL", True)
    monkeypatch.setattr(diagnostics.settings, "SMTP_USE_TLS", False)
    await diagnostics.test_smtp()
    assert ("ssl_connect", "smtp.example.com", diagnostics.settings.SMTP_PORT, 20) in smtp_calls


@pytest.mark.asyncio
async def test_redirect_targets_are_validated_on_every_hop(monkeypatch):
    import httpx
    import app.search.security as security

    checked = []

    async def validate(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise ValueError("Private or reserved network targets are not allowed")

    async def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"}, request=request)

    monkeypatch.setattr(security, "validate_public_url", validate)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Private or reserved"):
            await fetch_with_safe_redirects(client, "https://example.com/start")
    assert checked == ["https://example.com/start", "http://127.0.0.1/private"]


@pytest.mark.asyncio
async def test_fake_ip_dns_is_allowed_only_for_domain_names(monkeypatch):
    import socket
    import app.search.security as security

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.4.11", port))]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    await security.validate_public_url("https://html.duckduckgo.com/html/")

    with pytest.raises(ValueError, match="IP literals"):
        await security.validate_public_url("https://198.18.4.11/")

    def fake_ipv6_getaddrinfo(host, port):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001::c085:4dbd", port, 0, 0))]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_ipv6_getaddrinfo)
    await security.validate_public_url("https://html.duckduckgo.com/html/")

    with pytest.raises(ValueError, match="IP literals"):
        await security.validate_public_url("https://[2001::c085:4dbd]/")


def test_tool_failure_guard_opens_after_repeated_failures():
    guard = ToolFailureGuard(failure_limit=2)
    first = guard.observe("web_search", {"ok": False, "error": "network failed"})
    second = guard.observe("web_search", {"ok": False, "error": "network failed again"})

    assert "circuit_open" not in first
    assert second["circuit_open"] is True
    assert guard.before_call("web_search")["circuit_open"] is True
    assert guard.before_call("plan_get") is None


@pytest.mark.asyncio
async def test_duckduckgo_provider_filters_ads_and_deduplicates(monkeypatch):
    import httpx
    import app.search.providers as providers

    html = """
    <a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=example.com">广告</a>
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">Python asyncio</a>
    <a class="result__a" href="https://docs.python.org/3/library/asyncio.html">Python asyncio duplicate</a>
    """

    async def fetch(client, url, *, params=None):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text=html, request=request), 0

    monkeypatch.setattr(providers, "fetch_with_safe_redirects", fetch)
    results = await DuckDuckGoSearchProvider().search("asyncio", 5)
    assert [result.url for result in results] == ["https://docs.python.org/3/library/asyncio.html"]


@pytest.mark.asyncio
async def test_curated_resource_is_structured_listed_in_context_and_undoable(monkeypatch):
    import app.tools.web as web_tools
    from app.context import ContextAssembler

    async def allow_public_url(_):
        return None

    monkeypatch.setattr(web_tools, "validate_public_url", allow_public_url)
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("Curated resources"))
        run = AgentRun(owner_id="local", plan_id=plan.id, trigger="user_message", objective="找一门课程")
        legacy_ad = LearningResource(
            owner_id="local",
            plan_id=plan.id,
            title="Sponsored search result",
            url="https://duckduckgo.com/y.js?ad_domain=example.com",
            source="web_search",
        )
        db.add_all([run, legacy_ad])
        await db.commit()
        await db.refresh(run)
        result = await execute_tool(
            "resource_save",
            json.dumps({
                "plan_id": plan.id,
                "title": "Hugging Face LLM Course",
                "url": "https://huggingface.co/learn/llm-course/chapter1/1",
                "resource_type": "course",
                "provider": "Hugging Face",
                "language": "English",
                "difficulty": "intermediate",
                "summary": "A structured course covering transformer and LLM foundations with exercises.",
                "why_recommended": "Matches the plan's current implementation task and includes hands-on practice.",
            }),
            ToolContext(db=db, owner_id="local", run_id=run.id, trigger="user_message", plan_id=plan.id),
        )
        assert result["ok"] is True
        rows = await read_plan_resources(plan.id, db)
        assert len(rows) == 1
        assert rows[0].provider == "Hugging Face"
        assert rows[0].resource_type == "course"
        assert rows[0].verified_at is not None

        snapshot = await ContextAssembler(db).build("local", plan_id=plan.id, objective="现在该学什么")
        assert "Hugging Face LLM Course" in snapshot.markdown
        assert "hands-on practice" in snapshot.markdown
        assert "Sponsored search result" not in snapshot.markdown

        await undo_operation(result["data"]["operation_id"], db)
        curated = (await db.execute(
            select(LearningResource).where(LearningResource.url.like("%huggingface.co/%"))
        )).scalars().first()
        assert curated is None


class RecoveringCompletions:
    def __init__(self, *, timeout_first=False):
        self.timeout_first = timeout_first
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.timeout_first and self.calls == 1:
            raise TimeoutError
        if self.calls == (2 if self.timeout_first else 1):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="我先检查复习安排。",
                reasoning_content=None,
                tool_calls=None if self.timeout_first else [FakeToolCall("bad-review", "review_schedule", "{}")],
            ))])
        if not self.timeout_first and self.calls == 2:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="复习工具参数不完整，我没有写入错误安排。",
                reasoning_content=None,
                tool_calls=None,
            ))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="复习安排检查",
            reasoning_content=None,
            tool_calls=None,
        ))])


@pytest.mark.asyncio
async def test_tool_failure_isolated_from_runtime_database_session():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="检查复习安排")
        db.add(run)
        await db.commit()
        run_id = run.id

    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=RecoveringCompletions()))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        assert run.status == "completed"
        failed_tool = next(event for event in events if event.event_type == "tool.completed")
        assert failed_tool.payload["result"]["ok"] is False
        assert all(event.event_type != "run.failed" for event in events)


@pytest.mark.asyncio
async def test_model_timeout_retries_without_losing_conversation():
    async with AsyncSessionLocal() as db:
        run = AgentRun(owner_id="local", trigger="user_message", objective="给我一个今日建议")
        db.add(run)
        await db.commit()
        run_id = run.id

    completions = RecoveringCompletions(timeout_first=True)
    runtime = AgentRuntime()
    runtime.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    await runtime.run(run_id)

    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        events = list((await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars())
        assert run.status == "completed"
        assert any(event.event_type == "run.retrying" for event in events)
        messages = await read_session_messages(run.session_id, db)
        assert messages[-1].content == "我先检查复习安排。"


@pytest.mark.asyncio
async def test_planning_intake_requires_readiness_and_proposal_accept_is_idempotent():
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="共创 Python 计划")
        db.add(session)
        await db.flush()
        run = AgentRun(owner_id="local", session_id=session.id, trigger="user_message", objective="我想学 Python")
        db.add(run)
        await db.commit()
        await db.refresh(session)
        await db.refresh(run)
        ctx = ToolContext(
            db=db,
            owner_id="local",
            run_id=run.id,
            trigger="user_message",
            session_id=session.id,
        )

        collecting = await execute_tool(
            "planning_intake_update",
            json.dumps({
                "goal": "系统学习 Python",
                "confirmed_facts": [{"key": "目标", "value": "能写自动化脚本", "source": "user"}],
                "open_questions": [{
                    "id": "weekly_time",
                    "prompt": "每周能投入多少时间？",
                    "why": "决定计划节奏",
                    "options": ["3 小时", "5 小时"],
                    "allow_custom": True,
                }],
                "readiness": "collecting",
                "readiness_confidence": 0.55,
                "rationale": "还缺少时间约束",
            }, ensure_ascii=False),
            ctx,
        )
        assert collecting["ok"] is True
        assert collecting["data"]["open_questions"][0]["id"] == "weekly_time"

        blocked = await execute_tool(
            "plan_proposal_create",
            json.dumps({"plan": plan_payload("不能创建").model_dump(mode="json"), "rationale": "信息还不够"}),
            ctx,
        )
        assert blocked["ok"] is False

        ready = await execute_tool(
            "planning_intake_update",
            json.dumps({
                "goal": "系统学习 Python 并交付自动化项目",
                "confirmed_facts": [
                    {"key": "目标", "value": "完成可运行自动化项目", "source": "user"},
                    {"key": "每周时间", "value": "5 小时", "source": "user"},
                    {"key": "当前基础", "value": "会基础语法", "source": "user"},
                ],
                "open_questions": [],
                "readiness": "ready",
                "readiness_confidence": 0.92,
                "rationale": "目标、基础、时间和产出足以制定可执行计划",
            }, ensure_ascii=False),
            ctx,
        )
        assert ready["data"]["readiness"] == "ready"
        proposed = await execute_tool(
            "plan_proposal_create",
            json.dumps({
                "plan": json.dumps(
                    plan_payload("Python 自动化共创计划").model_dump(mode="json"),
                    ensure_ascii=False,
                ),
                "rationale": "根据已确认约束安排一个核心项目",
            }, ensure_ascii=False),
            ctx,
        )
        assert proposed["ok"] is True
        proposal_id = proposed["data"]["proposal_id"]
        state = await read_planning_state(session.id, db)
        assert state["intake"].readiness == "ready"
        assert state["proposal"].id == proposal_id

        first = await decide_plan_proposal(proposal_id, PlanProposalDecision(accepted=True), db)
        second = await decide_plan_proposal(proposal_id, PlanProposalDecision(accepted=True), db)
        assert first.plan_id == second.plan_id
        assert first.status == "accepted"
        plans = list((await db.execute(select(Plan).where(Plan.owner_id == "local"))).scalars())
        assert len(plans) == 1
        links = list((await db.execute(select(SessionPlanLink).where(
            SessionPlanLink.session_id == session.id,
            SessionPlanLink.relation_type == "created",
        ))).scalars())
        assert [link.plan_id for link in links] == [first.plan_id]


@pytest.mark.asyncio
async def test_message_edit_preserves_revision_and_excludes_superseded_tail(monkeypatch):
    import app.api.agent as agent_api
    from app.context import ContextAssembler

    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="可修订会话", summary="旧摘要")
        db.add(session)
        await db.flush()
        old_run = AgentRun(
            owner_id="local", session_id=session.id, trigger="user_message",
            objective="学习旧主题", status="completed",
        )
        db.add(old_run)
        await db.flush()
        user = ChatMessage(session_id=session.id, run_id=old_run.id, role="user", content="学习旧主题")
        assistant = ChatMessage(session_id=session.id, run_id=old_run.id, role="assistant", content="旧主题回答")
        derived_memory = Memory(
            owner_id="local", scope="session", scope_id=session.id, layer="semantic",
            content="用户决定学习旧主题", source_type="agent_run", source_id=old_run.id,
            status="confirmed",
        )
        db.add_all([user, assistant, derived_memory])
        await db.commit()
        await db.refresh(user)

        rerun = await edit_user_message(user.id, MessageEdit(content="学习新的 asyncio 主题"), db)
        assert rerun.session_id == session.id
        assert rerun.objective == "学习新的 asyncio 主题"
        visible = await read_session_messages(session.id, db)
        assert [(item.role, item.content) for item in visible] == [("user", "学习新的 asyncio 主题")]
        revision = (await db.execute(select(ChatMessageRevision).where(
            ChatMessageRevision.message_id == user.id
        ))).scalars().one()
        assert revision.content == "学习旧主题"
        stale = await db.get(ChatMessage, assistant.id)
        assert stale.message_metadata["superseded_by_edit"]
        await db.refresh(derived_memory)
        assert derived_memory.status == "archived"
        snapshot = await ContextAssembler(db).build(
            "local", session_id=session.id, run_id=rerun.id, objective=rerun.objective,
        )
        assert "学习新的 asyncio 主题" in snapshot.markdown
        assert "旧主题回答" not in snapshot.markdown


@pytest.mark.asyncio
async def test_study_state_selects_first_pending_task_and_tracks_evidence():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload("学习位置测试"))
        run = AgentRun(
            owner_id="local", plan_id=plan.id, trigger="user_message", objective="我现在做到哪了"
        )
        db.add(run)
        await db.commit()
        result = await execute_tool(
            "study_state_get",
            json.dumps({"plan_id": plan.id}),
            ToolContext(
                db=db, owner_id="local", run_id=run.id,
                trigger="user_message", plan_id=plan.id,
            ),
        )
        assert result["ok"] is True
        assert result["data"]["current_task"]["title"] == "Read event-loop guide"
        assert result["data"]["recommended_next"]["id"] == plan.stages[0].tasks[0].id
        assert result["data"]["counts"]["core_evidence_pending"] == 1
        assert result["data"]["plan_version"] == plan.version


@pytest.mark.asyncio
async def test_planning_delegate_creates_joined_child_runs(monkeypatch):
    import app.tools.planning as planning_tools

    class SpecialistCompletions:
        async def create(self, **kwargs):
            role_line = kwargs["messages"][1]["content"].splitlines()[0]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=f"{role_line}：建议保留一个核心交付和一次阶段考核。"
            ))])

    class SpecialistClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SpecialistCompletions())

    monkeypatch.setattr(planning_tools, "AsyncOpenAI", SpecialistClient)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="子 Agent 规划")
        db.add(session)
        await db.flush()
        parent = AgentRun(
            owner_id="local", session_id=session.id, trigger="user_message",
            objective="帮我制定计划", status="running",
        )
        db.add(parent)
        await db.commit()
        result = await execute_tool(
            "planning_delegate",
            json.dumps({"assignments": [
                {"role": "课程设计", "objective": "拆解阶段和依赖"},
                {"role": "考核审查", "objective": "检查证据与可执行性"},
            ]}, ensure_ascii=False),
            ToolContext(
                db=db, owner_id="local", run_id=parent.id, trigger="user_message",
                session_id=session.id,
            ),
        )
        assert result["ok"] is True
        assert len(result["data"]["reports"]) == 2
        children = list((await db.execute(select(AgentRun).where(
            AgentRun.parent_run_id == parent.id
        ))).scalars())
        assert {child.status for child in children} == {"completed"}
        assert all(child.trigger == "subagent" for child in children)
        assert all(child.session_id == session.id for child in children)
        assert not list((await db.execute(select(ChatMessage).where(
            ChatMessage.run_id.in_([child.id for child in children])
        ))).scalars())
