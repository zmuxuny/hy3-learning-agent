import copy
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from app.api.agent import create_run, list_sessions, read_session_messages, rename_session
from app.api.workspace import upload_workspace_file
from app.db.database import AsyncSessionLocal
from app.api.operations import undo_operation
from app.context.memory import MemoryManager
from app.models import AgentRun, ChatMessage, Memory, Notification, RunEvent, Session, TaskSubmission
from app.runtime.agent import AgentRuntime, ToolFailureGuard
from app.schemas import AgentRunCreate, PlanCreate, SessionUpdate, StageCreate, TaskCreate, TaskUpdate
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
async def test_core_evidence_progress_and_undo():
    async with AsyncSessionLocal() as db:
        plan = await plan_service.create_plan(db, "local", plan_payload())
        normal, core = plan.stages[0].tasks

        run = AgentRun(owner_id="local", trigger="user_message", objective="Update my progress")
        db.add(run)
        await db.commit()

        result = await execute_tool(
            "task_patch",
            json.dumps({"task_id": normal.id, "changes": {"status": "completed"}, "reason": "User finished it"}),
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
    assert len(tool_names) == 31
    contracts = tool_contracts()
    assert len(contracts) == 31
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
        "file_read",
        "file_write",
        "code_execute",
        "calendar_list",
        "calendar_create",
    }.issubset(tool_names)
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
