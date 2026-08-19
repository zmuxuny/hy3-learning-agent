"""Explicit skill graph and plan/task/resource mappings for V2 M14."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, union
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
) -> tuple[Competency, bool]:
    if scope not in {"global", "plan"}:
        raise ValueError("scope must be global or plan")
    if scope == "plan" and plan_id is None:
        raise ValueError("plan competency requires plan_id")
    if plan_id is not None:
        plan = await db.scalar(select(Plan).where(Plan.id == plan_id, Plan.owner_id == owner_id))
        if plan is None:
            raise ValueError("Plan not found")
        if scope == "global":
            raise ValueError("global competency cannot be bound to one plan")
    existing = await db.scalar(
        select(Competency).where(Competency.owner_id == owner_id, Competency.key == key)
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
    db.add(competency)
    await flush_uow(db)
    return competency, True


async def _would_cycle(db: AsyncSession, owner_id: str, source_id: int, target_id: int) -> bool:
    edges = list((await db.execute(
        select(CompetencyEdge).where(
            CompetencyEdge.owner_id == owner_id,
            CompetencyEdge.relation.in_(ACYCLIC_RELATIONS),
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
) -> tuple[CompetencyEdge, bool]:
    if relation not in VALID_RELATIONS:
        raise ValueError(f"Unsupported competency relation: {relation}")
    if source_id == target_id:
        raise ValueError("A competency cannot point to itself")
    await get_owned_competency(db, owner_id, source_id)
    await get_owned_competency(db, owner_id, target_id)
    if relation in ACYCLIC_RELATIONS and await _would_cycle(db, owner_id, source_id, target_id):
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
) -> dict[str, Any]:
    if sum(value is not None for value in (plan_id, task_id, resource_id)) != 1:
        raise ValueError("Provide exactly one plan_id, task_id, or resource_id")
    if target_stage not in VALID_STAGES:
        raise ValueError(f"Unsupported target stage: {target_stage}")
    competency = await get_owned_competency(db, owner_id, competency_id)
    if plan_id is not None:
        plan = await db.scalar(select(Plan).where(Plan.id == plan_id, Plan.owner_id == owner_id))
        if plan is None:
            raise ValueError("Plan not found")
        if plan.status == "archived":
            raise ValueError("Restore the plan before changing competency mappings")
        if competency.scope == "plan" and competency.plan_id != plan_id:
            raise ValueError("Plan competency cannot be linked to another plan")
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
        task = (await db.execute(
            select(Task)
            .join(Task.stage)
            .join(Plan)
            .where(Task.id == task_id, Plan.owner_id == owner_id)
            .options(selectinload(Task.stage).selectinload(Stage.plan))
        )).scalars().one_or_none()
        if task is None:
            raise ValueError("Task not found")
        plan = task.stage.plan
        if plan.status == "archived":
            raise ValueError("Restore the plan before changing competency mappings")
        if competency.scope == "plan" and competency.plan_id != plan.id:
            raise ValueError("Plan competency cannot be linked to another plan's task")
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
    resource = await db.scalar(select(LearningResource).where(
        LearningResource.id == resource_id, LearningResource.owner_id == owner_id,
    ))
    if resource is None:
        raise ValueError("Resource not found")
    if resource.plan_id is not None and competency.scope == "plan" and competency.plan_id != resource.plan_id:
        raise ValueError("Plan competency cannot be linked to another plan's resource")
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


async def graph_for_plan(db: AsyncSession, owner_id: str, plan_id: int | None = None) -> dict[str, Any]:
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
            .where(ResourceCompetencyLink.owner_id == owner_id, LearningResource.plan_id == plan_id),
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
        resource_links = list((await db.execute(select(ResourceCompetencyLink).join(LearningResource).where(
            ResourceCompetencyLink.owner_id == owner_id, LearningResource.plan_id == plan_id,
        ))).scalars())
    return {
        "plan_id": plan_id,
        "competencies": [competency_dict(item) for item in competencies],
        "edges": [{"id": edge.id, "source_id": edge.source_id, "target_id": edge.target_id, "relation": edge.relation} for edge in edges],
        "plan_links": [{"id": item.id, "plan_id": item.plan_id, "competency_id": item.competency_id, "relation": item.relation, "target_stage": item.target_stage} for item in plan_links],
        "task_links": [{"id": item.id, "task_id": item.task_id, "competency_id": item.competency_id, "relation": item.relation, "target_stage": item.target_stage} for item in task_links],
        "resource_links": [{"id": item.id, "resource_id": item.resource_id, "competency_id": item.competency_id, "relation": item.relation, "depth": item.depth} for item in resource_links],
    }
