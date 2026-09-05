"""Executable public seed fields for active Cases; unknown fields fail closed."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field
from typing_extensions import TypedDict


class SeedTask(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    title: str
    position: int


class SeedStage(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    title: str
    position: int
    tasks: list[SeedTask]


class RuntimeSeed(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    plan_title: str
    plan_version: Annotated[int, Field(ge=1)]
    weekly_minutes: Annotated[int, Field(ge=1)]
    goal: str
    current_level: str
    expected_outcome: str
    plan_description: str
    task_title: str
    submission_content: str
    submission_status: Literal["submitted", "accepted", "revision_required"]
    planning_readiness: Literal["ready", "collecting"]
    quiet_start: str
    quiet_end: str
    notification_cooldown_minutes: Annotated[int, Field(ge=0)]
    daily_notification_limit: Annotated[int, Field(ge=0)]
    additional_stages: list[SeedStage]
    evaluation_injected_failure: Literal["provider_error"]
