import os,json,subprocess
from pathlib import Path
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');w=Path(Path('/tmp/learning-travel-final-study-location').read_text());src=w/'source';design=json.loads((w/'design.json').read_text());config=dotenv_values(root/'.env');env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:config[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
for p in sorted((w/'runs').rglob('results.json')):
 d=json.loads(p.read_text());part=p.parts[-3];spec=design['parts'][part]
 if len(d['rows'])!=spec['repeats'] or any(r['status']=='in_progress' for r in d['rows']):continue
 bad=[r['repeat'] for r in d['rows'] if r['status']!='complete']
 if not bad:continue
 identity=p.parent.name;out=w/'recovery'/part/identity
 if out.exists():continue
 out.parent.mkdir(parents=True,exist_ok=True)
 cmd=[str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(src/spec['input']),'--case-id',identity,'--repeats',str(spec['repeats']),'--method',design['method'],'--output',str(out),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json']
 for rep in bad:cmd+=['--repeat-index',str(rep)]
 with (out.parent/(identity+'.log')).open('w') as log:r=subprocess.run(cmd,cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 print(identity,bad,r.returncode,flush=True)
