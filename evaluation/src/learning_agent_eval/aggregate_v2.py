"""Retired Aggregate entrypoints; historical artifacts are validation-only."""

from __future__ import annotations

from .active_aggregate import AggregateEvaluationV2Error, AggregateEvaluationV2Summary
from .legacy import legacy_execution_disabled


def aggregate_results_v2(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled("learning_agent_eval.aggregate_v2.aggregate_results_v2")


def aggregate_results_v3(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled("learning_agent_eval.aggregate_v2.aggregate_results_v3")


__all__ = [
    "AggregateEvaluationV2Error",
    "AggregateEvaluationV2Summary",
    "aggregate_results_v2",
    "aggregate_results_v3",
]
