"""Retired E3 Aggregate API; historical artifacts are validation-only."""

from __future__ import annotations

from .legacy import legacy_execution_disabled


def aggregate_results(*args: object, **kwargs: object) -> None:
    """Reject the retired Aggregate before interpreting inputs or creating output."""

    legacy_execution_disabled("learning_agent_eval.aggregate.aggregate_results")


__all__ = ["aggregate_results"]
