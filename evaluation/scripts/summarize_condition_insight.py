"""Verify fixed-evidence condition diagnostics and reproduce their comparison."""
import argparse
import json
from pathlib import Path

from learning_agent_eval import learning_quality_v10 as prompt_method
from learning_agent_eval import learning_quality_v11 as independent_method
from learning_agent_eval.canonical import sha256_digest
from summarize_final_method import write_csv, select_result
from summarize_quality_followup import verify_result

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'evaluation/artifacts/decisionbench-study-20260910/evidence/full/evidence'


def read(path):
    return json.loads(path.read_text())


def summarize(archive, output):
    review_rows = read(archive / 'review.json')['rows']
    reviews = {(r['part'], r['id'], r['repeat']): r for r in review_rows}
    assert len(reviews) == len(review_rows) == 24
    rows = []

    def append(part, identity, row, evidence):
        review = reviews[part, identity, row['repeat']]
        assert review['result_sha256'] == row['result_sha256']
        assert review['evidence_sha256'] == sha256_digest(evidence)
        assert review['checked_dimensions'] == [f'D{i}' for i in range(1, 8)]
        assert review['note'].strip()
        result = dict(part=part, id=identity, repeat=row['repeat'], status=row['status'],
                      score=row.get('score'), raw_score=row.get('raw_score'), outcome=row.get('outcome'),
                      severity=row.get('effective_severity'), result_sha256=row['result_sha256'],
                      condition_error_identified=review['condition_error_identified'],
                      note=review['note'])
        result.update(row.get('effective_dimensions', {}))
        rows.append(result)

    for part, method, commit, count in [
        ('prompt-check', prompt_method, '7636e12', 12),
        ('independent-check', independent_method, 'ed73ce1', 6),
    ]:
        found = 0
        for path in sorted((archive / part).glob('*/results.json')):
            document = read(path)
            assert document['source_commit'].startswith(commit)
            identity = path.parent.name
            evidence = read(EVIDENCE / (identity + '.json'))
            verify_result(document, {'id': identity}, evidence, method)
            assert [r['repeat'] for r in document['rows']] == [1, 2, 3]
            candidates = [path]
            amendment_path = archive / part / 'recovery-amendment.json'
            if amendment_path.exists():
                amendment = read(amendment_path)
                assert amendment['authorization'] == '作者：api不太稳定，你多试试'
                assert amendment['result_batches'] == ['recovery', 'recovery-2', 'recovery-3']
                candidates += [archive / part / batch / identity / 'results.json'
                               for batch in amendment['result_batches']]
            spec = {'id': identity, 'track': document['rows'][0]['track']}
            for row, initial, source in select_result(candidates, spec, evidence, 3, method):
                assert read(source)['source_commit'].startswith(commit)
                append(part, identity, row, evidence)
                rows[-1]['original_status'] = initial
                found += 1
        assert found == count

    design = read(archive / 'rating-probe/design.json')
    method = independent_method
    evidence = read(EVIDENCE / 'formal-i10-p.json')
    assert design['evidence_sha256'] == sha256_digest(evidence)
    assert design['method_sha256'] == method.METHOD_SHA256
    assert design['source_commit'].startswith('ed73ce1') and design['repeats'] == 3
    original = read(archive / 'independent-check/formal-i10-p/results.json')['rows'][0]
    assert design['fixed_audit'] == original['content_audit']
    assert design['control_challenge'] == original['condition_challenge']
    control = design['control_challenge']['checks']
    verified = design['verified_challenge']['checks']
    assert len(control) == len(verified)
    changed = [(a, b) for a, b in zip(control, verified) if a != b]
    assert len(changed) == 1 and changed[0][0]['unit_id'] == changed[0][1]['unit_id'] == 'unit-003'
    assert design['calculated_counterexample'] == [sum([1, 2][i] * [2][k-i]
                                                    for i in range(2) if 0 <= k-i < 1)
                                                for k in range(2)] == [2, 4]
    for variant in design['variants']:
        challenge = design['control_challenge' if variant == 'automatic_probe' else 'verified_challenge']
        method.validate_challenge(challenge, evidence)
        for repeat in range(1, 4):
            row = read(archive / 'rating-probe' / f'{variant}-{repeat}' / 'result.json')
            assert row['variant'] == variant and row['repeat'] == repeat
            assert row['result_sha256'] == sha256_digest({k: v for k, v in row.items() if k != 'result_sha256'})
            request = method.request_for(evidence, design['fixed_audit'], challenge)
            for number, attempt in enumerate(row['rating_attempts'], 1):
                assert attempt['number'] == number
                req = {**request, '_budget_call_id': f'insight-{variant}:repeat:{repeat}:rating:{number}'}
                assert attempt['request_sha256'] == sha256_digest(req)
                if attempt['status'] == 'complete':
                    assert number == len(row['rating_attempts'])
                    assert method.validate_rating(json.loads(attempt['reply']['content']), evidence) == row['rating'] == attempt['payload']
            if row['status'] == 'complete':
                for k, v in method.aggregate(row['rating'], evidence).items():
                    assert row[k] == v
                assert row['effective_dimensions'] == method.effective_levels(row['rating'], evidence)
            append('rating-probe', variant, row, evidence)
            rows[-1]['original_status'] = row['status']
    summary = {'reviewed_slots': len(rows), 'valid': sum(r['status'] == 'complete' for r in rows),
               'no_score': sum(r['status'] != 'complete' for r in rows),
               'original_errors': sum(r['original_status'] != 'complete' for r in rows),
               'diagnostic': {v: {'scores': [r['score'] for r in rows if r['id'] == v],
                                  'identified': sum(r['id'] == v and r['condition_error_identified'] for r in rows),
                                  'failed': sum(r['id'] == v and r['outcome'] == 'fail' for r in rows)}
                              for v in design['variants']}}
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / 'cases.csv', rows)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(summarize(args.archive, args.output), ensure_ascii=False, indent=2))
