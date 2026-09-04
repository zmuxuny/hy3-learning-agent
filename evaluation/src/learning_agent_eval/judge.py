"""Retired E3 Judge API; historical artifacts are validation-only."""

from __future__ import annotations

from .legacy import legacy_execution_disabled


def evaluate_judges(*args: object, **kwargs: object) -> None:
    """Reject the retired Judge before interpreting inputs or selecting a Provider."""

    legacy_execution_disabled("learning_agent_eval.judge.evaluate_judges")


__all__ = ["evaluate_judges"]
