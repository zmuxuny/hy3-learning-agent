import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.time import UTCInstant, canonical_utc, utc_now
from app.core.execution_policy import (
    code_execution_rejection,
    current_code_execution_policy,
)
from app.core.redaction import (
    REDACTED,
    configured_secret_values,
    redact_data,
    redact_event_fields,
    redact_text,
)
from app.core.trust import (
    EXTERNAL_CONTENT_TOOL_NAMES,
    authorize_side_effect,
    mark_external_untrusted_result,
)
from app.db.uow import (
    DatabaseBusyError,
    commit as commit_uow,
    flush as flush_uow,
    rollback as rollback_uow,
    run_short_transaction,
)
from app.models import (
    Achievement,
    ActivityDay,
    AgentRun,
    Intervention,
    LearningEvent,
    Notification,
    Operation,
    OutboxAction,
    Plan,
    Quiz,
    ReviewSchedule,
    RunEvent,
    Stage,
    Task,
    ToolInvocation,
    UserProfile,
)
from app.notifications import NotificationService
from app.runtime.events import serialize_event_write
from app.schemas import PlanCreate, TaskUpdate
from app.services import plans as plan_service
from app.services.sessions import link_session_plan
from app.tools.base import (
    EmptyArgs,
    ToolContext,
    ToolDefinition,
    ToolEffectKind,
    json_safe,
    parse_arguments,
)
from app.tools.calendar import CALENDAR_TOOLS
from app.tools.competencies import COMPETENCY_TOOLS
from app.tools.contracts import attach_output_contracts
from app.tools.learning import LEARNING_TOOLS
from app.tools.memory import MEMORY_TOOLS
from app.tools.planning import PLANNING_TOOLS
from app.tools.subagents import SUBAGENT_TOOLS
from app.tools.web import WEB_TOOLS
from app.tools.workspace import WORKSPACE_TOOLS


class PlanIdArgs(BaseModel):
    plan_id: int


class TaskPatchArgs(BaseModel):
    task_id: int
    changes: TaskUpdate
    reason: str
    expected_plan_version: int | None = None

    @field_validator("changes", mode="before")
    @classmethod
    def decode_serialized_changes(cls, value):
        """Normalize nested JSON from OpenAI-compatible tool callers."""
        if isinstance(value, str):
            return json.loads(value)
        return value


class ReviewScheduleArgs(BaseModel):
    plan_id: int
    task_id: int | None = None
    due_at: UTCInstant
    review_type: str = "quiz"


class ReviewResolveArgs(BaseModel):
    review_id: int
    action: Literal["complete", "snooze", "cancel"]
    reason: str = Field(min_length=1, max_length=1000)
    next_due_at: UTCInstant | None = None


class QuizCreateArgs(BaseModel):
    plan_id: int
    task_id: int | None = None
    prompt: str
    rubric: dict[str, Any] = Field(default_factory=dict)


class QuizIdArgs(BaseModel):
    quiz_id: int


class QuizGradeArgs(BaseModel):
    quiz_id: int
    answer: str
    score: float = Field(ge=0, le=100)
    feedback: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    next_review_at: UTCInstant | None = None


class MemoryProposalArgs(BaseModel):
    scope: Literal["global", "plan", "session"] = "global"
    scope_id: str | None = None
    layer: Literal["short_term", "long_term", "episodic", "semantic"] = "semantic"
    content: str
    confidence: float = Field(default=0.8, ge=0, le=1)
    supersedes_id: int | None = Field(
        default=None,
        ge=1,
        description="Existing active memory corrected by this proposal. Keep the same scope.",
    )


class NotificationArgs(BaseModel):
    title: str
    body: str
    plan_id: int | None = None
    channels: list[str] = Field(default_factory=lambda: ["in_app"])


class InvocationClaimLostError(RuntimeError):
    """The executor no longer owns the durable invocation lease."""


async def profile_get(ctx: ToolContext, _: EmptyArgs) -> dict:
    from app.models import UserProfile

    profile = await ctx.db.get(UserProfile, ctx.owner_id)
    return {
        "agent_style": profile.agent_style,
        "preferences": profile.preferences,
        "quiet_hours": profile.quiet_hours,
        "xp": profile.xp,
        "level": profile.level,
        "streak_days": profile.streak_days,
    }


async def plan_list(ctx: ToolContext, _: EmptyArgs) -> dict:
    plans = await plan_service.list_plans(ctx.db, ctx.owner_id)
    return {
        "plans": [
            {"id": plan.id, "title": plan.title, "status": plan.status, "progress": plan.progress, "version": plan.version}
            for plan in plans
        ]
    }


async def plan_get(ctx: ToolContext, args: PlanIdArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot inspect another plan"}
    plan = await plan_service.get_plan(ctx.db, ctx.owner_id, args.plan_id)
    return {
        "id": plan.id,
        "title": plan.title,
        "description": plan.description,
        "goal": plan.goal,
        "current_level": plan.current_level,
        "deadline": canonical_utc(plan.deadline),
        "weekly_minutes": plan.weekly_minutes,
        "preferences": plan.preferences,
        "expected_outcome": plan.expected_outcome,
        "available_resources": plan.available_resources,
        "avoid_methods": plan.avoid_methods,
        "status": plan.status,
        "progress": plan.progress,
        "version": plan.version,
        "memory_summary": plan.memory_summary,
        "stages": [
            {
                "id": stage.id,
                "title": stage.title,
                "description": stage.description,
                "objectives": stage.objectives,
                "status": stage.status,
                "tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "description": task.description,
                        "kind": task.kind,
                        "status": task.status,
                        "due_at": canonical_utc(task.due_at),
                        "review_due_at": canonical_utc(task.review_due_at),
                        "is_core": task.is_core,
                        "evidence_required": task.evidence_required,
                        "estimated_minutes": task.estimated_minutes,
                        "resource_url": task.resource_url,
                        "task_metadata": task.task_metadata,
                    }
                    for task in stage.tasks
                ],
            }
            for stage in plan.stages
        ],
    }


async def plan_create(ctx: ToolContext, args: PlanCreate) -> dict:
    if ctx.trigger not in {"user_message", "email_reply"} and not ctx.approval_granted:
        return {
            "approval_required": True,
            "blocking": True,
            "reason": "Background runs cannot create a plan without user approval.",
        }
    if ctx.session_id:
        return {
            "error": (
                "Conversation planning must use planning_intake_update and plan_proposal_create. "
                "The proposal is materialized only after explicit user acceptance."
            )
        }
    completeness_issues = plan_service.plan_completeness_issues(args)
    if completeness_issues:
        return {
            "error": "The formal plan is incomplete: " + "; ".join(completeness_issues),
            "issues": completeness_issues,
        }
    plan = await plan_service.create_plan(ctx.db, ctx.owner_id, args, ctx.run_id)
    run = await ctx.db.get(AgentRun, ctx.run_id)
    if run is not None:
        run.created_plan_id = plan.id
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="plan.create",
        entity_type="plan",
        entity_id=str(plan.id),
        forward_patch={"created": plan.id},
        inverse_patch={"delete": plan.id},
    )
    ctx.db.add(operation)
    if ctx.session_id:
        await link_session_plan(
            ctx.db,
            owner_id=ctx.owner_id,
            session_id=ctx.session_id,
            plan_id=plan.id,
            relation_type="created",
            source_run_id=ctx.run_id,
        )
    await flush_uow(ctx.db)
    return {"plan_id": plan.id, "title": plan.title, "stage_count": len(plan.stages), "operation_id": operation.id}


async def task_patch(ctx: ToolContext, args: TaskPatchArgs) -> dict:
    result = await ctx.db.execute(
        select(Task).join(Stage).join(Plan).where(Task.id == args.task_id, Plan.owner_id == ctx.owner_id)
    )
    task = result.scalars().one_or_none()
    if not task:
        return {"error": "Task not found"}
    task_plan_id = (await ctx.db.execute(select(Stage.plan_id).where(Stage.id == task.stage_id))).scalar_one()
    if ctx.plan_id is not None:
        if task_plan_id != ctx.plan_id:
            return {"error": "Plan-focused runs cannot modify another plan"}
    changes = args.changes.model_dump(exclude_unset=True)
    task_plan = await plan_service.get_plan(ctx.db, ctx.owner_id, task_plan_id)
    if task_plan.status == "archived":
        return {"error": "Restore the plan before changing its learning state"}
    if args.expected_plan_version is not None and task_plan.version != args.expected_plan_version:
        return {"error": f"Plan version conflict: expected {args.expected_plan_version}, current {task_plan.version}"}
    if (
        ctx.trigger not in {"user_message", "email_reply"}
        and {"is_core", "evidence_required"}.intersection(changes)
        and not ctx.approval_granted
    ):
        return {
            "approval_required": True,
            "blocking": True,
            "reason": "Background runs cannot change task evidence policy",
        }
    if (
        ctx.trigger not in {"user_message", "email_reply"}
        and changes.get("status") == "completed"
        and not changes.get("evidence")
        and not ctx.approval_granted
    ):
        return {
            "approval_required": True,
            "blocking": True,
            "reason": "Background runs cannot claim that the learner completed a task without evidence",
        }
    mutable_changes = {key: value for key, value in changes.items() if key != "evidence"}
    before = {key: getattr(task, key) for key in mutable_changes}
    if "status" in mutable_changes:
        before["completed_at"] = task.completed_at
    if "evidence" in changes:
        before["task_metadata"] = dict(task.task_metadata)
    updated = await plan_service.update_task(
        ctx.db,
        ctx.owner_id,
        task.id,
        args.changes,
        ctx.run_id,
        session_id=ctx.session_id,
    )
    forward_changes = {
        key: json_safe(getattr(updated, key))
        for key in before
    }
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="task.patch",
        entity_type="task",
        entity_id=str(task.id),
        forward_patch={
            "evidence_protocol": 1,
            "changes": forward_changes,
            "reason": args.reason,
        },
        inverse_patch={"changes": json_safe(before)},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    produced_evidence = list(getattr(updated, "_produced_evidence", []))
    if produced_evidence:
        from app.services.evidence import link_operation_observations

        await link_operation_observations(ctx.db, operation, produced_evidence)
    return {"task_id": updated.id, "status": updated.status, "operation_id": operation.id, "undo_available": True}


async def review_schedule(ctx: ToolContext, args: ReviewScheduleArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot schedule another plan"}
    plan = await ctx.db.get(Plan, args.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if plan.status == "archived":
        return {"error": "Restore the plan before changing its learning state"}
    if args.task_id is not None:
        task_plan_id = (await ctx.db.execute(
            select(Stage.plan_id).join(Task, Task.stage_id == Stage.id).where(Task.id == args.task_id)
        )).scalar_one_or_none()
        if task_plan_id != args.plan_id:
            return {"error": "Task does not belong to the selected plan"}
    duplicate = (await ctx.db.execute(
        select(ReviewSchedule.id).where(
            ReviewSchedule.owner_id == ctx.owner_id,
            ReviewSchedule.plan_id == args.plan_id,
            ReviewSchedule.task_id == args.task_id,
            ReviewSchedule.due_at == args.due_at,
            ReviewSchedule.review_type == args.review_type,
            ReviewSchedule.status == "scheduled",
        ).limit(1)
    )).scalar_one_or_none()
    if duplicate:
        return {"error": f"An identical review is already scheduled as review {duplicate}"}
    schedule = ReviewSchedule(owner_id=ctx.owner_id, **args.model_dump())
    ctx.db.add(schedule)
    await flush_uow(ctx.db)
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="review.schedule",
        entity_type="review_schedule",
        entity_id=str(schedule.id),
        forward_patch={"created": schedule.id},
        inverse_patch={"delete": schedule.id},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    return {
        "review_id": schedule.id,
        "due_at": canonical_utc(schedule.due_at),
        "operation_id": operation.id,
    }


async def review_resolve(ctx: ToolContext, args: ReviewResolveArgs) -> dict:
    review = await ctx.db.get(ReviewSchedule, args.review_id)
    if not review or review.owner_id != ctx.owner_id:
        return {"error": "Review schedule not found"}
    if ctx.plan_id is not None and review.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot resolve another plan's review"}
    plan = await ctx.db.get(Plan, review.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if plan.status == "archived":
        return {"error": "Restore the plan before changing its learning state"}
    if review.status != "scheduled":
        return {
            "error": "This review schedule is already resolved",
            "review_id": review.id,
            "status": review.status,
        }
    if args.action == "snooze" and args.next_due_at is None:
        return {"error": "Snoozing a review requires next_due_at"}
    if (
        args.action in {"complete", "cancel"}
        and ctx.trigger not in {"user_message", "email_reply"}
        and not ctx.approval_granted
    ):
        return {
            "approval_required": True,
            "blocking": True,
            "reason": "A background run cannot resolve a learner review without confirmation",
        }

    before = {"status": review.status, "due_at": canonical_utc(review.due_at)}
    if args.action == "snooze":
        review.due_at = args.next_due_at
    else:
        review.status = "completed" if args.action == "complete" else "cancelled"
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="review.resolve",
        entity_type="review_schedule",
        entity_id=str(review.id),
        forward_patch={
            "action": args.action,
            "status": review.status,
            "due_at": canonical_utc(review.due_at),
            "reason": args.reason,
        },
        inverse_patch={"changes": before},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    return {
        "review_id": review.id,
        "status": review.status,
        "due_at": canonical_utc(review.due_at),
        "operation_id": operation.id,
        "undo_available": True,
    }


async def quiz_create(ctx: ToolContext, args: QuizCreateArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot create a quiz for another plan"}
    plan = await ctx.db.get(Plan, args.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if plan.status == "archived":
        return {"error": "Restore the plan before changing its learning state"}
    if args.task_id is not None:
        task_plan_id = (await ctx.db.execute(
            select(Stage.plan_id).join(Task, Task.stage_id == Stage.id).where(Task.id == args.task_id)
        )).scalar_one_or_none()
        if task_plan_id != args.plan_id:
            return {"error": "Task does not belong to the selected plan"}
    quiz = Quiz(owner_id=ctx.owner_id, run_id=ctx.run_id, **args.model_dump())
    ctx.db.add(quiz)
    await flush_uow(ctx.db)
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="quiz.create",
        entity_type="quiz",
        entity_id=str(quiz.id),
        forward_patch={"created": quiz.id},
        inverse_patch={"delete": quiz.id},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    return {
        "quiz_id": quiz.id,
        "status": quiz.status,
        "prompt": quiz.prompt,
        "operation_id": operation.id,
        "undo_available": True,
    }


async def quiz_get(ctx: ToolContext, args: QuizIdArgs) -> dict:
    quiz = await ctx.db.get(Quiz, args.quiz_id)
    if not quiz or quiz.owner_id != ctx.owner_id:
        return {"error": "Quiz not found"}
    if ctx.plan_id is not None and quiz.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot inspect another plan's quiz"}
    return {
        "quiz_id": quiz.id,
        "plan_id": quiz.plan_id,
        "task_id": quiz.task_id,
        "prompt": quiz.prompt,
        "rubric": quiz.rubric,
        "status": quiz.status,
        "previous_answer": quiz.answer,
    }


async def quiz_grade(ctx: ToolContext, args: QuizGradeArgs) -> dict:
    quiz = await ctx.db.get(Quiz, args.quiz_id)
    if not quiz or quiz.owner_id != ctx.owner_id:
        return {"error": "Quiz not found"}
    if ctx.plan_id is not None and quiz.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot grade another plan's quiz"}
    plan = await ctx.db.get(Plan, quiz.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if plan.status == "archived":
        return {"error": "Restore the plan before changing its learning state"}
    if quiz.status != "open":
        return {
            "error": "This quiz attempt was already graded; create a new quiz for another attempt",
            "quiz_id": quiz.id,
            "status": quiz.status,
        }
    before = {
        "answer": quiz.answer,
        "score": quiz.score,
        "feedback": quiz.feedback,
        "evidence": quiz.evidence,
        "status": quiz.status,
        "graded_at": canonical_utc(quiz.graded_at),
    }
    quiz.answer = args.answer
    quiz.score = args.score
    quiz.feedback = args.feedback
    quiz.evidence = args.evidence
    quiz.status = "passed" if args.score >= 70 else "needs_review"
    quiz.graded_at = utc_now()
    learning_event = LearningEvent(
        owner_id=ctx.owner_id,
        plan_id=quiz.plan_id,
        task_id=quiz.task_id,
        run_id=ctx.run_id,
        event_type="quiz.graded",
        summary=f"Quiz {quiz.id} scored {args.score:.0f}",
        payload={"score": args.score, "status": quiz.status, "evidence": args.evidence},
        correlation_id=ctx.run_id,
        occurred_at=quiz.graded_at,
    )
    ctx.db.add(learning_event)
    review = None
    if args.next_review_at:
        review = ReviewSchedule(
            owner_id=ctx.owner_id,
            plan_id=quiz.plan_id,
            task_id=quiz.task_id,
            due_at=args.next_review_at,
            review_type="quiz",
        )
        ctx.db.add(review)
    profile = await ctx.db.get(UserProfile, ctx.owner_id)
    profile_before = None
    day = None
    day_before = None
    unlocked = []
    if profile:
        profile_before = {
            "xp": profile.xp,
            "level": profile.level,
            "streak_days": profile.streak_days,
        }
        earned_xp = (30 if args.score >= 70 else 10) if before["status"] == "open" else 0
        profile.xp += earned_xp
        profile.level = 1 + profile.xp // 100
        day_key = utc_now().date().isoformat()
        day = await ctx.db.scalar(
            select(ActivityDay).where(
                ActivityDay.owner_id == ctx.owner_id,
                ActivityDay.date == day_key,
            )
        )
        if day is None:
            day = ActivityDay(owner_id=ctx.owner_id, date=day_key)
            ctx.db.add(day)
            await flush_uow(ctx.db)
        else:
            day_before = {
                "xp": day.xp,
                "completed_tasks": day.completed_tasks,
                "passed_quizzes": day.passed_quizzes,
            }
        day.xp += earned_xp
        day.passed_quizzes += int(args.score >= 70)
        from app.services.gamification import evaluate_achievements

        unlocked = await evaluate_achievements(ctx.db, ctx.owner_id)
    await flush_uow(ctx.db)
    from app.services.evidence import (
        append_observation,
        artifact_ref,
        create_artifact,
        link_operation_observations,
        normalize_percentage_score,
        refresh_plan_evidence_projection,
    )

    quiz_artifact, _ = await create_artifact(
        ctx.db,
        owner_id=ctx.owner_id,
        artifact_type="quiz_answer",
        source_uri=f"quiz:{quiz.id}:answer",
        idempotency_key=f"quiz:{quiz.id}:answer:artifact",
        title=f"测验回答：{quiz.id}",
        content=args.answer,
        metadata={"quiz_id": quiz.id, "evidence": args.evidence},
        plan_id=quiz.plan_id,
        task_id=quiz.task_id,
        run_id=ctx.run_id,
        session_id=ctx.session_id,
    )

    observation, _ = await append_observation(
        ctx.db,
        owner_id=ctx.owner_id,
        source_type="quiz",
        source_id=f"{quiz.id}:grade",
        outcome="passed" if quiz.status == "passed" else "needs_revision",
        idempotency_key=f"quiz:{quiz.id}:graded",
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        plan_id=quiz.plan_id,
        task_id=quiz.task_id,
        normalized_score=normalize_percentage_score(args.score),
        is_correct=quiz.status == "passed",
        rubric_snapshot=quiz.rubric,
        evaluator={"type": "agent", "run_id": ctx.run_id},
        payload={"answer_length": len(args.answer), "evidence": args.evidence},
        artifact_refs=[artifact_ref(quiz_artifact, kind="quiz_answer")],
        occurred_at=quiz.graded_at,
        correlation_id=ctx.run_id,
        causation_id=f"learning_event:{learning_event.id}",
    )
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="quiz.grade",
        entity_type="quiz",
        entity_id=str(quiz.id),
        forward_patch={
            "evidence_protocol": 1,
            "changes": {
                "answer": quiz.answer,
                "score": quiz.score,
                "feedback": quiz.feedback,
                "evidence": quiz.evidence,
                "status": quiz.status,
                "graded_at": canonical_utc(quiz.graded_at),
            },
            "award": {
                "profile": (
                    {
                        "xp": profile.xp,
                        "level": profile.level,
                        "streak_days": profile.streak_days,
                    }
                    if profile is not None
                    else None
                ),
                "day": (
                    {
                        "id": day.id,
                        "date": day.date,
                        "xp": day.xp,
                        "completed_tasks": day.completed_tasks,
                        "passed_quizzes": day.passed_quizzes,
                    }
                    if day is not None
                    else None
                ),
                "achievements": [
                    {
                        "id": item.id,
                        "key": item.key,
                        "title": item.title,
                        "description": item.description,
                        "badge_kind": item.badge_kind,
                        "badge_image_url": item.badge_image_url,
                        "unlocked_at": canonical_utc(item.unlocked_at),
                    }
                    for item in unlocked
                ],
            },
            "review": (
                {
                    "id": review.id,
                    "owner_id": review.owner_id,
                    "plan_id": review.plan_id,
                    "task_id": review.task_id,
                    "due_at": canonical_utc(review.due_at),
                    "review_type": review.review_type,
                    "status": review.status,
                }
                if review is not None
                else None
            ),
        },
        inverse_patch={
            "changes": before,
            "award": {
                "profile": profile_before,
                "day_id": day.id if day is not None else None,
                "day": day_before,
                "delete_achievements": [item.id for item in unlocked],
            },
            "delete_learning_event": learning_event.id,
            "delete_review": review.id if review else None,
        },
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    await link_operation_observations(ctx.db, operation, [observation])
    await refresh_plan_evidence_projection(ctx.db, ctx.owner_id, quiz.plan_id)
    return {
        "quiz_id": quiz.id,
        "score": quiz.score,
        "status": quiz.status,
        "operation_id": operation.id,
        "undo_available": True,
    }


async def memory_propose(ctx: ToolContext, args: MemoryProposalArgs) -> dict:
    from app.context.memory import MemoryManager

    if args.scope == "plan":
        target_plan = int(args.scope_id) if args.scope_id else ctx.plan_id
        if target_plan is None:
            return {"error": "Plan memory requires scope_id or a focused plan"}
        if ctx.plan_id is not None and target_plan != ctx.plan_id:
            return {"error": "Plan-focused runs cannot write another plan's memory"}
        args.scope_id = str(target_plan)
    elif args.scope == "session":
        if ctx.session_id is None:
            return {"error": "Session memory requires an active conversation"}
        if args.scope_id not in {None, ctx.session_id}:
            return {"error": "A run cannot write another session's private memory"}
        args.scope_id = ctx.session_id
    try:
        memory, deduplicated = await MemoryManager(ctx.db).propose(
            ctx.owner_id,
            source_type="agent_run",
            source_id=ctx.run_id,
            **args.model_dump(),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    await flush_uow(ctx.db)
    await ctx.db.refresh(memory)
    return {
        "memory_id": memory.id,
        "status": memory.status,
        "deduplicated": deduplicated,
        "approval_required": memory.status == "proposed",
    }


async def notification_send(ctx: ToolContext, args: NotificationArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id not in {None, ctx.plan_id}:
        return {"error": "Plan-focused runs cannot notify about another plan"}
    if args.plan_id is None:
        args.plan_id = ctx.plan_id
    return await NotificationService(ctx.db).send(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        trigger=ctx.trigger,
        title=args.title,
        body=args.body,
        plan_id=args.plan_id,
        channels=args.channels,
        invocation_id=ctx.invocation_id,
        action_key=ctx.action_key,
        request_digest=ctx.request_digest,
        reply_to_intervention_id=ctx.reply_to_intervention_id,
    )


TOOLS = [
    ToolDefinition("profile_get", "Read the learner's global profile and notification preferences.", EmptyArgs, profile_get, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("plan_list", "List all learning plans and their current progress.", EmptyArgs, plan_list, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("plan_get", "Inspect one complete plan with stages and tasks.", PlanIdArgs, plan_get, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("plan_create", "Create a complete learning plan requested by the user.", PlanCreate, plan_create, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("task_patch", "Update a task status, due time, duration, or review time.", TaskPatchArgs, task_patch, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("review_schedule", "Schedule a future review or proactive quiz.", ReviewScheduleArgs, review_schedule, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("review_resolve", "Complete, snooze, or cancel one existing review schedule.", ReviewResolveArgs, review_resolve, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("quiz_create", "Create an evidence-based quiz for an active plan.", QuizCreateArgs, quiz_create, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("quiz_get", "Read a quiz prompt and grading rubric before evaluating an answer.", QuizIdArgs, quiz_get, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("quiz_grade", "Store an evidence-based quiz grade and schedule the next review.", QuizGradeArgs, quiz_grade, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("memory_propose", "Propose a long-term memory for user confirmation.", MemoryProposalArgs, memory_propose, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("notification_send", "Send an in-app notification and optionally queue email/browser delivery.", NotificationArgs, notification_send, effect_kind=ToolEffectKind.EXTERNAL_WRITE, idempotent=True),
] + PLANNING_TOOLS + SUBAGENT_TOOLS + LEARNING_TOOLS + MEMORY_TOOLS + WEB_TOOLS + WORKSPACE_TOOLS + CALENDAR_TOOLS + COMPETENCY_TOOLS

attach_output_contracts(TOOLS)

TOOL_MAP = {tool.name: tool for tool in TOOLS}


def _idempotency_key(
    run_id: str,
    tool_name: str,
    raw_arguments: str,
    tool_call_id: str | None = None,
) -> str:
    """Return a stable action identity that does not include request content."""

    # A direct caller without a provider id gets one fail-closed slot for this
    # tool/run.  Request content is never folded into the action identity.
    # A second different request therefore conflicts instead of becoming a
    # second, accidentally authorized action.
    del raw_arguments
    identity = f"provider:{tool_call_id}" if tool_call_id else "direct"
    digest = hashlib.sha256(f"{run_id}|{tool_name}|{identity}".encode("utf-8")).hexdigest()
    return f"{run_id[:8]}:{tool_name}:{digest[:48]}"


def _canonical_request(
    tool: ToolDefinition,
    raw_arguments: str,
) -> tuple[BaseModel, dict[str, Any], str]:
    """Validate first, expand defaults, then fingerprint canonical JSON."""

    args = tool.args_model.model_validate(parse_arguments(raw_arguments))
    canonical_args = args.model_dump(mode="json")
    encoded = json.dumps(
        canonical_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return args, canonical_args, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _forbidden_secret_material(
    raw_arguments: str,
    canonical_args: dict[str, Any],
    *,
    tool_call_id: str | None,
) -> str | None:
    """Reject credentials and redaction placeholders before durable claim."""

    canonical_text = json.dumps(
        canonical_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    candidates = (raw_arguments, canonical_text, tool_call_id or "")
    if any(REDACTED in candidate for candidate in candidates):
        return "REDACTED_SECRET_REPLAY_FORBIDDEN"
    if any(
        secret in candidate
        for secret in configured_secret_values()
        for candidate in candidates
    ):
        return "CONFIGURED_SECRET_IN_TOOL_ARGUMENTS"
    return None


def _busy_result(exc: DatabaseBusyError) -> dict[str, Any]:
    result = exc.as_result()
    result.setdefault("ok", False)
    return result


def _conflict_result(
    invocation: ToolInvocation,
    request_digest: str,
) -> dict[str, Any]:
    legacy = not bool(invocation.request_digest)
    return {
        "ok": False,
        "error": (
            "The existing action has no trustworthy request digest and requires reconciliation."
            if legacy
            else "The stable action key was already used with different validated arguments."
        ),
        "error_code": "idempotency_conflict",
        "status": "needs_reconciliation" if legacy else invocation.status,
        "retryable": False,
        "uncertain_outcome": legacy,
        "request_digest": request_digest,
    }


async def _existing_result(
    db: AsyncSession,
    invocation: ToolInvocation,
    request_digest: str,
) -> dict[str, Any] | None:
    if invocation.request_digest != request_digest:
        return _conflict_result(invocation, request_digest)
    if invocation.status == "committed":
        result: dict[str, Any] = {
            "ok": True,
            "data": redact_data(dict(invocation.result_payload or {})),
            "replayed": True,
            "status": "committed",
        }
        if invocation.effect_kind == ToolEffectKind.DATABASE_WRITE.value:
            # The original database-write completion was committed atomically
            # with its domain facts. Tell runtime never to append a duplicate;
            # include the durable event when available so SSE can project the
            # same sequence rather than inventing a new one.
            result["completion_event_persisted"] = True
            events = list((await db.execute(
                select(RunEvent).where(
                    RunEvent.run_id == invocation.run_id,
                    RunEvent.event_type == "tool.completed",
                ).order_by(RunEvent.sequence.desc())
            )).scalars())
            completion = next(
                (
                    event
                    for event in events
                    if (event.payload or {}).get("invocation_id") == invocation.id
                ),
                None,
            )
            if completion is not None:
                result["completion_event"] = {
                    "sequence": completion.sequence,
                    "type": completion.event_type,
                    "summary": completion.summary,
                    "payload": completion.payload,
                    "created_at": canonical_utc(completion.created_at),
                }
        return result
    if invocation.status == "needs_reconciliation":
        return {
            "ok": False,
            "error": "The external outcome is uncertain and must be reconciled before retrying.",
            "error_code": "needs_reconciliation",
            "status": "needs_reconciliation",
            "retryable": False,
            "uncertain_outcome": True,
        }
    if invocation.status in {"failed", "cancelled", "rejected"}:
        return {
            "ok": False,
            "error": redact_text(
                (invocation.result_payload or {}).get("error")
                or "The invocation failed."
            ),
            "error_code": (
                "approval_rejected"
                if invocation.status == "rejected"
                else "invocation_failed"
            ),
            "status": invocation.status,
            "retryable": False,
            "replayed": True,
        }
    return None


async def _linked_outbox_result(
    db: AsyncSession,
    invocation: ToolInvocation,
) -> tuple[bool, dict[str, Any] | None]:
    """Aggregate every delivery linked to an invocation, never just one row."""
    actions = list((await db.execute(
        select(OutboxAction).where(OutboxAction.invocation_id == invocation.id)
    )).scalars())
    if not actions:
        return False, None
    statuses = {action.status for action in actions}
    if statuses.intersection({"delivering", "needs_reconciliation"}):
        return True, {
            "ok": False,
            "error": "The external outcome is uncertain and requires reconciliation.",
            "error_code": "needs_reconciliation",
            "status": "needs_reconciliation",
            "retryable": False,
            "uncertain_outcome": True,
        }
    if statuses.intersection({"queued", "retry_pending"}):
        return True, {
            "ok": True,
            "data": redact_data(dict(invocation.result_payload or {})),
            "replayed": True,
            "status": "pending_delivery",
        }
    if statuses == {"delivered"}:
        return True, {
            "ok": True,
            "data": redact_data(dict(invocation.result_payload or {})),
            "replayed": True,
            "status": "committed",
        }
    failed = next(
        (action for action in actions if action.status in {"failed", "cancelled"}),
        None,
    )
    if failed is not None:
        return True, {
            "ok": False,
            "error": str(failed.last_error or "The external delivery failed."),
            "error_code": "external_delivery_failed",
            "status": failed.status,
            "retryable": False,
            "replayed": True,
        }
    return True, {
        "ok": False,
        "error": "The linked delivery set is in an unsupported durable state.",
        "error_code": "needs_reconciliation",
        "status": "needs_reconciliation",
        "retryable": False,
        "uncertain_outcome": True,
    }


async def _release_claim_read(ctx: ToolContext) -> None:
    """End the clean claim/read phase without expiring caller snapshots."""
    if (
        ctx.db.in_nested_transaction()
        or ctx.db.new
        or ctx.db.dirty
        or ctx.db.deleted
        or ctx.db.sync_session.info.get("h2_session_write_activity")
    ):
        raise RuntimeError("tool claim requires a clean caller session")
    # expire_on_commit=False keeps already-loaded direct-integration objects
    # usable while still guaranteeing no transaction crosses an external wait.
    await commit_uow(ctx.db)


async def _claim_invocation(
    tool: ToolDefinition,
    name: str,
    canonical_args: dict[str, Any],
    request_digest: str,
    ctx: ToolContext,
) -> tuple[ToolInvocation | None, dict[str, Any] | None]:
    """Claim one action in a short transaction and release SQLite's writer."""

    key = _idempotency_key(ctx.run_id, name, "", ctx.tool_call_id)
    claim_token = uuid.uuid4().hex
    claimed_at = utc_now()
    claim_ttl = 30 if tool.effect_kind in {
        ToolEffectKind.EXTERNAL_READ,
        ToolEffectKind.EXTERNAL_WRITE,
    } else 5
    claim_expires_at = claimed_at + timedelta(minutes=claim_ttl)
    if ctx.db.bind is None:  # pragma: no cover - an application session is always bound
        return None, {
            "ok": False,
            "error": "The tool session has no database binding.",
            "error_code": "database_unavailable",
            "retryable": True,
        }
    short_sessions = async_sessionmaker(
        ctx.db.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def insert_claim(short_db: AsyncSession) -> int:
        invocation = ToolInvocation(
            owner_id=ctx.owner_id,
            run_id=ctx.run_id,
            idempotency_key=key,
            tool_name=name,
            tool_call_id=ctx.tool_call_id,
            args_hash=request_digest,
            request_digest=request_digest,
            canonical_args=canonical_args,
            effect_kind=tool.effect_kind.value,
            claim_token=claim_token,
            status="running",
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
        )
        short_db.add(invocation)
        await flush_uow(short_db)
        return invocation.id

    try:
        invocation_id = await run_short_transaction(short_sessions, insert_claim)
        invocation = await ctx.db.get(ToolInvocation, invocation_id)
        return invocation, None
    except IntegrityError:
        pass
    except DatabaseBusyError as exc:
        return None, _busy_result(exc)

    existing = await ctx.db.scalar(
        select(ToolInvocation).where(ToolInvocation.idempotency_key == key)
    )
    if existing is None:
        return None, {
            "ok": False,
            "error": "The invocation claim conflicted but no durable owner was found.",
            "error_code": "invocation_claim_lost",
            "status": "retry_pending",
            "retryable": True,
        }
    observed = await _existing_result(ctx.db, existing, request_digest)
    if observed is not None:
        return None, observed

    if existing.status == "pending_delivery":
        has_actions, outbox_result = await _linked_outbox_result(ctx.db, existing)
        if not has_actions:
            return None, {
                "ok": False,
                "error": "A pending external action has no durable outbox intent.",
                "error_code": "needs_reconciliation",
                "status": "needs_reconciliation",
                "retryable": False,
                "uncertain_outcome": True,
            }
        return None, outbox_result

    retryable_statuses = {"retry_pending"}
    if existing.status == "pending_approval" and not ctx.approval_granted:
        return None, {
            "ok": True,
            "data": dict(existing.result_payload or {}),
            "replayed": True,
            "status": "pending_approval",
        }
    if existing.status == "pending_approval":
        retryable_statuses.add("pending_approval")
    if existing.status == "running":
        expires_at = existing.claim_expires_at
        if expires_at is None or expires_at > utc_now():
            return None, {
                "ok": False,
                "error": "This logical action is already claimed by another executor.",
                "error_code": "invocation_in_progress",
                "status": "running",
                "reason": "already_claimed",
                "retryable": True,
                "uncertain_outcome": True,
            }
        if tool.effect_kind == ToolEffectKind.EXTERNAL_WRITE:
            has_actions, outbox_result = await _linked_outbox_result(ctx.db, existing)
            if not has_actions:
                retryable_statuses.add("running")
            elif outbox_result and outbox_result.get("status") != "needs_reconciliation":
                return None, outbox_result
            else:
                existing_id = existing.id
                existing_claim_token = existing.claim_token
                existing_version = existing.version
                await _release_claim_read(ctx)

                async def fence_uncertain(short_db: AsyncSession) -> bool:
                    result = await short_db.execute(
                        update(ToolInvocation)
                        .where(
                            ToolInvocation.id == existing_id,
                            ToolInvocation.status == "running",
                            ToolInvocation.request_digest == request_digest,
                            ToolInvocation.claim_token == existing_claim_token,
                            ToolInvocation.version == existing_version,
                        )
                        .values(
                            status="needs_reconciliation",
                            claim_token=None,
                            version=ToolInvocation.version + 1,
                        )
                    )
                    return result.rowcount == 1

                try:
                    await run_short_transaction(short_sessions, fence_uncertain)
                except DatabaseBusyError as exc:
                    return None, _busy_result(exc)
                return None, {
                    "ok": False,
                    "error": "The expired external-write claim has an uncertain outcome.",
                    "error_code": "needs_reconciliation",
                    "status": "needs_reconciliation",
                    "retryable": False,
                    "uncertain_outcome": True,
                }
        else:
            retryable_statuses.add("running")
    if existing.status not in retryable_statuses:
        return None, {
            "ok": False,
            "error": f"Invocation is in unsupported durable state {existing.status!r}.",
            "error_code": "invocation_state_conflict",
            "status": existing.status,
            "retryable": False,
        }

    existing_id = existing.id
    existing_status = existing.status
    existing_claim_token = existing.claim_token
    existing_version = existing.version
    await _release_claim_read(ctx)

    async def reclaim(short_db: AsyncSession) -> bool:
        reclaim_conditions = [
            ToolInvocation.id == existing_id,
            ToolInvocation.status == existing_status,
            ToolInvocation.request_digest == request_digest,
            ToolInvocation.claim_token == existing_claim_token,
            ToolInvocation.version == existing_version,
        ]
        if existing_status == "running":
            reclaim_conditions.append(
                ToolInvocation.claim_expires_at <= claimed_at
            )
        result = await short_db.execute(
            update(ToolInvocation)
            .where(*reclaim_conditions)
            .values(
                status="running",
                claim_token=claim_token,
                claimed_at=claimed_at,
                claim_expires_at=claim_expires_at,
                attempt=ToolInvocation.attempt + 1,
                version=ToolInvocation.version + 1,
                result_payload={},
            )
        )
        return result.rowcount == 1

    try:
        reclaimed = await run_short_transaction(short_sessions, reclaim)
    except DatabaseBusyError as exc:
        return None, _busy_result(exc)
    if not reclaimed:
        return None, {
            "ok": False,
            "error": "Another executor reclaimed this invocation first.",
            "error_code": "invocation_in_progress",
            "status": "running",
            "reason": "already_claimed",
            "retryable": True,
        }
    invocation = await ctx.db.get(
        ToolInvocation,
        existing_id,
        populate_existing=True,
    )
    return invocation, None


async def _persist_invocation_failure(
    invocation_id: int,
    claim_token: str,
    claim_version: int,
    ctx: ToolContext,
    error: str,
) -> bool:
    safe_error = redact_text(error)
    await rollback_uow(ctx.db)
    result = await ctx.db.execute(
        update(ToolInvocation)
        .where(
            ToolInvocation.id == invocation_id,
            ToolInvocation.status == "running",
            ToolInvocation.claim_token == claim_token,
            ToolInvocation.version == claim_version,
        )
        .values(
            status="failed",
            result_payload={"error": safe_error},
            claim_token=None,
            completed_at=utc_now(),
            version=ToolInvocation.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await rollback_uow(ctx.db)
        return False
    await commit_uow(ctx.db)
    return True


async def _transition_owned_invocation(
    *,
    invocation_id: int,
    claim_token: str,
    claim_version: int,
    status: str,
    result_payload: dict[str, Any],
    ctx: ToolContext,
) -> ToolInvocation:
    result = await ctx.db.execute(
        update(ToolInvocation)
        .where(
            ToolInvocation.id == invocation_id,
            ToolInvocation.status == "running",
            ToolInvocation.claim_token == claim_token,
            ToolInvocation.version == claim_version,
        )
        .values(
            status=status,
            result_payload=redact_data(result_payload),
            claim_token=None,
            claimed_at=None,
            claim_expires_at=None,
            completed_at=utc_now() if status == "committed" else None,
            version=ToolInvocation.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await rollback_uow(ctx.db)
        raise InvocationClaimLostError("durable invocation lease changed")
    invocation = await ctx.db.get(
        ToolInvocation,
        invocation_id,
        populate_existing=True,
    )
    if invocation is None:  # pragma: no cover - CAS target cannot disappear in-UoW
        await rollback_uow(ctx.db)
        raise InvocationClaimLostError("durable invocation disappeared")
    return invocation


async def _mark_retry_pending(
    *,
    invocation_id: int,
    claim_token: str,
    claim_version: int,
    ctx: ToolContext,
) -> bool:
    """Release a claim after a safe pre-effect rollback/final-commit busy."""
    await rollback_uow(ctx.db)
    if ctx.db.bind is None:  # pragma: no cover - application sessions are bound
        return False
    sessions = async_sessionmaker(
        ctx.db.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def transition(short_db: AsyncSession) -> bool:
        result = await short_db.execute(
            update(ToolInvocation)
            .where(
                ToolInvocation.id == invocation_id,
                ToolInvocation.status == "running",
                ToolInvocation.claim_token == claim_token,
                ToolInvocation.version == claim_version,
            )
            .values(
                status="retry_pending",
                claim_token=None,
                version=ToolInvocation.version + 1,
            )
        )
        return result.rowcount == 1

    try:
        return bool(await run_short_transaction(sessions, transition))
    except DatabaseBusyError:
        return False


def _claim_lost_result() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "This executor no longer owns the durable invocation claim.",
        "error_code": "invocation_claim_lost",
        "status": "running",
        "retryable": True,
        "uncertain_outcome": False,
    }


async def _durable_claim_loss_result(
    *,
    invocation_id: int,
    request_digest: str,
    ctx: ToolContext,
) -> dict[str, Any]:
    """Prefer a stronger durable outbox/final result over a generic lost lease."""
    await rollback_uow(ctx.db)
    invocation = await ctx.db.get(ToolInvocation, invocation_id)
    if invocation is None:
        return _claim_lost_result()
    observed = await _existing_result(ctx.db, invocation, request_digest)
    if observed is not None:
        return observed
    if invocation.status == "pending_delivery":
        has_actions, outbox_result = await _linked_outbox_result(ctx.db, invocation)
        if has_actions and outbox_result is not None:
            return outbox_result
    result = _claim_lost_result()
    result["status"] = invocation.status
    return result


async def _append_atomic_completion(
    *,
    invocation_id: int,
    claim_token: str,
    claim_version: int,
    name: str,
    data: dict[str, Any],
    ctx: ToolContext,
) -> RunEvent:
    """Finalize domain facts, audit Operation, event, and result in one UoW."""

    pending_objects = [*ctx.db.identity_map.values(), *ctx.db.new]
    for item in pending_objects:
        if isinstance(item, Operation) and item.invocation_id is None:
            item.invocation_id = invocation_id
    next_sequence = int(
        await ctx.db.scalar(
            select(func.coalesce(func.max(RunEvent.sequence), 0)).where(
                RunEvent.run_id == ctx.run_id
            )
        )
        or 0
    ) + 1
    event_summary, event_payload = redact_event_fields(
        f"工具 {name} 完成",
        {
            "tool_call_id": ctx.tool_call_id,
            "name": name,
            "result": {"ok": True, "data": data},
            "invocation_id": invocation_id,
        },
    )
    event = RunEvent(
        run_id=ctx.run_id,
        sequence=next_sequence,
        event_type="tool.completed",
        event_key=f"tool:{ctx.tool_call_id}:completed" if ctx.tool_call_id else None,
        summary=event_summary,
        payload=event_payload,
    )
    ctx.db.add(event)
    invocation = await _transition_owned_invocation(
        invocation_id=invocation_id,
        claim_token=claim_token,
        claim_version=claim_version,
        status="committed",
        result_payload=data,
        ctx=ctx,
    )
    # Hold a strong reference through before_commit hooks and the actual
    # commit. SQLAlchemy's identity map is weak; dropping this local would make
    # fault injection/audit observers unable to see the atomic final state.
    assert invocation.status == "committed"
    await commit_uow(ctx.db)
    ctx.completion_event_persisted = True
    return event


def _read_only_forbidden(tool: ToolDefinition, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "Read-only continuations cannot execute this effect.",
        "error_code": "read_only_effect_forbidden",
        "reason_code": "READ_ONLY_EFFECT_FORBIDDEN",
        "guard_reason": reason,
        "effect_kind": tool.effect_kind.value,
        "retryable": False,
    }


async def execute_tool(name: str, raw_arguments: str, ctx: ToolContext) -> dict:
    tool = TOOL_MAP.get(name)
    if not tool:
        return {
            "ok": False,
            "error": f"Unknown tool: {name}",
            "error_code": "unknown_tool",
            "retryable": False,
        }
    # Capability availability is checked before argument handling or durable
    # invocation claim creation.  Keep this pre-execution policy seam distinct
    # from the external-untrusted authorization guard that H6-TRUST wires next.
    if name == "code_execute" and not current_code_execution_policy().available:
        return code_execution_rejection()
    try:
        args, canonical_args, request_digest = _canonical_request(tool, raw_arguments)
    except Exception as exc:
        error = redact_text(f"{type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "error": error,
            "error_code": "invalid_arguments",
            "retryable": False,
        }
    secret_reason = _forbidden_secret_material(
        raw_arguments,
        canonical_args,
        tool_call_id=ctx.tool_call_id,
    )
    if secret_reason is not None:
        return {
            "ok": False,
            "error": "Tool arguments cannot contain configured credentials or redacted placeholders.",
            "error_code": "secret_material_forbidden",
            "reason_code": secret_reason,
            "retryable": False,
        }
    if ctx.execution_mode == "read_only" and tool.effect_kind not in {
        ToolEffectKind.PURE_READ,
        ToolEffectKind.EXTERNAL_READ,
    }:
        if name != "notification_send":
            return _read_only_forbidden(tool, "state_changing_tool")
        if ctx.reply_to_intervention_id is None:
            return _read_only_forbidden(tool, "missing_reply_intervention")
        if args.plan_id not in {None, ctx.plan_id}:
            return _read_only_forbidden(tool, "cross_plan_reply")
        reply_target = await ctx.db.get(Intervention, ctx.reply_to_intervention_id)
        if (
            reply_target is None
            or reply_target.owner_id != ctx.owner_id
            or reply_target.plan_id != ctx.plan_id
            or reply_target.session_id != ctx.session_id
            or reply_target.state not in {"active", "replied"}
        ):
            return _read_only_forbidden(tool, "invalid_reply_intervention")
        original_channels = set(
            (
                await ctx.db.execute(
                    select(Notification.channel).where(
                        Notification.intervention_id == reply_target.id
                    )
                )
            ).scalars()
        )
        if not set(args.channels) or not set(args.channels).issubset(original_channels):
            return _read_only_forbidden(tool, "reply_channel_not_original")

    invocation: ToolInvocation | None = None
    invocation_id: int | None = None
    claim_token: str | None = None
    claim_version: int | None = None
    # Every non-pure effect is coordinated, regardless of the legacy marker.
    # Retain replay for explicitly idempotent pure probes/direct integrations.
    needs_claim = tool.idempotent or tool.effect_kind != ToolEffectKind.PURE_READ
    if needs_claim:
        if (
            ctx.db.in_nested_transaction()
            or ctx.db.new
            or ctx.db.dirty
            or ctx.db.deleted
            or ctx.db.sync_session.info.get("h2_session_write_activity")
        ):
            return {
                "ok": False,
                "error": (
                    "Tool execution requires a clean caller Unit of Work; "
                    "commit or roll it back explicitly before claiming an action."
                ),
                "error_code": "caller_unit_of_work_not_clean",
                "status": "not_claimed",
                "retryable": False,
            }
        # End a pre-existing read snapshot before the independent short claim,
        # otherwise SQLite WAL readers may not observe the newly inserted row.
        await _release_claim_read(ctx)
        invocation, observed = await _claim_invocation(
            tool,
            name,
            canonical_args,
            request_digest,
            ctx,
        )
        await _release_claim_read(ctx)
        if observed is not None:
            return observed
        if invocation is None:  # pragma: no cover - defensive
            return {
                "ok": False,
                "error": "Invocation claim did not produce a durable owner.",
                "error_code": "invocation_claim_lost",
                "retryable": True,
            }
        invocation_id = invocation.id
        claim_token = invocation.claim_token
        claim_version = invocation.version
        if not claim_token:  # pragma: no cover - every live claim has a token
            return _claim_lost_result()
        action_key = invocation.idempotency_key
        ctx.invocation_id = invocation_id
        ctx.action_key = action_key
        ctx.request_digest = request_digest
        ctx.claim_token = claim_token
        ctx.claim_version = claim_version
        # Keep only scalar claim identity; the clean caller transaction was
        # released above before any external wait.
        invocation = None

    if tool.effect_kind in {
        ToolEffectKind.DATABASE_WRITE,
        ToolEffectKind.EXTERNAL_WRITE,
    }:
        if (
            invocation_id is None
            or claim_token is None
            or claim_version is None
        ):  # pragma: no cover - every write effect is claimed above
            raise RuntimeError("write-effect authority guard requires a durable invocation")
        authority = await authorize_side_effect(
            ctx.db,
            owner_id=ctx.owner_id,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            tool_name=name,
            effect_kind=tool.effect_kind.value,
            request_digest=request_digest,
            canonical_args=canonical_args,
            invocation_id=invocation_id,
            claim_token=claim_token,
            claim_version=claim_version,
        )
        if not authority.allowed:
            if not authority.requires_approval:
                error = "The durable authority chain is invalid for this side effect."
                persisted = await _persist_invocation_failure(
                    invocation_id,
                    claim_token,
                    claim_version,
                    ctx,
                    error,
                )
                if not persisted:
                    return await _durable_claim_loss_result(
                        invocation_id=invocation_id,
                        request_digest=request_digest,
                        ctx=ctx,
                    )
                return {
                    "ok": False,
                    "error": error,
                    "error_code": "authority_invalid",
                    "reason_code": authority.reason_code,
                    "retryable": False,
                }
            approval_data = {
                "approval_required": True,
                "blocking": True,
                "reason": (
                    "External untrusted content cannot authorize this side effect "
                    "without approval of the exact request."
                ),
                "reason_code": authority.reason_code,
                "external_untrusted": authority.external_untrusted,
                "source_run_ids": list(authority.source_run_ids),
            }
            await _transition_owned_invocation(
                invocation_id=invocation_id,
                claim_token=claim_token,
                claim_version=claim_version,
                status="pending_approval",
                result_payload=approval_data,
                ctx=ctx,
            )
            await commit_uow(ctx.db)
            return {
                "ok": True,
                "data": approval_data,
                "status": "pending_approval",
                "invocation_id": invocation_id,
            }
        # Authority inspection is read-only. Release that snapshot before a
        # handler enters its write UoW or stages an external outbox intent.
        await _release_claim_read(ctx)

    event_guard = (
        serialize_event_write(ctx.run_id)
        if tool.effect_kind == ToolEffectKind.DATABASE_WRITE
        else None
    )
    event_guard_entered = False

    async def enter_database_write_phase() -> None:
        nonlocal event_guard_entered
        if event_guard is not None and not event_guard_entered:
            await event_guard.__aenter__()
            event_guard_entered = True

    if tool.effect_kind == ToolEffectKind.DATABASE_WRITE:
        ctx.database_write_ready = enter_database_write_phase
        if not tool.defer_write_guard:
            await enter_database_write_phase()
    try:
        data = await tool.handler(ctx, args)
        if "error" in data:
            error_code = str(data.get("error_code") or "tool_error")
            safe_error = redact_text(str(data["error"]))
            if (
                invocation_id is not None
                and claim_token is not None
                and claim_version is not None
            ):
                if error_code == "needs_reconciliation":
                    await _transition_owned_invocation(
                        invocation_id=invocation_id,
                        claim_token=claim_token,
                        claim_version=claim_version,
                        status="needs_reconciliation",
                        result_payload={"error": safe_error},
                        ctx=ctx,
                    )
                    await commit_uow(ctx.db)
                else:
                    persisted = await _persist_invocation_failure(
                        invocation_id,
                        claim_token,
                        claim_version,
                        ctx,
                        safe_error,
                    )
                    if not persisted:
                        return await _durable_claim_loss_result(
                            invocation_id=invocation_id,
                            request_digest=request_digest,
                            ctx=ctx,
                        )
            return {
                "ok": False,
                "error": safe_error,
                "error_code": error_code,
                **(
                    {"status": "needs_reconciliation", "uncertain_outcome": True}
                    if error_code == "needs_reconciliation"
                    else {}
                ),
                "retryable": bool(data.get("retryable", False)),
            }
        if name in EXTERNAL_CONTENT_TOOL_NAMES:
            data = mark_external_untrusted_result(data)
        if data.get("approval_required"):
            # Proposal-style tools still expose and validate their full success
            # contract. Minimal guard-only approval responses intentionally do
            # not pretend to be successful tool data.
            try:
                data = tool.output_model.model_validate(data).model_dump(mode="json")
            except Exception:
                pass
            data = redact_data(data)
            if (
                invocation_id is not None
                and claim_token is not None
                and claim_version is not None
            ):
                await _transition_owned_invocation(
                    invocation_id=invocation_id,
                    claim_token=claim_token,
                    claim_version=claim_version,
                    status="pending_approval",
                    result_payload=data,
                    ctx=ctx,
                )
                await commit_uow(ctx.db)
            return {
                "ok": True,
                "data": data,
                "status": "pending_approval",
                "invocation_id": invocation_id,
            }

        dispatch_action_key = data.pop("_dispatch_outbox_action_key", None)
        requested_status = str(data.pop("_invocation_status", ""))

        if dispatch_action_key:
            if (
                invocation_id is None
                or claim_token is None
                or claim_version is None
                or tool.effect_kind != ToolEffectKind.EXTERNAL_WRITE
            ):
                raise RuntimeError("only a claimed external-write tool may dispatch an outbox action")
            placeholder = tool.output_model.model_validate(data).model_dump(mode="json")
            placeholder = redact_data(placeholder)
            await _transition_owned_invocation(
                invocation_id=invocation_id,
                claim_token=claim_token,
                claim_version=claim_version,
                status="pending_delivery",
                result_payload=placeholder,
                ctx=ctx,
            )
            # Commit intent + pending state before the uncertainty fence and
            # subprocess wait.  dispatch_action uses separate short sessions.
            await commit_uow(ctx.db)
            if ctx.db.bind is None:  # pragma: no cover - application sessions are bound
                raise RuntimeError("the tool session lost its database binding")
            dispatch_sessions = async_sessionmaker(
                ctx.db.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            from app.outbox import dispatch_action

            dispatch_result = await dispatch_action(
                action_key=str(dispatch_action_key),
                session_factory=dispatch_sessions,
            )
            if dispatch_result.get("status") == "needs_reconciliation":
                return {
                    "ok": False,
                    "error": "The subprocess outcome is uncertain and requires reconciliation.",
                    "error_code": "needs_reconciliation",
                    "status": "needs_reconciliation",
                    "retryable": False,
                    "uncertain_outcome": True,
                }
            if not dispatch_result.get("delivered"):
                return {
                    "ok": False,
                    "error": "The durable subprocess action was not delivered.",
                    "error_code": "external_delivery_failed",
                    "status": str(dispatch_result.get("status") or "failed"),
                    "retryable": False,
                }
            delivered_data = tool.output_model.model_validate(
                dispatch_result.get("data") or {}
            ).model_dump(mode="json")
            delivered_data = redact_data(delivered_data)
            return {
                "ok": True,
                "data": delivered_data,
                "status": "committed",
            }

        data = tool.output_model.model_validate(data).model_dump(mode="json")
        data = redact_data(data)
        if invocation_id is not None:
            if claim_token is None or claim_version is None:
                raise InvocationClaimLostError("claimed invocation has no ownership token")
            if tool.effect_kind == ToolEffectKind.DATABASE_WRITE:
                # Deferred mixed-effect handlers enter immediately before
                # their first ORM mutation. This defensive call is a no-op in
                # the normal path and guarantees completion allocation is
                # always covered by the same serialization context.
                await enter_database_write_phase()
                completion_event = await _append_atomic_completion(
                    invocation_id=invocation_id,
                    claim_token=claim_token,
                    claim_version=claim_version,
                    name=name,
                    data=data,
                    ctx=ctx,
                )
                return {
                    "ok": True,
                    "data": data,
                    "status": "committed",
                    "completion_event_persisted": True,
                    "completion_event": {
                        "sequence": completion_event.sequence,
                        "type": completion_event.event_type,
                        "summary": completion_event.summary,
                        "payload": completion_event.payload,
                        "created_at": canonical_utc(completion_event.created_at),
                    },
                }
            final_status = (
                requested_status
                if requested_status in {"pending_delivery", "needs_reconciliation"}
                else "committed"
            )
            await _transition_owned_invocation(
                invocation_id=invocation_id,
                claim_token=claim_token,
                claim_version=claim_version,
                status=final_status,
                result_payload=data,
                ctx=ctx,
            )
            await commit_uow(ctx.db)
        return {
            "ok": True,
            "data": data,
            **({"status": final_status} if invocation_id is not None else {}),
        }
    except InvocationClaimLostError:
        if invocation_id is not None:
            return await _durable_claim_loss_result(
                invocation_id=invocation_id,
                request_digest=request_digest,
                ctx=ctx,
            )
        await rollback_uow(ctx.db)
        return _claim_lost_result()
    except DatabaseBusyError as exc:
        if (
            invocation_id is not None
            and claim_token is not None
            and claim_version is not None
        ):
            retry_marked = await _mark_retry_pending(
                invocation_id=invocation_id,
                claim_token=claim_token,
                claim_version=claim_version,
                ctx=ctx,
            )
            if not retry_marked:
                durable = await _durable_claim_loss_result(
                    invocation_id=invocation_id,
                    request_digest=request_digest,
                    ctx=ctx,
                )
                if durable.get("status") in {"committed", "needs_reconciliation"}:
                    return durable
                await rollback_uow(ctx.db)
        else:
            await rollback_uow(ctx.db)
        return _busy_result(exc)
    except Exception as exc:
        safe_error = redact_text(f"{type(exc).__name__}: {exc}")
        if (
            invocation_id is not None
            and claim_token is not None
            and claim_version is not None
        ):
            try:
                persisted = await _persist_invocation_failure(
                    invocation_id,
                    claim_token,
                    claim_version,
                    ctx,
                    safe_error,
                )
                if not persisted:
                    return await _durable_claim_loss_result(
                        invocation_id=invocation_id,
                        request_digest=request_digest,
                        ctx=ctx,
                    )
            except DatabaseBusyError as busy:
                return _busy_result(busy)
        else:
            await rollback_uow(ctx.db)
        return {
            "ok": False,
            "error": safe_error,
            "error_code": "tool_execution_failed",
            "retryable": False,
        }
    finally:
        ctx.database_write_ready = None
        if event_guard is not None and event_guard_entered:
            await event_guard.__aexit__(None, None, None)


def openai_tools() -> list[dict]:
    policy = current_code_execution_policy()
    return [
        tool.openai_schema()
        for tool in TOOLS
        if tool.name != "code_execute" or policy.available
    ]


def tool_contracts() -> list[dict]:
    return [tool.contract_schema() for tool in TOOLS]
