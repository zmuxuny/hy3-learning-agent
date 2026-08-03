from pydantic import BaseModel, Field

from app.context.memory import MemoryManager
from app.tools.base import EmptyArgs, ToolContext, ToolDefinition


class MemorySearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    plan_id: int | None = None
    limit: int = Field(default=12, ge=1, le=40)


async def memory_search(ctx: ToolContext, args: MemorySearchArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if ctx.plan_id is not None and plan_id != ctx.plan_id:
        return {"error": "Plan-focused runs cannot retrieve another plan's private memory"}
    memories, breakdowns = await MemoryManager(ctx.db).retrieve_with_scores(
        ctx.owner_id,
        plan_id=plan_id,
        session_id=ctx.session_id,
        query=args.query,
        limit=args.limit,
    )
    return {
        "memories": [{
            "id": item.id, "scope": item.scope, "scope_id": item.scope_id,
            "layer": item.layer, "content": item.content, "confidence": item.confidence,
            "source": {"type": item.source_type, "id": item.source_id},
        } for item in memories],
        "score_breakdown": breakdowns,
    }


async def memory_maintain(ctx: ToolContext, _: EmptyArgs) -> dict:
    result = await MemoryManager(ctx.db).maintain(ctx.owner_id)
    await ctx.db.commit()
    return result


MEMORY_TOOLS = [
    ToolDefinition("memory_search", "Retrieve relevant confirmed memory by scope, layer, recency, confidence, and query overlap.", MemorySearchArgs, memory_search),
    ToolDefinition("memory_maintain", "Expire stale memory and refresh durable plan summaries without deleting raw events or conversations.", EmptyArgs, memory_maintain, idempotent=True),
]
