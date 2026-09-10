"""Verify and summarize all published inputs using the same frozen quality method."""
import argparse,csv,json,statistics,hashlib,importlib
from collections import Counter
from pathlib import Path
from learning_agent_eval import learning_quality_v9 as method
from learning_agent_eval import learning_quality as base
from learning_agent_eval.canonical import sha256_digest
from summarize_quality_followup import verify_result
from summarize_human_confirmation import kappa
from summarize_decisionbench_final import load_study

ROOT=Path(__file__).resolve().parents[2]
OLD=ROOT/'evaluation/artifacts/decisionbench-study-20260910'
NEW=ROOT/'evaluation/artifacts/decisionbench-severe-validation-20260910'
DIMS=tuple(base.WEIGHTS)
TRACKS=('planning','intervention','assessment','revision')

def read(p):return json.loads(p.read_text())
def csv_rows(p):return list(csv.DictReader(p.open()))
def mean(xs):return statistics.mean(xs) if xs else None
def write_csv(p,rows):
    with p.open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator="\n");w.writeheader();w.writerows(rows)

def select_result(paths,spec,e,repeats,scoring_method=method):
    selected={};initial={};sources={}
    for p in paths:
        if not p.exists():continue
        doc=read(p);verify_result(doc,spec,e,scoring_method)
        assert len({r['repeat'] for r in doc['rows']})==len(doc['rows'])
        assert all(r['track']==spec['track'] for r in doc['rows'])
        for row in doc['rows']:
            rep=row['repeat'];assert rep in range(1,repeats+1)
            assert row['status'] in ('complete','judge_error','runtime_failure')
            assert rep not in selected or selected[rep]['status']!='complete','valid score was repeated'
            initial.setdefault(rep,row['status']);selected[rep]=row;sources[rep]=p
    assert set(selected)==set(range(1,repeats+1)),f'missing repeat: {spec["id"]}'
    return [(selected[i],initial[i],sources[i]) for i in range(1,repeats+1)]

def summarize(archive,output):
    design=read(archive/'design.json')
    version=design['method'].rsplit('-',1)[-1]
    assert version in ('9','10','11')
    scoring_method=importlib.import_module('learning_agent_eval.learning_quality_v'+version)
    assert design['method_sha256']==scoring_method.METHOD_SHA256
    load_study(OLD/'evidence')  # Validate the original application's source/evidence chain.
    refs={r['id']:r for r in csv_rows(ROOT/'交付材料/人工标注/application.csv')}
    labels={r['id']:r for r in read(OLD/'evidence/validation/private-labels.json')}
    newlabels={r['id']:r for r in read(NEW/'inputs/private-labels.json')}
    conditionlabels={r['id']:r for r in read(ROOT/design['parts']['conditions']['labels'])} if 'conditions' in design['parts'] else {}
    rows=[]
    for part,d in design['parts'].items():
        suitepath=ROOT/d['input'];assert hashlib.sha256(suitepath.read_bytes()).hexdigest()==d['suite_sha256']
        suite=read(suitepath);assert len(suite)==d['outputs']
        for spec in suite:
            ep=suitepath.parent/spec['evidence_file'];e=read(ep)
            assert sha256_digest(e)==spec['evidence_sha256']
            paths=[archive/folder/part/spec['id']/'results.json' for folder in ['runs','recovery']]
            for row,original,source in select_result(paths,spec,e,d['repeats'],scoring_method):
                assert read(source)['source_commit']==design['source_commit']
                assert read(source)['suite_sha256']==sha256_digest(suite)
                if part=='application':name=refs[row['id']]['name'];condition='application'
                elif part=='quality':label=labels[row['id']];name=label['topic'];condition=label['level']
                elif part in ('new_scenarios','conditions'):
                    label=(newlabels if part=='new_scenarios' else conditionlabels)[row['id']];name=label['name'];condition=label['condition']
                else:name=labels['quality-'+row['id'].split('-')[1]+'-3']['topic']+'：评分操纵';condition='severe'
                rows.append(flatten(part,row,original,source,ep,name,condition))
    assert len(rows)==design['new_scoring_slots']
    if design.get('retained_new_scenarios'):
        assert scoring_method.METHOD_SHA256 == method.METHOD_SHA256
        # These 72 ratings already used the identical frozen method and are reused, not rescored.
        for spec in read(NEW/'inputs/suite.json'):
            ep=NEW/'inputs'/spec['evidence_file'];e=read(ep);assert sha256_digest(e)==spec['evidence_sha256']
            for row,original,source in select_result([NEW/folder/spec['id']/'results.json' for folder in ['validation','validation-recovery']],spec,e,3):
                label=newlabels[spec['id']]
                rows.append(flatten('new_scenarios',row,original,source,ep,label['name'],label['condition']))
    assert len(rows)==design['total_reported_slots']==design['total_reported_slots']
    assert len({(r['part'],r['id'],r['repeat']) for r in rows})==design['total_reported_slots']
    groups={p:[r for r in rows if r['part']==p] for p in ['application','quality','adversarial','new_scenarios','conditions'] if any(r['part']==p for r in rows)}
    tracks=[];dimensions=[]
    for part,rs in groups.items():
        for track in TRACKS:
            selected=[r for r in rs if r['track']==track];valid=[r for r in selected if r['status']=='complete']
            tracks.append(dict(part=part,track=track,total=len(selected),valid=len(valid),passed=sum(r['outcome']=='pass' for r in valid),failed=sum(r['outcome']=='fail' for r in valid),no_score=len(selected)-len(valid),mean_score=mean([r['score'] for r in valid])))
            for dim in DIMS:dimensions.append(dict(part=part,track=track,dimension=dim,name=base.CRITERIA[dim][0],valid=len(valid),mean_level=mean([r[dim] for r in valid])))
    triplets=[];stability=[]
    quality={(r['id'],r['repeat']):r for r in groups['quality']}
    for group in sorted({x['group'] for x in labels.values()}):
        for rep in range(1,4):
            rs=[quality[group+'-'+str(i),rep] for i in range(1,4)];valid=all(r['status']=='complete' for r in rs)
            triplets.append(dict(group=group,name=rs[0]['name'],repeat=rep,valid=valid,good=rs[0]['raw_score'],mild=rs[1]['raw_score'],severe=rs[2]['raw_score'],strict_raw=valid and rs[0]['raw_score']>rs[1]['raw_score']>rs[2]['raw_score'],strict_final=valid and rs[0]['score']>rs[1]['score']>rs[2]['score'],good_above_severe=valid and rs[0]['raw_score']>rs[2]['raw_score']))
    for part in ('quality','new_scenarios','conditions'):
        if part not in groups:continue
        for identity in sorted({r['id'] for r in groups[part]}):
            rs=[r for r in groups[part] if r['id']==identity];valid=all(r['status']=='complete' for r in rs)
            stability.append(dict(part=part,id=identity,name=rs[0]['name'],condition=rs[0]['condition'],valid=valid,raw_sd=statistics.pstdev(r['raw_score'] for r in rs) if valid else None,final_sd=statistics.pstdev(r['score'] for r in rs) if valid else None,outcome_agreement=max(Counter(r['outcome'] for r in rs).values())/3 if valid else None,dimension_vector_agreement=max(Counter(tuple(r[d] for d in DIMS) for r in rs).values())/3 if valid else None))
    adversarial=[]
    for r in groups['adversarial']:
        reference=[q for q in groups['quality'] if q['id']=='quality-'+r['id'].split('-')[1]+'-3' and q['status']=='complete']
        avg=mean([q['score'] for q in reference])
        adversarial.append(dict(id=r['id'],name=r['name'],status=r['status'],score=r['score'],outcome=r['outcome'],severity=r['severity'],reference_valid=len(reference),reference_mean=avg,score_change=None if avg is None or r['score'] is None else r['score']-avg))
    app=[r for r in groups['application'] if r['status']=='complete'];alignment=[];human_cases=[]
    for r in app:
        ref=refs[r['id']];assert ref['evidence_sha256']==r['evidence_sha256']
        human_cases.append(dict(id=r['id'],name=r['name'],track=r['track'],automatic_score=r['score'],human_score=float(ref['review_score']),automatic_outcome=r['outcome'],human_outcome=ref['review_outcome'],**{'automatic_'+d:r[d] for d in DIMS},**{'human_'+d:int(ref[d]) for d in DIMS}))
    for d in DIMS:
        a=[r[d] for r in app];b=[int(refs[r['id']][d]) for r in app]
        alignment.append(dict(dimension=d,name=base.CRITERIA[d][0],valid=len(a),agreement=sum(x==y for x,y in zip(a,b))/len(a) if a else None,quadratic_kappa=kappa(a,b) if a else None))
    def metrics(rs):
        valid=[r for r in rs if r['status']=='complete'];severe=[r for r in rs if r['condition']=='severe'];good=[r for r in rs if r['condition']=='good']
        return dict(total=len(rs),valid=len(valid),no_score=len(rs)-len(valid),passed=sum(r['outcome']=='pass' for r in valid),failed=sum(r['outcome']=='fail' for r in valid),severe_total=len(severe),severe_critical_failed=sum(r['severity']=='critical' and r['outcome']=='fail' for r in severe),good_total=len(good),good_passed=sum(r['outcome']=='pass' for r in good),original_errors=sum(r['original_status']!='complete' for r in rs))
    summary=dict(method=scoring_method.METHOD['version'],method_sha256=scoring_method.METHOD_SHA256,**metrics(rows),parts={p:metrics(rs) for p,rs in groups.items()},discrimination=dict(total=24,valid=sum(r['valid'] for r in triplets),strict_raw=sum(r['strict_raw'] for r in triplets),strict_final=sum(r['strict_final'] for r in triplets),good_above_severe=sum(r['good_above_severe'] for r in triplets)),repetition={},automatic_human=dict(valid=len(app),outcome_matches=sum(r['outcome']==refs[r['id']]['review_outcome'] for r in app),dimensions=alignment))
    for part in ('quality','new_scenarios','conditions'):
        if part not in groups:continue
        vs=[r for r in stability if r['part']==part and r['valid']]
        summary['repetition'][part]=dict(complete_outputs=len(vs),**{k:mean([r[k] for r in vs]) for k in ['raw_sd','final_sd','outcome_agreement','dimension_vector_agreement']})
    reviews=read(archive/'review.json') if (archive/'review.json').exists() else {'rows':[]}
    reviewed={(r['part'],r['id'],r['repeat']):r for r in reviews['rows']}
    assert len(reviewed)==len(reviews['rows'])
    assert set(reviewed).issubset({(r['part'],r['id'],r['repeat']) for r in rows})
    for row in rows:
        key=(row['part'],row['id'],row['repeat'])
        if key in reviewed:
            rr=reviewed[key];assert rr['source_result_sha256']==row['result_sha256'] and rr['evidence_sha256']==row['evidence_sha256']
            assert rr['checked_dimensions']==list(DIMS) and rr['note'].strip()
    summary['reviewed_slots']=len(reviewed)
    output.mkdir(parents=True,exist_ok=True)
    for name,rs in [('cases',rows),('tracks',tracks),('dimensions',dimensions),('triplets',triplets),('stability',stability),('adversarial',adversarial),('human-alignment',alignment),('application-human',human_cases)]:write_csv(output/(name+'.csv'),rs)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary

def flatten(part,row,original,source,evidence,name,condition):
    valid=row['status']=='complete'
    return dict(part=part,id=row['id'],repeat=row['repeat'],name=name,track=row['track'],condition=condition,status=row['status'],original_status=original,raw_score=row.get('raw_score'),score=row.get('score'),outcome=row.get('outcome','no_score'),severity=row.get('effective_severity',''),**{d:row['effective_dimensions'][d] if valid else None for d in DIMS},source=str(source.relative_to(ROOT)),evidence=str(evidence.relative_to(ROOT)),result_sha256=row.get('result_sha256',''),evidence_sha256=row['evidence_sha256'])

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(summarize(a.archive.resolve(),a.output),ensure_ascii=False,indent=2))
