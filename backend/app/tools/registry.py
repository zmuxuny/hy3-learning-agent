import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityDay, LearningEvent, Memory, Operation, Plan, Quiz, ReviewSchedule, Stage, Task, UserProfile
from app.notifications import NotificationService
from app.schemas import PlanCreate, TaskUpdate
from app.services import plans as plan_service


class EmptyArgs(BaseModel):
    pass


class PlanIdArgs(BaseModel):
    plan_id: int


class TaskPatchArgs(BaseModel):
    task_id: int
    changes: TaskUpdate
    reason: str


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
    scope: str = "global"
    scope_id: str | None = None
    layer: str = "semantic"
    content: str
    confidence: float = Field(default=0.8, ge=0, le=1)


class NotificationArgs(BaseModel):
    title: str
    body: str
    plan_id: int | None = None
    channels: list[str] = Field(default_factory=lambda: ["in_app"])


@dataclass
class ToolContext:
    db: AsyncSession
    owner_id: str
    run_id: str
    trigger: str


@dataclass
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Any

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


async def profile_get(ctx: ToolContext, _: EmptyArgs) -> dict:
    from app.models import UserProfile

    profile = await ctx.db.get(UserProfile, ctx.owner_id)
    return {
        "coach_style": profile.coach_style,
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
    plan = await plan_service.get_plan(ctx.db, ctx.owner_id, args.plan_id)
    return {
        "id": plan.id,
        "title": plan.title,
        "goal": plan.goal,
        "version": plan.version,
        "memory_summary": plan.memory_summary,
        "stages": [
            {
                "id": stage.id,
                "title": stage.title,
                "status": stage.status,
                "tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "status": task.status,
                        "due_at": task.due_at.isoformat() if task.due_at else None,
                        "review_due_at": task.review_due_at.isoformat() if task.review_due_at else None,
                        "is_core": task.is_core,
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
    changes = args.changes.model_dump(exclude_unset=True)
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
        forward_patch={"changes": _json_safe(changes), "reason": args.reason},
        inverse_patch={"changes": _json_safe(before)},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {"task_id": updated.id, "status": updated.status, "operation_id": operation.id, "undo_available": True}


async def review_schedule(ctx: ToolContext, args: ReviewScheduleArgs) -> dict:
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
    return await NotificationService(ctx.db).send(
        owner_id=ctx.owner_id,
        run_id=ctx.run_id,
        trigger=ctx.trigger,
        title=args.title,
        body=args.body,
        plan_id=args.plan_id,
        channels=args.channels,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


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
]

TOOL_MAP = {tool.name: tool for tool in TOOLS}


async def execute_tool(name: str, raw_arguments: str, ctx: ToolContext) -> dict:
    tool = TOOL_MAP.get(name)
    if not tool:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        payload = json.loads(raw_arguments or "{}")
        args = tool.args_model.model_validate(payload)
        data = await tool.handler(ctx, args)
        return {"ok": "error" not in data, "data": data}
    except Exception as exc:
        await ctx.db.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def openai_tools() -> list[dict]:
    return [tool.openai_schema() for tool in TOOLS]
