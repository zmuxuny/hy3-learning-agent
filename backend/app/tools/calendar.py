from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.models import CalendarEvent, Operation, Plan, Task
from app.tools.base import ToolContext, ToolDefinition, json_safe


class CalendarListArgs(BaseModel):
    starts_after: datetime | None = None
    starts_before: datetime | None = None
    plan_id: int | None = None
    limit: int = Field(default=30, ge=1, le=100)


class CalendarCreateArgs(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    starts_at: datetime
    ends_at: datetime | None = None
    plan_id: int | None = None
    task_id: int | None = None


class CalendarPatchArgs(BaseModel):
    event_id: int
    title: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: str | None = None


def _scope_error(ctx: ToolContext, plan_id: int | None) -> dict | None:
    if ctx.plan_id is not None and plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs may only operate on their focused plan calendar"}
    return None


async def calendar_list(ctx: ToolContext, args: CalendarListArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if error := _scope_error(ctx, plan_id):
        return error
    query = select(CalendarEvent).where(CalendarEvent.owner_id == ctx.owner_id)
    if plan_id is not None:
        query = query.where(CalendarEvent.plan_id == plan_id)
    if args.starts_after:
        query = query.where(CalendarEvent.starts_at >= args.starts_after)
    if args.starts_before:
        query = query.where(CalendarEvent.starts_at <= args.starts_before)
    events = list((await ctx.db.execute(query.order_by(CalendarEvent.starts_at).limit(args.limit))).scalars())
    return {"events": [{
        "id": event.id, "title": event.title, "description": event.description,
        "starts_at": event.starts_at.isoformat(), "ends_at": event.ends_at.isoformat() if event.ends_at else None,
        "plan_id": event.plan_id, "task_id": event.task_id, "status": event.status,
    } for event in events]}


async def calendar_create(ctx: ToolContext, args: CalendarCreateArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if error := _scope_error(ctx, plan_id):
        return error
    if args.ends_at and args.ends_at <= args.starts_at:
        return {"error": "ends_at must be later than starts_at"}
    if plan_id is not None:
        plan = await ctx.db.get(Plan, plan_id)
        if not plan or plan.owner_id != ctx.owner_id:
            return {"error": "Plan not found"}
    if args.task_id is not None:
        task = (await ctx.db.execute(
            select(Task).join(Task.stage).join(Plan).where(
                Task.id == args.task_id,
                Plan.owner_id == ctx.owner_id,
                Plan.id == plan_id,
            )
        )).scalars().one_or_none()
        if not task:
            return {"error": "Task does not belong to the selected plan"}
    event = CalendarEvent(owner_id=ctx.owner_id, source="agent", **args.model_dump(exclude={"plan_id"}), plan_id=plan_id)
    ctx.db.add(event)
    await ctx.db.flush()
    operation = Operation(
        owner_id=ctx.owner_id, run_id=ctx.run_id, tool_name="calendar.create",
        entity_type="calendar_event", entity_id=str(event.id),
        forward_patch={"created": event.id}, inverse_patch={"delete": event.id},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {"event_id": event.id, "starts_at": event.starts_at.isoformat(), "operation_id": operation.id, "undo_available": True}


async def calendar_patch(ctx: ToolContext, args: CalendarPatchArgs) -> dict:
    event = await ctx.db.get(CalendarEvent, args.event_id)
    if not event or event.owner_id != ctx.owner_id:
        return {"error": "Calendar event not found"}
    if error := _scope_error(ctx, event.plan_id):
        return error
    changes = args.model_dump(exclude={"event_id"}, exclude_unset=True)
    before = {key: json_safe(getattr(event, key)) for key in changes}
    for key, value in changes.items():
        setattr(event, key, value)
    event.updated_at = datetime.now(timezone.utc)
    operation = Operation(
        owner_id=ctx.owner_id, run_id=ctx.run_id, tool_name="calendar.patch",
        entity_type="calendar_event", entity_id=str(event.id),
        forward_patch={"changes": json_safe(changes)}, inverse_patch={"changes": before},
    )
    ctx.db.add(operation)
    await ctx.db.commit()
    return {"event_id": event.id, "status": event.status, "operation_id": operation.id, "undo_available": True}


CALENDAR_TOOLS = [
    ToolDefinition("calendar_list", "Inspect scheduled personal study events, optionally limited to the focused plan.", CalendarListArgs, calendar_list),
    ToolDefinition("calendar_create", "Create a reversible study calendar event for a plan or task.", CalendarCreateArgs, calendar_create, idempotent=True),
    ToolDefinition("calendar_patch", "Reschedule or update an existing study calendar event; changes are reversible.", CalendarPatchArgs, calendar_patch, idempotent=True),
]
