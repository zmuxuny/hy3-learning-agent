"""Retired E2 Rules API; historical artifacts are validation-only."""

from __future__ import annotations

from ._historical_rule_runner import RuleEvaluationError, RuleEvaluationSummary
from .legacy import legacy_execution_disabled


def evaluate_run_rules(*args: object, **kwargs: object) -> None:
    """Reject the retired Rules executor before interpreting caller arguments."""

    legacy_execution_disabled("learning_agent_eval.rule_runner.evaluate_run_rules")


__all__ = ["RuleEvaluationError", "RuleEvaluationSummary", "evaluate_run_rules"]
