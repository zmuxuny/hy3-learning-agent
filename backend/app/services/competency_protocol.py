"""Durable mutation/reversal protocol for the competency graph."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.db.uow import flush as flush_uow
from app.models import (
    CompetencyEdge,
    CompetencyGraphMutation,
    CompetencyGraphState,
    EvidenceCompetencyLink,
    EvidenceObservation,
    Operation,
    OperationDependency,
    PlanCompetencyLink,
    ResourceCompetencyLink,
    TaskCompetencyLink,
)


GraphEntityRef = tuple[str, str | int]


@dataclass(frozen=True)
class CompetencyDependency:
    entity_type: str
    entity_id: str


async def graph_revision(db: AsyncSession, owner_id: str) -> int:
    revision = await db.scalar(
        select(CompetencyGraphState.revision).where(
            CompetencyGraphState.owner_id == owner_id
        )
    )
    return int(revision or 0)


async def record_graph_mutation(
    db: AsyncSession,
    *,
    owner_id: str,
    action_key: str,
    action: str,
    entity_type: str,
    entity_id: str | int,
    operation_id: str | None,
) -> tuple[int, bool]:
    """Atomically allocate one revision and append its immutable mutation.

    Replaying the same stable action key observes the original revision and
    never advances the graph again.  The caller owns the surrounding UoW, so
    the domain mutation, Operation, revision and ledger row commit together.
    """

    if action not in {"baseline", "apply", "undo", "redo"}:
        raise ValueError(f"Unsupported competency graph action: {action}")
    if not action_key or len(action_key) > 180:
        raise ValueError("Competency graph action_key must contain 1-180 characters")

    existing = await db.scalar(
        select(CompetencyGraphMutation).where(
            CompetencyGraphMutation.action_key == action_key
        )
    )
    if existing is not None:
        if (
            existing.owner_id != owner_id
            or existing.action != action
            or existing.entity_type != entity_type
            or existing.entity_id != str(entity_id)
            or existing.operation_id != operation_id
        ):
            raise ValueError("Competency graph action_key conflicts with another mutation")
        return existing.revision, False

    now = utc_now()
    allocated = await db.execute(
        sqlite_insert(CompetencyGraphState)
        .values(owner_id=owner_id, revision=1, updated_at=now)
        .on_conflict_do_update(
            index_elements=[CompetencyGraphState.owner_id],
            set_={
                "revision": CompetencyGraphState.revision + 1,
                "updated_at": now,
            },
        )
        .returning(CompetencyGraphState.revision)
    )
    revision = int(allocated.scalar_one())
    db.add(
        CompetencyGraphMutation(
            owner_id=owner_id,
            revision=revision,
            operation_id=operation_id,
            action_key=action_key,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
        )
    )
    await flush_uow(db)
    return revision, True


async def _creation_operation(
    db: AsyncSession,
    *,
    owner_id: str,
    entity_type: str,
    entity_id: str,
) -> Operation | None:
    operations = list(
        (
            await db.execute(
                select(Operation)
                .where(
                    Operation.owner_id == owner_id,
                    Operation.entity_type == entity_type,
                    Operation.entity_id == entity_id,
                    Operation.status == "committed",
                )
                .order_by(Operation.created_at.asc(), Operation.id.asc())
            )
        ).scalars()
    )
    for operation in operations:
        if str(operation.inverse_patch.get("delete", "")) == entity_id:
            return operation
    return None


async def record_operation_dependencies(
    db: AsyncSession,
    *,
    operation: Operation,
    prerequisites: Sequence[GraphEntityRef],
    dependency_kind: str,
) -> list[OperationDependency]:
    """Link a dependent graph Operation to provable entity creation ops."""

    if not operation.id:
        raise ValueError("The dependent Operation must be flushed first")
    recorded: list[OperationDependency] = []
    seen: set[str] = set()
    for entity_type, raw_entity_id in prerequisites:
        entity_id = str(raw_entity_id)
        prerequisite = await _creation_operation(
            db,
            owner_id=operation.owner_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if (
            prerequisite is None
            or prerequisite.id == operation.id
            or prerequisite.id in seen
        ):
            continue
        seen.add(prerequisite.id)
        existing = await db.scalar(
            select(OperationDependency).where(
                OperationDependency.operation_id == operation.id,
                OperationDependency.depends_on_operation_id == prerequisite.id,
                OperationDependency.dependency_kind == dependency_kind,
            )
        )
        if existing is not None:
            recorded.append(existing)
            continue
        dependency = OperationDependency(
            operation_id=operation.id,
            depends_on_operation_id=prerequisite.id,
            dependency_kind=dependency_kind,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        db.add(dependency)
        recorded.append(dependency)
    if recorded:
        await flush_uow(db)
    return recorded


async def active_operation_dependents(
    db: AsyncSession,
    *,
    operation_id: str,
) -> list[OperationDependency]:
    """Return later graph Operations that still block undoing a prerequisite."""

    return list(
        (
            await db.execute(
                select(OperationDependency)
                .join(Operation, Operation.id == OperationDependency.operation_id)
                .where(
                    OperationDependency.depends_on_operation_id == operation_id,
                    Operation.status.in_(
                        ("committed", "undo_pending", "needs_reconciliation")
                    ),
                )
                .order_by(OperationDependency.id)
            )
        ).scalars()
    )


async def competency_node_dependencies(
    db: AsyncSession,
    *,
    owner_id: str,
    competency_id: int,
) -> list[CompetencyDependency]:
    """Resolve every durable row that must block deleting a competency node."""

    dependencies: list[CompetencyDependency] = []
    edges = list(
        (
            await db.execute(
                select(CompetencyEdge).where(
                    CompetencyEdge.owner_id == owner_id,
                    (CompetencyEdge.source_id == competency_id)
                    | (CompetencyEdge.target_id == competency_id),
                )
            )
        ).scalars()
    )
    dependencies.extend(
        CompetencyDependency("competency_edge", str(item.id)) for item in edges
    )
    for model, entity_type in (
        (PlanCompetencyLink, "plan_competency_link"),
        (TaskCompetencyLink, "task_competency_link"),
        (ResourceCompetencyLink, "resource_competency_link"),
    ):
        links = list(
            (
                await db.execute(
                    select(model).where(
                        model.owner_id == owner_id,
                        model.competency_id == competency_id,
                    )
                )
            ).scalars()
        )
        dependencies.extend(
            CompetencyDependency(entity_type, str(item.id)) for item in links
        )
    evidence_links = list(
        (
            await db.execute(
                select(EvidenceCompetencyLink)
                .join(
                    EvidenceObservation,
                    EvidenceObservation.id == EvidenceCompetencyLink.observation_id,
                )
                .where(
                    EvidenceObservation.owner_id == owner_id,
                    EvidenceCompetencyLink.competency_id == competency_id,
                )
            )
        ).scalars()
    )
    dependencies.extend(
        CompetencyDependency("evidence_competency_link", str(item.id))
        for item in evidence_links
    )
    return dependencies
