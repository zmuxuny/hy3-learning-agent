import os,json,subprocess,concurrent.futures
from pathlib import Path
from dotenv import dotenv_values
w=Path(Path('/tmp/learning-travel-method9-location').read_text());root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=w/'source'
env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:dotenv_values(root/'.env')[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
suite=json.loads((w/'inputs/suite.json').read_text());pending=[]
for spec in suite:
 rows=json.loads((w/'validation'/spec['id']/'results.json').read_text())['rows']
 assert len(rows)==3 and all(r['status'] in ('complete','judge_error') for r in rows)
 missing=[r['repeat'] for r in rows if r['status']=='judge_error']
 if missing:pending.append((spec['id'],missing))
target=w/'validation-recovery';target.mkdir();(target/'recovery-design.json').write_text(json.dumps(dict(policy='predeclared single extra cycle; only invalid slots, no valid ratings repeated',slots=pending),indent=2))
def run(item):
 identity,reps=item
 cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(w/'inputs/suite.json'),'--output',str(target/identity),'--method','learning-quality-9','--repeats','3','--case-id',identity,'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json']
 for rep in reps:cmd+=['--repeat-index',str(rep)]
 with (target/(identity+'.txt')).open('w') as f:p=subprocess.run(cmd,cwd=src,env=env,stdout=f,stderr=subprocess.STDOUT)
 print(identity,reps,p.returncode,flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(run,pending))
