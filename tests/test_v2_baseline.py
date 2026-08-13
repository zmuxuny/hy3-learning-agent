from app.services.evidence_baseline import SCENARIOS, evaluate_baseline


def test_m13_baseline_has_forty_fixed_scenarios_and_all_pass():
    report = evaluate_baseline()
    assert len(SCENARIOS) >= 40
    assert report["scenario_count"] == len(SCENARIOS)
    assert report["passed"] == len(SCENARIOS)
    assert report["ok"] is True


def test_m13_baseline_is_reproducible():
    first = evaluate_baseline()
    second = evaluate_baseline()
    assert [item["digest"] for item in first["results"]] == [item["digest"] for item in second["results"]]
