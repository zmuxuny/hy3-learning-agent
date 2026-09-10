"""Regression checks for ID-aligned, independent human annotation comparison."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('human_summary', ROOT/'evaluation/scripts/summarize_human_confirmation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def labels():
    folder = ROOT/'交付材料/人工标注'
    return module.read_csv(folder/'application_czy.csv'), module.read_csv(folder/'application_zyq.csv')


def test_independent_files_reproduce_observed_disagreements():
    left, right = labels()
    summary, differences = module.compare(left, list(reversed(right)))
    assert summary['dimension_matches'] == 329
    assert summary['dimension_slots'] == 336
    assert summary['vector_matches'] == 41
    assert summary['outcome_matches'] == 48
    assert len(differences) == 7
    assert all(abs(r['left']-r['right']) == 1 for r in differences)
    assert summary['dimensions'][5]['quadratic_kappa'] == pytest.approx(0.4838709677419355)


@pytest.mark.parametrize('corruption', ['duplicate', 'missing', 'different_id'])
def test_does_not_silently_pair_incomplete_labels(corruption):
    left, right = labels()
    if corruption == 'duplicate':
        right[-1] = right[0]
    elif corruption == 'missing':
        right.pop()
    else:
        right[-1]['id'] = 'another-case'
    with pytest.raises(ValueError):
        module.compare(left, right)


def test_constant_label_kappa_remains_undefined():
    assert module.kappa([2, 2], [2, 2]) is None
    with pytest.raises(ValueError):
        module.kappa([2], [2, 1])


def test_annotation_totals_follow_fixed_weights_and_severity():
    left, right = labels()
    module.validate_annotation_scores(left)
    module.validate_annotation_scores(right)
    row = next(r for r in right if r['id'] == 'formal-i03-a')
    assert module.annotation_score(row) == (92.5, 92.5, 'pass')
    row['review_score'] = '100'
    with pytest.raises(ValueError, match='does not match dimensions'):
        module.validate_annotation_scores([row])


def test_corrected_major_case_retains_failed_outcome():
    _, right = labels()
    row = next(r for r in right if r['id'] == 'formal-i10-p')
    assert module.annotation_score(row) == (67.5, 67.5, 'fail')
