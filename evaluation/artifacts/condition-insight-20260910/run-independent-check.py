import os,json,subprocess,concurrent.futures
from pathlib import Path
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=Path('/tmp/learning-travel-condition-structured');out=Path('/tmp/stage3-condition-structured-development');out.mkdir()
env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:dotenv_values(root/'.env')[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
ids=['formal-i10-p','formal-i11-r']
(out/'design.json').write_text(json.dumps({'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=src,text=True).strip(),'method':'learning-quality-11','development_cases':ids,'repeats':3,'purpose':'Known content misses plus two known normal/attribution controls; development only, not unseen effectiveness evidence','selection':'First valid; max3 transport/format attempts per stage; no score based retries'},ensure_ascii=False,indent=2)+'\n')
def run(identity):
 with (out/(identity+'.log')).open('w') as log:
  p=subprocess.run([str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite','evaluation/artifacts/decisionbench-study-20260910/evidence/full/suite.json','--case-id',identity,'--repeats','3','--method','learning-quality-11','--output',str(out/identity),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json'],cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 print(identity,p.returncode,flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as p:list(p.map(run,ids))
