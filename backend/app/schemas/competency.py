"""Strict public schemas for the competency graph read boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictCompetencyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompetencyNodeOutput(StrictCompetencyOutput):
    id: int = Field(gt=0)
    key: str
    title: str
    description: str
    type: Literal["concept", "skill", "workflow", "project", "habit"]
    scope: Literal["global", "plan"]
    plan_id: int | None
    status: Literal["active", "archived"]
    version: int = Field(ge=1)


class CompetencyCreateOutput(StrictCompetencyOutput):
    competency_id: int = Field(gt=0)
    key: str
    created: bool
    operation_id: str


class CompetencyLinkOutput(StrictCompetencyOutput):
    kind: Literal["plan", "task", "resource"]
    link_id: int = Field(gt=0)
    competency_id: int = Field(gt=0)
    created: bool
    operation_id: str


class CompetencyEdgeMutationOutput(StrictCompetencyOutput):
    edge_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    target_id: int = Field(gt=0)
    relation: Literal["prerequisite", "part_of", "related_to", "equivalent_to"]
    created: bool
    operation_id: str


class CompetencyEdgeOutput(StrictCompetencyOutput):
    id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    target_id: int = Field(gt=0)
    relation: Literal["prerequisite", "part_of", "related_to", "equivalent_to"]


class PlanCompetencyLinkOutput(StrictCompetencyOutput):
    id: int = Field(gt=0)
    plan_id: int = Field(gt=0)
    competency_id: int = Field(gt=0)
    relation: Literal["targets"]
    target_stage: Literal[
        "unknown", "exposed", "practicing", "demonstrated", "retained"
    ]


class TaskCompetencyLinkOutput(StrictCompetencyOutput):
    id: int = Field(gt=0)
    task_id: int = Field(gt=0)
    competency_id: int = Field(gt=0)
    relation: Literal["teaches", "assesses"]
    target_stage: Literal[
        "unknown", "exposed", "practicing", "demonstrated", "retained"
    ]


class ResourceCompetencyLinkOutput(StrictCompetencyOutput):
    id: int = Field(gt=0)
    resource_id: int = Field(gt=0)
    competency_id: int = Field(gt=0)
    relation: Literal["covers"]
    depth: str


class CompetencyGraphOutput(StrictCompetencyOutput):
    plan_id: int | None
    revision: int = Field(ge=0)
    competencies: list[CompetencyNodeOutput]
    edges: list[CompetencyEdgeOutput]
    plan_links: list[PlanCompetencyLinkOutput]
    task_links: list[TaskCompetencyLinkOutput]
    resource_links: list[ResourceCompetencyLinkOutput]
