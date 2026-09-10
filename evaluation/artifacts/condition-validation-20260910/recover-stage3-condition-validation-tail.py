import os,json,subprocess
from pathlib import Path
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=Path(Path('/tmp/learning-travel-final-study-location').read_text())/'source';suite=root/'evaluation/datasets/decisionbench-learning-v1/extensions/condition-validation-20260910/suite.json';base=Path('/tmp/stage3-condition-validation');config=dotenv_values(root/'.env');env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:config[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
for p in sorted(base.glob('condition-06-*/results.json')):
 d=json.loads(p.read_text());bad=[r['repeat'] for r in d['rows'] if r['status']!='complete']
 if not bad:continue
 identity=p.parent.name;out=base/'recovery'/identity
 if out.exists():continue
 out.parent.mkdir(exist_ok=True)
 cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(suite),'--case-id',identity,'--repeats','3','--method','learning-quality-9','--output',str(out),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json']
 for rep in bad:cmd+=['--repeat-index',str(rep)]
 with (out.parent/(identity+'.log')).open('w') as log:r=subprocess.run(cmd,cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 print(identity,bad,r.returncode,flush=True)
 if r.returncode or any(x['status']!='complete' for x in json.loads((out/'results.json').read_text())['rows']):
  print('Stopped after unsuccessful recovery; remaining inputs not attempted.',flush=True);break
