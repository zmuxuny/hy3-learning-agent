"""Score zero, missingness, and paired denominators must survive comparison."""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from summarize_e7 import summarize_pairs


def row(case, arm, score):
    return dict(
        case_id=case,
        track="planning",
        arm=arm,
        score=score,
        status="complete" if score is not None else "judge_error",
        outcome="pass"
        if score is not None and score >= 70
        else "fail"
        if score is not None
        else "judge_error",
        dimensions={f"D{i}": 1 for i in range(1, 8)} if score is not None else {},
    )


def test_fixed_denominator_keeps_zero_and_unscored_pairs():
    rows = [
        row("zero", "baseline", 0),
        row("zero", "candidate", 20),
        row("missing", "baseline", None),
        row("missing", "candidate", 100),
        row("loss", "baseline", 90),
        row("loss", "candidate", 70),
        row("tie", "baseline", 80),
        row("tie", "candidate", 80),
    ]
    result = summarize_pairs(rows)
    assert (result["expected_pairs"], result["valid_pairs"]) == (4, 3)
    assert result["changes"] == dict(improved=1, unscored_pair=1, regressed=1, tied=1)
    assert result["tracks"]["planning"]["paired_score_delta_mean"] == 0
    assert result["tracks"]["planning"]["arms"]["baseline"]["pass_rate_all"] == 0.5


def test_duplicate_or_missing_arm_is_not_a_pair():
    with pytest.raises(ValueError):
        summarize_pairs([row("x", "baseline", 100), row("x", "baseline", 100)])
