"""Create the deterministic, public-data-only fixture for the H7 browser gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.database import AsyncSessionLocal, create_schema, engine  # noqa: E402
from app.db.uow import commit as commit_uow, flush as flush_uow  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    ChatMessage,
    LearningResource,
    Owner,
    Plan,
    RunEvent,
    Session,
    Stage,
    Task,
    UserProfile,
)
from app.notifications.service import NotificationService  # noqa: E402
from app.services.competencies import create_competency, link_competency  # noqa: E402
from app.services.evidence import append_observation  # noqa: E402


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,80}$")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def seed(token: str) -> None:
    await create_schema(state_root=PROJECT_ROOT)
    async with AsyncSessionLocal() as db:
        if await db.get(Owner, "local") is not None:
            raise RuntimeError("H7 browser fixture database must start empty")
        db.add(Owner(id="local", display_name="Browser Learner", timezone="Asia/Shanghai"))
        db.add(UserProfile(owner_id="local", xp=420, level=4, streak_days=7))

        plan = Plan(
            owner_id="local",
            title=f"H7 可解释学习计划 {token}",
            goal="能够说明当前任务训练什么、证明什么，以及判断所依据的事实。",
            current_level="已了解基本概念",
            weekly_minutes=180,
            expected_outcome="一份可复现、可审计的事务实验",
            status="active",
            progress=0.5,
        )
        stage = Stage(
            title="从理解到证明",
            description="先建立概念，再用独立实验验证。",
            position=0,
            status="active",
        )
        task = Task(
            title="编写并解释原子回滚实验",
            description="构造中途失败，证明所有写入全部提交或全部回滚。",
            kind="project",
            status="active",
            is_core=True,
            evidence_required=True,
            estimated_minutes=45,
            position=0,
            due_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        stage.tasks.append(task)
        plan.stages.append(stage)
        db.add(plan)
        await flush_uow(db)

        alpha = Session(
            owner_id="local",
            plan_id=plan.id,
            title=f"H7 synthetic alpha {token}",
        )
        beta = Session(
            owner_id="local",
            title=f"H7 synthetic beta {token}",
        )
        db.add_all([alpha, beta])
        await flush_uow(db)

        long_url = "https://example.test/learning/" + ("long-segment-" * 16)
        for index in range(100):
            role = "user" if index % 2 == 0 else "assistant"
            content = (
                f"合成消息 {index + 1}：用于 H7 长会话布局验证。\n\n"
                f"| 字段 | 值 |\n| --- | --- |\n| 次序 | {index + 1} |\n\n"
                f"```python\nassert {index + 1} > 0\n```\n\n{long_url}"
                if index in {0, 98, 99}
                else f"合成消息 {index + 1}：中英文 mixed content for deterministic layout."
            )
            db.add(ChatMessage(
                session_id=alpha.id,
                role=role,
                content=content,
                version=1,
                content_hash=content_hash(content),
                message_metadata={"fixture": "h7", "ordinal": index + 1},
            ))

        completed_at = datetime.now(timezone.utc)
        completed_run = AgentRun(
            id="h7-completed-run",
            owner_id="local",
            session_id=alpha.id,
            plan_id=plan.id,
            trigger="user_message",
            objective="核对事务实验并给出可追溯反馈",
            status="completed",
            phase="terminal",
            state_version=1,
            model="fixture-model",
            output="实验通过；工具成功与失败边界均已记录。",
            started_at=completed_at - timedelta(seconds=5),
            completed_at=completed_at,
        )
        db.add(completed_run)
        await flush_uow(db)
        run_question = "请核对事务实验，并解释一次预期失败。"
        run_answer = "实验通过。预期失败没有留下部分写入，证据和工具记录可展开查看。"
        db.add_all([
            ChatMessage(
                session_id=alpha.id,
                run_id=completed_run.id,
                message_key="h7-run-user",
                role="user",
                content=run_question,
                version=1,
                content_hash=content_hash(run_question),
                message_metadata={"fixture": "h7"},
            ),
            ChatMessage(
                session_id=alpha.id,
                run_id=completed_run.id,
                message_key="h7-run-assistant",
                role="assistant",
                content=run_answer,
                version=1,
                content_hash=content_hash(run_answer),
                message_metadata={"fixture": "h7", "cards": []},
            ),
        ])
        for sequence, event_type, summary, payload in [
            (1, "run.started", "开始核对公开合成实验", {}),
            (2, "tool.started", "读取事务实验", {"tool_name": "file_read"}),
            (3, "tool.completed", "实验文件读取成功", {"result": {"ok": True}}),
            (4, "tool.started", "执行预期失败分支", {"tool_name": "submission_check"}),
            (5, "tool.completed", "预期失败已隔离", {"result": {"ok": False, "expected": True}}),
            (6, "subagent.started", "审计子 Agent 开始", {"child_run_id": "fixture-child", "role": "审计"}),
            (7, "subagent.completed", "审计子 Agent 完成", {"child_run_id": "fixture-child"}),
            (8, "run.completed", run_answer, {}),
        ]:
            db.add(RunEvent(
                run_id=completed_run.id,
                sequence=sequence,
                event_type=event_type,
                event_key=f"h7-event-{sequence}",
                summary=summary,
                payload=payload,
            ))

        db.add(LearningResource(
            owner_id="local",
            plan_id=plan.id,
            title="SQLite transactions",
            url="https://www.sqlite.org/lang_transaction.html",
            resource_type="documentation",
            provider="SQLite",
            language="en",
            difficulty="intermediate",
            summary="事务与锁的权威参考。",
            why_recommended="用于核对实验观察与数据库语义。",
            source="h7_fixture",
            verified_at=datetime.now(timezone.utc),
        ))

        competency, _ = await create_competency(
            db,
            "local",
            key="transaction-atomicity",
            title="事务原子性",
            description="识别部分提交并设计回滚边界。",
            competency_type="concept",
            scope="plan",
            plan_id=plan.id,
            focused_plan_id=plan.id,
        )
        await link_competency(
            db,
            "local",
            competency_id=competency.id,
            task_id=task.id,
            relation="teaches",
            target_stage="practicing",
            focused_plan_id=plan.id,
        )
        await link_competency(
            db,
            "local",
            competency_id=competency.id,
            task_id=task.id,
            relation="assesses",
            target_stage="demonstrated",
            focused_plan_id=plan.id,
        )
        await append_observation(
            db,
            owner_id="local",
            source_type="self_report",
            source_id=f"h7-browser-{token}",
            outcome="completed",
            idempotency_key=f"h7-browser-{token}",
            plan_id=plan.id,
            task_id=task.id,
            normalized_score=0.8,
            payload={"feedback": "公开合成 Evidence，用于浏览器只读投影。"},
        )
        await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=beta.id,
            trigger="manual_heartbeat",
            title="H7 合成复习提醒",
            body="请回复这条公开合成提醒。",
            plan_id=None,
            channels=["in_app"],
        )
        await commit_uow(db)
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("token")
    args = parser.parse_args()
    if not TOKEN_PATTERN.fullmatch(args.token):
        parser.error("token must be 16-80 URL-safe characters")
    asyncio.run(seed(args.token))


if __name__ == "__main__":
    main()
