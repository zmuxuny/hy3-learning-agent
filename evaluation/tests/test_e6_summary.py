"""Reporting must not improve a rate by dropping failures or zero scores."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/summarize_e6_run.py"
SPEC = importlib.util.spec_from_file_location("e6_summary", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def row(identity, score=None, status="runtime_failure", outcome="runtime_failure"):
    return {
        "case_id": identity, "track": "assessment", "status": status,
        "raw_score": score, "rule_capped_score": score, "reviewed_score": score,
        "reviewed_outcome": outcome, "dimensions": {"D1": 0} if score is not None else {},
        "confirmed_critical": [], "pending_candidates": [],
    }


def test_zero_is_scored_and_failures_remain_in_denominator():
    rows = [row("a", 0, "complete", "fail"), row("b", 100, "complete", "pass"), row("c")]
    result = REPORT.summarize_rows(rows, {"assessment": 3})["assessment"]
    assert result["score_count"] == 2
    assert result["reviewed_score_mean"] == 50
    assert result["pass_rate_all_cases"] == 1 / 3
    assert result["pass_rate_scored_cases"] == 1 / 2


def test_unscored_batch_is_null_not_zero():
    result = REPORT.summarize_rows([row("a"), row("b", status="not_attempted", outcome="not_attempted")], {"assessment": 2})["assessment"]
    assert result["reviewed_score_mean"] is None
    assert result["score_count"] == 0
    assert result["pass_rate_scored_cases"] is None
    assert result["pass_rate_all_cases"] == 0


def test_dropped_or_duplicate_case_is_rejected():
    with pytest.raises(ValueError, match="fixed_denominator_mismatch"):
        REPORT.summarize_rows([row("a")], {"assessment": 2})
    with pytest.raises(ValueError, match="duplicate_case"):
        REPORT.summarize_rows([row("a"), row("a")], {"assessment": 2})


@pytest.mark.parametrize("bad", [row("a", status="complete"), row("a", 0)])
def test_status_and_score_cannot_disagree(bad):
    with pytest.raises(ValueError):
        REPORT.summarize_rows([bad], {"assessment": 1})
