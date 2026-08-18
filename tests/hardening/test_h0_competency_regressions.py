"""H0 failure baselines for the H4 competency/evidence hardening gate.

Every marked test describes desired H4 behavior and must fail on the reviewed
implementation.  H4 removes the matching strict xfail when the implementation
is corrected.  The tests use a fresh temporary SQLite database and never touch
the configured application or user database.

Defect mapping:

* H4-COMP-001: plan-scoped key uniqueness
* H4-COMP-002: both competency-edge endpoints need scope guards
* H4-COMP-003/H4-COMP-004: task/resource link endpoint scope guards
* H4-COMP-005: durable graph revision
* H4-COMP-006: undo dependency protection
* H4-EVID-007: Task ``assesses`` mapping and one-to-many competency links
* H4-EVID-008: competency filtering before pagination
* H4-SCHEMA-001: typed nested ``evidence_list`` output schema
* H4-SCHEMA-002: extra fields forbidden at every public schema boundary
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.operations import undo_operation
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    Competency,
    CompetencyEdge,
    LearningResource,
    Operation,
    Plan,
    ResourceCompetencyLink,
    Stage,
    Task,
    TaskCompetencyLink,
)
from app.services.competencies import create_competency
from app.services.evidence import append_observation
from app.tools import ToolContext, execute_tool
from app.tools.competencies import EvidenceListArgs
from app.tools.contracts import TOOL_OUTPUT_MODELS
from app.tools.registry import tool_contracts


OWNER_ID = "local"


@pytest_asyncio.fixture
async def isolated_db() -> AsyncIterator[AsyncSession]:
    """Open a session on conftest's disposable, per-test-reset SQLite DB."""

    async with AsyncSessionLocal() as db:
        yield db


async def _add_plan(db: AsyncSession, title: str) -> tuple[Plan, Task]:
    task = Task(title=f"{title} task", position=0)
    stage = Stage(title=f"{title} stage", position=0, tasks=[task])
    plan = Plan(owner_id=OWNER_ID, title=title, stages=[stage])
    db.add(plan)
    await db.flush()
    return plan, task


async def _add_run(db: AsyncSession, plan_id: int | None) -> AgentRun:
    run = AgentRun(
        owner_id=OWNER_ID,
        plan_id=plan_id,
        trigger="user_message",
        objective="H0 competency regression",
    )
    db.add(run)
    await db.flush()
    return run


def _context(db: AsyncSession, run: AgentRun) -> ToolContext:
    return ToolContext(
        db=db,
        owner_id=OWNER_ID,
        run_id=run.id,
        trigger="user_message",
        plan_id=run.plan_id,
    )


async def _tool_ok(name: str, payload: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await execute_tool(name, json.dumps(payload), ctx)
    if result.get("ok") is not True:
        raise RuntimeError(f"Fixture setup tool {name} failed: {result}")
    return result["data"]


async def _add_competency(
    db: AsyncSession,
    *,
    key: str,
    title: str,
    plan_id: int | None,
) -> Competency:
    competency, created = await create_competency(
        db,
        OWNER_ID,
        key=key,
        title=title,
        scope="plan" if plan_id is not None else "global",
        plan_id=plan_id,
    )
    if not created:
        raise RuntimeError(f"Fixture competency was unexpectedly reused: {key}")
    return competency


async def _grade_quiz(ctx: ToolContext, plan_id: int, task_id: int) -> None:
    quiz = await _tool_ok(
        "quiz_create",
        {
            "plan_id": plan_id,
            "task_id": task_id,
            "prompt": "Explain the result.",
            "rubric": {"pass_threshold": 70},
        },
        ctx,
    )
    await _tool_ok(
        "quiz_grade",
        {
            "quiz_id": quiz["quiz_id"],
            "answer": "A supported answer",
            "score": 90,
            "feedback": "Meets the rubric",
            "evidence": [{"kind": "answer", "verified": True}],
        },
        ctx,
    )


async def _list_evidence(
    ctx: ToolContext,
    *,
    plan_id: int,
    competency_id: int | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"plan_id": plan_id, "limit": limit}
    if competency_id is not None:
        payload["competency_id"] = competency_id
    result = await execute_tool("evidence_list", json.dumps(payload), ctx)
    if result.get("ok") is not True:
        raise RuntimeError(f"Fixture evidence_list failed: {result}")
    return result["data"]["observations"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-COMP-001: plan-private keys are incorrectly unique across the owner",
)
@pytest.mark.asyncio
async def test_plan_scoped_competency_key_is_unique_per_plan(isolated_db: AsyncSession):
    plan_a, _ = await _add_plan(isolated_db, "Plan A")
    plan_b, _ = await _add_plan(isolated_db, "Plan B")
    run = await _add_run(isolated_db, None)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)

    first = await _tool_ok(
        "competency_create",
        {"key": "python.async", "title": "Async Python", "scope": "plan", "plan_id": plan_a.id},
        ctx,
    )
    second = await execute_tool(
        "competency_create",
        json.dumps({"key": "python.async", "title": "Async Python", "scope": "plan", "plan_id": plan_b.id}),
        ctx,
    )

    assert second.get("ok") is True and second["data"]["competency_id"] != first["competency_id"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-COMP-002: competency_edge does not guard both plan-scoped endpoints",
)
@pytest.mark.parametrize("foreign_endpoint", ["source", "target"])
@pytest.mark.asyncio
async def test_edge_rejects_a_foreign_plan_endpoint(
    isolated_db: AsyncSession,
    foreign_endpoint: str,
):
    plan_a, _ = await _add_plan(isolated_db, "Plan A")
    plan_b, _ = await _add_plan(isolated_db, "Plan B")
    local = await _add_competency(isolated_db, key="plan-a", title="Plan A skill", plan_id=plan_a.id)
    foreign = await _add_competency(isolated_db, key="plan-b", title="Plan B skill", plan_id=plan_b.id)
    run = await _add_run(isolated_db, plan_a.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)
    source_id, target_id = (
        (foreign.id, local.id) if foreign_endpoint == "source" else (local.id, foreign.id)
    )

    result = await execute_tool(
        "competency_edge",
        json.dumps({"source_id": source_id, "target_id": target_id, "relation": "related_to"}),
        ctx,
    )
    stored = await isolated_db.scalar(
        select(func.count()).select_from(CompetencyEdge).where(
            CompetencyEdge.source_id == source_id,
            CompetencyEdge.target_id == target_id,
        )
    )

    assert result.get("ok") is False and stored == 0


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-COMP-003: task link scope is inferred from the Run instead of the task row",
)
@pytest.mark.asyncio
async def test_task_link_resolves_the_target_task_plan(isolated_db: AsyncSession):
    plan_a, _ = await _add_plan(isolated_db, "Plan A")
    plan_b, task_b = await _add_plan(isolated_db, "Plan B")
    global_competency = await _add_competency(
        isolated_db,
        key="global.skill",
        title="Global skill",
        plan_id=None,
    )
    run = await _add_run(isolated_db, plan_a.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)

    result = await execute_tool(
        "competency_link",
        json.dumps(
            {
                "competency_id": global_competency.id,
                "task_id": task_b.id,
                "relation": "assesses",
                "target_stage": "demonstrated",
            }
        ),
        ctx,
    )
    stored = await isolated_db.scalar(
        select(func.count()).select_from(TaskCompetencyLink).where(
            TaskCompetencyLink.task_id == task_b.id,
            TaskCompetencyLink.competency_id == global_competency.id,
        )
    )

    assert result.get("ok") is False and stored == 0


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-COMP-004: resource link does not guard the competency endpoint scope",
)
@pytest.mark.asyncio
async def test_resource_link_resolves_the_competency_plan(isolated_db: AsyncSession):
    plan_a, _ = await _add_plan(isolated_db, "Plan A")
    plan_b, _ = await _add_plan(isolated_db, "Plan B")
    foreign_competency = await _add_competency(
        isolated_db,
        key="plan-b.private",
        title="Plan B private skill",
        plan_id=plan_b.id,
    )
    resource = LearningResource(owner_id=OWNER_ID, title="Global resource", plan_id=None)
    isolated_db.add(resource)
    run = await _add_run(isolated_db, plan_a.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)

    result = await execute_tool(
        "competency_link",
        json.dumps(
            {
                "competency_id": foreign_competency.id,
                "resource_id": resource.id,
                "relation": "covers",
            }
        ),
        ctx,
    )
    stored = await isolated_db.scalar(
        select(func.count()).select_from(ResourceCompetencyLink).where(
            ResourceCompetencyLink.resource_id == resource.id,
            ResourceCompetencyLink.competency_id == foreign_competency.id,
        )
    )

    assert result.get("ok") is False and stored == 0


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-COMP-005: competency graph mutations do not advance a durable revision",
)
@pytest.mark.asyncio
async def test_graph_revision_advances_after_a_mutation(isolated_db: AsyncSession):
    plan, _ = await _add_plan(isolated_db, "Revision plan")
    run = await _add_run(isolated_db, plan.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)

    before = await _tool_ok("competency_graph_get", {"plan_id": plan.id}, ctx)
    await _tool_ok(
        "competency_create",
        {"key": "revision.skill", "title": "Revision skill", "scope": "plan", "plan_id": plan.id},
        ctx,
    )
    after = await _tool_ok("competency_graph_get", {"plan_id": plan.id}, ctx)

    assert (
        isinstance(before.get("revision"), int)
        and isinstance(after.get("revision"), int)
        and after["revision"] == before["revision"] + 1
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-COMP-006: undoing a node silently cascades over later graph operations",
)
@pytest.mark.asyncio
async def test_node_undo_rejects_later_edge_dependencies(isolated_db: AsyncSession):
    plan, _ = await _add_plan(isolated_db, "Dependency plan")
    run = await _add_run(isolated_db, plan.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)
    source = await _tool_ok(
        "competency_create",
        {"key": "dependency.source", "title": "Source", "scope": "plan", "plan_id": plan.id},
        ctx,
    )
    target = await _tool_ok(
        "competency_create",
        {"key": "dependency.target", "title": "Target", "scope": "plan", "plan_id": plan.id},
        ctx,
    )
    edge = await _tool_ok(
        "competency_edge",
        {"source_id": source["competency_id"], "target_id": target["competency_id"], "relation": "related_to"},
        ctx,
    )

    rejected = False
    try:
        await undo_operation(source["operation_id"], isolated_db)
    except HTTPException as exc:
        rejected = exc.status_code == 409

    source_row = await isolated_db.get(Competency, source["competency_id"])
    edge_row = await isolated_db.get(CompetencyEdge, edge["edge_id"])
    edge_operation = await isolated_db.get(Operation, edge["operation_id"])

    assert (
        rejected
        and source_row is not None
        and edge_row is not None
        and edge_operation is not None
        and edge_operation.status == "committed"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-EVID-007: Task assesses mappings are not attached to produced evidence",
)
@pytest.mark.asyncio
async def test_task_assesses_mapping_is_applied_to_quiz_evidence(isolated_db: AsyncSession):
    plan, task = await _add_plan(isolated_db, "Assessment plan")
    competency = await _add_competency(
        isolated_db,
        key="assessment.skill",
        title="Assessed skill",
        plan_id=plan.id,
    )
    run = await _add_run(isolated_db, plan.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)
    await _tool_ok(
        "competency_link",
        {
            "competency_id": competency.id,
            "task_id": task.id,
            "relation": "assesses",
            "target_stage": "demonstrated",
        },
        ctx,
    )

    await _grade_quiz(ctx, plan.id, task.id)
    observations = await _list_evidence(ctx, plan_id=plan.id, competency_id=competency.id)
    passed = [item for item in observations if item["source_type"] == "quiz" and item["outcome"] == "passed"]

    assert len(passed) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-EVID-007: one observation cannot represent multiple assessed competencies",
)
@pytest.mark.asyncio
async def test_one_quiz_observation_maps_to_multiple_assessed_competencies(
    isolated_db: AsyncSession,
):
    plan, task = await _add_plan(isolated_db, "Multi-skill plan")
    first = await _add_competency(
        isolated_db,
        key="multi.first",
        title="First assessed skill",
        plan_id=plan.id,
    )
    second = await _add_competency(
        isolated_db,
        key="multi.second",
        title="Second assessed skill",
        plan_id=plan.id,
    )
    run = await _add_run(isolated_db, plan.id)
    await isolated_db.commit()
    ctx = _context(isolated_db, run)
    for competency in (first, second):
        await _tool_ok(
            "competency_link",
            {
                "competency_id": competency.id,
                "task_id": task.id,
                "relation": "assesses",
                "target_stage": "demonstrated",
            },
            ctx,
        )

    await _grade_quiz(ctx, plan.id, task.id)
    all_observations = await _list_evidence(ctx, plan_id=plan.id)
    first_observations = await _list_evidence(ctx, plan_id=plan.id, competency_id=first.id)
    second_observations = await _list_evidence(ctx, plan_id=plan.id, competency_id=second.id)
    primary = [
        item
        for item in all_observations
        if item["source_type"] == "quiz" and item["outcome"] == "passed"
    ]
    first_ids = [item["id"] for item in first_observations if item["outcome"] == "passed"]
    second_ids = [item["id"] for item in second_observations if item["outcome"] == "passed"]

    assert len(primary) == 1 and first_ids == second_ids == [primary[0]["id"]]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-EVID-008: competency filtering happens after SQL pagination",
)
@pytest.mark.asyncio
async def test_evidence_list_filters_competency_before_limit(isolated_db: AsyncSession):
    plan, task = await _add_plan(isolated_db, "Pagination plan")
    target = await _add_competency(
        isolated_db,
        key="pagination.target",
        title="Pagination target",
        plan_id=plan.id,
    )
    noise = await _add_competency(
        isolated_db,
        key="pagination.noise",
        title="Pagination noise",
        plan_id=plan.id,
    )
    run = await _add_run(isolated_db, plan.id)
    await append_observation(
        isolated_db,
        owner_id=OWNER_ID,
        source_type="manual",
        source_id="older-target",
        outcome="passed",
        idempotency_key="h0:pagination:target",
        plan_id=plan.id,
        task_id=task.id,
        competency_id=target.id,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    await append_observation(
        isolated_db,
        owner_id=OWNER_ID,
        source_type="manual",
        source_id="newer-noise",
        outcome="passed",
        idempotency_key="h0:pagination:noise",
        plan_id=plan.id,
        task_id=task.id,
        competency_id=noise.id,
        occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    await isolated_db.commit()
    ctx = _context(isolated_db, run)

    observations = await _list_evidence(
        ctx,
        plan_id=plan.id,
        competency_id=target.id,
        limit=1,
    )

    assert [item["source_id"] for item in observations] == ["older-target"]


def _resolve_local_ref(root: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    current = node
    while "$ref" in current:
        path = current["$ref"].removeprefix("#/").split("/")
        resolved: Any = root
        for segment in path:
            resolved = resolved[segment]
        current = resolved
    return current


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-SCHEMA-001: evidence_list exposes untyped list/dict output items",
)
def test_evidence_list_contract_has_typed_nested_models():
    contract = next(item for item in tool_contracts() if item["name"] == "evidence_list")
    schema = contract["output_schema"]
    observation_schema = _resolve_local_ref(
        schema,
        schema["properties"]["observations"]["items"],
    )
    observation_properties = observation_schema.get("properties", {})
    expected_observation_fields = {
        "id",
        "source_type",
        "source_id",
        "plan_id",
        "task_id",
        "outcome",
        "artifact_refs",
        "occurred_at",
    }
    artifact_schema = _resolve_local_ref(
        schema,
        observation_properties.get("artifact_refs", {}).get("items", {}),
    )

    assert (
        observation_schema.get("type") == "object"
        and expected_observation_fields <= set(observation_properties)
        and artifact_schema.get("type") == "object"
        and {"artifact_id", "kind", "uri", "content_hash"}
        <= set(artifact_schema.get("properties", {}))
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="H4-SCHEMA-002: evidence_list silently accepts undeclared input/output fields",
)
def test_evidence_list_contract_forbids_extra_fields():
    input_rejected = False
    output_rejected = False
    try:
        EvidenceListArgs.model_validate({"limit": 1, "unexpected": True})
    except ValidationError:
        input_rejected = True
    try:
        TOOL_OUTPUT_MODELS["evidence_list"].model_validate(
            {"observations": [], "unexpected": True}
        )
    except ValidationError:
        output_rejected = True

    contract = next(item for item in tool_contracts() if item["name"] == "evidence_list")
    input_schema = contract["input_schema"]
    output_schema = contract["output_schema"]
    observation_schema = _resolve_local_ref(
        output_schema,
        output_schema["properties"]["observations"]["items"],
    )
    artifact_schema = _resolve_local_ref(
        output_schema,
        observation_schema.get("properties", {}).get("artifact_refs", {}).get("items", {}),
    )

    assert (
        input_rejected
        and output_rejected
        and input_schema.get("additionalProperties") is False
        and output_schema.get("additionalProperties") is False
        and observation_schema.get("additionalProperties") is False
        and artifact_schema.get("additionalProperties") is False
    )
