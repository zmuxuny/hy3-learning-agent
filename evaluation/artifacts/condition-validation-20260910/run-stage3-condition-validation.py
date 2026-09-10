import os,json,subprocess,concurrent.futures,shutil
from pathlib import Path
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=Path(Path('/tmp/learning-travel-final-study-location').read_text())/'source';suite=root/'evaluation/datasets/decisionbench-learning-v1/extensions/condition-validation-20260910/suite.json';out=Path('/tmp/stage3-condition-validation');out.mkdir();shutil.copyfile(suite.parent/'design.json',out/'design.json')
config=dotenv_values(root/'.env');env=dict(os.environ,PYTHONPATH='evaluation/src:backend',**{k:config[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
def run(spec):
 with (out/(spec['id']+'.log')).open('w') as log:r=subprocess.run([str(root/'.venv/bin/python'),'evaluation/scripts/run_learning_quality.py','--suite',str(suite),'--case-id',spec['id'],'--repeats','3','--method','learning-quality-9','--output',str(out/spec['id']),'--budget-ledger','/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json'],cwd=src,env=env,stdout=log,stderr=subprocess.STDOUT)
 print(spec['id'],r.returncode,flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p:list(p.map(run,json.loads(suite.read_text())))
