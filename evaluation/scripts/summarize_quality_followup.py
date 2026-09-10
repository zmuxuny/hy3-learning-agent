"""Recompute source-bound held-out quality validation, including failures."""
import argparse,csv,json,statistics
from pathlib import Path
from learning_agent_eval import learning_quality_v9 as method
from learning_agent_eval import learning_quality as old
from learning_agent_eval import quality_content_audit as audit
from learning_agent_eval.canonical import sha256_digest,canonical_json_bytes


def verify_result(document,spec,e):
    assert document['method_sha256']==method.METHOD_SHA256
    assert document['method']==method.METHOD
    for row in document['rows']:
        assert row['id']==spec['id'] and row['evidence_sha256']==sha256_digest(e)
        if 'result_sha256' in row:
            assert sha256_digest({k:v for k,v in row.items() if k!='result_sha256'})==row['result_sha256']
        for stage in ('audit','rating'):
            attempts=row.get(stage+'_attempts',[])
            request=method.audit_request(e,old.reading_view,old.catalog) if stage=='audit' else method.request_for(e,row.get('content_audit'))
            for n,a in enumerate(attempts,1):
                assert a['number']==n
                req={**request,'_budget_call_id':f"{spec['id']}:repeat:{row['repeat']}:{stage}:{n}"}
                assert sha256_digest(req)==a['request_sha256']
                if a['status']=='complete':
                    assert n==len(attempts)
                    validate=audit.validate if stage=='audit' else method.validate_rating
                    assert validate(json.loads(a['reply']['content']),e)==a['payload']
                    assert row['content_audit' if stage=='audit' else 'rating']==a['payload']
        if row['status']=='complete':
            for k,v in method.aggregate(row['rating'],e).items():assert row[k]==v
            assert row['effective_dimensions']==method.effective_levels(row['rating'],e)
            assert row['effective_severity']==method.reconciliation_result(row['rating'],e)['effective_severity']


def summarize(root,output):
    design=json.loads((root/'design.json').read_text())
    suite=json.loads((root/'inputs/suite.json').read_text());labels=json.loads((root/'inputs/private-labels.json').read_text())
    assert sha256_digest(suite)==design['suite_sha256'] and sha256_digest(labels)==design['labels_sha256']
    assert design['method_sha256']==method.METHOD_SHA256
    labelmap={r['id']:r for r in labels};rows=[]
    for spec in suite:
        e=json.loads((root/'inputs'/spec['evidence_file']).read_text());assert sha256_digest(e)==spec['evidence_sha256']
        candidates=[]
        for batch in design['result_batches']:
            path=root/batch/spec['id']/'results.json'
            if path.exists():
                doc=json.loads(path.read_text());verify_result(doc,spec,e)
                assert doc['suite_sha256']==design['suite_sha256']
                for row in doc['rows']:candidates.append((str(path.relative_to(root)),row))
        for rep in range(1,design['repeats']+1):
            variants=[(p,r) for p,r in candidates if r['repeat']==rep]
            valid=[(p,r) for p,r in variants if r['status']=='complete']
            # A second successful result for a slot is never silently selected.
            assert len(valid)<=1,(spec['id'],rep,'duplicate valid result')
            p,r=valid[0] if valid else (variants[-1] if variants else ('',{'status':'missing'}))
            label=labelmap[spec['id']]
            rows.append(dict(id=spec['id'],name=label['name'],family=label['family'],track=spec['track'],condition=label['condition'],repeat=rep,
                status=r['status'],raw_score=r.get('raw_score'),score=r.get('score'),outcome=r.get('outcome'),severity=r.get('effective_severity'),
                rule_critical=bool(r.get('critical_findings')),source=p,result_sha256=r.get('result_sha256'),
                initial_status=variants[0][1]['status'] if variants else 'missing',
                **r.get('effective_dimensions',{})))
    def metrics(selected):
        severe=[r for r in selected if r['condition']=='severe'];good=[r for r in selected if r['condition']=='good']
        return dict(total=len(selected),valid=sum(r['status']=='complete' for r in selected),
            severe_slots=len(severe),severe_failed=sum(r['outcome']=='fail' for r in severe),
            severe_identified_critical=sum(r['severity']=='critical' for r in severe),
            good_slots=len(good),good_passed=sum(r['outcome']=='pass' for r in good),
            good_false_fail=sum(r['outcome']=='fail' for r in good),no_score=sum(r['status']!='complete' for r in selected))
    result=metrics(rows);result.update(method_sha256=method.METHOD_SHA256,tracks={t:metrics([r for r in rows if r['track']==t]) for t in ['planning','intervention','assessment','revision']})
    result['cases']=[]
    for spec in suite:
        rs=[r for r in rows if r['id']==spec['id']];scores=[r['score'] for r in rs if r['score'] is not None]
        result['cases'].append(dict(id=spec['id'],name=rs[0]['name'],condition=rs[0]['condition'],valid=len(scores),
                                   scores=scores,mean=statistics.mean(scores) if scores else None,sd=statistics.pstdev(scores) if scores else None))
    output.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with (output/'cases.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    (output/'summary.json').write_bytes(canonical_json_bytes(result))
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(summarize(a.archive,a.output),ensure_ascii=False,indent=2))
