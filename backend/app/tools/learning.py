from __future__ import annotations

from typing import Any, Literal

from app.core.time import UTCInstant, canonical_utc, coerce_legacy_utc, utc_now
from app.db.uow import flush as flush_uow
from app.models import (
    Achievement,
    ActivityDay,
    AgentRun,
    Artifact,
    LearningEvent,
    LearningResource,
    Operation,
    Plan,
    QueuedMessage,
    ReviewSchedule,
    Stage,
    Task,
    TaskSubmission,
    UserProfile,
)
from app.runtime.state import NONTERMINAL_RUN_STATUSES
from app.schemas import TaskCreate, TaskUpdate
from app.services import plans as plan_service
from app.services.evidence import (
    append_observation,
    artifact_ref,
    build_plan_evidence_state,
    create_artifact,
    link_operation_observations,
    normalize_percentage_score,
    refresh_plan_evidence_projection,
)
from app.tools.base import ToolContext, ToolDefinition, ToolEffectKind, json_safe
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload


class PlanPatchArgs(BaseModel):
    plan_id: int
    expected_version: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    goal: str | None = None
    current_level: str | None = None
    deadline: UTCInstant | None = None
    weekly_minutes: int | None = Field(default=None, ge=0, le=10080)
    preferences: dict[str, Any] | None = None
    expected_outcome: str | None = None
    available_resources: list[str] | None = None
    avoid_methods: list[str] | None = None
    status: Literal["active", "paused", "completed", "archived"] | None = None
    reason: str = Field(min_length=3, max_length=1000)


class StageCreateArgs(BaseModel):
    plan_id: int
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    objectives: list[str] = Field(default_factory=list)
    position: int | None = Field(default=None, ge=0)


class TaskCreateArgs(TaskCreate):
    stage_id: int


class SubmissionCreateArgs(BaseModel):
    task_id: int
    submission_type: Literal["text", "file", "code", "link"] = "text"
    content: str = Field(default="", max_length=50000)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class SubmissionIdArgs(BaseModel):
    submission_id: int


class SubmissionListArgs(BaseModel):
    task_id: int | None = None
    plan_id: int | None = None
    limit: int = Field(default=20, ge=1, le=100)


class SubmissionCheckArgs(BaseModel):
    submission_id: int
    score: float = Field(ge=0, le=100)
    feedback: str = Field(min_length=1, max_length=10000)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    pass_threshold: float = Field(default=70, ge=0, le=100)


class ResourceListArgs(BaseModel):
    plan_id: int | None = None
    limit: int = Field(default=30, ge=1, le=100)


class EventListArgs(BaseModel):
    plan_id: int | None = None
    task_id: int | None = None
    event_types: list[str] = Field(default_factory=list)
    limit: int = Field(default=30, ge=1, le=100)


class StudyStateArgs(BaseModel):
    plan_id: int | None = None


def _focused(ctx: ToolContext, plan_id: int) -> dict | None:
    if ctx.plan_id is not None and ctx.plan_id != plan_id:
        return {"error": "Plan-focused runs cannot operate on another plan"}
    return None


def _archived(plan: Plan) -> dict | None:
    if plan.status == "archived":
        return {"error": "Restore the plan before changing its learning state"}
    return None


async def _owned_task(ctx: ToolContext, task_id: int) -> Task | None:
    result = await ctx.db.execute(
        select(Task).join(Stage).join(Plan)
        .where(Task.id == task_id, Plan.owner_id == ctx.owner_id)
        .options(selectinload(Task.stage).selectinload(Stage.plan).selectinload(Plan.stages).selectinload(Stage.tasks))
    )
    return result.scalars().one_or_none()


async def plan_patch(ctx: ToolContext, args: PlanPatchArgs) -> dict:
    if error := _focused(ctx, args.plan_id):
        return error
    plan = await plan_service.get_plan(ctx.db, ctx.owner_id, args.plan_id)
    if args.expected_version is not None and plan.version != args.expected_version:
        return {"error": f"Plan version conflict: expected {args.expected_version}, current {plan.version}"}
    changes = args.model_dump(exclude={"plan_id", "expected_version", "reason"}, exclude_unset=True)
    if plan.status == "archived" and changes.get("status") not in {"active", "paused"}:
        return {"error": "Restore the plan before changing its learning state"}
    if changes.get("status") == "completed":
        tasks = [task for stage in plan.stages for task in stage.tasks]
        if (
            not tasks
            or not any(task.status == "completed" for task in tasks)
            or any(task.is_core and task.status != "completed" for task in tasks)
            or any(task.status not in {"completed", "skipped"} for task in tasks)
        ):
            return {
                "error": (
                    "A plan can be completed only after every core task is completed "
                    "and every other task is completed or explicitly skipped"
                )
            }
    protected = {"goal", "status"}.intersection(changes)
    if protected and ctx.trigger not in {"user_message", "email_reply"}:
        return {
            "approval_required": True,
            "blocking": True,
            "reason": f"Background runs cannot change {', '.join(sorted(protected))}",
        }
    if changes.get("status") == "archived":
        other_active_run = (await ctx.db.execute(
            select(AgentRun.id).where(
                AgentRun.owner_id == ctx.owner_id,
                AgentRun.plan_id == plan.id,
                AgentRun.id != ctx.run_id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(NONTERMINAL_RUN_STATUSES),
            ).limit(1)
        )).scalar_one_or_none()
        if other_active_run:
            return {"error": "Stop or resolve the plan's other active run before archiving it"}
        queued_message = (await ctx.db.execute(
            select(QueuedMessage.id).where(
                QueuedMessage.owner_id == ctx.owner_id,
                QueuedMessage.plan_id == plan.id,
            ).limit(1)
        )).scalar_one_or_none()
        if queued_message:
            return {"error": "Send or delete queued messages before archiving this plan"}
    before = {key: json_safe(getattr(plan, key)) for key in changes}
    if "status" in changes:
        before["archived_from_status"] = plan.archived_from_status
        next_status = changes["status"]
        if next_status == "archived" and plan.status != "archived":
            plan.archived_from_status = plan.status
        elif plan.status == "archived" and next_status != "archived":
            plan.archived_from_status = None
    for key, value in changes.items():
        setattr(plan, key, value)
    plan.version += 1
    event = LearningEvent(
        owner_id=ctx.owner_id, plan_id=plan.id, run_id=ctx.run_id,
        event_type="plan.updated", summary=f"Updated plan: {plan.title}",
        payload={"changes": json_safe(changes), "reason": args.reason},
    )
    ctx.db.add(event)
    operation = Operation(
        owner_id=ctx.owner_id, invocation_id=ctx.invocation_id,
        run_id=ctx.run_id, tool_name="plan.patch",
        entity_type="plan", entity_id=str(plan.id),
        forward_patch={"changes": json_safe(changes), "reason": args.reason},
        inverse_patch={"changes": before},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    return {"plan_id": plan.id, "version": plan.version, "operation_id": operation.id, "undo_available": True}


async def stage_create(ctx: ToolContext, args: StageCreateArgs) -> dict:
    if error := _focused(ctx, args.plan_id):
        return error
    plan = await plan_service.get_plan(ctx.db, ctx.owner_id, args.plan_id)
    if error := _archived(plan):
        return error
    position = len(plan.stages) if args.position is None else args.position
    before_version = plan.version
    shifted = []
    for existing_stage in plan.stages:
        if existing_stage.position >= position:
            shifted.append({"stage_id": existing_stage.id, "changes": {"position": existing_stage.position}})
            existing_stage.position += 1
    stage = Stage(plan_id=plan.id, title=args.title, description=args.description, objectives=args.objectives, position=position)
    ctx.db.add(stage)
    await flush_uow(ctx.db)
    plan.version += 1
    operation = Operation(
        owner_id=ctx.owner_id, invocation_id=ctx.invocation_id,
        run_id=ctx.run_id, tool_name="stage.create",
        entity_type="stage", entity_id=str(stage.id),
        forward_patch={"created": stage.id, "affected": {"plan_id": plan.id},
                       "plan": {"version": plan.version},
                       "entities": [{"stage_id": item["stage_id"], "changes": {"position": item["changes"]["position"] + 1}} for item in shifted]},
        inverse_patch={"delete": stage.id, "affected": {"plan_id": plan.id},
                       "plan": {"version": before_version}, "entities": shifted},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    return {"stage_id": stage.id, "plan_id": plan.id, "operation_id": operation.id, "undo_available": True}


async def task_create(ctx: ToolContext, args: TaskCreateArgs) -> dict:
    stage = await ctx.db.get(Stage, args.stage_id)
    if not stage:
        return {"error": "Stage not found"}
    plan = await ctx.db.get(Plan, stage.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Stage not found"}
    if error := _focused(ctx, plan.id):
        return error
    if error := _archived(plan):
        return error
    existing = list((await ctx.db.execute(select(Task).where(Task.stage_id == stage.id))).scalars())
    before_version = plan.version
    values = args.model_dump(exclude={"stage_id", "metadata"})
    task = Task(stage_id=stage.id, position=len(existing), task_metadata=args.metadata, **values)
    ctx.db.add(task)
    await flush_uow(ctx.db)
    plan.version += 1
    operation = Operation(
        owner_id=ctx.owner_id, invocation_id=ctx.invocation_id,
        run_id=ctx.run_id, tool_name="task.create",
        entity_type="task", entity_id=str(task.id),
        forward_patch={"created": task.id, "affected": {"plan_id": plan.id}, "plan": {"version": plan.version}},
        inverse_patch={"delete": task.id, "affected": {"plan_id": plan.id}, "plan": {"version": before_version}},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    return {"task_id": task.id, "stage_id": stage.id, "operation_id": operation.id, "undo_available": True}


async def submission_create(ctx: ToolContext, args: SubmissionCreateArgs) -> dict:
    task = await _owned_task(ctx, args.task_id)
    if not task:
        return {"error": "Task not found"}
    if error := _focused(ctx, task.stage.plan_id):
        return error
    if error := _archived(task.stage.plan):
        return error
    if not args.content.strip() and not args.artifacts:
        return {"error": "A submission needs text or at least one artifact"}
    submission = TaskSubmission(
        owner_id=ctx.owner_id, plan_id=task.stage.plan_id, task_id=task.id, run_id=ctx.run_id,
        submission_type=args.submission_type, content=args.content, artifacts=args.artifacts,
    )
    ctx.db.add(submission)
    await flush_uow(ctx.db)
    submission_artifact, _ = await create_artifact(
        ctx.db,
        owner_id=ctx.owner_id,
        artifact_type="submission",
        source_uri=f"submission:{submission.id}",
        idempotency_key=f"submission:{submission.id}:artifact",
        title=f"提交：{task.title}",
        content=submission.content,
        metadata={"submission_type": submission.submission_type, "artifacts": submission.artifacts},
        plan_id=submission.plan_id,
        task_id=submission.task_id,
        run_id=ctx.run_id,
        session_id=ctx.session_id,
    )
    learning_event = LearningEvent(
        owner_id=ctx.owner_id, plan_id=task.stage.plan_id, task_id=task.id, run_id=ctx.run_id,
        event_type="submission.created", summary=f"Submitted evidence for: {task.title}",
        payload={"submission_id": submission.id, "type": submission.submission_type, "artifacts": submission.artifacts},
        correlation_id=ctx.run_id,
        occurred_at=coerce_legacy_utc(submission.created_at),
    )
    ctx.db.add(learning_event)
    await flush_uow(ctx.db)
    await append_observation(
        ctx.db,
        owner_id=ctx.owner_id,
        source_type="submission",
        source_id=str(submission.id),
        outcome="submitted",
        idempotency_key=f"submission:{submission.id}:created",
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        plan_id=submission.plan_id,
        task_id=submission.task_id,
        payload={
            "submission_type": submission.submission_type,
            "artifact_count": len(submission.artifacts),
            "has_text": bool(submission.content.strip()),
        },
        artifact_refs=[artifact_ref(submission_artifact, kind="submission")],
        occurred_at=coerce_legacy_utc(submission.created_at),
        correlation_id=ctx.run_id,
        causation_id=f"learning_event:{learning_event.id}",
    )
    await refresh_plan_evidence_projection(ctx.db, ctx.owner_id, submission.plan_id)
    await flush_uow(ctx.db)
    return {"submission_id": submission.id, "status": submission.status, "task_id": task.id}


async def submission_get(ctx: ToolContext, args: SubmissionIdArgs) -> dict:
    submission = await ctx.db.get(TaskSubmission, args.submission_id)
    if not submission or submission.owner_id != ctx.owner_id:
        return {"error": "Submission not found"}
    if error := _focused(ctx, submission.plan_id):
        return error
    return _submission_dict(submission)


async def submission_list(ctx: ToolContext, args: SubmissionListArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if plan_id is not None and (error := _focused(ctx, plan_id)):
        return error
    query = select(TaskSubmission).where(TaskSubmission.owner_id == ctx.owner_id)
    if plan_id is not None:
        query = query.where(TaskSubmission.plan_id == plan_id)
    if args.task_id is not None:
        query = query.where(TaskSubmission.task_id == args.task_id)
    items = list((await ctx.db.execute(query.order_by(TaskSubmission.created_at.desc()).limit(args.limit))).scalars())
    return {"submissions": [_submission_dict(item) for item in items]}


async def submission_check(ctx: ToolContext, args: SubmissionCheckArgs) -> dict:
    submission = await ctx.db.get(TaskSubmission, args.submission_id)
    if not submission or submission.owner_id != ctx.owner_id:
        return {"error": "Submission not found"}
    if error := _focused(ctx, submission.plan_id):
        return error
    if submission.status != "submitted":
        return {
            "error": "This submission already has a verdict; create a new submission for a revised attempt",
            "submission_id": submission.id,
            "status": submission.status,
        }
    task = await _owned_task(ctx, submission.task_id)
    if not task:
        return {"error": "Task not found"}
    if error := _archived(task.stage.plan):
        return error
    before_submission = {"status": submission.status, "score": submission.score, "feedback": submission.feedback, "checked_at": submission.checked_at}
    before_task = {"status": task.status, "completed_at": task.completed_at, "task_metadata": dict(task.task_metadata)}
    before_stage = {"status": task.stage.status}
    before_plan = {"progress": task.stage.plan.progress}
    passed = args.score >= args.pass_threshold
    submission.score = args.score
    submission.feedback = args.feedback
    submission.status = "accepted" if passed else "revision_required"
    submission.checked_at = utc_now()
    submission_artifact = await ctx.db.scalar(
        select(Artifact).where(
            Artifact.owner_id == ctx.owner_id,
            Artifact.idempotency_key == f"submission:{submission.id}:artifact",
        )
    )
    if submission_artifact is None:
        submission_artifact, _ = await create_artifact(
            ctx.db,
            owner_id=ctx.owner_id,
            artifact_type="submission",
            source_uri=f"submission:{submission.id}",
            idempotency_key=f"submission:{submission.id}:artifact",
            title=f"提交：{task.title}",
            content=submission.content,
            metadata={"submission_type": submission.submission_type, "artifacts": submission.artifacts},
            plan_id=submission.plan_id,
            task_id=submission.task_id,
            run_id=submission.run_id or ctx.run_id,
            session_id=ctx.session_id,
        )
    evidence = [{"submission_id": submission.id, "score": args.score, "checks": args.checks}]
    award_inverse = None
    if passed and task.status != "completed":
        await plan_service.update_task(
            ctx.db,
            ctx.owner_id,
            task.id,
            TaskUpdate(status="completed", evidence=evidence),
            ctx.run_id,
            session_id=ctx.session_id,
            emit_evidence=False,
        )
        award_inverse = await _award_completion(ctx, task)
    elif task.status == "pending":
        task.status = "active"
    learning_event = LearningEvent(
        owner_id=ctx.owner_id, plan_id=submission.plan_id, task_id=submission.task_id, run_id=ctx.run_id,
        event_type="submission.checked", summary=f"Submission {submission.id} {'accepted' if passed else 'needs revision'}",
        payload={"score": args.score, "feedback": args.feedback, "checks": args.checks},
        correlation_id=ctx.run_id,
        occurred_at=submission.checked_at,
    )
    ctx.db.add(learning_event)
    await flush_uow(ctx.db)
    observation, _ = await append_observation(
        ctx.db,
        owner_id=ctx.owner_id,
        source_type="submission",
        source_id=f"{submission.id}:check",
        outcome="accepted" if passed else "needs_revision",
        idempotency_key=f"submission:{submission.id}:checked",
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        plan_id=submission.plan_id,
        task_id=submission.task_id,
        normalized_score=normalize_percentage_score(args.score),
        is_correct=passed,
        rubric_snapshot={"pass_threshold": args.pass_threshold, "checks": args.checks},
        evaluator={"type": "agent", "run_id": ctx.run_id},
        payload={"feedback": args.feedback},
        artifact_refs=[artifact_ref(submission_artifact, kind="submission")],
        occurred_at=submission.checked_at,
        correlation_id=ctx.run_id,
        causation_id=f"learning_event:{learning_event.id}",
    )
    profile_after = await ctx.db.get(UserProfile, ctx.owner_id)
    forward_award = None
    if award_inverse:
        day_after = await ctx.db.get(ActivityDay, award_inverse["day_id"])
        achievement_ids = award_inverse.get("delete_achievements", [])
        achievement_rows = list(
            (
                await ctx.db.execute(
                    select(Achievement)
                    .where(Achievement.id.in_(achievement_ids))
                    .order_by(Achievement.id)
                )
            ).scalars()
        ) if achievement_ids else []
        forward_award = {
            "profile": (
                {
                    "xp": profile_after.xp,
                    "level": profile_after.level,
                    "streak_days": profile_after.streak_days,
                }
                if profile_after is not None
                else None
            ),
            "day": (
                {
                    "id": day_after.id,
                    "date": day_after.date,
                    "xp": day_after.xp,
                    "completed_tasks": day_after.completed_tasks,
                    "passed_quizzes": day_after.passed_quizzes,
                }
                if day_after is not None
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
                for item in achievement_rows
            ],
        }
    operation = Operation(
        owner_id=ctx.owner_id, invocation_id=ctx.invocation_id,
        run_id=ctx.run_id, tool_name="submission.check",
        entity_type="submission", entity_id=str(submission.id),
        forward_patch={
            "evidence_protocol": 1,
            "submission": {
                "status": submission.status,
                "score": submission.score,
                "feedback": submission.feedback,
                "checked_at": canonical_utc(submission.checked_at),
            },
            "task": {
                "status": task.status,
                "completed_at": canonical_utc(task.completed_at),
                "task_metadata": task.task_metadata,
            },
            "stage": {"status": task.stage.status},
            "plan": {"progress": task.stage.plan.progress},
            "affected": {
                "plan_id": task.stage.plan_id,
                "stage_id": task.stage_id,
                "task_id": task.id,
            },
            "award": forward_award,
        },
        inverse_patch={
            "submission": json_safe(before_submission),
            "task": json_safe(before_task),
            "stage": json_safe(before_stage),
            "plan": json_safe(before_plan),
            "affected": {
                "plan_id": task.stage.plan_id,
                "stage_id": task.stage_id,
                "task_id": task.id,
            },
            "award": award_inverse,
        },
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    await link_operation_observations(ctx.db, operation, [observation])
    await refresh_plan_evidence_projection(ctx.db, ctx.owner_id, submission.plan_id)
    return {
        "submission_id": submission.id, "task_id": task.id, "status": submission.status,
        "task_status": task.status, "score": submission.score,
        "operation_id": operation.id, "undo_available": True,
    }


async def resource_list(ctx: ToolContext, args: ResourceListArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if plan_id is not None and (error := _focused(ctx, plan_id)):
        return error
    query = select(LearningResource).where(LearningResource.owner_id == ctx.owner_id)
    query = query.where(LearningResource.url.not_like("%duckduckgo.com/y.js%"))
    if plan_id is not None:
        query = query.where(LearningResource.plan_id == plan_id)
    resources = list((await ctx.db.execute(query.order_by(LearningResource.created_at.desc()).limit(args.limit))).scalars())
    return {"resources": [{
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "resource_type": item.resource_type,
        "provider": item.provider,
        "language": item.language,
        "difficulty": item.difficulty,
        "summary": item.summary,
        "why_recommended": item.why_recommended,
        "verified_at": canonical_utc(item.verified_at),
        "source": item.source,
    } for item in resources]}


async def learning_event_list(ctx: ToolContext, args: EventListArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if plan_id is not None and (error := _focused(ctx, plan_id)):
        return error
    query = select(LearningEvent).where(LearningEvent.owner_id == ctx.owner_id)
    if plan_id is not None:
        query = query.where(LearningEvent.plan_id == plan_id)
    if args.task_id is not None:
        query = query.where(LearningEvent.task_id == args.task_id)
    if args.event_types:
        query = query.where(LearningEvent.event_type.in_(args.event_types))
    events = list((await ctx.db.execute(query.order_by(LearningEvent.created_at.desc()).limit(args.limit))).scalars())
    return {
        "events": [
            {
                "id": item.id,
                "type": item.event_type,
                "summary": item.summary,
                "payload": item.payload,
                "created_at": canonical_utc(item.created_at),
            }
            for item in events
        ]
    }


async def study_state_get(ctx: ToolContext, args: StudyStateArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if plan_id is None:
        return {"error": "study_state_get requires plan_id or a focused plan"}
    if error := _focused(ctx, plan_id):
        return error
    plan = await plan_service.get_plan(ctx.db, ctx.owner_id, plan_id)
    ordered = [(stage, task) for stage in plan.stages for task in stage.tasks]
    active = next(((stage, task) for stage, task in ordered if task.status == "active"), None)
    blocked = [(stage, task) for stage, task in ordered if task.status == "blocked"]
    next_pending = next(((stage, task) for stage, task in ordered if task.status == "pending"), None)
    current = active or (blocked[0] if blocked else None) or next_pending
    recommended = active or next_pending
    now = utc_now()

    def is_overdue(task: Task) -> bool:
        if task.status in {"completed", "skipped"} or task.due_at is None:
            return False
        due = coerce_legacy_utc(task.due_at)
        return due < now

    overdue = [task for _, task in ordered if is_overdue(task)]
    reviews = list((await ctx.db.execute(
        select(ReviewSchedule).where(
            ReviewSchedule.owner_id == ctx.owner_id,
            ReviewSchedule.plan_id == plan.id,
            ReviewSchedule.status == "scheduled",
        ).order_by(ReviewSchedule.due_at).limit(20)
    )).scalars())
    submissions = list((await ctx.db.execute(
        select(TaskSubmission).where(
            TaskSubmission.owner_id == ctx.owner_id,
            TaskSubmission.plan_id == plan.id,
        ).order_by(TaskSubmission.created_at.desc()).limit(8)
    )).scalars())
    evidence_state = await build_plan_evidence_state(ctx.db, ctx.owner_id, plan.id)
    completed = [task for _, task in ordered if task.status == "completed"]
    core_incomplete = [task for _, task in ordered if task.is_core and task.status != "completed"]
    return {
        "plan_id": plan.id,
        "plan_version": plan.version,
        "progress": plan.progress,
        "current_stage": current[0].title if current else None,
        "current_task": {"id": current[1].id, "title": current[1].title, "status": current[1].status} if current else None,
        "recommended_next": ({
            "id": recommended[1].id,
            "title": recommended[1].title,
            "reason": "继续当前活动任务" if active else "计划中第一个尚未开始的可执行任务",
        } if recommended else None),
        "counts": {
            "total": len(ordered),
            "completed": len(completed),
            "blocked": len(blocked),
            "overdue": len(overdue),
            "core_evidence_pending": len(core_incomplete),
            "reviews_due": len(reviews),
        },
        "overdue_tasks": [
            {"id": task.id, "title": task.title, "due_at": canonical_utc(task.due_at)}
            for task in overdue
        ],
        "blocked_tasks": [{"id": task.id, "title": task.title} for _, task in blocked],
        "scheduled_reviews": [
            {
                "id": review.id,
                "task_id": review.task_id,
                "due_at": canonical_utc(review.due_at),
            }
            for review in reviews
        ],
        "recent_submissions": [{"id": item.id, "task_id": item.task_id, "status": item.status, "score": item.score} for item in submissions],
        "evidence_state": evidence_state,
        "weekly_minutes": plan.weekly_minutes,
        "completed_estimated_minutes": sum(task.estimated_minutes for task in completed),
        "generated_at": canonical_utc(now),
    }


def _submission_dict(item: TaskSubmission) -> dict:
    return {
        "id": item.id, "plan_id": item.plan_id, "task_id": item.task_id,
        "type": item.submission_type, "content": item.content, "artifacts": item.artifacts,
        "status": item.status, "score": item.score, "feedback": item.feedback,
        "created_at": canonical_utc(item.created_at),
    }


async def _award_completion(ctx: ToolContext, task: Task) -> dict:
    profile = await ctx.db.get(UserProfile, ctx.owner_id)
    profile_before = None
    if profile:
        profile_before = {
            "xp": profile.xp,
            "level": profile.level,
            "streak_days": profile.streak_days,
        }
        profile.xp += 25 if task.is_core else 10
        profile.level = 1 + profile.xp // 100
    day_key = utc_now().date().isoformat()
    result = await ctx.db.execute(select(ActivityDay).where(ActivityDay.owner_id == ctx.owner_id, ActivityDay.date == day_key))
    day = result.scalars().one_or_none()
    if day is None:
        day = ActivityDay(owner_id=ctx.owner_id, date=day_key)
        ctx.db.add(day)
        await flush_uow(ctx.db)
        day_before = None
    else:
        day_before = {"xp": day.xp, "completed_tasks": day.completed_tasks, "passed_quizzes": day.passed_quizzes}
    day.completed_tasks += 1
    day.xp += 25 if task.is_core else 10
    from app.services.gamification import evaluate_achievements

    unlocked = await evaluate_achievements(ctx.db, ctx.owner_id)
    await flush_uow(ctx.db)
    return {
        "profile": profile_before,
        "day_id": day.id,
        "day": day_before,
        "delete_achievements": [item.id for item in unlocked],
    }


LEARNING_TOOLS = [
    ToolDefinition("plan_patch", "Modify plan metadata or timing. Background goal/status changes require approval; successful changes are reversible.", PlanPatchArgs, plan_patch, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("stage_create", "Append a reversible stage to the focused learning plan.", StageCreateArgs, stage_create, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("task_create", "Add a reversible task to a stage in the focused learning plan.", TaskCreateArgs, task_create, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("submission_create", "Submit text, code, file references, or links as durable evidence for a task.", SubmissionCreateArgs, submission_create, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("submission_get", "Inspect one task submission and its artifacts before evaluating it.", SubmissionIdArgs, submission_get, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("submission_list", "List recent submissions for a task or focused plan.", SubmissionListArgs, submission_list, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("submission_check", "Record an evidence-based submission verdict; accepted work completes the task and awards progress.", SubmissionCheckArgs, submission_check, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True),
    ToolDefinition("resource_list", "List learning resources saved from web research for the focused plan.", ResourceListArgs, resource_list, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("learning_event_list", "Retrieve immutable learning events relevant to the current plan or task.", EventListArgs, learning_event_list, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("study_state_get", "Read one canonical, versioned progress snapshot with the current task, recommended next step, evidence, blockers, overdue work, reviews, and recent submissions.", StudyStateArgs, study_state_get, effect_kind=ToolEffectKind.PURE_READ),
]
