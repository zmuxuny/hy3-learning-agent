"""Recompute learning-quality results and validity statistics from public evidence."""
from __future__ import annotations
import argparse,csv,json,statistics
from collections import Counter
from pathlib import Path
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from learning_agent_eval.learning_quality import METHOD_SHA256,validate_rating,aggregate,schedule_facts

def write_csv(path,rows):
    if not rows:return
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def summarize(root,output):
    output.mkdir(parents=True,exist_ok=False)
    design=json.loads((root/'design.json').read_text());assert design['method_sha256']==METHOD_SHA256
    result={};allrows=[]
    for kind in ['validation','full']:
        suite_root=root/kind
        specs=json.loads((suite_root/'suite.json').read_text());byid={s['id']:s for s in specs}
        assert sha256_digest(specs)==design[kind]['suite_sha256']
        rows=[];seen=set()
        for shard in [0,1]:
            data=json.loads((root/f'{kind}-results-{shard}/results.json').read_text())
            assert data['method_sha256']==METHOD_SHA256
            assert sha256_digest(data['method'])==METHOD_SHA256
            assert data['suite_sha256']==sha256_digest(specs)
            for row in data['rows']:
                identity=(row['id'],row['repeat']);assert identity not in seen;seen.add(identity)
                spec=byid[row['id']];assert row['track']==spec['track']
                if spec.get('evidence_file'):
                    e=json.loads((suite_root/spec['evidence_file']).read_text());assert sha256_digest(e)==spec['evidence_sha256']==row['evidence_sha256']
                    content={k:v for k,v in row.items() if k!='result_sha256'};assert sha256_digest(content)==row['result_sha256']
                    if row['status']=='complete':
                        assert validate_rating(json.loads(row['reply']['content']),e)==row['rating']
                        assert schedule_facts(e)==row['schedule_facts']
                        assert all(row[k]==v for k,v in aggregate(row['rating'],e).items())
                rows.append(row)
        repeats=design[kind]['repeats'];assert seen=={(i,r) for i in byid for r in range(1,repeats+1)}
        result[kind]={'slots':len(rows),'status':dict(Counter(r['status'] for r in rows))}
        for row in rows:
            dims={d['dimension']:d['level'] for d in row.get('rating',{}).get('dimensions',[])}
            allrows.append(dict(experiment=kind,id=row['id'],repeat=row['repeat'],track=row['track'],status=row['status'],
                raw_score=row.get('raw_score'),score=row.get('score'),outcome=row.get('outcome'),
                **{d:dims.get(d) for d in ['D1','D2','D3','D4','D5','D6','D7']}))
        if kind=='validation':
            labels=json.loads((suite_root/'private-labels.json').read_text());assert sha256_digest(labels)==design[kind]['labels_sha256']
            groups={l['group'] for l in labels};lookup={(r['id'],r['repeat']):r for r in rows}
            triplets=[]
            for group in sorted(groups):
                ids={l['level']:l['id'] for l in labels if l['group']==group}
                for rep in range(1,repeats+1):
                    selected=[lookup[ids[level],rep] for level in ['good','mild','severe']]
                    complete=all(r['status']=='complete' for r in selected)
                    scores=[r.get('raw_score') for r in selected];capped=[r.get('score') for r in selected]
                    triplets.append(dict(group=group,repeat=rep,complete=complete,good=scores[0],mild=scores[1],severe=scores[2],
                        strict_raw=complete and scores[0]>scores[1]>scores[2],good_above_severe=complete and scores[0]>scores[2],
                        strict_final=complete and capped[0]>capped[1]>capped[2]))
            stability=[]
            for identity in byid:
                selected=[lookup[identity,rep] for rep in range(1,repeats+1)];valid=[r for r in selected if r['status']=='complete']
                complete=len(valid)==repeats
                stability.append(dict(id=identity,valid=len(valid),expected=repeats,complete=complete,
                    raw_sd=statistics.pstdev(r['raw_score'] for r in valid) if complete else None,
                    final_sd=statistics.pstdev(r['score'] for r in valid) if complete else None,
                    outcome_agreement=max(Counter(r['outcome'] for r in valid).values())/repeats if complete else None,
                    dimension_vector_agreement=max(Counter(tuple(d['level'] for d in r['rating']['dimensions']) for r in valid).values())/repeats if complete else None))
            valid_stability=[r for r in stability if r['complete']]
            result[kind].update(triplet_groups=len(triplets),complete_triplets=sum(r['complete'] for r in triplets),
                strict_raw=sum(r['strict_raw'] for r in triplets),strict_final=sum(r['strict_final'] for r in triplets),
                good_above_severe=sum(r['good_above_severe'] for r in triplets),complete_repeated_examples=len(valid_stability),
                mean_raw_population_sd=statistics.mean(r['raw_sd'] for r in valid_stability) if valid_stability else None,
                mean_final_population_sd=statistics.mean(r['final_sd'] for r in valid_stability) if valid_stability else None,
                mean_outcome_agreement=statistics.mean(r['outcome_agreement'] for r in valid_stability) if valid_stability else None,
                mean_dimension_vector_agreement=statistics.mean(r['dimension_vector_agreement'] for r in valid_stability) if valid_stability else None)
            write_csv(output/'triplets.csv',triplets);write_csv(output/'stability.csv',stability)
        else:
            tracks=[]
            for track in ['planning','intervention','assessment','revision']:
                selected=[r for r in rows if r['track']==track];valid=[r for r in selected if r['status']=='complete']
                dims={d:statistics.mean(next(x['level'] for x in r['rating']['dimensions'] if x['dimension']==d) for r in valid) if valid else None for d in ['D1','D2','D3','D4','D5','D6','D7']}
                tracks.append(dict(track=track,total=len(selected),valid=len(valid),passed=sum(r['outcome']=='pass' for r in valid),
                    failed=sum(r['outcome']=='fail' for r in valid),unscored=len(selected)-len(valid),
                    mean_score=statistics.mean(r['score'] for r in valid) if valid else None,**dims))
            result[kind]['tracks']=tracks;write_csv(output/'tracks.csv',tracks)
    write_csv(output/'cases.csv',allrows)
    (output/'summary.json').write_bytes(canonical_json_bytes(result));print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();summarize(a.evidence,a.output)
