"""Compare independent annotations and the agreed reference with automatic scores."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIMS = tuple(f'D{i}' for i in range(1, 8))
WEIGHTS = dict(zip(DIMS, (15, 15, 20, 20, 15, 5, 10)))
CAPS = {'none': 100, 'minor': 100, 'major': 69, 'critical': 39}


def annotation_score(row):
    levels = {d: int(row[d]) for d in DIMS}
    if any(v not in (0, 1, 2) for v in levels.values()):
        raise ValueError('Annotation levels must be 0–2')
    raw = sum(WEIGHTS[d]*levels[d]/2 for d in DIMS)
    score = min(raw, CAPS[row['review_severity']])
    return raw, score, 'pass' if score >= 70 else 'fail'


def validate_annotation_scores(rows):
    for row in rows:
        expected = annotation_score(row)
        actual = (float(row['review_raw_score']), float(row['review_score']), row['review_outcome'])
        if actual != expected:
            raise ValueError(f'Annotation score does not match dimensions: {row["id"]}')


def keyed(rows):
    result = {r['id']: r for r in rows}
    if len(result) != len(rows) or not result:
        raise ValueError('Missing or duplicate case identifiers')
    return result


def compare(left, right, outcome_left='review_outcome', outcome_right='review_outcome'):
    a, b = keyed(left), keyed(right)
    if a.keys() != b.keys():
        raise ValueError('Annotation case identifiers do not match')
    ids = sorted(a)
    dimensions, differences = [], []
    for d in DIMS:
        x = [int(a[i][d]) for i in ids]
        y = [int(b[i][d]) for i in ids]
        dimensions.append(dict(dimension=d, n=len(ids), matches=sum(u == v for u, v in zip(x, y)),
                               agreement=sum(u == v for u, v in zip(x, y))/len(ids)))
        for i, u, v in zip(ids, x, y):
            if u != v:
                differences.append(dict(id=i, name=a[i]['name'], dimension=d, left=u, right=v))
    outcome_matches = sum(a[i][outcome_left] == b[i][outcome_right] for i in ids)
    return dict(cases=len(ids), dimensions=dimensions,
                dimension_slots=len(ids)*len(DIMS), dimension_matches=sum(r['matches'] for r in dimensions),
                vector_matches=sum(all(int(a[i][d]) == int(b[i][d]) for d in DIMS) for i in ids),
                outcome_matches=outcome_matches, outcome_agreement=outcome_matches/len(ids)), differences


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields=None):
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def summarize(archive, output, annotations=None):
    annotations = annotations or ROOT/'交付材料/人工标注'
    decision = json.loads((annotations/'adjudication.json').read_text())
    for name, digest in decision['sha256'].items():
        if hashlib.sha256((annotations/name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Annotation digest mismatch: {name}')
    left = read_csv(annotations/'application_czy.csv')
    right = read_csv(annotations/'application_zyq.csv')
    reference = read_csv(annotations/decision['reference_file'])
    for rows in (left, right, reference):
        validate_annotation_scores(rows)
    if reference != read_csv(annotations/decision['selected_file']):
        raise ValueError('Final reference differs from the agreed annotation')
    if len(reference) != 48:
        raise ValueError('Expected 48 application annotations')
    automatic = [r for r in read_csv(archive/'automatic/cases.csv') if r['part'] == 'application']
    human, differences = compare(left, right)
    aligned, _ = compare(automatic, reference, 'outcome')
    metrics = [dict(dimension=r['dimension'], n=r['n'], automatic_human_agreement=r['agreement'])
               for r in aligned['dimensions']]
    result = dict(confirmation_date=decision['date'], source='independent annotation files and author-confirmed adjudication',
                  annotation_files=decision['sha256'], reference_file='交付材料/人工标注/'+decision['reference_file'],
                  adjudication=decision['decision'], coverage_cases=len(reference),
                  human_human=human,
                  application=dict(cases=len(reference), dimensions=metrics,
                                   automatic_human_outcome_matches=aligned['outcome_matches'],
                                   automatic_human_outcome_agreement=aligned['outcome_agreement']))
    output.mkdir(parents=True, exist_ok=True)
    (output/'confirmation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    write_csv(output/'application-alignment.csv', metrics)
    write_csv(output/'human-human-alignment.csv', human['dimensions'])
    write_csv(output/'annotation-differences.csv', differences, ['id', 'name', 'dimension', 'left', 'right'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=ROOT/'evaluation/artifacts/decisionbench-final-method-20260910')
    parser.add_argument('--annotations', type=Path, default=ROOT/'交付材料/人工标注')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.archive, args.output, args.annotations), ensure_ascii=False, indent=2))
