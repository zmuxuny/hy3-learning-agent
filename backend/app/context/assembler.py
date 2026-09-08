import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import selectinload

from app.core.config import PROJECT_ROOT, settings
from app.core.learner_time import learner_clock
from app.core.redaction import redact_data, redact_text
from app.core.time import canonical_utc, coerce_legacy_utc, utc_now
from app.db.uow import commit as commit_uow, flush as flush_uow
from app.models import (
    AgentRun,
    CalendarEvent,
    ChatMessage,
    ContextSnapshot,
    ContextSnapshotBlock,
    ContextState,
    LearningEvent,
    LearningResource,
    Intervention,
    Notification,
    Plan,
    Quiz,
    ReviewSchedule,
    Session,
    SessionHandoff,
    SessionPlanLink,
    SessionSummary,
    Stage,
    TaskSubmission,
    UserProfile,
)
from app.context.memory import MemoryManager, search_terms
from app.context.provenance import (
    append_provenance_edge,
    canonical_digest,
    ensure_provenance_node,
    read_context_generation,
)
from app.core.prompt_envelope import (
    PROMPT_ENVELOPE_VERSION,
    TOKEN_ESTIMATOR_VERSION,
    envelope_inputs,
    estimate_context_message_tokens,
    estimate_text_tokens,
    snapshot_budget_breakdown,
)
from app.services.evidence import build_plan_evidence_state


_CONTEXT_PROJECTIONS_KEY = "h2_context_projections"
_CONTEXT_PROJECTION_FAILURES_KEY = "h2_context_projection_failures"
_SESSION_WRITE_ACTIVITY_KEY = "h2_session_write_activity"
_ASSEMBLER_VERSION = "h5-context-blocks-v1"


class ContextGenerationChanged(RuntimeError):
    """A source mutation invalidated a Context build before it became valid."""


@dataclass
class _ContextBlock:
    block_id: str
    block_type: str
    section: str
    text: str
    source: dict[str, Any]
    priority: int
    ordinal: int
    source_node_kind: str | None = None
    source_node_key: str = ""
    source_version: int = 1
    source_digest: str = ""
    source_plan_id: int | None = None
    source_session_id: str | None = None
    estimated_tokens: int = field(init=False)
    block_digest: str = field(init=False)

    def __post_init__(self) -> None:
        self.estimated_tokens = max(1, estimate_text_tokens(self.text))
        self.block_digest = canonical_digest(self.text)
        if not self.source_digest:
            self.source_digest = self.block_digest

    def manifest(self, *, reason_code: str = "") -> dict[str, Any]:
        item = {
            **self.source,
            "id": (
                self.source["id"]
                if self.source.get("id") is not None
                else _manifest_source_id(self.source)
            ),
            "block_id": self.block_id,
            "block_type": self.block_type,
            "estimated_tokens": self.estimated_tokens,
            "priority": self.priority,
        }
        if reason_code:
            item["reason_code"] = reason_code
        return item


@dataclass(frozen=True)
class _ContextSection:
    heading: str
    preamble: tuple[str, ...] = ()


def _manifest_source_id(source: dict[str, Any]) -> str:
    for field in ("id", "task_id", "plan_id"):
        value = source.get(field)
        if value is not None:
            return str(value)
    return "singleton"


def _plan_source_digest(plan: Plan) -> str:
    """Hash one canonical plan fact for both index and focused projections."""

    return canonical_digest(
        {
            "id": plan.id,
            "status": plan.status,
            "title": plan.title,
            "goal": plan.goal,
            "current_level": plan.current_level,
            "deadline": canonical_utc(plan.deadline),
            "progress": plan.progress,
            "version": plan.version,
            "memory_summary": plan.memory_summary,
        }
    )


def _legacy_notification_source_digest(notification: Notification) -> str:
    """Hash the single legacy delivery fact independently of its rendered view."""

    return canonical_digest(
        {
            "id": notification.id,
            "owner_id": notification.owner_id,
            "plan_id": notification.plan_id,
            "session_id": notification.session_id,
            "title": notification.title,
            "body": notification.body,
            "channel": notification.channel,
            "status": notification.status,
            "reply_token": notification.reply_token,
            "created_at": canonical_utc(notification.created_at),
        }
    )


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
        prompt_system: str | None = None,
        prompt_tools: Sequence[dict[str, Any]] | None = None,
        prompt_prefix: str | None = None,
    ) -> ContextSnapshot:
        if self.db.in_nested_transaction():
            raise RuntimeError("ContextAssembler cannot stage projections inside a SAVEPOINT")
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
            await commit_uow(self.db)

        if session_id is not None:
            scoped_session = await self.db.get(Session, session_id)
            if scoped_session is None or scoped_session.owner_id != owner_id:
                raise ValueError("Context Session is unavailable for this owner")
            if plan_id is None:
                plan_id = scoped_session.plan_id
            elif scoped_session.plan_id != plan_id:
                raise ValueError("Context Session does not match the requested plan")

        # Housekeeping is deliberately completed before the generation-fenced
        # read. Provider work and access telemetry must not share the Snapshot UoW.
        memory_manager = MemoryManager(self.db)
        await memory_manager.maintain(owner_id)
        await commit_uow(self.db)
        relevant_memories, memory_scores = await memory_manager.retrieve_with_scores(
            owner_id,
            plan_id=plan_id,
            session_id=session_id,
            query=objective,
            limit=24,
        )
        verified_pairs = [
            (memory, score)
            for memory, score in zip(relevant_memories, memory_scores, strict=True)
            if memory.validity_state == "valid" and memory.provenance_node_id is not None
        ]
        relevant_memories = [memory for memory, _score in verified_pairs]
        memory_scores = [score for _memory, score in verified_pairs]
        await commit_uow(self.db)

        context_generation = await self.db.scalar(
            select(ContextState.generation).where(ContextState.owner_id == owner_id)
        )
        if context_generation is None:
            # Materializing the initial zero fence is a short write UoW. Do not
            # carry its SQLite RESERVED lock through source reads and rendering.
            await read_context_generation(self.db, owner_id)
            await commit_uow(self.db)
            context_generation = await self.db.scalar(
                select(ContextState.generation).where(ContextState.owner_id == owner_id)
            )
        if context_generation is None:
            raise RuntimeError("Context generation row could not be established")
        context_generation = int(context_generation)
        generated_at = utc_now()
        blocks: list[_ContextBlock] = []
        section_order: list[str] = []
        sections: dict[str, _ContextSection] = {}

        def define_section(key: str, heading: str, *preamble: str) -> None:
            if key not in sections:
                section_order.append(key)
                sections[key] = _ContextSection(heading, tuple(preamble))

        def add_block(
            *,
            block_type: str,
            section: str,
            text: str,
            source: dict[str, Any],
            priority: int,
            source_node_kind: str | None = None,
            source_node_key: str | int | None = None,
            source_version: int = 1,
            source_digest: str = "",
            source_plan_id: int | None = None,
            source_session_id: str | None = None,
        ) -> None:
            raw_source_id = _manifest_source_id(source)
            safe_source = redact_data(source)
            source_type = str(safe_source.get("type") or block_type)
            source_id = _manifest_source_id(safe_source)
            block_id = f"{source_type}:{source_id}:v{source_version}:{block_type}"
            # ``source_digest`` is an irreversible identity of the canonical
            # source fact and must stay stable for provenance invalidation.
            # _ContextBlock derives its separate ``block_digest`` and token
            # count from the redacted text that is actually persisted.
            blocks.append(
                _ContextBlock(
                    block_id=block_id,
                    block_type=block_type,
                    section=section,
                    text=redact_text(text),
                    source=safe_source,
                    priority=priority,
                    ordinal=len(blocks),
                    source_node_kind=source_node_kind,
                    source_node_key=str(
                        source_node_key if source_node_key is not None else raw_source_id
                    ),
                    source_version=max(1, int(source_version)),
                    source_digest=source_digest,
                    source_plan_id=source_plan_id,
                    source_session_id=source_session_id,
                )
            )

        profile = await self.db.get(UserProfile, owner_id)
        if profile:
            define_section("profile", "## Global learner profile")
            profile_text = "\n".join(
                [
                    f"- Agent style: {profile.agent_style}",
                    f"- Preferences: {profile.preferences}",
                    f"- Quiet hours: {profile.quiet_hours}",
                    f"- Level: {profile.level}; XP: {profile.xp}; streak: {profile.streak_days}",
                ]
            )
            profile_digest = canonical_digest(profile_text)
            add_block(
                block_type="profile",
                section="profile",
                text=profile_text,
                source={"type": "profile", "id": owner_id},
                priority=100,
                # UserProfile has no semantic version. Digest-keying the
                # immutable projection prevents a changed profile from
                # reusing a verified node for old content.
                source_node_kind="profile",
                source_node_key=f"{owner_id}:{profile_digest}",
                source_digest=profile_digest,
            )

        if relevant_memories:
            define_section("memory", "## Confirmed memory")
            for memory, score in zip(relevant_memories, memory_scores, strict=True):
                content_digest = memory.content_hash if len(memory.content_hash or "") == 64 else canonical_digest(memory.content)
                add_block(
                    block_type="memory",
                    section="memory",
                    text=f"- [{memory.layer}/{memory.scope}] {memory.content} (memory:{memory.id})",
                    source={
                        "type": "memory",
                        "id": memory.id,
                        "scope": memory.scope,
                        "layer": memory.layer,
                        "score_breakdown": score,
                    },
                    priority=82 if memory.scope in {"session", "plan"} else 76,
                    source_node_kind="memory",
                    source_node_key=memory.id,
                    source_version=getattr(memory, "lifecycle_version", 1),
                    source_digest=content_digest,
                    source_plan_id=int(memory.scope_id) if memory.scope == "plan" and memory.scope_id else None,
                    source_session_id=memory.scope_id if memory.scope == "session" else None,
                )

        related_plan_links: list[SessionPlanLink] = []
        if session_id:
            related_plan_links = list(
                (
                    await self.db.execute(
                        select(SessionPlanLink).where(
                            SessionPlanLink.owner_id == owner_id,
                            SessionPlanLink.session_id == session_id,
                        )
                    )
                ).scalars()
            )
        related_plan_ids = {link.plan_id for link in related_plan_links}

        if plan_id is None:
            # A SessionPlanLink is navigation metadata only. It affects ordering
            # and relation labels, never authorization for plan-private blocks.
            plan_query = (
                select(Plan)
                .where(
                    Plan.owner_id == owner_id,
                    or_(Plan.status != "archived", Plan.id.in_(related_plan_ids))
                    if related_plan_ids
                    else Plan.status != "archived",
                )
                .order_by(Plan.updated_at.desc())
                .limit(24)
            )
            plans = list((await self.db.execute(plan_query)).scalars())
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
                define_section(
                    "plan_index",
                    "## Plan index",
                    "Compact summaries only. Use plan_get before relying on task-level details or changing a plan.",
                )
                for plan in plans:
                    relation = ",".join(sorted(relation_map.get(plan.id, set()))) or "none"
                    plan_text = (
                        f"- plan:{plan.id} [{plan.status}] {plan.title}; progress={plan.progress:.0%}; "
                        f"deadline={canonical_utc(plan.deadline)}; version={plan.version}; "
                        f"relation={relation}"
                    )
                    add_block(
                        block_type="plan_index",
                        section="plan_index",
                        text=plan_text,
                        source={"type": "plan_index", "id": plan.id, "version": plan.version},
                        priority=72 + (4 if plan.id in related_plan_ids else 0),
                        source_node_kind="plan_summary",
                        source_node_key=plan.id,
                        source_version=plan.version,
                        source_digest=_plan_source_digest(plan),
                        source_plan_id=plan.id,
                    )

        if plan_id is not None:
            plan_result = await self.db.execute(
                select(Plan)
                .where(Plan.id == plan_id, Plan.owner_id == owner_id)
                .options(selectinload(Plan.stages).selectinload(Stage.tasks))
            )
            plan = plan_result.scalars().unique().one_or_none()
            if plan:
                define_section("active_plan", "## Active plan")
                detail_lines = [
                    f"- Plan: {plan.title} (plan:{plan.id}, version:{plan.version})",
                    f"- Goal: {plan.goal}",
                    f"- Current level: {plan.current_level}",
                    f"- Deadline: {canonical_utc(plan.deadline)}",
                    f"- Progress: {plan.progress:.0%}",
                    f"- Plan memory: {plan.memory_summary or '(empty)'}",
                ]
                for stage in plan.stages:
                    detail_lines.append(f"### {stage.title} [{stage.status}]")
                    for task in stage.tasks:
                        detail_lines.append(
                            f"- task:{task.id} [{task.status}] {task.title}; "
                            f"due={canonical_utc(task.due_at)}; "
                            f"review={canonical_utc(task.review_due_at)}; core={task.is_core}"
                        )
                plan_detail_text = "\n".join(detail_lines)
                plan_detail_digest = canonical_digest(plan_detail_text)
                add_block(
                    block_type="active_plan",
                    section="active_plan",
                    text=plan_detail_text,
                    source={"type": "plan", "id": plan.id, "version": plan.version},
                    priority=96,
                    source_node_kind="plan_summary",
                    # Task/Stage rows do not yet expose independent semantic
                    # versions. Key this immutable detail projection by its
                    # full digest so a task change cannot reuse a stale node.
                    source_node_key=f"{plan.id}:detail:{plan_detail_digest}",
                    source_version=plan.version,
                    source_digest=plan_detail_digest,
                    source_plan_id=plan.id,
                )
                evidence_state = await build_plan_evidence_state(self.db, owner_id, plan.id)
                define_section(
                    "evidence",
                    "## Evidence state (v2)",
                    "- This is a conservative evidence projection, not a mastery probability. A self-report or checkbox alone cannot mark a skill demonstrated.",
                )
                add_block(
                    block_type="evidence_projection",
                    section="evidence",
                    text=(
                        f"- evidence observations: {evidence_state['observation_count']}; "
                        f"digest: {evidence_state['digest']}"
                    ),
                    source={
                        "type": "evidence_state",
                        "plan_id": plan.id,
                        "task_id": None,
                        "digest": evidence_state["digest"],
                        "observation_count": evidence_state["observation_count"],
                    },
                    priority=95,
                    source_node_kind="evidence_projection",
                    source_node_key=f"{plan.id}:{evidence_state['digest']}",
                    source_digest=evidence_state["digest"],
                    source_plan_id=plan.id,
                )
                for item in evidence_state["by_task"][:24]:
                    add_block(
                        block_type="evidence_task",
                        section="evidence",
                        text=(
                            f"- task:{item['task_id']} [{item['evidence_stage']}] "
                            f"observations={item['observation_count']}; latest={item['latest_outcome']}; "
                            f"best_score={item['best_score']}; last={item['last_observed_at']}"
                        ),
                        source={
                            "type": "evidence_state",
                            "plan_id": plan.id,
                            "task_id": item["task_id"],
                            "digest": evidence_state["digest"],
                        },
                        priority=90,
                        source_node_kind="evidence_projection",
                        source_node_key=(
                            f"{plan.id}:task:{item['task_id']}:{evidence_state['digest']}"
                        ),
                        source_digest=evidence_state["digest"],
                        source_plan_id=plan.id,
                    )

        event_query = select(LearningEvent).where(LearningEvent.owner_id == owner_id)
        event_query = event_query.where(
            LearningEvent.plan_id == plan_id
            if plan_id is not None
            else LearningEvent.plan_id.is_(None)
        )
        events = list(
            (
                await self.db.execute(
                    event_query.order_by(LearningEvent.created_at.desc()).limit(
                        settings.AGENT_CONTEXT_EVENT_LIMIT * 3
                    )
                )
            ).scalars()
        )
        if objective:
            terms = search_terms(objective)
            events.sort(
                key=lambda event: len(
                    terms.intersection(search_terms(f"{event.event_type} {event.summary}"))
                ),
                reverse=True,
            )
        events = events[: settings.AGENT_CONTEXT_EVENT_LIMIT]
        if events:
            define_section("events", "## Recent learning events")
            for event in reversed(events):
                event_text = (
                    f"- {canonical_utc(event.created_at)}: "
                    f"{event.event_type} — {event.summary} (event:{event.id})"
                )
                add_block(
                    block_type="learning_event",
                    section="events",
                    text=event_text,
                    source={"type": "learning_event", "id": event.id},
                    priority=64,
                    source_node_kind="learning_event",
                    source_node_key=event.id,
                    source_digest=canonical_digest(
                        {
                            "event_type": event.event_type,
                            "summary": event.summary,
                            "created_at": canonical_utc(event.created_at),
                        }
                    ),
                    source_plan_id=event.plan_id,
                )

        review_query = select(ReviewSchedule).where(
            ReviewSchedule.owner_id == owner_id,
            ReviewSchedule.status == "scheduled",
        )
        quiz_query = select(Quiz).where(Quiz.owner_id == owner_id, Quiz.status == "open")
        intervention_query = select(Intervention).where(
            Intervention.owner_id == owner_id,
            Intervention.archived_at.is_(None),
            Intervention.state != "building",
        )
        legacy_notification_query = select(Notification).where(
            Notification.owner_id == owner_id,
            Notification.archived_at.is_(None),
            Notification.intervention_id.is_(None),
        )
        if plan_id is not None:
            review_query = review_query.where(ReviewSchedule.plan_id == plan_id)
            quiz_query = quiz_query.where(Quiz.plan_id == plan_id)
            intervention_query = intervention_query.where(Intervention.plan_id == plan_id)
            legacy_notification_query = legacy_notification_query.where(
                Notification.plan_id == plan_id
            )
        else:
            # Relation labels never widen these plan-private queries.
            review_query = review_query.where(ReviewSchedule.id.is_(None))
            quiz_query = quiz_query.where(Quiz.id.is_(None))
            intervention_query = intervention_query.where(Intervention.plan_id.is_(None))
            legacy_notification_query = legacy_notification_query.where(
                Notification.plan_id.is_(None)
            )
        if session_id:
            # The current Intervention is already present as its canonical
            # conversation message. Query logical Interventions directly;
            # delivery rows never decide representative channel or status.
            intervention_query = intervention_query.where(
                Intervention.session_id != session_id
            )
            legacy_notification_query = legacy_notification_query.where(
                or_(Notification.session_id.is_(None), Notification.session_id != session_id)
            )
        reviews = list(
            (
                await self.db.execute(
                    review_query.order_by(ReviewSchedule.due_at).limit(20)
                )
            ).scalars()
        )
        quizzes = list(
            (
                await self.db.execute(
                    quiz_query.order_by(Quiz.created_at.desc()).limit(10)
                )
            ).scalars()
        )
        interventions = list(
            (
                await self.db.execute(
                    intervention_query.order_by(
                        Intervention.created_at.desc(),
                        Intervention.id.desc(),
                    ).limit(10)
                )
            ).scalars()
        )
        legacy_notifications = list(
            (
                await self.db.execute(
                    legacy_notification_query.order_by(
                        Notification.created_at.desc(),
                        Notification.id.desc(),
                    ).limit(10)
                )
            ).scalars()
        )
        if reviews or quizzes:
            define_section("actionable", "## Actionable learning state")
            for review in reviews:
                text = (
                    f"- review:{review.id} due={canonical_utc(review.due_at)}; "
                    f"plan={review.plan_id}; task={review.task_id}; type={review.review_type}"
                )
                digest = canonical_digest(text)
                add_block(
                    block_type="review",
                    section="actionable",
                    text=text,
                    source={"type": "review", "id": review.id},
                    priority=88,
                    source_node_kind="review_schedule",
                    source_node_key=f"{review.id}:{digest}",
                    source_digest=digest,
                    source_plan_id=review.plan_id,
                )
            for quiz in quizzes:
                text = (
                    f"- quiz:{quiz.id} [open] plan={quiz.plan_id}; "
                    f"task={quiz.task_id}; prompt={quiz.prompt}"
                )
                digest = canonical_digest(text)
                add_block(
                    block_type="quiz",
                    section="actionable",
                    text=text,
                    source={"type": "quiz", "id": quiz.id},
                    priority=88,
                    source_node_kind="quiz",
                    source_node_key=f"{quiz.id}:{digest}",
                    source_digest=digest,
                    source_plan_id=quiz.plan_id,
                )
        if interventions or legacy_notifications:
            define_section("notifications", "## Recent notifications")
            for intervention in reversed(interventions):
                text = (
                    f"- {canonical_utc(intervention.created_at)}: "
                    f"[intervention/{intervention.state}] "
                    f"{intervention.title} — {intervention.body}"
                )
                add_block(
                    block_type="intervention",
                    section="notifications",
                    text=text,
                    source={"type": "intervention", "id": intervention.id},
                    priority=78,
                    source_node_kind="intervention",
                    # Plan/global Context consumes a scope-level projection,
                    # distinct from the canonical Session Intervention node.
                    source_node_key=(
                        f"{intervention.id}:scope:{intervention.plan_id or 'global'}"
                    ),
                    source_digest=intervention.content_digest,
                    source_plan_id=intervention.plan_id,
                    source_session_id=None,
                )
            for notification in reversed(legacy_notifications):
                text = (
                    f"- {canonical_utc(notification.created_at)}: "
                    f"[{notification.channel}/{notification.status}] "
                    f"{notification.title} — {notification.body}"
                )
                add_block(
                    block_type="notification",
                    section="notifications",
                    text=text,
                    source={"type": "notification", "id": notification.id},
                    priority=78,
                    source_node_kind="legacy_notification",
                    source_node_key=notification.id,
                    source_digest=_legacy_notification_source_digest(notification),
                    source_plan_id=notification.plan_id,
                    source_session_id=None,
                )

        calendar_query = select(CalendarEvent).where(
            CalendarEvent.owner_id == owner_id,
            CalendarEvent.status == "scheduled",
        )
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
            resources = list(
                (
                    await self.db.execute(
                        resource_query.order_by(LearningResource.created_at.desc()).limit(12)
                    )
                ).scalars()
            )
            submissions = list(
                (
                    await self.db.execute(
                        submission_query.order_by(TaskSubmission.created_at.desc()).limit(12)
                    )
                ).scalars()
            )
        else:
            resources = []
            submissions = []
            calendar_query = calendar_query.where(CalendarEvent.plan_id.is_(None))
        calendar_events = list(
            (
                await self.db.execute(
                    calendar_query.order_by(CalendarEvent.starts_at).limit(20)
                )
            ).scalars()
        )
        if resources:
            define_section("resources", "## Saved learning resources")
            for resource in resources:
                text = (
                    f"- resource:{resource.id} [{resource.resource_type}/{resource.difficulty or 'mixed'}] "
                    f"{resource.provider or 'Web'} — {resource.title} — {resource.url}; "
                    f"why={resource.why_recommended or resource.summary or '(not curated)'}"
                )
                digest = canonical_digest(text)
                add_block(
                    block_type="resource",
                    section="resources",
                    text=text,
                    source={"type": "resource", "id": resource.id},
                    priority=62,
                    source_node_kind="learning_resource",
                    source_node_key=f"{resource.id}:{digest}",
                    source_digest=digest,
                    source_plan_id=resource.plan_id,
                )
        if submissions:
            define_section("submissions", "## Recent task submissions")
            for submission in reversed(submissions):
                text = (
                    f"- submission:{submission.id} task={submission.task_id} "
                    f"status={submission.status} score={submission.score}; {submission.content[:240]}"
                )
                digest = canonical_digest(text)
                add_block(
                    block_type="submission",
                    section="submissions",
                    text=text,
                    source={"type": "submission", "id": submission.id},
                    priority=68,
                    source_node_kind="task_submission",
                    source_node_key=f"{submission.id}:{digest}",
                    source_digest=digest,
                    source_plan_id=submission.plan_id,
                )
        if calendar_events:
            define_section("calendar", "## Study calendar")
            for event in calendar_events:
                text = (
                    f"- calendar:{event.id} {canonical_utc(event.starts_at)} — "
                    f"{event.title} [{event.status}]"
                )
                digest = canonical_digest(text)
                add_block(
                    block_type="calendar_event",
                    section="calendar",
                    text=text,
                    source={"type": "calendar_event", "id": event.id},
                    priority=84,
                    source_node_kind="calendar_event",
                    source_node_key=f"{event.id}:{digest}",
                    source_digest=digest,
                    source_plan_id=event.plan_id,
                )

        conversation_session: Session | None = None
        if session_id:
            session = await self.db.get(Session, session_id)
            if session and session.owner_id == owner_id:
                conversation_session = session
                define_section("conversation", "## Conversation")
                active_summary = (
                    await self.db.execute(
                        select(SessionSummary)
                        .where(
                            SessionSummary.owner_id == owner_id,
                            SessionSummary.session_id == session.id,
                            SessionSummary.validity_state == "valid",
                        )
                        .order_by(SessionSummary.version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if active_summary is not None:
                    add_block(
                        block_type="session_summary",
                        section="conversation",
                        text=(
                            "Session summary "
                            f"(id:{active_summary.id}, version:{active_summary.version}, "
                            f"hash:{active_summary.content_hash}): {active_summary.content}"
                        ),
                        source={
                            "type": "session_summary",
                            "id": active_summary.id,
                            "version": active_summary.version,
                            "content_hash": active_summary.content_hash,
                        },
                        priority=106,
                        source_node_kind="session_summary",
                        source_node_key=active_summary.id,
                        source_version=active_summary.version,
                        source_digest=active_summary.content_hash,
                        source_plan_id=session.plan_id,
                        source_session_id=session.id,
                    )
                active_handoff = (
                    await self.db.execute(
                        select(SessionHandoff)
                        .where(
                            SessionHandoff.owner_id == owner_id,
                            SessionHandoff.target_session_id == session.id,
                            SessionHandoff.validity_state == "valid",
                        )
                        .order_by(SessionHandoff.version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if active_handoff is not None:
                    add_block(
                        block_type="session_handoff",
                        section="conversation",
                        text=(
                            "Handoff from parent session "
                            f"(id:{active_handoff.id}, version:{active_handoff.version}, "
                            f"hash:{active_handoff.content_hash}):\n{active_handoff.content}"
                        ),
                        source={
                            "type": "session_handoff",
                            "id": active_handoff.id,
                            "version": active_handoff.version,
                            "content_hash": active_handoff.content_hash,
                        },
                        priority=108,
                        source_node_kind="session_handoff",
                        source_node_key=active_handoff.id,
                        source_version=active_handoff.version,
                        source_digest=active_handoff.content_hash,
                        source_plan_id=active_handoff.plan_id,
                        source_session_id=session.id,
                    )
                message_query = (
                    select(ChatMessage)
                    .where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.validity_state == "active",
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
                messages = [
                    message
                    for message in (await self.db.execute(message_query)).scalars()
                    if not message.message_metadata.get("superseded_by_edit")
                ][-settings.AGENT_RECENT_MESSAGE_LIMIT :]
                run_reply_target_id = None
                if run_id:
                    run_reply_target_id = await self.db.scalar(
                        select(AgentRun.reply_to_intervention_id).where(
                            AgentRun.id == run_id,
                            AgentRun.owner_id == owner_id,
                            AgentRun.session_id == session.id,
                        )
                    )
                message_reply_target_id = next(
                    (
                        message.reply_to_intervention_id
                        for message in reversed(messages)
                        if message.run_id == run_id
                        and message.role == "user"
                        and message.reply_to_intervention_id
                    ),
                    None,
                )
                intervention_target_id = run_reply_target_id or message_reply_target_id
                intervention_target = (
                    await self.db.get(Intervention, intervention_target_id)
                    if intervention_target_id
                    else None
                )
                if (
                    intervention_target is not None
                    and intervention_target.owner_id == owner_id
                    and intervention_target.session_id == session.id
                    and intervention_target.state
                    in {"active", "replied", "resolved"}
                ):
                    add_block(
                        block_type="intervention_reply_target",
                        section="conversation",
                        text=(
                            f"Reply target intervention:{intervention_target.id} — "
                            f"{intervention_target.title}: {intervention_target.body}"
                        ),
                        source={
                            "type": "intervention_reply_target",
                            "id": intervention_target.id,
                            "content_hash": intervention_target.content_digest,
                        },
                        priority=112,
                        source_node_kind="intervention",
                        source_node_key=intervention_target.id,
                        source_version=1,
                        source_digest=intervention_target.content_digest,
                        source_plan_id=intervention_target.plan_id,
                        source_session_id=session.id,
                    )
                legacy_reply_target_id = next(
                    (
                        message.message_metadata.get("reply_to_notification_id")
                        for message in reversed(messages)
                        if message.run_id == run_id
                        and message.role == "user"
                        and message.message_metadata.get("reply_to_notification_id")
                    ),
                    None,
                )
                reply_target = (
                    await self.db.get(Notification, legacy_reply_target_id)
                    if legacy_reply_target_id and intervention_target is None
                    else None
                )
                if (
                    reply_target
                    and reply_target.owner_id == owner_id
                    and reply_target.session_id == session_id
                ):
                    reply_target_text = (
                        f"Reply target notification:{reply_target.id} — "
                        f"{reply_target.title}: {reply_target.body}"
                    )
                    add_block(
                        block_type="notification_reply_target",
                        section="conversation",
                        text=reply_target_text,
                        source={"type": "notification_reply_target", "id": reply_target.id},
                        priority=112,
                        source_node_kind="legacy_notification",
                        source_node_key=reply_target.id,
                        source_digest=_legacy_notification_source_digest(reply_target),
                        source_plan_id=reply_target.plan_id,
                        source_session_id=None,
                    )
                for index, message in enumerate(messages):
                    metadata = message.message_metadata or {}
                    if metadata.get("ui_kind") == "proactive_notification":
                        if (
                            intervention_target_id
                            and metadata.get("intervention_id") == intervention_target_id
                        ):
                            continue
                        notification_ids = metadata.get("notification_ids") or [
                            metadata.get("notification_id")
                        ]
                        if legacy_reply_target_id and legacy_reply_target_id in notification_ids:
                            continue
                        title = metadata.get("notification_title") or "主动提醒"
                        text = f"- assistant [proactive reminder: {title}]: {message.content}"
                    elif message.reply_to_intervention_id:
                        text = (
                            f"- user [replying to intervention:{message.reply_to_intervention_id}]: "
                            f"{message.content}"
                        )
                    elif metadata.get("reply_to_notification_id"):
                        text = (
                            f"- user [replying to notification:{metadata['reply_to_notification_id']}]: "
                            f"{message.content}"
                        )
                    else:
                        text = f"- {message.role}: {message.content}"
                    message_digest = (
                        message.content_hash
                        if len(message.content_hash or "") == 64
                        else canonical_digest(message.content)
                    )
                    add_block(
                        block_type="message",
                        section="conversation",
                        text=text,
                        source={
                            "type": "message",
                            "id": message.id,
                            "version": message.version,
                            "content_hash": message_digest,
                        },
                        priority=110 + index,
                        source_node_kind="message",
                        source_node_key=message.id,
                        source_version=message.version,
                        source_digest=message_digest,
                        source_plan_id=session.plan_id,
                        source_session_id=session.id,
                    )

        if prompt_system is None or prompt_tools is None:
            from app.runtime.prompt import SYSTEM_PROMPT
            from app.tools import openai_tools

            prompt_system = SYSTEM_PROMPT if prompt_system is None else prompt_system
            prompt_tools = openai_tools() if prompt_tools is None else prompt_tools
        prefix = prompt_prefix if prompt_prefix is not None else f"Objective: {objective}\n\n"
        budget_inputs = envelope_inputs(system_prompt=prompt_system, tools=prompt_tools)
        clock = await learner_clock(self.db, owner_id)
        base_lines = [
            "# Agent Context", f"Generated: {canonical_utc(generated_at)}",
            f"Learner timezone: {clock['timezone']}",
            f"Current local time: {clock['now_local']}",
            "Quiet hours and learner date/time requests use this local timezone; Z timestamps are UTC.",
        ]
        retained_ids: set[str] = set()
        dropped_reason: dict[str, str] = {}

        def render_context(selected: set[str]) -> str:
            rendered = list(base_lines)
            for section_key in section_order:
                retained = [
                    block
                    for block in blocks
                    if block.section == section_key and block.block_id in selected
                ]
                if not retained:
                    continue
                section = sections[section_key]
                rendered.append(section.heading)
                rendered.extend(section.preamble)
                rendered.extend(block.text for block in retained)
            return "\n".join(rendered).strip() + "\n"

        base_context_tokens = estimate_context_message_tokens(
            system_prompt=prompt_system,
            user_content=prefix + render_context(set()),
        )
        if base_context_tokens > budget_inputs.effective_context_budget:
            raise RuntimeError("base Context request envelope exceeds the model window")
        for block in sorted(blocks, key=lambda item: (-item.priority, item.ordinal)):
            tentative_ids = {*retained_ids, block.block_id}
            tentative_markdown = render_context(tentative_ids)
            tentative_tokens = estimate_context_message_tokens(
                system_prompt=prompt_system,
                user_content=prefix + tentative_markdown,
            )
            if tentative_tokens <= budget_inputs.effective_context_budget:
                retained_ids.add(block.block_id)
            else:
                dropped_reason[block.block_id] = "context_budget_exceeded"

        markdown = render_context(retained_ids)
        context_tokens = estimate_context_message_tokens(
            system_prompt=prompt_system,
            user_content=prefix + markdown,
        )
        if context_tokens > budget_inputs.effective_context_budget:
            raise RuntimeError("typed Context block selection exceeded its token budget")

        retained_blocks = [block for block in blocks if block.block_id in retained_ids]
        dropped_blocks = [block for block in blocks if block.block_id not in retained_ids]
        manifest = [block.manifest() for block in retained_blocks]
        dropped_manifest = [
            block.manifest(reason_code=dropped_reason[block.block_id])
            for block in dropped_blocks
        ]
        budget_breakdown = snapshot_budget_breakdown(
            budget_inputs,
            context_tokens=context_tokens,
        )
        if budget_breakdown["total_tokens"] > budget_breakdown["model_context_window"]:
            raise RuntimeError("complete Context request envelope exceeds the model window")

        context_digest = canonical_digest(markdown)
        source_digest = canonical_digest(
            {"retained": manifest, "dropped": dropped_manifest}
        )
        snapshot = ContextSnapshot(
            owner_id=owner_id,
            plan_id=plan_id,
            session_id=session_id,
            run_id=run_id,
            markdown=markdown,
            source_manifest=manifest,
            dropped_source_manifest=dropped_manifest,
            budget_breakdown=budget_breakdown,
            estimated_tokens=context_tokens,
            context_generation=context_generation,
            snapshot_version=1,
            assembler_version=_ASSEMBLER_VERSION,
            context_digest=context_digest,
            source_digest=source_digest,
            validity_state="building",
            created_at=generated_at,
        )
        self.db.add(snapshot)
        await flush_uow(self.db)
        snapshot_node = await ensure_provenance_node(
            self.db,
            owner_id=owner_id,
            kind="context_snapshot",
            entity_key=snapshot.id,
            entity_version=snapshot.snapshot_version,
            content_digest=context_digest,
            plan_id=plan_id,
            session_id=session_id,
            metadata={
                "assembler_version": _ASSEMBLER_VERSION,
                "envelope_version": PROMPT_ENVELOPE_VERSION,
                "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
                "source_digest": source_digest,
            },
        )
        disposition_ordinals = {"retained": 0, "dropped": 0}
        for block in blocks:
            disposition = "retained" if block.block_id in retained_ids else "dropped"
            reason_code = "" if disposition == "retained" else dropped_reason[block.block_id]
            ordinal = disposition_ordinals[disposition]
            disposition_ordinals[disposition] += 1
            if not block.source_node_kind:
                raise RuntimeError(f"Context block has no typed source node: {block.block_id}")
            source_node = await ensure_provenance_node(
                self.db,
                owner_id=owner_id,
                kind=block.source_node_kind,
                entity_key=block.source_node_key,
                entity_version=block.source_version,
                content_digest=block.source_digest,
                plan_id=block.source_plan_id,
                session_id=block.source_session_id,
            )
            self.db.add(
                ContextSnapshotBlock(
                    owner_id=owner_id,
                    snapshot_id=snapshot.id,
                    source_node_id=source_node.id,
                    disposition=disposition,
                    ordinal=ordinal,
                    block_type=block.block_type,
                    source_type=str(block.source.get("type") or block.block_type),
                    source_id=_manifest_source_id(block.source),
                    source_version=block.source_version,
                    source_digest=block.source_digest,
                    block_digest=block.block_digest,
                    token_count=block.estimated_tokens,
                    priority=block.priority,
                    reason_code=reason_code,
                    block_metadata={"block_id": block.block_id, "section": block.section},
                )
            )
            await append_provenance_edge(
                self.db,
                owner_id=owner_id,
                source_node_id=source_node.id,
                target_node_id=snapshot_node.id,
                relation=f"context_{disposition}",
                ordinal=ordinal,
                disposition=disposition,
                reason_code=reason_code,
                token_count=block.estimated_tokens,
                metadata={
                    "block_id": block.block_id,
                    "block_type": block.block_type,
                    "block_digest": block.block_digest,
                },
            )
        await flush_uow(self.db)
        current_generation = await self.db.scalar(
            select(ContextState.generation).where(ContextState.owner_id == owner_id)
        )
        if current_generation is None or int(current_generation) != context_generation:
            raise ContextGenerationChanged("Context generation changed during Snapshot build")
        # The database lifecycle trigger permits exactly one building -> valid
        # transition. Publish the provenance pointer in that same final update;
        # exposing it earlier would create a partially finalized fact.
        snapshot.provenance_node_id = snapshot_node.id
        snapshot.validity_state = "valid"
        await flush_uow(self.db)

        runtime_root = settings.RUNTIME_STATE_ROOT if settings.EVALUATION_MODE else PROJECT_ROOT
        context_root = runtime_root / "data" / "context"
        if run_id:
            _stage_context_projection(
                self.db,
                context_root / "runs" / f"{run_id}.md",
                markdown,
            )
        canonical_lines: list[str] = []
        for line in markdown.splitlines():
            if line == "## Conversation":
                break
            canonical_lines.append(line)
        canonical_markdown = "\n".join(canonical_lines).rstrip() + "\n"
        canonical_path = (
            context_root / "global.md"
            if plan_id is None
            else context_root / "plans" / f"{plan_id}.md"
        )
        _stage_context_projection(self.db, canonical_path, canonical_markdown)
        return snapshot
