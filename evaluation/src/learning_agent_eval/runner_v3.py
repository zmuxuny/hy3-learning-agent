"""Retired E3.1 Runtime entrypoints; historical artifacts are validation-only."""

from __future__ import annotations

from .active_runtime import RunAgentV3Error, RunAgentV3Summary
from .legacy import legacy_execution_disabled


def run_agent_v3(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled("learning_agent_eval.runner_v3.run_agent_v3")


def run_agent_v4(*args: object, **kwargs: object) -> None:
    legacy_execution_disabled("learning_agent_eval.runner_v3.run_agent_v4")


__all__ = ["RunAgentV3Error", "RunAgentV3Summary", "run_agent_v3", "run_agent_v4"]
