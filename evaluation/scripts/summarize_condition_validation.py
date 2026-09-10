"""Verify the final method's fixed condition pairs and recompute their results."""
import argparse,csv,json,statistics
from pathlib import Path
from learning_agent_eval import learning_quality_v9 as method
from learning_agent_eval.canonical import sha256_digest
from summarize_final_method import select_result,flatten,write_csv
ROOT=Path(__file__).resolve().parents[2]
def read(p):return json.loads(p.read_text())
def summarize(archive,output):
    design=read(archive/'design.json');inputs=ROOT/'evaluation/datasets/decisionbench-learning-v1/extensions/condition-validation-20260910';suite=read(inputs/'suite.json');labels=read(inputs/'private-labels.json');lm={r['id']:r for r in labels}
    assert design['method']=='learning-quality-9' and sha256_digest(suite)==design['suite_sha256'] and sha256_digest(labels)==design['labels_sha256']
    batches=['runs','recovery']
    if (archive/'recovery-amendment.json').exists():
        amendment=read(archive/'recovery-amendment.json')
        assert amendment['authorization']=='作者：api不太稳定，你多试试'
        assert amendment['result_batches']==['runs','recovery','recovery-2','recovery-3','recovery-4']
        batches=amendment['result_batches']
    rows=[]
    for spec in suite:
        ep=inputs/spec['evidence_file'];e=read(ep)
        for row,initial,source in select_result([archive/f/spec['id']/'results.json' for f in batches],spec,e,design['repeats'],method):
            assert read(source)['source_commit']==design['method_source_commit'] and read(source)['suite_sha256']==design['suite_sha256']
            label=lm[spec['id']];r=flatten('conditions',row,initial,source,ep,label['name'],label['condition']);r['expected_outcome']=label['expected_outcome'];r['outcome_matches']=r['outcome']==label['expected_outcome'];rows.append(r)
    assert len(rows)==design['total_slots']==36
    reviewed=read(archive/'review.json')['rows'];reviews={(r['id'],r['repeat']):r for r in reviewed};assert len(reviews)==len(reviewed)==36
    for r in rows:
        review=reviews[r['id'],r['repeat']];assert review['source_result_sha256']==r['result_sha256'] and review['evidence_sha256']==r['evidence_sha256'];assert review['checked_dimensions']==[f'D{i}' for i in range(1,8)] and review['note'].strip()
        r['reviewed_condition_error']=review['condition_error_identified'];r['review_note']=review['note']
    summary={'method':design['method'],'method_sha256':method.METHOD_SHA256,'total':36,'valid':sum(r['status']=='complete' for r in rows),'no_score':sum(r['status']!='complete' for r in rows),'original_errors':sum(r['original_status']!='complete' for r in rows),'normal_total':18,'normal_passed':sum(r['condition']=='good' and r['outcome']=='pass' for r in rows),'defect_total':18,'defect_failed':sum(r['condition']=='defect' and r['outcome']=='fail' for r in rows),'condition_error_identified':sum(r['condition']=='defect' and r['reviewed_condition_error'] for r in rows),'reviewed_slots':36}
    stability=[]
    for spec in suite:
        rs=[r for r in rows if r['id']==spec['id']];valid=[r['score'] for r in rs if r['status']=='complete'];stability.append(dict(id=spec['id'],name=lm[spec['id']]['name'],condition=lm[spec['id']]['condition'],valid=len(valid),mean=statistics.mean(valid) if valid else None,sd=statistics.pstdev(valid) if len(valid)==3 else None))
    output.mkdir(parents=True,exist_ok=True);write_csv(output/'cases.csv',rows);write_csv(output/'repetition.csv',stability);(output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');return summary
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(summarize(a.archive.resolve(),a.output),ensure_ascii=False,indent=2))
