from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.models import LearningEvent, Memory, Operation, Plan, Quiz, ReviewSchedule, Stage, Task, UserProfile
from app.notifications import NotificationService
from app.schemas import PlanCreate, TaskUpdate
from app.services import plans as plan_service
from app.tools.base import EmptyArgs, ToolContext, ToolDefinition, json_safe, parse_arguments
from app.tools.calendar import CALENDAR_TOOLS
from app.tools.contracts import attach_output_contracts
from app.tools.learning import LEARNING_TOOLS
from app.tools.memory import MEMORY_TOOLS
from app.tools.web import WEB_TOOLS
from app.tools.workspace import WORKSPACE_TOOLS


class PlanIdArgs(BaseModel):
    plan_id: int


class TaskPatchArgs(BaseModel):
    task_id: int
    changes: TaskUpdate
    reason: str
    expected_plan_version: int | None = None


class ReviewScheduleArgs(BaseModel):
    plan_id: int
    task_id: int | None = None
    due_at: datetime
    review_type: str = "quiz"


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
    next_review_at: datetime | None = None


class MemoryProposalArgs(BaseModel):
    scope: Literal["global", "plan", "session"] = "global"
    scope_id: str | None = None
    layer: Literal["short_term", "long_term", "episodic", "semantic"] = "semantic"
    content: str
    confidence: float = Field(default=0.8, ge=0, le=1)


class NotificationArgs(BaseModel):
    title: str
    body: str
    plan_id: int | None = None
    channels: list[str] = Field(default_factory=lambda: ["in_app"])


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
        "deadline": plan.deadline.isoformat() if plan.deadline else None,
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
                        "due_at": task.due_at.isoformat() if task.due_at else None,
                        "review_due_at": task.review_due_at.isoformat() if task.review_due_at else None,
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
    if ctx.trigger not in {"user_message"}:
        return {"approval_required": True, "reason": "Background runs cannot create a plan without user approval."}
    plan = await plan_service.create_plan(ctx.db, ctx.owner_id, args, ctx.run_id, commit=False)
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        tool_name="plan.create",
        entity_type="plan",
        entity_id=str(plan.id),
        forward_patch={"created": plan.id},
        inverse_patch={"delete": plan.id},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
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
    if args.expected_plan_version is not None and task_plan.version != args.expected_plan_version:
        return {"error": f"Plan version conflict: expected {args.expected_plan_version}, current {task_plan.version}"}
    if ctx.trigger != "user_message" and {"is_core", "evidence_required"}.intersection(changes):
        return {"approval_required": True, "reason": "Background runs cannot change task evidence policy"}
    mutable_changes = {key: value for key, value in changes.items() if key != "evidence"}
    before = {key: getattr(task, key) for key in mutable_changes}
    if "status" in mutable_changes:
        before["completed_at"] = task.completed_at
    if "evidence" in changes:
        before["task_metadata"] = dict(task.task_metadata)
    updated = await plan_service.update_task(ctx.db, ctx.owner_id, task.id, args.changes, ctx.run_id, commit=False)
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        tool_name="task.patch",
        entity_type="task",
        entity_id=str(task.id),
        forward_patch={"changes": json_safe(changes), "reason": args.reason},
        inverse_patch={"changes": json_safe(before)},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {"task_id": updated.id, "status": updated.status, "operation_id": operation.id, "undo_available": True}


async def review_schedule(ctx: ToolContext, args: ReviewScheduleArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot schedule another plan"}
    plan = await ctx.db.get(Plan, args.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if args.task_id is not None:
        task_plan_id = (await ctx.db.execute(
            select(Stage.plan_id).join(Task, Task.stage_id == Stage.id).where(Task.id == args.task_id)
        )).scalar_one_or_none()
        if task_plan_id != args.plan_id:
            return {"error": "Task does not belong to the selected plan"}
    schedule = ReviewSchedule(owner_id=ctx.owner_id, **args.model_dump())
    ctx.db.add(schedule)
    await ctx.db.flush()
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        tool_name="review.schedule",
        entity_type="review_schedule",
        entity_id=str(schedule.id),
        forward_patch={"created": schedule.id},
        inverse_patch={"delete": schedule.id},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {"review_id": schedule.id, "due_at": schedule.due_at.isoformat(), "operation_id": operation.id}


async def quiz_create(ctx: ToolContext, args: QuizCreateArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot create a quiz for another plan"}
    plan = await ctx.db.get(Plan, args.plan_id)
    if not plan or plan.owner_id != ctx.owner_id:
        return {"error": "Plan not found"}
    if args.task_id is not None:
        task_plan_id = (await ctx.db.execute(
            select(Stage.plan_id).join(Task, Task.stage_id == Stage.id).where(Task.id == args.task_id)
        )).scalar_one_or_none()
        if task_plan_id != args.plan_id:
            return {"error": "Task does not belong to the selected plan"}
    quiz = Quiz(owner_id=ctx.owner_id, run_id=ctx.run_id, **args.model_dump())
    ctx.db.add(quiz)
    await ctx.db.flush()
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        tool_name="quiz.create",
        entity_type="quiz",
        entity_id=str(quiz.id),
        forward_patch={"created": quiz.id},
        inverse_patch={"delete": quiz.id},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
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
    before = {
        "answer": quiz.answer,
        "score": quiz.score,
        "feedback": quiz.feedback,
        "evidence": quiz.evidence,
        "status": quiz.status,
        "graded_at": quiz.graded_at.isoformat() if quiz.graded_at else None,
    }
    quiz.answer = args.answer
    quiz.score = args.score
    quiz.feedback = args.feedback
    quiz.evidence = args.evidence
    quiz.status = "passed" if args.score >= 70 else "needs_review"
    quiz.graded_at = datetime.now().astimezone()
    learning_event = LearningEvent(
        owner_id=ctx.owner_id,
        plan_id=quiz.plan_id,
        task_id=quiz.task_id,
        run_id=ctx.run_id,
        event_type="quiz.graded",
        summary=f"Quiz {quiz.id} scored {args.score:.0f}",
        payload={"score": args.score, "status": quiz.status, "evidence": args.evidence},
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
    if profile:
        profile_before = {"xp": profile.xp, "level": profile.level}
        earned_xp = (30 if args.score >= 70 else 10) if before["status"] == "open" else 0
        profile.xp += earned_xp
        profile.level = 1 + profile.xp // 100
    await ctx.db.flush()
    operation = Operation(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        tool_name="quiz.grade",
        entity_type="quiz",
        entity_id=str(quiz.id),
        forward_patch={"score": args.score, "status": quiz.status},
        inverse_patch={
            "changes": before,
            "profile": profile_before,
            "delete_learning_event": learning_event.id,
            "delete_review": review.id if review else None,
        },
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {
        "quiz_id": quiz.id,
        "score": quiz.score,
        "status": quiz.status,
        "operation_id": operation.id,
        "undo_available": True,
    }


async def memory_propose(ctx: ToolContext, args: MemoryProposalArgs) -> dict:
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
    memory = Memory(
        owner_id=ctx.owner_id,
        source_type="agent_run",
        source_id=ctx.run_id,
        status="proposed",
        **args.model_dump(),
    )
    ctx.db.add(memory)
    await ctx.db.commit()
    await ctx.db.refresh(memory)
    return {"memory_id": memory.id, "status": "proposed", "approval_required": True}


async def notification_send(ctx: ToolContext, args: NotificationArgs) -> dict:
    if ctx.plan_id is not None and args.plan_id not in {None, ctx.plan_id}:
        return {"error": "Plan-focused runs cannot notify about another plan"}
    if args.plan_id is None:
        args.plan_id = ctx.plan_id
    return await NotificationService(ctx.db).send(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        trigger=ctx.trigger,
        title=args.title,
        body=args.body,
        plan_id=args.plan_id,
        channels=args.channels,
    )


TOOLS = [
    ToolDefinition("profile_get", "Read the learner's global profile and notification preferences.", EmptyArgs, profile_get),
    ToolDefinition("plan_list", "List all learning plans and their current progress.", EmptyArgs, plan_list),
    ToolDefinition("plan_get", "Inspect one complete plan with stages and tasks.", PlanIdArgs, plan_get),
    ToolDefinition("plan_create", "Create a complete learning plan requested by the user.", PlanCreate, plan_create),
    ToolDefinition("task_patch", "Update a task status, due time, duration, or review time.", TaskPatchArgs, task_patch),
    ToolDefinition("review_schedule", "Schedule a future review or proactive quiz.", ReviewScheduleArgs, review_schedule),
    ToolDefinition("quiz_create", "Create an evidence-based quiz for an active plan.", QuizCreateArgs, quiz_create),
    ToolDefinition("quiz_get", "Read a quiz prompt and grading rubric before evaluating an answer.", QuizIdArgs, quiz_get),
    ToolDefinition("quiz_grade", "Store an evidence-based quiz grade and schedule the next review.", QuizGradeArgs, quiz_grade),
    ToolDefinition("memory_propose", "Propose a long-term memory for user confirmation.", MemoryProposalArgs, memory_propose),
    ToolDefinition("notification_send", "Send an in-app notification and optionally queue email/browser delivery.", NotificationArgs, notification_send),
] + LEARNING_TOOLS + MEMORY_TOOLS + WEB_TOOLS + WORKSPACE_TOOLS + CALENDAR_TOOLS

attach_output_contracts(TOOLS)

TOOL_MAP = {tool.name: tool for tool in TOOLS}


async def execute_tool(name: str, raw_arguments: str, ctx: ToolContext) -> dict:
    tool = TOOL_MAP.get(name)
    if not tool:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        payload = parse_arguments(raw_arguments)
        args = tool.args_model.model_validate(payload)
        data = await tool.handler(ctx, args)
        if "error" in data:
            return {"ok": False, "error": str(data["error"]), "retryable": False}
        if not data.get("approval_required"):
            data = tool.output_model.model_validate(data).model_dump(mode="json")
        return {"ok": True, "data": data}
    except Exception as exc:
        await ctx.db.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def openai_tools() -> list[dict]:
    return [tool.openai_schema() for tool in TOOLS]


def tool_contracts() -> list[dict]:
    return [tool.contract_schema() for tool in TOOLS]
