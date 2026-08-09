import pytest
from sqlalchemy import select

from app.context.assembler import ContextAssembler
from app.db.database import AsyncSessionLocal
from app.models import AgentRun, ChatMessage, Notification, Plan, Session
from app.notifications.conversation import open_notification_in_conversation
from app.notifications.service import NotificationService
from app.schemas import AgentRunCreate


@pytest.mark.asyncio
async def test_proactive_notifications_reuse_one_plan_session_and_become_messages():
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="蛙泳入门", goal="完成基础蛙泳训练")
        db.add(plan)
        await db.commit()
        await db.refresh(plan)

        first = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="manual_heartbeat",
            title="进度确认",
            body="已经五天没有新的学习证据，现在进展如何？",
            plan_id=plan.id,
            channels=["in_app"],
        )
        second = await NotificationService(db).send(
            owner_id="local",
            run_id=None,
            session_id=None,
            trigger="manual_heartbeat",
            title="复习安排",
            body="今天适合复习蛙泳腿动作。",
            plan_id=plan.id,
            channels=["in_app"],
        )

        assert first["session_id"] == second["session_id"]
        session = await db.get(Session, first["session_id"])
        assert session.plan_id == plan.id
        messages = list((await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.id)
        )).scalars())
        assert [message.content for message in messages] == [
            "已经五天没有新的学习证据，现在进展如何？",
            "今天适合复习蛙泳腿动作。",
        ]
        assert all(message.message_metadata["ui_kind"] == "proactive_notification" for message in messages)


@pytest.mark.asyncio
async def test_opening_legacy_notification_repairs_thread_once_and_context_does_not_duplicate_it():
    async with AsyncSessionLocal() as db:
        plan = Plan(owner_id="local", title="旧计划", goal="继续学习")
        db.add(plan)
        await db.flush()
        notification = Notification(
            owner_id="local",
            plan_id=plan.id,
            channel="in_app",
            title="旧提醒",
            body="请告诉我目前的阻塞。",
            status="sent",
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)

        session, first_message, _ = await open_notification_in_conversation(db, notification)
        _, second_message, _ = await open_notification_in_conversation(db, notification)
        await db.commit()

        assert notification.session_id == session.id
        assert first_message.id == second_message.id
        run = AgentRun(
            owner_id="local",
            session_id=session.id,
            plan_id=plan.id,
            trigger="user_message",
            objective="我卡在练习环境，想调整本周任务。",
        )
        db.add(run)
        await db.flush()
        db.add(ChatMessage(
            session_id=session.id,
            run_id=run.id,
            role="user",
            content="我卡在练习环境，想调整本周任务。",
            message_metadata={"reply_to_notification_id": notification.id},
        ))
        await db.commit()
        snapshot = await ContextAssembler(db).build(
            "local",
            plan_id=plan.id,
            session_id=session.id,
            run_id=run.id,
            objective="回复这条提醒",
        )
        assert f"Reply target notification:{notification.id} — 旧提醒: 请告诉我目前的阻塞。" in snapshot.markdown
        assert f"- user [replying to notification:{notification.id}]: 我卡在练习环境，想调整本周任务。" in snapshot.markdown
        assert f"Plan: {plan.title} (plan:{plan.id}" in snapshot.markdown
        assert snapshot.markdown.count("请告诉我目前的阻塞。") == 1


@pytest.mark.asyncio
async def test_run_reply_persists_explicit_notification_target(monkeypatch):
    from app.api import agent as agent_api

    monkeypatch.setattr(agent_api, "_start_runtime", lambda _run_id: None)
    async with AsyncSessionLocal() as db:
        session = Session(owner_id="local", title="跟进对话")
        db.add(session)
        await db.flush()
        notification = Notification(
            owner_id="local",
            session_id=session.id,
            channel="in_app",
            title="学习提醒",
            body="今天是否遇到阻塞？",
            status="sent",
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)

        run = await agent_api.create_run(
            AgentRunCreate(
                objective="我需要重新安排时间。",
                session_id=session.id,
                reply_to_notification_id=notification.id,
            ),
            db,
        )
        message = (await db.execute(
            select(ChatMessage).where(ChatMessage.run_id == run.id, ChatMessage.role == "user")
        )).scalars().one()
        assert message.message_metadata == {"reply_to_notification_id": notification.id}
