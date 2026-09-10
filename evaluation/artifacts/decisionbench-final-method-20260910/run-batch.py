import os,json,subprocess,concurrent.futures
from pathlib import Path
from dotenv import dotenv_values
w=Path(Path('/tmp/learning-travel-final-study-location').read_text());root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=w/'source';design=json.loads((w/'design.json').read_text())
configuration=dotenv_values(root/'.env');env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:configuration[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
jobs=[]
for part,d in design['parts'].items():
 suite=src/d['input'];target=w/'runs'/part;target.mkdir(parents=True)
 for spec in json.loads(suite.read_text()):jobs.append((part,d,suite,target,spec))
def run(item):
 part,d,suite,target,spec=item
 cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(suite),'--output',str(target/spec['id']),'--case-id',spec['id'],'--repeats',str(d['repeats']),'--method',design['method'],'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json']
 with (target/(spec['id']+'.log')).open('w') as log:p=subprocess.run(cmd,cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 result=json.loads((target/spec['id']/'results.json').read_text()) if (target/spec['id']/'results.json').exists() else {}
 print(part,spec['id'],p.returncode,[(r['repeat'],r['status'],r.get('score')) for r in result.get('rows',[])],flush=True)
 return p.returncode
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:codes=list(pool.map(run,jobs))
print('COMPLETE',len(codes),'runs','exit errors',sum(bool(c) for c in codes),flush=True)
