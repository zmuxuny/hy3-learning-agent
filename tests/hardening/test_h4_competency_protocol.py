"""Durable H4 Competency/Evidence protocol regressions.

All database access uses conftest's disposable SQLite copy.  These tests are
kept separate from the H0 failure baselines so the latter can be removed one
defect at a time without losing the broader protocol matrix.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.operations import undo_operation
from app.db.database import AsyncSessionLocal
from app.models import (
    AgentRun,
    Competency,
    CompetencyEdge,
    CompetencyGraphMutation,
    LearningResource,
    OperationDependency,
    Owner,
    Plan,
    ResourceCompetencyLink,
    Stage,
    Task,
    TaskCompetencyLink,
)
from app.schemas.evidence import (
    EvidenceArtifactRef,
    EvidenceCheck,
    EvidenceClaim,
    EvidenceCompetencyRef,
    EvidenceEvaluatorEnvelope,
    EvidenceListInput,
    EvidenceListOutput,
    EvidenceObservationOutput,
    EvidencePayloadEnvelope,
    EvidenceRubricEnvelope,
)
from app.services.competencies import (
    add_edge,
    create_competency,
    link_competency,
    list_merge_candidates,
)
from app.services.competency_protocol import (
    active_operation_dependents,
    competency_node_dependencies,
)
from app.tools import ToolContext, execute_tool
from app.tools.competencies import CompetencyCreateArgs
from app.tools.contracts import TOOL_OUTPUT_MODELS


OWNER_ID = "local"


@pytest_asyncio.fixture
async def isolated_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as db:
        yield db


async def _plan_with_task(db: AsyncSession, title: str) -> tuple[Plan, Task]:
    task = Task(title=f"{title} task", position=0)
    plan = Plan(
        owner_id=OWNER_ID,
        title=title,
        stages=[Stage(title=f"{title} stage", position=0, tasks=[task])],
    )
    db.add(plan)
    await db.flush()
    return plan, task


async def _competency(
    db: AsyncSession,
    *,
    key: str,
    plan_id: int | None,
) -> Competency:
    competency, created = await create_competency(
        db,
        OWNER_ID,
        key=key,
        title=key,
        scope="plan" if plan_id is not None else "global",
        plan_id=plan_id,
    )
    assert created
    return competency


async def _run_context(db: AsyncSession, plan_id: int | None) -> ToolContext:
    run = AgentRun(
        owner_id=OWNER_ID,
        plan_id=plan_id,
        trigger="user_message",
        objective="H4 competency protocol",
    )
    db.add(run)
    await db.commit()
    return ToolContext(
        db=db,
        owner_id=OWNER_ID,
        run_id=run.id,
        trigger="user_message",
        plan_id=plan_id,
    )


async def _tool_ok(
    ctx: ToolContext,
    name: str,
    payload: dict[str, Any],
    *,
    call_id: str,
) -> dict[str, Any]:
    ctx.tool_call_id = call_id
    result = await execute_tool(name, json.dumps(payload), ctx)
    assert result.get("ok") is True, result
    return result["data"]


@pytest.mark.asyncio
async def test_edge_scope_uses_both_database_endpoints_and_allows_global_plus_private(
    isolated_db: AsyncSession,
):
    plan_a, _ = await _plan_with_task(isolated_db, "A")
    plan_b, _ = await _plan_with_task(isolated_db, "B")
    global_node = await _competency(isolated_db, key="global-node", plan_id=None)
    plan_a_node = await _competency(isolated_db, key="plan-a-node", plan_id=plan_a.id)
    plan_b_node = await _competency(isolated_db, key="plan-b-node", plan_id=plan_b.id)
    ids = (global_node.id, plan_a_node.id, plan_b_node.id, plan_a.id)

    allowed, created = await add_edge(
        isolated_db,
        OWNER_ID,
        source_id=ids[0],
        target_id=ids[1],
        relation="related_to",
        focused_plan_id=ids[3],
    )
    assert created and allowed.source_id == ids[0]

    with pytest.raises(ValueError, match="different plans"):
        await add_edge(
            isolated_db,
            OWNER_ID,
            source_id=ids[1],
            target_id=ids[2],
            relation="related_to",
            focused_plan_id=ids[3],
        )
    assert await isolated_db.scalar(select(func.count()).select_from(CompetencyEdge)) == 1


@pytest.mark.asyncio
async def test_competency_key_identity_is_partitioned_by_scope_and_plan(
    isolated_db: AsyncSession,
):
    plan_a, _ = await _plan_with_task(isolated_db, "Key A")
    plan_b, _ = await _plan_with_task(isolated_db, "Key B")

    global_node = await _competency(isolated_db, key="shared-key", plan_id=None)
    plan_a_node = await _competency(isolated_db, key="shared-key", plan_id=plan_a.id)
    plan_b_node = await _competency(isolated_db, key="shared-key", plan_id=plan_b.id)
    replay, created = await create_competency(
        isolated_db,
        OWNER_ID,
        key="shared-key",
        title="shared-key",
        scope="plan",
        plan_id=plan_a.id,
    )

    assert len({global_node.id, plan_a_node.id, plan_b_node.id}) == 3
    assert created is False and replay.id == plan_a_node.id


@pytest.mark.asyncio
async def test_merge_candidates_are_read_only_owner_isolated_active_private_proposals(
    isolated_db: AsyncSession,
):
    plan_a, _ = await _plan_with_task(isolated_db, "Candidate A")
    plan_b, _ = await _plan_with_task(isolated_db, "Candidate B")
    plan_archived_node, _ = await _plan_with_task(isolated_db, "Archived node")
    archived_plan, _ = await _plan_with_task(isolated_db, "Archived plan")
    first = await _competency(isolated_db, key="candidate-key", plan_id=plan_a.id)
    second = await _competency(isolated_db, key="candidate-key", plan_id=plan_b.id)
    await _competency(isolated_db, key="candidate-key", plan_id=None)
    archived_node = await _competency(
        isolated_db,
        key="candidate-key",
        plan_id=plan_archived_node.id,
    )
    await _competency(isolated_db, key="candidate-key", plan_id=archived_plan.id)
    archived_node.status = "archived"
    archived_plan.status = "archived"

    isolated_db.add(Owner(id="foreign", display_name="Foreign"))
    await isolated_db.flush()
    foreign_a = Plan(owner_id="foreign", title="Foreign A")
    foreign_b = Plan(owner_id="foreign", title="Foreign B")
    isolated_db.add_all((foreign_a, foreign_b))
    await isolated_db.flush()
    for plan in (foreign_a, foreign_b):
        _, created = await create_competency(
            isolated_db,
            "foreign",
            key="candidate-key",
            title="Foreign candidate",
            scope="plan",
            plan_id=plan.id,
            focused_plan_id=plan.id,
        )
        assert created
    expected_ids = sorted((first.id, second.id))
    expected_plan_ids = sorted((plan_a.id, plan_b.id))
    await isolated_db.commit()

    candidates = await list_merge_candidates(isolated_db, OWNER_ID)
    mutation_count = await isolated_db.scalar(
        select(func.count()).select_from(CompetencyGraphMutation)
    )

    assert candidates == [
        {
            "key": "candidate-key",
            "competency_ids": expected_ids,
            "plan_ids": expected_plan_ids,
            "status": "proposed",
        }
    ]
    assert mutation_count == 0


@pytest.mark.asyncio
async def test_task_and_resource_links_use_real_target_and_competency_plans(
    isolated_db: AsyncSession,
):
    plan_a, _ = await _plan_with_task(isolated_db, "A")
    plan_b, task_b = await _plan_with_task(isolated_db, "B")
    private_b = await _competency(isolated_db, key="private-b", plan_id=plan_b.id)
    global_resource = LearningResource(owner_id=OWNER_ID, title="Global", plan_id=None)
    isolated_db.add(global_resource)
    await isolated_db.flush()
    plan_a_id = plan_a.id
    task_b_id = task_b.id
    private_b_id = private_b.id
    resource_id = global_resource.id

    with pytest.raises(ValueError, match="Plan-focused runs"):
        await link_competency(
            isolated_db,
            OWNER_ID,
            competency_id=private_b_id,
            task_id=task_b_id,
            relation="assesses",
            focused_plan_id=plan_a_id,
        )
    with pytest.raises(ValueError, match="Plan-focused runs"):
        await link_competency(
            isolated_db,
            OWNER_ID,
            competency_id=private_b_id,
            resource_id=resource_id,
            relation="covers",
            focused_plan_id=plan_a_id,
        )

    assert await isolated_db.scalar(select(func.count()).select_from(TaskCompetencyLink)) == 0
    assert await isolated_db.scalar(select(func.count()).select_from(ResourceCompetencyLink)) == 0


@pytest.mark.asyncio
async def test_unfocused_id_only_mutations_cannot_enter_a_private_graph(
    isolated_db: AsyncSession,
):
    plan, task = await _plan_with_task(isolated_db, "Private")
    private_node = await _competency(isolated_db, key="private-node", plan_id=plan.id)
    global_node = await _competency(isolated_db, key="global-peer", plan_id=None)

    with pytest.raises(ValueError, match="focused or explicit plan"):
        await add_edge(
            isolated_db,
            OWNER_ID,
            source_id=private_node.id,
            target_id=global_node.id,
            relation="related_to",
        )
    with pytest.raises(ValueError, match="focused or explicit plan"):
        await link_competency(
            isolated_db,
            OWNER_ID,
            competency_id=global_node.id,
            task_id=task.id,
            relation="teaches",
        )


@pytest.mark.asyncio
async def test_archived_plan_and_competency_are_rejected_for_graph_mutation(
    isolated_db: AsyncSession,
):
    plan, _ = await _plan_with_task(isolated_db, "Archived")
    node = await _competency(isolated_db, key="archived-node", plan_id=plan.id)
    global_node = await _competency(isolated_db, key="active-global", plan_id=None)
    node.status = "archived"
    await isolated_db.flush()

    with pytest.raises(ValueError, match="Archived competencies"):
        await add_edge(
            isolated_db,
            OWNER_ID,
            source_id=node.id,
            target_id=global_node.id,
            relation="related_to",
            focused_plan_id=plan.id,
        )

    node.status = "active"
    plan.status = "archived"
    await isolated_db.flush()
    with pytest.raises(ValueError, match="Restore the plan"):
        await add_edge(
            isolated_db,
            OWNER_ID,
            source_id=node.id,
            target_id=global_node.id,
            relation="related_to",
            focused_plan_id=plan.id,
        )


@pytest.mark.asyncio
async def test_real_mutations_advance_revision_once_but_replay_and_noop_do_not(
    isolated_db: AsyncSession,
):
    plan, _ = await _plan_with_task(isolated_db, "Revision")
    plan_id = plan.id
    ctx = await _run_context(isolated_db, plan_id)

    before = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-graph-before",
    )
    payload = {
        "key": "revision-node",
        "title": "Revision node",
        "scope": "plan",
        "plan_id": plan_id,
    }
    created = await _tool_ok(
        ctx,
        "competency_create",
        payload,
        call_id="h4-create-once",
    )
    replay = await _tool_ok(
        ctx,
        "competency_create",
        payload,
        call_id="h4-create-once",
    )
    noop = await _tool_ok(
        ctx,
        "competency_create",
        payload,
        call_id="h4-create-noop-new-invocation",
    )
    after = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-graph-after",
    )

    assert before["revision"] == 0
    assert created["created"] is True
    assert replay == created
    assert noop["created"] is False
    assert after["revision"] == 1
    mutations = list(
        (
            await isolated_db.execute(
                select(CompetencyGraphMutation).where(
                    CompetencyGraphMutation.owner_id == OWNER_ID
                )
            )
        ).scalars()
    )
    assert [(item.revision, item.action) for item in mutations] == [(1, "apply")]


@pytest.mark.asyncio
async def test_edge_operation_records_both_node_dependencies_and_revision(
    isolated_db: AsyncSession,
):
    plan, _ = await _plan_with_task(isolated_db, "Dependencies")
    plan_id = plan.id
    ctx = await _run_context(isolated_db, plan_id)
    source = await _tool_ok(
        ctx,
        "competency_create",
        {"key": "dep-source", "title": "Source", "scope": "plan", "plan_id": plan_id},
        call_id="h4-dep-source",
    )
    target = await _tool_ok(
        ctx,
        "competency_create",
        {"key": "dep-target", "title": "Target", "scope": "plan", "plan_id": plan_id},
        call_id="h4-dep-target",
    )
    edge = await _tool_ok(
        ctx,
        "competency_edge",
        {
            "source_id": source["competency_id"],
            "target_id": target["competency_id"],
            "relation": "related_to",
        },
        call_id="h4-dep-edge",
    )

    dependencies = list(
        (
            await isolated_db.execute(
                select(OperationDependency).where(
                    OperationDependency.operation_id == edge["operation_id"]
                )
            )
        ).scalars()
    )
    active = await active_operation_dependents(
        isolated_db,
        operation_id=source["operation_id"],
    )
    structural = await competency_node_dependencies(
        isolated_db,
        owner_id=OWNER_ID,
        competency_id=source["competency_id"],
    )
    graph = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-dep-graph",
    )

    assert {item.depends_on_operation_id for item in dependencies} == {
        source["operation_id"],
        target["operation_id"],
    }
    assert [item.operation_id for item in active] == [edge["operation_id"]]
    assert ("competency_edge", str(edge["edge_id"])) in {
        (item.entity_type, item.entity_id) for item in structural
    }
    assert graph["revision"] == 3

    await undo_operation(edge["operation_id"], isolated_db)
    active_after = await active_operation_dependents(
        isolated_db,
        operation_id=source["operation_id"],
    )
    structural_after = await competency_node_dependencies(
        isolated_db,
        owner_id=OWNER_ID,
        competency_id=source["competency_id"],
    )
    graph_after = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-dep-graph-after-undo",
    )
    assert active_after == []
    assert structural_after == []
    assert graph_after["revision"] == 4


@pytest.mark.asyncio
async def test_each_link_kind_advances_revision_and_global_resource_is_visible(
    isolated_db: AsyncSession,
):
    plan, task = await _plan_with_task(isolated_db, "Links")
    resource = LearningResource(owner_id=OWNER_ID, title="Global resource", plan_id=None)
    isolated_db.add(resource)
    await isolated_db.flush()
    plan_id, task_id, resource_id = plan.id, task.id, resource.id
    ctx = await _run_context(isolated_db, plan_id)
    competency = await _tool_ok(
        ctx,
        "competency_create",
        {"key": "linked", "title": "Linked", "scope": "plan", "plan_id": plan_id},
        call_id="h4-links-node",
    )
    link_payloads = (
        {
            "competency_id": competency["competency_id"],
            "plan_id": plan_id,
            "relation": "targets",
        },
        {
            "competency_id": competency["competency_id"],
            "task_id": task_id,
            "relation": "teaches",
        },
        {
            "competency_id": competency["competency_id"],
            "resource_id": resource_id,
            "relation": "covers",
        },
    )
    links: list[dict[str, Any]] = []
    for index, payload in enumerate(link_payloads):
        result = await _tool_ok(
            ctx,
            "competency_link",
            payload,
            call_id=f"h4-link-{index}",
        )
        assert result["created"] is True
        links.append(result)

    noop = await _tool_ok(
        ctx,
        "competency_link",
        link_payloads[0],
        call_id="h4-link-plan-noop",
    )
    graph = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-links-graph",
    )

    assert noop["created"] is False
    assert graph["revision"] == 4
    assert [item["resource_id"] for item in graph["resource_links"]] == [resource_id]

    for link in reversed(links):
        await undo_operation(link["operation_id"], isolated_db)
    after_undo = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-links-after-undo",
    )
    assert after_undo["revision"] == 7
    assert after_undo["plan_links"] == []
    assert after_undo["task_links"] == []
    assert after_undo["resource_links"] == []


@pytest.mark.asyncio
async def test_successful_graph_undo_advances_once_and_replay_does_not(
    isolated_db: AsyncSession,
):
    plan, _ = await _plan_with_task(isolated_db, "Undo revision")
    plan_id = plan.id
    ctx = await _run_context(isolated_db, plan_id)
    created = await _tool_ok(
        ctx,
        "competency_create",
        {"key": "undo-node", "title": "Undo node", "scope": "plan", "plan_id": plan_id},
        call_id="h4-undo-node",
    )

    first = await undo_operation(created["operation_id"], isolated_db)
    second = await undo_operation(created["operation_id"], isolated_db)
    graph = await _tool_ok(
        ctx,
        "competency_graph_get",
        {"plan_id": plan_id},
        call_id="h4-undo-graph",
    )
    mutations = list(
        (
            await isolated_db.execute(
                select(CompetencyGraphMutation)
                .where(CompetencyGraphMutation.owner_id == OWNER_ID)
                .order_by(CompetencyGraphMutation.revision)
            )
        ).scalars()
    )

    assert first.status == second.status == "undone"
    assert await isolated_db.get(Competency, created["competency_id"]) is None
    assert graph["revision"] == 2
    assert [(item.revision, item.action) for item in mutations] == [
        (1, "apply"),
        (2, "undo"),
    ]

@pytest.mark.parametrize(
    ("model", "value"),
    [
        (EvidenceListInput, {"limit": 1, "unexpected": True}),
        (EvidenceListOutput, {"observations": [], "unexpected": True}),
        (
            EvidenceObservationOutput,
            {
                "id": 1,
                "source_type": "quiz",
                "source_id": "1",
                "run_id": None,
                "session_id": None,
                "plan_id": None,
                "task_id": None,
                "fact_kind": "observation",
                "target_observation_id": None,
                "reason_code": "",
                "evidence_role": "primary",
                "eligibility_stage": "demonstrated",
                "eligibility_reason": "VERIFIED_PRIMARY_SUCCESS",
                "eligibility_policy_version": "evidence-eligibility-v1",
                "counts_as_success": True,
                "outcome": "passed",
                "normalized_score": 1.0,
                "is_correct": True,
                "assistance_level": "none",
                "transfer_level": "near",
                "rubric_snapshot": {},
                "evaluator": {},
                "artifact_refs": [],
                "payload": {},
                "occurred_at": "2026-01-01T00:00:00Z",
                "recorded_at": "2026-01-01T00:00:00Z",
                "schema_version": 1,
                "correlation_id": None,
                "causation_id": None,
                "idempotency_key": "one",
                "unexpected": True,
            },
        ),
        (
            EvidenceArtifactRef,
            {
                "artifact_id": 1,
                "kind": "answer",
                "uri": "artifact:1",
                "content_hash": "a" * 64,
                "ordinal": 0,
                "unexpected": True,
            },
        ),
        (
            EvidenceCompetencyRef,
            {
                "competency_id": 1,
                "competency_key": "python.async",
                "association_kind": "task_assesses",
                "task_competency_link_id_snapshot": 2,
                "unexpected": True,
            },
        ),
        (EvidenceRubricEnvelope, {"pass_threshold": 70, "unexpected": True}),
        (EvidenceCheck, {"key": "correctness", "unexpected": True}),
        (EvidenceEvaluatorEnvelope, {"type": "agent", "unexpected": True}),
        (EvidencePayloadEnvelope, {"feedback": "ok", "unexpected": True}),
        (EvidenceClaim, {"kind": "answer", "unexpected": True}),
    ],
)
def test_evidence_contract_forbids_extra_fields_at_every_named_level(model, value):
    with pytest.raises(ValidationError):
        model.model_validate(value)

    assert model.model_json_schema().get("additionalProperties") is False


def test_competency_tool_contracts_are_strict_and_graph_items_are_typed():
    for name in (
        "competency_create",
        "competency_link",
        "competency_edge",
        "competency_graph_get",
        "competency_get",
        "evidence_list",
    ):
        schema = TOOL_OUTPUT_MODELS[name].model_json_schema()
        assert schema.get("additionalProperties") is False

    with pytest.raises(ValidationError):
        CompetencyCreateArgs.model_validate(
            {"key": "strict", "title": "Strict", "unexpected": True}
        )

    graph_schema = TOOL_OUTPUT_MODELS["competency_graph_get"].model_json_schema()
    for field in ("competencies", "edges", "plan_links", "task_links", "resource_links"):
        assert "$ref" in graph_schema["properties"][field]["items"]


def test_evidence_schema_forbids_extras_recursively_except_controlled_data_envelopes():
    schema = EvidenceListOutput.model_json_schema()
    for name, definition in schema.get("$defs", {}).items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name

    for name in (
        "EvidenceCheck",
        "EvidenceClaim",
        "EvidenceEvaluatorEnvelope",
        "EvidencePayloadEnvelope",
        "EvidenceRubricEnvelope",
    ):
        data_schema = schema["$defs"][name]["properties"]["data"]
        assert data_schema.get("type") == "object"
