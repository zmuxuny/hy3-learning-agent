"""Retired E1/E2 Runtime API; historical artifacts are validation-only."""

from __future__ import annotations

from ._historical_runner import RunAgentError, RunAgentSummary
from .legacy import legacy_execution_disabled


def run_agent(*args: object, **kwargs: object) -> None:
    """Reject the retired Runtime before interpreting caller arguments."""

    legacy_execution_disabled("learning_agent_eval.runner.run_agent")


__all__ = ["RunAgentError", "RunAgentSummary", "run_agent"]
