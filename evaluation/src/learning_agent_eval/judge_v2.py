"""Retired Judge entrypoints; historical artifacts are validation-only."""

from __future__ import annotations

from .active_judge import JudgeEvaluationV2Error, JudgeEvaluationV2Summary
from .legacy import legacy_execution_disabled


def evaluate_judges_v2(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled("learning_agent_eval.judge_v2.evaluate_judges_v2")


def evaluate_judges_v3(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled("learning_agent_eval.judge_v2.evaluate_judges_v3")


__all__ = [
    "JudgeEvaluationV2Error",
    "JudgeEvaluationV2Summary",
    "evaluate_judges_v2",
    "evaluate_judges_v3",
]
