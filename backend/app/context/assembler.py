from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import PROJECT_ROOT, settings
from app.models import (
    CalendarEvent,
    ChatMessage,
    ContextSnapshot,
    LearningEvent,
    LearningResource,
    Notification,
    Plan,
    Quiz,
    ReviewSchedule,
    Session,
    Stage,
    TaskSubmission,
    UserProfile,
)
from app.context.memory import MemoryManager, search_terms


class ContextAssembler:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(
        self,
        owner_id: str,
        *,
        plan_id: int | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        objective: str = "",
    ) -> ContextSnapshot:
        manifest: list[dict] = []
        sections = ["# Agent Context", f"Generated: {datetime.now(timezone.utc).isoformat()}"]

        profile = await self.db.get(UserProfile, owner_id)
        if profile:
            sections.extend(
                [
                    "## Global learner profile",
                    f"- Agent style: {profile.agent_style}",
                    f"- Preferences: {profile.preferences}",
                    f"- Quiet hours: {profile.quiet_hours}",
                    f"- Level: {profile.level}; XP: {profile.xp}; streak: {profile.streak_days}",
                ]
            )
            manifest.append({"type": "profile", "id": owner_id})

        memory_manager = MemoryManager(self.db)
        await memory_manager.maintain(owner_id)
        relevant_memories = await memory_manager.retrieve(
            owner_id,
            plan_id=plan_id,
            session_id=session_id,
            query=objective,
            limit=24,
        )
        if relevant_memories:
            sections.append("## Confirmed memory")
            for memory in relevant_memories:
                sections.append(f"- [{memory.layer}/{memory.scope}] {memory.content} (memory:{memory.id})")
                manifest.append({"type": "memory", "id": memory.id})

        if plan_id is not None:
            plan_result = await self.db.execute(
                select(Plan)
                .where(Plan.id == plan_id, Plan.owner_id == owner_id)
                .options(selectinload(Plan.stages).selectinload(Stage.tasks))
            )
            plan = plan_result.scalars().unique().one_or_none()
            if plan:
                sections.extend(
                    [
                        "## Active plan",
                        f"- Plan: {plan.title} (plan:{plan.id}, version:{plan.version})",
                        f"- Goal: {plan.goal}",
                        f"- Current level: {plan.current_level}",
                        f"- Deadline: {plan.deadline}",
                        f"- Progress: {plan.progress:.0%}",
                        f"- Plan memory: {plan.memory_summary or '(empty)'}",
                    ]
                )
                for stage in plan.stages:
                    sections.append(f"### {stage.title} [{stage.status}]")
                    for task in stage.tasks:
                        sections.append(
                            f"- task:{task.id} [{task.status}] {task.title}; due={task.due_at}; review={task.review_due_at}; core={task.is_core}"
                        )
                manifest.append({"type": "plan", "id": plan.id, "version": plan.version})

        event_query = select(LearningEvent).where(LearningEvent.owner_id == owner_id)
        if plan_id is not None:
            event_query = event_query.where(LearningEvent.plan_id == plan_id)
        event_query = event_query.order_by(LearningEvent.created_at.desc()).limit(settings.AGENT_CONTEXT_EVENT_LIMIT * 3)
        events = list((await self.db.execute(event_query)).scalars())
        if objective:
            terms = search_terms(objective)
            events.sort(
                key=lambda event: len(terms.intersection(search_terms(f"{event.event_type} {event.summary}"))),
                reverse=True,
            )
        events = events[: settings.AGENT_CONTEXT_EVENT_LIMIT]
        if events:
            sections.append("## Recent learning events")
            for event in reversed(events):
                sections.append(f"- {event.created_at}: {event.event_type} — {event.summary} (event:{event.id})")
                manifest.append({"type": "learning_event", "id": event.id})

        review_query = select(ReviewSchedule).where(
            ReviewSchedule.owner_id == owner_id,
            ReviewSchedule.status == "scheduled",
        )
        quiz_query = select(Quiz).where(Quiz.owner_id == owner_id, Quiz.status == "open")
        notification_query = select(Notification).where(Notification.owner_id == owner_id)
        if plan_id is not None:
            review_query = review_query.where(ReviewSchedule.plan_id == plan_id)
            quiz_query = quiz_query.where(Quiz.plan_id == plan_id)
            notification_query = notification_query.where(Notification.plan_id == plan_id)
        reviews = list((await self.db.execute(review_query.order_by(ReviewSchedule.due_at).limit(20))).scalars())
        quizzes = list((await self.db.execute(quiz_query.order_by(Quiz.created_at.desc()).limit(10))).scalars())
        notifications = list(
            (await self.db.execute(notification_query.order_by(Notification.created_at.desc()).limit(10))).scalars()
        )
        if reviews or quizzes:
            sections.append("## Actionable learning state")
            for review in reviews:
                sections.append(
                    f"- review:{review.id} due={review.due_at}; plan={review.plan_id}; task={review.task_id}; type={review.review_type}"
                )
                manifest.append({"type": "review", "id": review.id})
            for quiz in quizzes:
                sections.append(
                    f"- quiz:{quiz.id} [open] plan={quiz.plan_id}; task={quiz.task_id}; prompt={quiz.prompt}"
                )
                manifest.append({"type": "quiz", "id": quiz.id})
        if notifications:
            sections.append("## Recent notifications")
            for notification in reversed(notifications):
                sections.append(
                    f"- {notification.created_at}: [{notification.channel}/{notification.status}] {notification.title} — {notification.body}"
                )
                manifest.append({"type": "notification", "id": notification.id})

        resource_query = select(LearningResource).where(LearningResource.owner_id == owner_id)
        submission_query = select(TaskSubmission).where(TaskSubmission.owner_id == owner_id)
        calendar_query = select(CalendarEvent).where(CalendarEvent.owner_id == owner_id, CalendarEvent.status == "scheduled")
        if plan_id is not None:
            resource_query = resource_query.where(LearningResource.plan_id == plan_id)
            submission_query = submission_query.where(TaskSubmission.plan_id == plan_id)
            calendar_query = calendar_query.where(CalendarEvent.plan_id == plan_id)
        resources = list((await self.db.execute(resource_query.order_by(LearningResource.created_at.desc()).limit(12))).scalars())
        submissions = list((await self.db.execute(submission_query.order_by(TaskSubmission.created_at.desc()).limit(12))).scalars())
        calendar_events = list((await self.db.execute(calendar_query.order_by(CalendarEvent.starts_at).limit(20))).scalars())
        if resources:
            sections.append("## Saved learning resources")
            for resource in resources:
                sections.append(f"- resource:{resource.id} {resource.title} — {resource.url}")
                manifest.append({"type": "resource", "id": resource.id})
        if submissions:
            sections.append("## Recent task submissions")
            for submission in reversed(submissions):
                sections.append(
                    f"- submission:{submission.id} task={submission.task_id} status={submission.status} score={submission.score}; {submission.content[:240]}"
                )
                manifest.append({"type": "submission", "id": submission.id})
        if calendar_events:
            sections.append("## Study calendar")
            for event in calendar_events:
                sections.append(f"- calendar:{event.id} {event.starts_at} — {event.title} [{event.status}]")
                manifest.append({"type": "calendar_event", "id": event.id})

        if session_id:
            session = await self.db.get(Session, session_id)
            if session and session.owner_id == owner_id:
                message_query = (
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at.desc())
                    .limit(settings.AGENT_RECENT_MESSAGE_LIMIT)
                )
                messages = list(reversed(list((await self.db.execute(message_query)).scalars())))
                sections.extend(["## Conversation", f"Session summary: {session.summary or '(empty)'}"])
                for message in messages:
                    sections.append(f"- {message.role}: {message.content}")
                    manifest.append({"type": "message", "id": message.id})

        markdown = "\n".join(sections).strip() + "\n"
        max_chars = settings.AGENT_CONTEXT_TOKEN_BUDGET * 4
        if len(markdown) > max_chars:
            head_limit = int(max_chars * 0.68)
            tail_limit = max_chars - head_limit
            head = markdown[:head_limit].rsplit("\n", 1)[0]
            tail = markdown[-tail_limit:].split("\n", 1)[-1]
            markdown = f"{head}\n\n[Lower-priority context compacted at configured token budget]\n\n{tail}"
        snapshot = ContextSnapshot(
            owner_id=owner_id,
            plan_id=plan_id,
            run_id=run_id,
            markdown=markdown,
            source_manifest=manifest,
            estimated_tokens=max(1, len(markdown) // 4),
        )
        self.db.add(snapshot)
        await self.db.flush()

        context_root = PROJECT_ROOT / "data" / "context"
        if plan_id is None:
            path = context_root / "global.md"
        else:
            path = context_root / "plans" / f"{plan_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return snapshot
    LearningResource,
    TaskSubmission,
