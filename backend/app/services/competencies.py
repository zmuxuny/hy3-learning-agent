"""Explicit skill graph and plan/task/resource mappings for V2 M14."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.uow import flush as flush_uow
from app.models import (
    Competency,
    CompetencyEdge,
    LearningResource,
    Plan,
    PlanCompetencyLink,
    ResourceCompetencyLink,
    Task,
    Stage,
    TaskCompetencyLink,
)


ACYCLIC_RELATIONS = {"prerequisite", "part_of"}
VALID_RELATIONS = ACYCLIC_RELATIONS | {"related_to", "equivalent_to"}
VALID_STAGES = {"unknown", "exposed", "practicing", "demonstrated", "retained"}


@dataclass(frozen=True)
class CompetencyScopeResolution:
    """Authoritative database scope for one graph operation.

    A global competency contributes no plan restriction.  Every non-global
    endpoint and every concrete target contributes its real plan; an operation
    is valid only when all of those facts collapse to at most one plan.
    """

    plan_id: int | None
    competencies: tuple[Competency, ...]
    plan: Plan | None = None
    task: Task | None = None
    resource: LearningResource | None = None


async def _owned_plan(
    db: AsyncSession,
    owner_id: str,
    plan_id: int,
    *,
    writable: bool,
) -> Plan:
    plan = await db.scalar(select(Plan).where(Plan.id == plan_id, Plan.owner_id == owner_id))
    if plan is None:
        raise ValueError("Plan not found")
    if writable and plan.status == "archived":
        raise ValueError("Restore the plan before changing the competency graph")
    return plan


async def resolve_competency_scope(
    db: AsyncSession,
    owner_id: str,
    *,
    competency_ids: Sequence[int] = (),
    plan_id: int | None = None,
    task_id: int | None = None,
    resource_id: int | None = None,
    focused_plan_id: int | None = None,
    writable: bool = True,
) -> CompetencyScopeResolution:
    """Resolve graph scope from owned database rows, never caller assertions."""

    if sum(value is not None for value in (plan_id, task_id, resource_id)) > 1:
        raise ValueError("Provide at most one plan_id, task_id, or resource_id")

    ordered_ids = tuple(dict.fromkeys(int(value) for value in competency_ids))
    competencies: tuple[Competency, ...] = ()
    if ordered_ids:
        loaded = list(
            (
                await db.execute(
                    select(Competency).where(
                        Competency.owner_id == owner_id,
                        Competency.id.in_(ordered_ids),
                    )
                )
            ).scalars()
        )
        by_id = {item.id: item for item in loaded}
        if any(competency_id not in by_id for competency_id in ordered_ids):
            raise ValueError("Competency not found")
        competencies = tuple(by_id[competency_id] for competency_id in ordered_ids)

    effective_plan_ids: set[int] = set()
    plan_rows: dict[int, Plan] = {}
    for competency in competencies:
        if writable and competency.status != "active":
            raise ValueError("Archived competencies cannot be changed")
        if competency.scope == "global":
            if competency.plan_id is not None:
                raise ValueError("Global competency has an invalid plan binding")
            continue
        if competency.scope != "plan" or competency.plan_id is None:
            raise ValueError("Plan competency has an invalid scope binding")
        effective_plan_ids.add(competency.plan_id)

    resolved_plan: Plan | None = None
    resolved_task: Task | None = None
    resolved_resource: LearningResource | None = None
    if plan_id is not None:
        resolved_plan = await _owned_plan(db, owner_id, plan_id, writable=writable)
        effective_plan_ids.add(resolved_plan.id)
        plan_rows[resolved_plan.id] = resolved_plan
    elif task_id is not None:
        resolved_task = (
            await db.execute(
                select(Task)
                .join(Task.stage)
                .join(Plan)
                .where(Task.id == task_id, Plan.owner_id == owner_id)
                .options(selectinload(Task.stage).selectinload(Stage.plan))
            )
        ).scalars().one_or_none()
        if resolved_task is None:
            raise ValueError("Task not found")
        resolved_plan = resolved_task.stage.plan
        if writable and resolved_plan.status == "archived":
            raise ValueError("Restore the plan before changing competency mappings")
        effective_plan_ids.add(resolved_plan.id)
        plan_rows[resolved_plan.id] = resolved_plan
    elif resource_id is not None:
        resolved_resource = await db.scalar(
            select(LearningResource).where(
                LearningResource.id == resource_id,
                LearningResource.owner_id == owner_id,
            )
        )
        if resolved_resource is None:
            raise ValueError("Resource not found")
        if resolved_resource.plan_id is not None:
            resolved_plan = await _owned_plan(
                db,
                owner_id,
                resolved_resource.plan_id,
                writable=writable,
            )
            effective_plan_ids.add(resolved_plan.id)
            plan_rows[resolved_plan.id] = resolved_plan

    # Validate the plans referenced by private competencies independently of
    # the target rows.  A plain FK is not an owner/scope authorization guard.
    for referenced_plan_id in sorted(effective_plan_ids):
        if referenced_plan_id not in plan_rows:
            plan_rows[referenced_plan_id] = await _owned_plan(
                db,
                owner_id,
                referenced_plan_id,
                writable=writable,
            )

    if len(effective_plan_ids) > 1:
        raise ValueError("Competency graph endpoints belong to different plans")
    effective_plan_id = next(iter(effective_plan_ids), None)
    if (
        effective_plan_id is not None
        and focused_plan_id is None
        and plan_id is None
    ):
        raise ValueError("A focused or explicit plan is required for plan-private competency changes")
    if (
        focused_plan_id is not None
        and effective_plan_id is not None
        and focused_plan_id != effective_plan_id
    ):
        raise ValueError("Plan-focused runs cannot operate on another plan's competency graph")

    return CompetencyScopeResolution(
        plan_id=effective_plan_id,
        competencies=competencies,
        plan=resolved_plan,
        task=resolved_task,
        resource=resolved_resource,
    )


async def get_owned_competency(db: AsyncSession, owner_id: str, competency_id: int) -> Competency:
    competency = await db.scalar(
        select(Competency).where(Competency.id == competency_id, Competency.owner_id == owner_id)
    )
    if competency is None:
        raise HTTPException(status_code=404, detail="Competency not found")
    return competency


async def create_competency(
    db: AsyncSession,
    owner_id: str,
    *,
    key: str,
    title: str,
    description: str = "",
    competency_type: str = "concept",
    scope: str = "global",
    plan_id: int | None = None,
    focused_plan_id: int | None = None,
) -> tuple[Competency, bool]:
    if scope not in {"global", "plan"}:
        raise ValueError("scope must be global or plan")
    if scope == "plan" and plan_id is None:
        raise ValueError("plan competency requires plan_id")
    if scope == "global" and plan_id is not None:
        raise ValueError("global competency cannot be bound to one plan")
    await resolve_competency_scope(
        db,
        owner_id,
        plan_id=plan_id,
        focused_plan_id=focused_plan_id,
    )
    existing = await db.scalar(
        select(Competency).where(
            Competency.owner_id == owner_id,
            Competency.key == key,
            Competency.scope == scope,
            Competency.plan_id.is_(None) if plan_id is None else Competency.plan_id == plan_id,
        )
    )
    if existing is not None:
        if existing.title != title or (plan_id is not None and existing.plan_id != plan_id):
            raise ValueError("Competency key already belongs to a different node")
        return existing, False
    competency = Competency(
        owner_id=owner_id,
        key=key,
        title=title,
        description=description,
        competency_type=competency_type,
        scope=scope,
        plan_id=plan_id,
    )
    try:
        async with db.begin_nested():
            db.add(competency)
            await flush_uow(db)
        return competency, True
    except IntegrityError:
        existing = await db.scalar(
            select(Competency).where(
                Competency.owner_id == owner_id,
                Competency.key == key,
                Competency.scope == scope,
                Competency.plan_id.is_(None)
                if plan_id is None
                else Competency.plan_id == plan_id,
            )
        )
        if existing is None:
            raise
        if existing.title != title:
            raise ValueError("Competency key already belongs to a different node")
        return existing, False


async def _would_cycle(
    db: AsyncSession,
    owner_id: str,
    source_id: int,
    target_id: int,
    *,
    plan_id: int | None,
) -> bool:
    node_scope = select(Competency.id).where(
        Competency.owner_id == owner_id,
        (Competency.scope == "global")
        | ((Competency.scope == "plan") & (Competency.plan_id == plan_id)),
    )
    edges = list((await db.execute(
        select(CompetencyEdge).where(
            CompetencyEdge.owner_id == owner_id,
            CompetencyEdge.relation.in_(ACYCLIC_RELATIONS),
            CompetencyEdge.source_id.in_(node_scope),
            CompetencyEdge.target_id.in_(node_scope),
        )
    )).scalars())
    adjacency: dict[int, set[int]] = defaultdict(set)
    for edge in edges:
        adjacency[edge.source_id].add(edge.target_id)
    adjacency[source_id].add(target_id)
    queue = deque([target_id])
    visited: set[int] = set()
    while queue:
        node = queue.popleft()
        if node == source_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        queue.extend(adjacency[node])
    return False


async def add_edge(
    db: AsyncSession,
    owner_id: str,
    *,
    source_id: int,
    target_id: int,
    relation: str,
    focused_plan_id: int | None = None,
) -> tuple[CompetencyEdge, bool]:
    if relation not in VALID_RELATIONS:
        raise ValueError(f"Unsupported competency relation: {relation}")
    if source_id == target_id:
        raise ValueError("A competency cannot point to itself")
    resolved = await resolve_competency_scope(
        db,
        owner_id,
        competency_ids=(source_id, target_id),
        focused_plan_id=focused_plan_id,
    )
    if relation in ACYCLIC_RELATIONS and await _would_cycle(
        db,
        owner_id,
        source_id,
        target_id,
        plan_id=resolved.plan_id,
    ):
        raise ValueError("This competency edge would create a cycle")
    existing = await db.scalar(select(CompetencyEdge).where(
        CompetencyEdge.owner_id == owner_id,
        CompetencyEdge.source_id == source_id,
        CompetencyEdge.target_id == target_id,
        CompetencyEdge.relation == relation,
    ))
    if existing is not None:
        return existing, False
    edge = CompetencyEdge(
        owner_id=owner_id,
        source_id=source_id,
        target_id=target_id,
        relation=relation,
    )
    db.add(edge)
    await flush_uow(db)
    return edge, True


async def link_competency(
    db: AsyncSession,
    owner_id: str,
    *,
    competency_id: int,
    plan_id: int | None = None,
    task_id: int | None = None,
    resource_id: int | None = None,
    relation: str = "teaches",
    target_stage: str = "practicing",
    depth: str = "overview",
    focused_plan_id: int | None = None,
) -> dict[str, Any]:
    if sum(value is not None for value in (plan_id, task_id, resource_id)) != 1:
        raise ValueError("Provide exactly one plan_id, task_id, or resource_id")
    if target_stage not in VALID_STAGES:
        raise ValueError(f"Unsupported target stage: {target_stage}")
    resolved = await resolve_competency_scope(
        db,
        owner_id,
        competency_ids=(competency_id,),
        plan_id=plan_id,
        task_id=task_id,
        resource_id=resource_id,
        focused_plan_id=focused_plan_id,
    )
    competency = resolved.competencies[0]
    if plan_id is not None:
        if relation != "targets":
            raise ValueError("Plan competency mappings use the targets relation")
        existing = await db.scalar(select(PlanCompetencyLink).where(
            PlanCompetencyLink.owner_id == owner_id,
            PlanCompetencyLink.plan_id == plan_id,
            PlanCompetencyLink.competency_id == competency_id,
        ))
        if existing is None:
            existing = PlanCompetencyLink(
                owner_id=owner_id, plan_id=plan_id, competency_id=competency_id,
                relation="targets", target_stage=target_stage,
            )
            db.add(existing)
            await flush_uow(db)
            created = True
        else:
            created = False
        return {"kind": "plan", "link_id": existing.id, "competency_id": competency.id, "created": created}
    if task_id is not None:
        if relation not in {"teaches", "assesses"}:
            raise ValueError("Task competency relation must be teaches or assesses")
        existing = await db.scalar(select(TaskCompetencyLink).where(
            TaskCompetencyLink.owner_id == owner_id,
            TaskCompetencyLink.task_id == task_id,
            TaskCompetencyLink.competency_id == competency_id,
            TaskCompetencyLink.relation == relation,
        ))
        if existing is None:
            existing = TaskCompetencyLink(
                owner_id=owner_id, task_id=task_id, competency_id=competency_id,
                relation=relation, target_stage=target_stage,
            )
            db.add(existing)
            await flush_uow(db)
            created = True
        else:
            created = False
        return {"kind": "task", "link_id": existing.id, "competency_id": competency.id, "created": created}
    if relation != "covers":
        raise ValueError("Resource competency mappings use the covers relation")
    existing = await db.scalar(select(ResourceCompetencyLink).where(
        ResourceCompetencyLink.owner_id == owner_id,
        ResourceCompetencyLink.resource_id == resource_id,
        ResourceCompetencyLink.competency_id == competency_id,
    ))
    if existing is None:
        existing = ResourceCompetencyLink(
            owner_id=owner_id, resource_id=resource_id, competency_id=competency_id,
            depth=depth, relation="covers",
        )
        db.add(existing)
        await flush_uow(db)
        created = True
    else:
        created = False
    return {"kind": "resource", "link_id": existing.id, "competency_id": competency.id, "created": created}


def competency_dict(item: Competency) -> dict[str, Any]:
    return {
        "id": item.id,
        "key": item.key,
        "title": item.title,
        "description": item.description,
        "type": item.competency_type,
        "scope": item.scope,
        "plan_id": item.plan_id,
        "status": item.status,
        "version": item.version,
    }


async def list_merge_candidates(
    db: AsyncSession,
    owner_id: str,
) -> list[dict[str, Any]]:
    """Propose equal-key private nodes without mutating or merging the graph."""

    nodes = list(
        (
            await db.execute(
                select(Competency)
                .join(Plan, Plan.id == Competency.plan_id)
                .where(
                    Competency.owner_id == owner_id,
                    Competency.scope == "plan",
                    Competency.plan_id.is_not(None),
                    Competency.status == "active",
                    Plan.owner_id == owner_id,
                    Plan.status != "archived",
                )
                .order_by(
                    Competency.key,
                    Competency.plan_id,
                    Competency.id,
                )
            )
        ).scalars()
    )
    by_key: dict[str, list[Competency]] = defaultdict(list)
    for node in nodes:
        by_key[node.key].append(node)

    candidates: list[dict[str, Any]] = []
    for key in sorted(by_key):
        matching = by_key[key]
        plan_ids = sorted({int(node.plan_id) for node in matching if node.plan_id is not None})
        if len(plan_ids) < 2:
            continue
        candidates.append(
            {
                "key": key,
                "competency_ids": sorted(node.id for node in matching),
                "plan_ids": plan_ids,
                "status": "proposed",
            }
        )
    return candidates


async def graph_for_plan(db: AsyncSession, owner_id: str, plan_id: int | None = None) -> dict[str, Any]:
    if plan_id is not None:
        await resolve_competency_scope(
            db,
            owner_id,
            plan_id=plan_id,
            focused_plan_id=plan_id,
            writable=False,
        )
    query = select(Competency).where(Competency.owner_id == owner_id)
    if plan_id is not None:
        linked_ids = union(
            select(PlanCompetencyLink.competency_id).where(
                PlanCompetencyLink.owner_id == owner_id,
                PlanCompetencyLink.plan_id == plan_id,
            ),
            select(TaskCompetencyLink.competency_id)
            .join(Task, Task.id == TaskCompetencyLink.task_id)
            .join(Stage, Stage.id == Task.stage_id)
            .where(TaskCompetencyLink.owner_id == owner_id, Stage.plan_id == plan_id),
            select(ResourceCompetencyLink.competency_id)
            .join(LearningResource, LearningResource.id == ResourceCompetencyLink.resource_id)
            .join(Competency, Competency.id == ResourceCompetencyLink.competency_id)
            .where(
                ResourceCompetencyLink.owner_id == owner_id,
                or_(
                    LearningResource.plan_id == plan_id,
                    and_(
                        LearningResource.plan_id.is_(None),
                        Competency.scope == "plan",
                        Competency.plan_id == plan_id,
                    ),
                ),
            ),
        ).subquery()
        query = query.where(
            ((Competency.scope == "plan") & (Competency.plan_id == plan_id))
            | Competency.id.in_(select(linked_ids.c.competency_id))
        )
    competencies = list((await db.execute(query.order_by(Competency.title))).scalars())
    ids = {item.id for item in competencies}
    edges = list((await db.execute(select(CompetencyEdge).where(
        CompetencyEdge.owner_id == owner_id,
        CompetencyEdge.source_id.in_(ids or {-1}),
        CompetencyEdge.target_id.in_(ids or {-1}),
    ))).scalars())
    plan_links = []
    task_links = []
    resource_links = []
    if plan_id is not None:
        plan_links = list((await db.execute(select(PlanCompetencyLink).where(
            PlanCompetencyLink.owner_id == owner_id, PlanCompetencyLink.plan_id == plan_id,
        ))).scalars())
        task_links = list((await db.execute(select(TaskCompetencyLink).join(Task).join(Task.stage).where(
            TaskCompetencyLink.owner_id == owner_id, Task.stage.has(Stage.plan_id == plan_id),
        ))).scalars())
        resource_links = list(
            (
                await db.execute(
                    select(ResourceCompetencyLink)
                    .join(LearningResource)
                    .join(Competency, Competency.id == ResourceCompetencyLink.competency_id)
                    .where(
                        ResourceCompetencyLink.owner_id == owner_id,
                        or_(
                            LearningResource.plan_id == plan_id,
                            and_(
                                LearningResource.plan_id.is_(None),
                                Competency.scope == "plan",
                                Competency.plan_id == plan_id,
                            ),
                        ),
                    )
                )
            ).scalars()
        )
    return {
        "plan_id": plan_id,
        "competencies": [competency_dict(item) for item in competencies],
        "edges": [{"id": edge.id, "source_id": edge.source_id, "target_id": edge.target_id, "relation": edge.relation} for edge in edges],
        "plan_links": [{"id": item.id, "plan_id": item.plan_id, "competency_id": item.competency_id, "relation": item.relation, "target_stage": item.target_stage} for item in plan_links],
        "task_links": [{"id": item.id, "task_id": item.task_id, "competency_id": item.competency_id, "relation": item.relation, "target_stage": item.target_stage} for item in task_links],
        "resource_links": [{"id": item.id, "resource_id": item.resource_id, "competency_id": item.competency_id, "relation": item.relation, "depth": item.depth} for item in resource_links],
    }
