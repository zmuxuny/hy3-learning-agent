"""Private regression seams for frozen pre-Release-1.0 execution fixtures.

These helpers are intentionally test-owned.  Production modules expose only
read-only validation for the corresponding historical contracts.
"""

from learning_agent_eval._historical_aggregate import _aggregate_results_historical_v1
from learning_agent_eval._historical_judge import _evaluate_judges_historical_v1
from learning_agent_eval._historical_rule_runner import (
    _evaluate_run_rules_historical_v1,
)
from learning_agent_eval._historical_runner import _run_agent_historical_v2
from learning_agent_eval.active_aggregate import _aggregate_results_historical_v2
from learning_agent_eval.active_judge import _evaluate_judges_historical_v2
from learning_agent_eval.active_rules import _evaluate_run_rules_historical_v2
from learning_agent_eval.active_runtime import _run_agent_historical_v3

aggregate_results_v1 = _aggregate_results_historical_v1
aggregate_results_v2 = _aggregate_results_historical_v2
evaluate_judges_v1 = _evaluate_judges_historical_v1
evaluate_judges_v2 = _evaluate_judges_historical_v2
evaluate_run_rules_v1 = _evaluate_run_rules_historical_v1
evaluate_run_rules_v2 = _evaluate_run_rules_historical_v2
run_agent_v2 = _run_agent_historical_v2
run_agent_v3 = _run_agent_historical_v3

__all__ = [
    "aggregate_results_v1",
    "aggregate_results_v2",
    "evaluate_judges_v1",
    "evaluate_judges_v2",
    "evaluate_run_rules_v1",
    "evaluate_run_rules_v2",
    "run_agent_v2",
    "run_agent_v3",
]
