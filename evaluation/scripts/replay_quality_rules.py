"""Hold source observations and original model ratings fixed; change only rules."""
import argparse,json
from pathlib import Path
from summarize_decisionbench_final import load_study
from learning_agent_eval import learning_quality_v9 as new


def replay(root,output):
    _,study=load_study(root)
    specs={s['id']:s for s in json.loads((root/'validation/suite.json').read_text())};rows=[]
    for (identity,rep),r in study['validation'].items():
        e=json.loads((root/'validation'/specs[identity]['evidence_file']).read_text());a=new.aggregate(r['rating'],e)
        rows.append(dict(id=identity,repeat=rep,source_result_sha256=r['result_sha256'],old_score=r['score'],new_score=a['score'],
                         old_outcome=r['outcome'],new_outcome=a['outcome'],new_critical=bool(new.critical_findings(e)),changed=r['score']!=a['score']))
    result=dict(experiment='same original evidence and exact same raw rating; only deterministic postprocessing changes; no new model calls',new_method_sha256=new.METHOD_SHA256,rows=rows)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();replay(a.evidence,a.output)
