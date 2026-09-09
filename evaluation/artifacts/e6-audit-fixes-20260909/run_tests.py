import os,subprocess,json,time
from pathlib import Path
r=Path(__file__).parent;clone=r/'test-copy';env={**os.environ,'PYTHONPATH':str(clone/'evaluation/src')+':'+str(clone/'backend'),'PYTHONDONTWRITEBYTECODE':'1'}
cmd=['/root/workspace/tencent_rhinobird2026/learning_travel/.venv/bin/python','-m','pytest','-q','evaluation/tests/test_e6_audit_fixes.py','evaluation/tests/test_e6_final_repairs.py','evaluation/tests/test_e6_repairs.py','tests/test_e6_deadline_constraints.py','tests/test_planning_card_snapshots.py','tests/test_learning_loop.py::test_planning_intake_requires_readiness_and_proposal_accept_is_idempotent','--basetemp',str(r/'pytest-temp')]
start=time.time()
with (r/'tests-first.log').open('w') as f:result=subprocess.run(cmd,cwd=clone,env=env,stdout=f,stderr=subprocess.STDOUT)
(r/'tests-first.json').write_text(json.dumps({'command':cmd,'exit_code':result.returncode,'seconds':time.time()-start},indent=2));print(result.returncode)
