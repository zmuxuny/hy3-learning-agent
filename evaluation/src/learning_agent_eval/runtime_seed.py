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


class SeedNotification(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    minutes_ago: Annotated[int, Field(ge=0)]
    title: str
    body: str


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
    task_description: str
    task_estimated_minutes: Annotated[int, Field(ge=1)]
    task_status: Literal["pending", "in_progress", "completed"]
    submission_content: str
    submission_artifacts: list[dict[str, str]]
    submission_status: Literal["submitted", "accepted", "revision_required"]
    planning_readiness: Literal["ready", "collecting"]
    planning_open_questions: list[str]
    planning_confirmed_facts: list[dict[str, str]]
    quiet_start: str
    quiet_end: str
    notification_cooldown_minutes: Annotated[int, Field(ge=0, le=1440)]
    daily_notification_limit: Annotated[int, Field(ge=0)]
    additional_stages: list[SeedStage]
    prior_notifications: list[SeedNotification]
    evaluation_injected_failure: Literal["provider_error"]
