"""Verify recorded requests, evidence and ratings; recompute the fixed final study."""
from __future__ import annotations
import argparse,json,statistics
from collections import Counter
from pathlib import Path
from learning_agent_eval import learning_quality as method
from learning_agent_eval.quality_content_audit import request as audit_request,validate as validate_audit
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from summarize_learning_quality import write_csv

DIMS=tuple(method.WEIGHTS)

def verify_digest(row):
    assert sha256_digest({k:v for k,v in row.items() if k!='result_sha256'})==row['result_sha256'], 'result digest mismatch'

def verify_attempts(attempts,request,e,validator,identity,stage):
    selected=None
    assert 1<=len(attempts)<=3
    for number,attempt in enumerate(attempts,1):
        assert attempt['number']==number and selected is None
        req={**request,'_budget_call_id':f'{identity}:{stage}:{number}'}
        assert sha256_digest(req)==attempt['request_sha256'], 'request mismatch'
        reply=attempt['reply']
        if attempt['status']=='complete':
            assert reply['status']=='completed' and reply['finish_reason']!='length'
            selected=validator(json.loads(reply['content']),e)
            assert selected==attempt['payload']
        else:
            assert attempt['status']=='invalid'
            if reply['finish_reason']=='length':assert attempt['validation_error']=='output_truncated'
            elif reply['status']=='completed':
                try: validator(json.loads(reply['content']),e)
                except (ValueError,TypeError):pass
                else:
                    # Published parser erratum permits unused descriptive labels.
                    assert stage=='audit' and attempt.get('validation_error')=='ValueError: every listed assertion must be checked, no invented identifiers'
    return selected

def verify_complete(row,e):
    assert row['effective_dimensions']==method.effective_levels(row['rating'],e)
    for key in ['schedule_facts','undo_version_facts','exponential_facts']:
        assert row[key]==getattr(method,key)(e)
    assert all(row[k]==v for k,v in method.aggregate(row['rating'],e).items()), 'score mismatch'

def load_study(root):
    design=json.loads((root/'design.json').read_text())
    assert design['method_sha256']==method.METHOD_SHA256==sha256_digest(design['method'])
    study={}
    for kind in ['full','validation','adversarial']:
        specs=json.loads((root/kind/'suite.json').read_text());byid={s['id']:s for s in specs}
        assert len(byid)==design[kind]['count'] and sha256_digest(specs)==design[kind]['suite_sha256']
        rows={}
        for shard in range(design['shards']):
            p=root/f'{kind}-results-{shard}/results.json';data=json.loads(p.read_text())
            assert data['method_sha256']==sha256_digest(data['method'])==method.METHOD_SHA256
            assert data['suite_sha256']==sha256_digest(specs)
            for old in data['rows']:
                key=(old['id'],old['repeat']);assert key not in rows
                spec=byid[old['id']];assert old['track']==spec['track']
                assert old['status']!='in_progress', 'experiment not finished'
                if not spec.get('evidence_file'):
                    assert old['status']=='runtime_failure';rows[key]=old;continue
                e=json.loads((root/kind/spec['evidence_file']).read_text())
                assert sha256_digest(e)==spec['evidence_sha256']==old['evidence_sha256']
                verify_digest(old)
                identity=f"{old['id']}:repeat:{old['repeat']}"
                audit=verify_attempts(old['audit_attempts'],audit_request(e,method.reading_view,method.catalog),e,validate_audit,identity,'audit')
                rating=None
                if audit is not None:
                    assert audit==old['content_audit']
                    rating=verify_attempts(old['rating_attempts'],method.request_for(e,audit),e,method.validate_rating,identity,'rating')
                assert (rating is not None)==(old['status']=='complete')
                if rating is not None:assert rating==old['rating'];verify_complete(old,e)
                row=old
                recovery=root/'ingestion-recovery'/f'{kind}-{old["id"]}-{old["repeat"]}'/'results.json'
                if recovery.exists():
                    new=json.loads(recovery.read_text());verify_digest(new)
                    assert new['original_result_sha256']==old['result_sha256'] and new['original_file']==str(p.relative_to(root))
                    assert old['status']=='judge_error' and new['method_sha256']==method.METHOD_SHA256
                    eligible=[]
                    for attempt in old['audit_attempts']:
                        if attempt.get('validation_error')!='ValueError: every listed assertion must be checked, no invented identifiers':continue
                        try:payload=validate_audit(json.loads(attempt['reply']['content']),e)
                        except (ValueError,TypeError):continue
                        eligible.append((attempt['number'],payload))
                    assert eligible and eligible[0]==(new['recovered_audit_attempt'],new['content_audit'])
                    identity=f"{kind}-{old['id']}-{old['repeat']}:ingestion-recovery"
                    rating=verify_attempts(new['rating_attempts'],method.request_for(e,new['content_audit']),e,method.validate_rating,identity,'rating')
                    assert (rating is not None)==(new['status']=='complete') and new['status']!='in_progress'
                    if rating is not None:assert rating==new['rating'];verify_complete(new,e)
                    row={**new,'original_status':old['status']}
                operational=root/'operational-recovery'/f'{kind}-{old["id"]}-{old["repeat"]}'/'results.json'
                if operational.exists():
                    assert not recovery.exists() and old['status']=='judge_error'
                    new=json.loads(operational.read_text());verify_digest(new)
                    assert new['original_result_sha256']==old['result_sha256'] and new['original_file']==str(p.relative_to(root))
                    assert new['method_sha256']==method.METHOD_SHA256
                    identity=f"{kind}-{old['id']}-{old['repeat']}:operational-recovery"
                    if new.get('reused_original_audit'):
                        assert audit is not None and audit==new['content_audit']
                    else:
                        audit=verify_attempts(new['audit_attempts'],audit_request(e,method.reading_view,method.catalog),e,validate_audit,identity,'audit')
                    rating=None
                    if audit is not None:
                        assert audit==new['content_audit']
                        rating=verify_attempts(new['rating_attempts'],method.request_for(e,audit),e,method.validate_rating,identity,'rating')
                    assert (rating is not None)==(new['status']=='complete') and new['status']!='in_progress'
                    if rating is not None:assert rating==new['rating'];verify_complete(new,e)
                    row={**new,'original_status':old['status']}
                rows[key]=row
        assert set(rows)=={(i,r) for i in byid for r in range(1,design[kind]['repeats']+1)}, 'fixed slots incomplete'
        study[kind]=rows
    return design,study

def summarize(root,output):
    design,study=load_study(root)
    output.mkdir(parents=True,exist_ok=False)
    summary={};flat=[];dimensions=[]
    for kind,lookup in study.items():
        rows=list(lookup.values());valid=[r for r in rows if r['status']=='complete']
        summary[kind]={'slots':len(rows),'status':dict(Counter(r['status'] for r in rows)),
            'original_status':dict(Counter(r.get('original_status',r['status']) for r in rows)),
            'parser_recovered':sum(r.get('recovery')=='additional-finding-label-parser-fix' for r in rows),
            'operational_recovered':sum(r.get('recovery')=='one-unscored-operational-cycle' and r['status']=='complete' for r in rows)}
        for r in rows:
            flat.append(dict(experiment=kind,id=r['id'],repeat=r['repeat'],track=r['track'],status=r['status'],
                original_status=r.get('original_status',r['status']),recovery=r.get('recovery',''),
                raw_score=r.get('raw_score'),score=r.get('score'),outcome=r.get('outcome'),
                **{d:r.get('effective_dimensions',{}).get(d) for d in DIMS}))
        if kind=='full':
            tracks=[]
            for track in ['planning','intervention','assessment','revision']:
                selected=[r for r in rows if r['track']==track];ok=[r for r in selected if r['status']=='complete']
                tracks.append(dict(track=track,total=len(selected),valid=len(ok),passed=sum(r['outcome']=='pass' for r in ok),
                    failed=sum(r['outcome']=='fail' for r in ok),unscored=len(selected)-len(ok),
                    mean_score=statistics.mean(r['score'] for r in ok) if ok else None,
                    **{d:statistics.mean(r['effective_dimensions'][d] for r in ok) if ok else None for d in DIMS}))
            summary[kind]['tracks']=tracks;write_csv(output/'tracks.csv',tracks)
        for d in DIMS:
            levels=Counter(r['effective_dimensions'][d] for r in valid)
            dimensions.append(dict(experiment=kind,dimension=d,valid=len(valid),unscored=len(rows)-len(valid),
                level0=levels[0],level1=levels[1],level2=levels[2],mean=statistics.mean(r['effective_dimensions'][d] for r in valid) if valid else None))
    labels=json.loads((root/'validation/private-labels.json').read_text());assert sha256_digest(labels)==design['validation']['labels_sha256']
    lookup=study['validation'];repeats=design['validation']['repeats'];triplets=[];stability=[]
    for group in sorted({l['group'] for l in labels}):
        ids={l['level']:l['id'] for l in labels if l['group']==group}
        for rep in range(1,repeats+1):
            rs=[lookup[ids[level],rep] for level in ['good','mild','severe']];complete=all(r['status']=='complete' for r in rs)
            scores=[r.get('raw_score') for r in rs];capped=[r.get('score') for r in rs]
            triplets.append(dict(group=group,repeat=rep,complete=complete,good=scores[0],mild=scores[1],severe=scores[2],
                strict_raw=complete and scores[0]>scores[1]>scores[2],good_above_severe=complete and scores[0]>scores[2],
                strict_final=complete and capped[0]>capped[1]>capped[2]))
    for identity in sorted({i for i,r in lookup}):
        rs=[lookup[identity,rep] for rep in range(1,repeats+1)];valid=[r for r in rs if r['status']=='complete'];complete=len(valid)==repeats
        stability.append(dict(id=identity,valid=len(valid),expected=repeats,complete=complete,
            raw_sd=statistics.pstdev(r['raw_score'] for r in valid) if complete else None,
            final_sd=statistics.pstdev(r['score'] for r in valid) if complete else None,
            outcome_agreement=max(Counter(r['outcome'] for r in valid).values())/repeats if complete else None,
            dimension_vector_agreement=max(Counter(tuple(r['effective_dimensions'][d] for d in DIMS) for r in valid).values())/repeats if complete else None))
    ok=[r for r in stability if r['complete']]
    summary['validation'].update(triplet_groups=len(triplets),complete_triplets=sum(r['complete'] for r in triplets),
        strict_raw=sum(r['strict_raw'] for r in triplets),strict_final=sum(r['strict_final'] for r in triplets),
        good_above_severe=sum(r['good_above_severe'] for r in triplets),complete_repeated_examples=len(ok),
        **{f'mean_{k}':statistics.mean(r[k] for r in ok) if ok else None for k in ['raw_sd','final_sd','outcome_agreement','dimension_vector_agreement']})
    labels=json.loads((root/'adversarial/private-labels.json').read_text());assert sha256_digest(labels)==design['adversarial']['labels_sha256']
    attacks=[]
    for label in labels:
        row=study['adversarial'][label['id'],1];partners=[r for (i,rep),r in study['validation'].items() if i==label['partner_id'] and r['status']=='complete']
        score=row.get('score');reference=statistics.mean(r['score'] for r in partners) if partners else None
        attacks.append(dict(id=label['id'],partner_id=label['partner_id'],status=row['status'],score=score,
            false_pass=row.get('outcome')=='pass',partner_valid=len(partners),partner_mean=reference,
            score_change=score-reference if score is not None and reference is not None else None))
    summary['adversarial'].update(false_passes=sum(r['false_pass'] for r in attacks),valid=sum(r['status']=='complete' for r in attacks))
    for name,rows in [('cases',flat),('dimensions',dimensions),('triplets',triplets),('stability',stability),('adversarial',attacks)]:write_csv(output/f'{name}.csv',rows)
    (output/'summary.json').write_bytes(canonical_json_bytes(summary))
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(summarize(a.evidence,a.output),ensure_ascii=False,indent=2))
