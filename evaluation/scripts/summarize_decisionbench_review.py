"""Bind authored case reviews to immutable evidence and retain automatic scores."""
import argparse,json,statistics
from pathlib import Path
from collections import Counter
from summarize_decisionbench_final import load_study,DIMS
from summarize_learning_quality import write_csv
from learning_agent_eval import learning_quality as method
from learning_agent_eval.canonical import sha256_digest,canonical_json_bytes
from learning_agent_eval.validator import resolve_evidence_path

def summarize(root,notes_path,output):
    _,study=load_study(root)
    notes=json.loads(notes_path.read_text());assert notes['reviewer_kind']=='development-assistant-ai'
    assert len(notes['rows'])==128
    lookup={(r['experiment'],r['id'],r['repeat']):r for r in notes['rows']}
    assert len(lookup)==128
    rows=[];dimensions=[]
    for kind,data in study.items():
        specs={s['id']:s for s in json.loads((root/kind/'suite.json').read_text())}
        for (identity,repeat),automatic in data.items():
            n=lookup[kind,identity,repeat];spec=specs[identity]
            assert n['automatic_result_sha256']==automatic['result_sha256']
            assert n['evidence_sha256']==spec['evidence_sha256']
            e=json.loads((root/kind/spec['evidence_file']).read_text())
            assert n['note'].strip() and n['name'].strip() and n['checked_dimensions']==list(DIMS)
            assert n['evidence_paths']
            for path in n['evidence_paths']:assert resolve_evidence_path(e,path,roots=set(e))[0],(identity,path)
            row=dict(experiment=kind,id=identity,repeat=repeat,name=n['name'],note=n['note'],
                automatic_status=automatic['status'],automatic_score=automatic.get('score'),
                automatic_result_sha256=automatic['result_sha256'],evidence_sha256=spec['evidence_sha256'])
            if kind=='full':
                levels=n['review_dimensions'];assert set(levels)==set(DIMS) and all(v in (0,1,2) for v in levels.values())
                raw=sum(method.WEIGHTS[d]*levels[d]/2 for d in DIMS);score=min(raw,method.METHOD['caps'][n['severity']])
                row.update(track=automatic['track'],review_raw_score=raw,review_score=score,review_outcome='pass' if score>=70 else 'fail',
                    review_severity=n['severity'],dimensions_changed=levels!=automatic.get('effective_dimensions'),**levels)
                for d in DIMS:dimensions.append(dict(id=identity,name=n['name'],track=automatic['track'],dimension=d,
                    automatic=automatic.get('effective_dimensions',{}).get(d),review=levels[d]))
            else:row['review_finding']=n['review_finding']
            rows.append(row)
    full=[r for r in rows if r['experiment']=='full'];tracks=[]
    for t in ['planning','intervention','assessment','revision']:
        selected=[r for r in full if r['track']==t]
        tracks.append(dict(track=t,total=len(selected),passed=sum(r['review_outcome']=='pass' for r in selected),
            failed=sum(r['review_outcome']=='fail' for r in selected),mean_score=statistics.mean(r['review_score'] for r in selected),
            **{d:statistics.mean(r[d] for r in selected) for d in DIMS}))
    summary=dict(reviewed_slots=len(rows),reviewer_kind=notes['reviewer_kind'],notes_sha256=sha256_digest(notes),
        application=dict(total=48,dimensions_changed=sum(r['dimensions_changed'] for r in full),
            defect_cases=sum(r['review_severity']!='none' for r in full),passed=sum(r['review_outcome']=='pass' for r in full),
            failed=sum(r['review_outcome']=='fail' for r in full),mean_score=statistics.mean(r['review_score'] for r in full),tracks=tracks),
        validity_findings=dict(Counter(r['review_finding'] for r in rows if r['experiment']!='full')))
    output.mkdir(parents=True,exist_ok=False)
    for name,data in [('reviews',rows),('application',full),('dimensions',dimensions),('tracks',tracks)]:
        fields=list(dict.fromkeys(k for row in data for k in row))
        write_csv(output/f'{name}.csv',[{k:row.get(k) for k in fields} for row in data])
    (output/'summary.json').write_bytes(canonical_json_bytes(summary));return summary
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--notes',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(summarize(a.evidence,a.notes,a.output),ensure_ascii=False,indent=2))
