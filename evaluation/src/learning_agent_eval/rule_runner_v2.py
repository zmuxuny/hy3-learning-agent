"""Retired Rules entrypoints; historical artifacts are validation-only."""

from __future__ import annotations

from .active_rules import RuleEvaluationV2Error, RuleEvaluationV2Summary
from .legacy import legacy_execution_disabled


def evaluate_run_rules_v2(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled(
        "learning_agent_eval.rule_runner_v2.evaluate_run_rules_v2"
    )


def evaluate_run_rules_v3(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled(
        "learning_agent_eval.rule_runner_v2.evaluate_run_rules_v3"
    )


__all__ = [
    "RuleEvaluationV2Error",
    "RuleEvaluationV2Summary",
    "evaluate_run_rules_v2",
    "evaluate_run_rules_v3",
]
