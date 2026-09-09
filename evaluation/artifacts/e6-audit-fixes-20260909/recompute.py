import subprocess,os,json,hashlib
from pathlib import Path
r=Path(__file__).parent;repo=Path('/root/workspace/tencent_rhinobird2026/learning_travel');rows=[]
for part,commit,version in [('formal','83fbf52','1.9'),('formal113','35e77cf','1.13')]:
 clone=r/(part+'-recompute-copy');subprocess.run(['git','clone','--quiet','--no-hardlinks',str(repo),str(clone)],check=True);subprocess.run(['git','checkout','--quiet','--detach',commit],cwd=clone,check=True)
 assert not (clone/'.env').exists()
 env={**os.environ,'PYTHONPATH':str(clone/'evaluation/src')+':'+str(clone/'backend'),'PYTHONDONTWRITEBYTECODE':'1'}
 cmd=[str(repo/'.venv/bin/python'),'evaluation/scripts/summarize_e6_run.py','--dataset',f'evaluation/datasets/decisionbench-v{version}-e6-final-test','--run',str(r/'frozen-evidence'/part),'--output',str(r/(part+'-recomputed'))]
 with (r/(part+'-recompute.log')).open('w') as log:p=subprocess.run(cmd,cwd=clone,env=env,stdout=log,stderr=subprocess.STDOUT)
 row={'part':part,'frozen_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=clone,text=True).strip(),'command':cmd,'exit_code':p.returncode,'files':[]}
 assert p.returncode==0
 for name in ['summary.json','cases.csv']:
  a=(r/(part+'-recomputed')/name).read_bytes();b=(r/'frozen-evidence'/part/'report'/name).read_bytes();assert a==b;row['files'].append({'name':name,'sha256':hashlib.sha256(a).hexdigest(),'byte_equal':True})
 rows.append(row)
(r/'frozen-recompute-verification.json').write_text(json.dumps(rows,indent=2));print('H and I byte equal using frozen checkouts with Git history')
