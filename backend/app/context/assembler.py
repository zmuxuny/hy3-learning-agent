import os
import tempfile
from pathlib import Path

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import selectinload

from app.core.config import PROJECT_ROOT, settings
from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.uow import commit as commit_uow, flush as flush_uow
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
    SessionPlanLink,
    Stage,
    TaskSubmission,
    UserProfile,
)
from app.context.memory import MemoryManager, search_terms
from app.services.evidence import build_plan_evidence_state


_CONTEXT_PROJECTIONS_KEY = "h2_context_projections"
_CONTEXT_PROJECTION_FAILURES_KEY = "h2_context_projection_failures"
_SESSION_WRITE_ACTIVITY_KEY = "h2_session_write_activity"


def _atomic_write_text(path: Path, content: str) -> None:
    """Publish a derived context projection without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_context_projection(db: AsyncSession, path: Path, content: str) -> None:
    projections = db.sync_session.info.setdefault(_CONTEXT_PROJECTIONS_KEY, {})
    projections[str(path)] = content


@event.listens_for(SyncSession, "after_commit")
def _publish_committed_context_projections(session: SyncSession) -> None:
    """Write only projections whose source ContextSnapshot is durable."""

    if session.in_nested_transaction():
        # SQLAlchemy emits after_commit for SAVEPOINT release as well.  The
        # projection and write-activity marker belong to the outer caller UoW.
        return
    projections = session.info.pop(_CONTEXT_PROJECTIONS_KEY, {})
    session.info.pop(_SESSION_WRITE_ACTIVITY_KEY, None)
    failures: list[str] = []
    for raw_path, content in projections.items():
        try:
            _atomic_write_text(Path(raw_path), content)
        except OSError as exc:
            # Markdown files are rebuildable projections; a filesystem error
            # cannot roll back an already-committed SQLite transaction.  Keep
            # only the exception type for diagnostics, never context content.
            failures.append(type(exc).__name__)
    if failures:
        session.info[_CONTEXT_PROJECTION_FAILURES_KEY] = failures


@event.listens_for(SyncSession, "after_rollback")
def _discard_rolled_back_context_projections(session: SyncSession) -> None:
    if session.in_nested_transaction():
        # A SAVEPOINT rollback does not end or invalidate the outer UoW.
        return
    session.info.pop(_CONTEXT_PROJECTIONS_KEY, None)
    session.info.pop(_SESSION_WRITE_ACTIVITY_KEY, None)


@event.listens_for(SyncSession, "before_flush")
def _track_session_flush(session: SyncSession, *_args) -> None:
    if session.new or session.dirty or session.deleted:
        session.info[_SESSION_WRITE_ACTIVITY_KEY] = True


@event.listens_for(SyncSession, "do_orm_execute")
def _track_session_dml(execute_state) -> None:
    if any(
        getattr(execute_state, attribute, False)
        for attribute in ("is_insert", "is_update", "is_delete")
    ):
        execute_state.session.info[_SESSION_WRITE_ACTIVITY_KEY] = True


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
        if self.db.in_nested_transaction():
            raise RuntimeError(
                "ContextAssembler cannot stage projections inside a SAVEPOINT"
            )
        # Context assembly is a multi-phase coordinator, never an implicit
        # commit boundary for caller-owned domain work. A read-only transaction
        # can be released; staged or already-flushed writes must be committed
        # explicitly by the owning API/runtime/tool coordinator first.
        if (
            self.db.new
            or self.db.dirty
            or self.db.deleted
            or self.db.sync_session.info.get(_SESSION_WRITE_ACTIVITY_KEY)
        ):
            raise RuntimeError(
                "ContextAssembler requires a clean session; commit the caller Unit of Work first"
            )
        if self.db.in_transaction():
            # A read-only commit releases the snapshot without expiring ORM
            # objects (application sessions use expire_on_commit=False).
            await commit_uow(self.db)
        manifest: list[dict] = []
        generated_at = utc_now()
        sections = ["# Agent Context", f"Generated: {canonical_utc(generated_at)}"]

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
        # Maintenance is its own short UoW.  In particular, no embedding
        # provider call below may inherit its writer lock.
        await commit_uow(self.db)
        relevant_memories, memory_scores = await memory_manager.retrieve_with_scores(
            owner_id,
            plan_id=plan_id,
            session_id=session_id,
            query=objective,
            limit=24,
        )
        # Access counters are durable housekeeping, not part of the snapshot
        # insert. Release that writer before the remaining context reads.
        await commit_uow(self.db)
        if relevant_memories:
            sections.append("## Confirmed memory")
            for memory, score in zip(relevant_memories, memory_scores, strict=True):
                sections.append(f"- [{memory.layer}/{memory.scope}] {memory.content} (memory:{memory.id})")
                manifest.append({
                    "type": "memory",
                    "id": memory.id,
                    "scope": memory.scope,
                    "layer": memory.layer,
                    "score_breakdown": score,
                })

        related_plan_links: list[SessionPlanLink] = []
        if session_id:
            related_plan_links = list((await self.db.execute(
                select(SessionPlanLink).where(
                    SessionPlanLink.owner_id == owner_id,
                    SessionPlanLink.session_id == session_id,
                )
            )).scalars())

        if plan_id is None:
            related_plan_ids = {link.plan_id for link in related_plan_links}
            plan_query = (
                select(Plan)
                .where(
                    Plan.owner_id == owner_id,
                    or_(Plan.status != "archived", Plan.id.in_(related_plan_ids)) if related_plan_ids else Plan.status != "archived",
                )
                .options(selectinload(Plan.stages).selectinload(Stage.tasks))
                .order_by(Plan.updated_at.desc())
                .limit(24)
            )
            plans = list((await self.db.execute(plan_query)).scalars().unique())
            relation_map: dict[int, set[str]] = {}
            for link in related_plan_links:
                relation_map.setdefault(link.plan_id, set()).add(link.relation_type)
            plans.sort(
                key=lambda plan: (
                    plan.id in related_plan_ids,
                    coerce_legacy_utc(plan.updated_at),
                ),
                reverse=True,
            )
            if plans:
                sections.append("## Plan index")
                sections.append("Compact summaries only. Use plan_get before relying on task-level details or changing a plan.")
                for plan in plans:
                    tasks = [task for stage in plan.stages for task in stage.tasks]
                    current = [task.title for task in tasks if task.status in {"active", "blocked"}]
                    if not current:
                        current = [task.title for task in tasks if task.status == "pending"][:1]
                    relation = ",".join(sorted(relation_map.get(plan.id, set()))) or "none"
                    sections.append(
                        f"- plan:{plan.id} [{plan.status}] {plan.title}; progress={plan.progress:.0%}; "
                        f"deadline={canonical_utc(plan.deadline)}; version={plan.version}; "
                        f"relation={relation}; "
                        f"current={'、'.join(current[:2]) or '无'}; summary={plan.memory_summary or '(empty)'}"
                    )
                    manifest.append({"type": "plan_index", "id": plan.id, "version": plan.version})

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
                        f"- Deadline: {canonical_utc(plan.deadline)}",
                        f"- Progress: {plan.progress:.0%}",
                        f"- Plan memory: {plan.memory_summary or '(empty)'}",
                    ]
                )
                for stage in plan.stages:
                    sections.append(f"### {stage.title} [{stage.status}]")
                    for task in stage.tasks:
                        sections.append(
                            f"- task:{task.id} [{task.status}] {task.title}; "
                            f"due={canonical_utc(task.due_at)}; "
                            f"review={canonical_utc(task.review_due_at)}; core={task.is_core}"
                        )
                manifest.append({"type": "plan", "id": plan.id, "version": plan.version})
                evidence_state = await build_plan_evidence_state(self.db, owner_id, plan.id)
                if evidence_state["observation_count"]:
                    sections.append("## Evidence state (v2)")
                    sections.append(
                        f"- evidence observations: {evidence_state['observation_count']}; "
                        f"digest: {evidence_state['digest']}"
                    )
                    sections.append(
                        "- This is a conservative evidence projection, not a mastery probability. "
                        "A self-report or checkbox alone cannot mark a skill demonstrated."
                    )
                    for item in evidence_state["by_task"][:24]:
                        sections.append(
                            f"- task:{item['task_id']} [{item['evidence_stage']}] "
                            f"observations={item['observation_count']}; latest={item['latest_outcome']}; "
                            f"best_score={item['best_score']}; last={item['last_observed_at']}"
                        )
                        manifest.append({
                            "type": "evidence_state",
                            "plan_id": plan.id,
                            "task_id": item["task_id"],
                            "digest": evidence_state["digest"],
                        })

        event_query = select(LearningEvent).where(LearningEvent.owner_id == owner_id)
        if plan_id is not None:
            event_query = event_query.where(LearningEvent.plan_id == plan_id)
        elif related_plan_ids:
            event_query = event_query.where(
                or_(LearningEvent.plan_id.is_(None), LearningEvent.plan_id.in_(related_plan_ids))
            )
        else:
            event_query = event_query.where(LearningEvent.plan_id.is_(None))
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
                sections.append(
                    f"- {canonical_utc(event.created_at)}: "
                    f"{event.event_type} — {event.summary} (event:{event.id})"
                )
                manifest.append({"type": "learning_event", "id": event.id})

        review_query = select(ReviewSchedule).where(
            ReviewSchedule.owner_id == owner_id,
            ReviewSchedule.status == "scheduled",
        )
        quiz_query = select(Quiz).where(Quiz.owner_id == owner_id, Quiz.status == "open")
        notification_query = select(Notification).where(
            Notification.owner_id == owner_id,
            Notification.archived_at.is_(None),
        )
        if plan_id is not None:
            review_query = review_query.where(ReviewSchedule.plan_id == plan_id)
            quiz_query = quiz_query.where(Quiz.plan_id == plan_id)
            notification_query = notification_query.where(Notification.plan_id == plan_id)
        elif related_plan_ids:
            review_query = review_query.where(ReviewSchedule.plan_id.in_(related_plan_ids))
            quiz_query = quiz_query.where(Quiz.plan_id.in_(related_plan_ids))
            notification_query = notification_query.where(
                or_(Notification.plan_id.is_(None), Notification.plan_id.in_(related_plan_ids))
            )
        else:
            # A global conversation gets a compact plan index. Task-level state
            # is loaded only after the Agent intentionally focuses a plan.
            review_query = review_query.where(ReviewSchedule.id.is_(None))
            quiz_query = quiz_query.where(Quiz.id.is_(None))
            notification_query = notification_query.where(Notification.plan_id.is_(None))
        if session_id:
            # Notifications projected into this Session are already present in the
            # conversation below. Keep them out of the generic list to avoid giving
            # the model the same reminder twice.
            notification_query = notification_query.where(
                or_(Notification.session_id.is_(None), Notification.session_id != session_id)
            )
        reviews = list((await self.db.execute(review_query.order_by(ReviewSchedule.due_at).limit(20))).scalars())
        quizzes = list((await self.db.execute(quiz_query.order_by(Quiz.created_at.desc()).limit(10))).scalars())
        notifications = list(
            (await self.db.execute(notification_query.order_by(Notification.created_at.desc()).limit(10))).scalars()
        )
        if reviews or quizzes:
            sections.append("## Actionable learning state")
            for review in reviews:
                sections.append(
                    f"- review:{review.id} due={canonical_utc(review.due_at)}; "
                    f"plan={review.plan_id}; task={review.task_id}; type={review.review_type}"
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
                    f"- {canonical_utc(notification.created_at)}: "
                    f"[{notification.channel}/{notification.status}] "
                    f"{notification.title} — {notification.body}"
                )
                manifest.append({"type": "notification", "id": notification.id})

        calendar_query = select(CalendarEvent).where(CalendarEvent.owner_id == owner_id, CalendarEvent.status == "scheduled")
        if plan_id is not None:
            resource_query = select(LearningResource).where(
                LearningResource.owner_id == owner_id,
                LearningResource.plan_id == plan_id,
                LearningResource.url.not_like("%duckduckgo.com/y.js%"),
            )
            submission_query = select(TaskSubmission).where(
                TaskSubmission.owner_id == owner_id,
                TaskSubmission.plan_id == plan_id,
            )
            calendar_query = calendar_query.where(CalendarEvent.plan_id == plan_id)
            resources = list((await self.db.execute(
                resource_query.order_by(LearningResource.created_at.desc()).limit(12)
            )).scalars())
            submissions = list((await self.db.execute(
                submission_query.order_by(TaskSubmission.created_at.desc()).limit(12)
            )).scalars())
        else:
            resources = []
            submissions = []
            if related_plan_ids:
                calendar_query = calendar_query.where(
                    or_(CalendarEvent.plan_id.is_(None), CalendarEvent.plan_id.in_(related_plan_ids))
                )
            else:
                calendar_query = calendar_query.where(CalendarEvent.plan_id.is_(None))
        calendar_events = list((await self.db.execute(calendar_query.order_by(CalendarEvent.starts_at).limit(20))).scalars())
        if resources:
            sections.append("## Saved learning resources")
            for resource in resources:
                sections.append(
                    f"- resource:{resource.id} [{resource.resource_type}/{resource.difficulty or 'mixed'}] "
                    f"{resource.provider or 'Web'} — {resource.title} — {resource.url}; "
                    f"why={resource.why_recommended or resource.summary or '(not curated)'}"
                )
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
                sections.append(
                    f"- calendar:{event.id} {canonical_utc(event.starts_at)} — "
                    f"{event.title} [{event.status}]"
                )
                manifest.append({"type": "calendar_event", "id": event.id})

        if session_id:
            session = await self.db.get(Session, session_id)
            if session and session.owner_id == owner_id:
                message_query = (
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
                messages = [
                    message for message in (await self.db.execute(message_query)).scalars()
                    if not message.message_metadata.get("superseded_by_edit")
                ][-settings.AGENT_RECENT_MESSAGE_LIMIT:]
                sections.extend(["## Conversation", f"Session summary: {session.summary or '(empty)'}"])
                if session.handoff_summary:
                    sections.append(f"Handoff from parent session:\n{session.handoff_summary}")
                reply_target_id = next((
                    message.message_metadata.get("reply_to_notification_id")
                    for message in reversed(messages)
                    if message.run_id == run_id and message.role == "user"
                    and message.message_metadata.get("reply_to_notification_id")
                ), None)
                reply_target = await self.db.get(Notification, reply_target_id) if reply_target_id else None
                if reply_target and reply_target.owner_id == owner_id and reply_target.session_id == session_id:
                    sections.append(
                        f"Reply target notification:{reply_target.id} — {reply_target.title}: {reply_target.body}"
                    )
                    manifest.append({"type": "notification_reply_target", "id": reply_target.id})
                for message in messages:
                    metadata = message.message_metadata or {}
                    if metadata.get("ui_kind") == "proactive_notification":
                        notification_ids = metadata.get("notification_ids") or [metadata.get("notification_id")]
                        if reply_target_id and reply_target_id in notification_ids:
                            continue
                        title = metadata.get("notification_title") or "主动提醒"
                        sections.append(f"- assistant [proactive reminder: {title}]: {message.content}")
                    elif metadata.get("reply_to_notification_id"):
                        sections.append(
                            f"- user [replying to notification:{metadata['reply_to_notification_id']}]: {message.content}"
                        )
                    else:
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
            created_at=generated_at,
        )
        self.db.add(snapshot)
        await flush_uow(self.db)

        context_root = PROJECT_ROOT / "data" / "context"
        if run_id:
            run_path = context_root / "runs" / f"{run_id}.md"
            _stage_context_projection(self.db, run_path, markdown)

        # The global/plan files are canonical readable projections. Never let
        # one Session's private transcript leak into that shared projection;
        # the exact per-Run input remains available above and in SQLite.
        canonical_markdown = markdown.split("\n## Conversation\n", 1)[0].rstrip() + "\n"
        canonical_path = (
            context_root / "global.md"
            if plan_id is None
            else context_root / "plans" / f"{plan_id}.md"
        )
        _stage_context_projection(self.db, canonical_path, canonical_markdown)
        return snapshot
