import os,json,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=Path(Path('/tmp/learning-travel-final-study-location').read_text())/'source';suite=root/'evaluation/datasets/decisionbench-learning-v1/extensions/condition-validation-20260910/suite.json';base=Path('/tmp/stage3-condition-validation');config=dotenv_values(root/'.env');env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:config[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
policy={'recorded_at':datetime.now(timezone.utc).isoformat(),'authorization':'作者：api不太稳定，你多试试','scope':'Only invalid positions; identical method/input/repeat; retain first valid and all failed attempts. No score-dependent reruns.','additional_cycles':3,'cooldown_seconds':30,'result_batches':['runs','recovery','recovery-2','recovery-3','recovery-4']};(base/'recovery-amendment.json').write_text(json.dumps(policy,ensure_ascii=False,indent=2)+'\n')
for cycle in range(2,5):
 failed=False
 for p in sorted(base.glob('*/results.json')):
  if p.parent.name=='recovery':continue
  identity=p.parent.name
  if not identity.startswith('condition-'):continue
  latest={r['repeat']:r for r in json.loads(p.read_text())['rows']}
  first=base/'recovery'/identity/'results.json'
  if not first.exists() or len(json.loads(first.read_text())['rows'])!=sum(r['status']!='complete' for r in latest.values()):continue
  for batch in ['recovery']+[f'recovery-{n}' for n in range(2,cycle)]:
   source=base/batch/identity/'results.json'
   if source.exists():
    for r in json.loads(source.read_text())['rows']:latest[r['repeat']]=r
  if any(r['status']=='in_progress' for r in latest.values()):continue
  bad=[i for i,r in latest.items() if r['status']!='complete']
  if not bad:continue
  out=base/f'recovery-{cycle}'/identity
  if out.exists():continue
  out.parent.mkdir(exist_ok=True)
  cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(suite),'--case-id',identity,'--repeats','3','--method','learning-quality-9','--output',str(out),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json']
  for rep in bad:cmd+=['--repeat-index',str(rep)]
  with (out.parent/(identity+'.log')).open('w') as log:r=subprocess.run(cmd,cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
  print(cycle,identity,bad,r.returncode,flush=True)
  failed=failed or r.returncode!=0 or any(x['status']!='complete' for x in json.loads((out/'results.json').read_text())['rows'])
 if not failed:break
 time.sleep(30)
