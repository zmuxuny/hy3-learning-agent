from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.uow import flush as flush_uow
from app.models import Operation
from app.schemas.evidence import EvidenceListInput
from app.services.competencies import (
    add_edge,
    competency_dict,
    create_competency,
    get_owned_competency,
    graph_for_plan,
    link_competency,
    resolve_competency_scope,
)
from app.services.competency_protocol import (
    graph_revision,
    record_graph_mutation,
    record_operation_dependencies,
)
from app.services.evidence import list_observations, observation_dict
from app.tools.base import ToolContext, ToolDefinition, ToolEffectKind, json_safe


class StrictCompetencyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompetencyCreateArgs(StrictCompetencyArgs):
    key: str = Field(min_length=2, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=2000)
    competency_type: Literal["concept", "skill", "workflow", "project", "habit"] = "concept"
    scope: Literal["global", "plan"] = "plan"
    plan_id: int | None = None


class CompetencyLinkArgs(StrictCompetencyArgs):
    competency_id: int
    plan_id: int | None = None
    task_id: int | None = None
    resource_id: int | None = None
    relation: Literal["targets", "teaches", "assesses", "covers"] = "teaches"
    target_stage: Literal["unknown", "exposed", "practicing", "demonstrated", "retained"] = "practicing"
    depth: str = Field(default="overview", max_length=24)


class CompetencyEdgeArgs(StrictCompetencyArgs):
    source_id: int
    target_id: int
    relation: Literal["prerequisite", "part_of", "related_to", "equivalent_to"]


class CompetencyGraphArgs(StrictCompetencyArgs):
    plan_id: int | None = None


class CompetencyGetArgs(StrictCompetencyArgs):
    competency_id: int


EvidenceListArgs = EvidenceListInput


def _scope_plan(ctx: ToolContext, plan_id: int | None) -> dict | None:
    if ctx.plan_id is not None and plan_id not in {None, ctx.plan_id}:
        return {"error": "Plan-focused runs cannot operate on another plan's competency graph"}
    return None


def _needs_approval(ctx: ToolContext) -> dict | None:
    if ctx.trigger not in {"user_message", "email_reply"} and not ctx.approval_granted:
        return {"approval_required": True, "blocking": True, "reason": "Background runs cannot change the competency graph without approval"}
    return None


async def competency_create(ctx: ToolContext, args: CompetencyCreateArgs) -> dict:
    if error := _scope_plan(ctx, args.plan_id):
        return error
    if error := _needs_approval(ctx):
        return error
    values = args.model_dump()
    if args.scope == "plan" and args.plan_id is None:
        values["plan_id"] = ctx.plan_id
    try:
        competency, created = await create_competency(
            ctx.db,
            ctx.owner_id,
            **values,
            focused_plan_id=ctx.plan_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="competency.create",
        entity_type="competency",
        entity_id=str(competency.id),
        forward_patch={"created": competency.id, "created_now": created},
        inverse_patch={"delete": competency.id} if created else {},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    if created:
        revision, _ = await record_graph_mutation(
            ctx.db,
            owner_id=ctx.owner_id,
            operation_id=operation.id,
            action_key=f"operation:{operation.id}:apply",
            action="apply",
            entity_type=operation.entity_type,
            entity_id=operation.entity_id,
        )
        operation.forward_patch = {**operation.forward_patch, "graph_revision": revision}
        await flush_uow(ctx.db)
    return {"competency_id": competency.id, "key": competency.key, "created": created, "operation_id": operation.id}


async def competency_link(ctx: ToolContext, args: CompetencyLinkArgs) -> dict:
    if args.plan_id is not None and args.relation != "targets":
        return {"error": "Plan competency mappings use the targets relation"}
    if error := _needs_approval(ctx):
        return error
    try:
        result = await link_competency(
            ctx.db,
            ctx.owner_id,
            **args.model_dump(),
            focused_plan_id=ctx.plan_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="competency.link",
        entity_type=f"{result['kind']}_competency_link",
        entity_id=str(result["link_id"]),
        forward_patch={"created": result["link_id"], "competency_id": args.competency_id},
        inverse_patch={"delete": result["link_id"]} if result["created"] else {},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    if result["created"]:
        prerequisites: list[tuple[str, int]] = [("competency", args.competency_id)]
        if args.plan_id is not None:
            prerequisites.append(("plan", args.plan_id))
        elif args.task_id is not None:
            prerequisites.append(("task", args.task_id))
        elif args.resource_id is not None:
            prerequisites.append(("learning_resource", args.resource_id))
        await record_operation_dependencies(
            ctx.db,
            operation=operation,
            prerequisites=prerequisites,
            dependency_kind="competency_link_endpoint",
        )
        revision, _ = await record_graph_mutation(
            ctx.db,
            owner_id=ctx.owner_id,
            operation_id=operation.id,
            action_key=f"operation:{operation.id}:apply",
            action="apply",
            entity_type=operation.entity_type,
            entity_id=operation.entity_id,
        )
        operation.forward_patch = {**operation.forward_patch, "graph_revision": revision}
        await flush_uow(ctx.db)
    return {**result, "operation_id": operation.id}


async def competency_edge(ctx: ToolContext, args: CompetencyEdgeArgs) -> dict:
    if error := _needs_approval(ctx):
        return error
    try:
        edge, created = await add_edge(
            ctx.db,
            ctx.owner_id,
            **args.model_dump(),
            focused_plan_id=ctx.plan_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    operation = Operation(
        owner_id=ctx.owner_id,
        invocation_id=ctx.invocation_id,
        run_id=ctx.run_id,
        tool_name="competency.edge",
        entity_type="competency_edge",
        entity_id=str(edge.id),
        forward_patch={"created": edge.id, "created_now": created},
        inverse_patch={"delete": edge.id} if created else {},
    )
    ctx.db.add(operation)
    await flush_uow(ctx.db)
    if created:
        await record_operation_dependencies(
            ctx.db,
            operation=operation,
            prerequisites=(
                ("competency", edge.source_id),
                ("competency", edge.target_id),
            ),
            dependency_kind="competency_edge_endpoint",
        )
        revision, _ = await record_graph_mutation(
            ctx.db,
            owner_id=ctx.owner_id,
            operation_id=operation.id,
            action_key=f"operation:{operation.id}:apply",
            action="apply",
            entity_type=operation.entity_type,
            entity_id=operation.entity_id,
        )
        operation.forward_patch = {**operation.forward_patch, "graph_revision": revision}
        await flush_uow(ctx.db)
    return {"edge_id": edge.id, "source_id": edge.source_id, "target_id": edge.target_id, "relation": edge.relation, "created": created, "operation_id": operation.id}


async def competency_graph_get(ctx: ToolContext, args: CompetencyGraphArgs) -> dict:
    if error := _scope_plan(ctx, args.plan_id):
        return error
    graph = await graph_for_plan(ctx.db, ctx.owner_id, args.plan_id if args.plan_id is not None else ctx.plan_id)
    graph["revision"] = await graph_revision(ctx.db, ctx.owner_id)
    return graph


async def competency_get(ctx: ToolContext, args: CompetencyGetArgs) -> dict:
    try:
        competency = await get_owned_competency(ctx.db, ctx.owner_id, args.competency_id)
    except Exception as exc:
        return {"error": str(exc.detail) if hasattr(exc, "detail") else str(exc)}
    if competency.scope == "plan" and ctx.plan_id != competency.plan_id:
        return {"error": "Plan-focused runs cannot inspect another plan's competency"}
    return competency_dict(competency)


async def evidence_list(ctx: ToolContext, args: EvidenceListArgs) -> dict:
    plan_id = args.plan_id if args.plan_id is not None else ctx.plan_id
    if error := _scope_plan(ctx, plan_id):
        return error
    try:
        await resolve_competency_scope(
            ctx.db,
            ctx.owner_id,
            competency_ids=(args.competency_id,) if args.competency_id is not None else (),
            plan_id=plan_id if args.task_id is None else None,
            task_id=args.task_id,
            focused_plan_id=plan_id,
            writable=False,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    observations = await list_observations(
        ctx.db,
        ctx.owner_id,
        plan_id=plan_id,
        task_id=args.task_id,
        competency_id=args.competency_id,
        limit=args.limit,
    )
    return {"observations": [observation_dict(item) for item in observations]}


COMPETENCY_TOOLS = [
    ToolDefinition("competency_create", "Create an explicitly named skill or concept node without silently merging similar titles.", CompetencyCreateArgs, competency_create, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("competency_link", "Map an explicit competency to a plan, task, or curated resource.", CompetencyLinkArgs, competency_link, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("competency_edge", "Add an auditable relation between competency nodes; prerequisite and part_of edges are cycle-checked.", CompetencyEdgeArgs, competency_edge, effect_kind=ToolEffectKind.DATABASE_WRITE, idempotent=True, blocking=True),
    ToolDefinition("competency_graph_get", "Read the explicit competency graph and plan/task/resource mappings.", CompetencyGraphArgs, competency_graph_get, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("competency_get", "Read one competency node without inferring mastery.", CompetencyGetArgs, competency_get, effect_kind=ToolEffectKind.PURE_READ),
    ToolDefinition("evidence_list", "List immutable evidence observations for the focused plan, task, or competency.", EvidenceListArgs, evidence_list, effect_kind=ToolEffectKind.PURE_READ),
]
