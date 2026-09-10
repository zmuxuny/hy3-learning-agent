import os,sys,json,subprocess,concurrent.futures
from pathlib import Path
from dotenv import dotenv_values
w=Path('/tmp/learning-travel-method9-location').read_text();w=Path(w)
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=w/'source'
env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:dotenv_values(root/'.env')[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
phase=sys.argv[1];suite=Path(sys.argv[2]);repeats=int(sys.argv[3]);method=sys.argv[4] if len(sys.argv)>4 else 'learning-quality-9'
rows=json.loads(suite.read_text());target=w/phase;target.mkdir()
if phase=='development-refined':rows=[r for r in rows if r['id'] in ['quality-01-3','quality-03-1','quality-03-3','quality-04-3','quality-08-2']]
elif phase.startswith('development'):rows=[r for r in rows if r['id'].endswith(('-1','-3'))]
(target/'execution-design.json').write_text(json.dumps(dict(phase=phase,suite=str(suite),ids=[r['id'] for r in rows],repeats=repeats,method=method,concurrency=4,selection='first valid only; all errors retained'),indent=2))
def run(r):
 cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(suite),'--output',str(target/r['id']),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json','--case-id',r['id'],'--repeats',str(repeats),'--method',method]
 with (target/(r['id']+'.txt')).open('w') as log:p=subprocess.run(cmd,cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 print(r['id'],'exit',p.returncode,flush=True)
 return p.returncode
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:codes=list(pool.map(run,rows))
sys.exit(int(any(codes)))
