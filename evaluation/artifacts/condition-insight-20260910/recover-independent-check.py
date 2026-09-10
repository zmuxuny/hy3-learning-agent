import os,json,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=Path('/tmp/learning-travel-condition-structured');base=Path('/tmp/stage3-condition-structured-development');design=json.loads((base/'design.json').read_text());config=dotenv_values(root/'.env');env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:config[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
suite=src/'evaluation/artifacts/decisionbench-study-20260910/evidence/full/suite.json'
assert suite.exists(),suite
(base/'recovery-amendment.json').write_text(json.dumps({'recorded_at':datetime.now(timezone.utc).isoformat(),'authorization':'作者：api不太稳定，你多试试','scope':'Failure-only recovery, same frozen method/evidence/repeat, first valid; original failures retained','result_batches':['recovery','recovery-2','recovery-3']},ensure_ascii=False,indent=2)+'\n')
for cycle in range(1,4):
 batch='recovery' if cycle==1 else f'recovery-{cycle}';d=json.loads((base/'formal-i11-r/results.json').read_text());latest={r['repeat']:r for r in d['rows']}
 for prior in ['recovery']+[f'recovery-{i}' for i in range(2,cycle)]:
  p=base/prior/'formal-i11-r/results.json'
  if p.exists():
   for r in json.loads(p.read_text())['rows']:latest[r['repeat']]=r
 bad=[i for i,r in latest.items() if r['status']!='complete']
 if not bad:break
 out=base/batch/'formal-i11-r';out.parent.mkdir(exist_ok=True)
 cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(suite),'--case-id','formal-i11-r','--repeats','3','--method','learning-quality-11','--output',str(out),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json']
 for rep in bad:cmd+=['--repeat-index',str(rep)]
 with (out.parent/'formal-i11-r.log').open('w') as log:r=subprocess.run(cmd,cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 print(cycle,bad,r.returncode,flush=True)
 if r.returncode:break
 if any(r['status']!='complete' for r in json.loads((out/'results.json').read_text())['rows']):time.sleep(30)
