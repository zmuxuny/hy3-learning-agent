"""Offline verification from the actual delivery tar; never loads credentials."""
import hashlib,json,os,subprocess,tarfile,sys
from pathlib import Path
artifact=Path(__file__).parent
work=Path(sys.argv[1]);source=Path(sys.argv[2]);python=sys.argv[3]
evidence=work/'delivered-evidence';evidence.mkdir()
archive=artifact/'public-evidence.tar.gz'
with tarfile.open(archive) as tar:
 for member in tar.getmembers():
  if member.isfile() and member.name.startswith(('development/','calibration/')):
   dest=evidence/member.name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(tar.extractfile(member).read())
rows=[];env={**os.environ,'PYTHONPATH':str(source/'evaluation/src')+':'+str(source/'backend'),'PYTHONDONTWRITEBYTECODE':'1'}
for batch in ['development','calibration']:
 output=work/('delivered-recompute-'+batch)
 cmd=[python,'evaluation/scripts/summarize_e6_run.py','--dataset',f'evaluation/datasets/decisionbench-v1.15-validation/{batch}','--run',str(evidence/batch),'--output',str(output)]
 with (artifact/(batch+'-delivered-recompute.log')).open('x') as log:p=subprocess.run(cmd,cwd=source,env=env,stdout=log,stderr=subprocess.STDOUT)
 assert p.returncode==0
 for name in ['summary.json','cases.csv']:
  original=(evidence/batch/'report'/name).read_bytes();assert original==(output/name).read_bytes();rows.append({'batch':batch,'file':name,'sha256':hashlib.sha256(original).hexdigest(),'byte_equal':True})
(artifact/'delivered-recompute-verification.json').write_text(json.dumps({'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),'paid_calls':0,'rows':rows},indent=2)+'\n')
print('delivered archive reports byte-equal')
