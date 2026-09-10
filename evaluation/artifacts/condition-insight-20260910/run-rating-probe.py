import json,os,sys,subprocess,concurrent.futures,dataclasses
from pathlib import Path
from dotenv import dotenv_values
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');src=Path('/tmp/learning-travel-condition-structured');os.chdir(src);sys.path[:0]=[str(src/'evaluation/src'),str(src/'evaluation/scripts'),str(src/'backend')]
from learning_agent_eval import learning_quality_v11 as method
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from run_learning_quality import QualityProvider,complete_stage
config=dotenv_values(root/'.env');os.environ.update({k:config[k] for k in ('OPENAI_API_KEY','OPENAI_API_BASE','MODEL_NAME')})
ep=src/'evaluation/artifacts/decisionbench-study-20260910/evidence/full/evidence/formal-i10-p.json';e=json.loads(ep.read_text());original=json.loads(Path('/tmp/stage3-condition-structured-development/formal-i10-p/results.json').read_text())['rows'][0];assert original['status']=='complete'
control=original['condition_challenge'];verified=json.loads(json.dumps(control));calculated=[sum([1,2][i]*[2][k-i] for i in range(2) if 0<=k-i<1) for k in range(2)];assert calculated==[2,4]
for c in verified['checks']:
 if c['unit_id']=='unit-003':
  c.update(statement='单元素卷积保持原序列',explicit_conditions='该边界任务只限定另一序列为单元素，没有限定元素值为1；前面任务给[1]这一特例。',probe_input='a=[1,2], b=[2]；b长度为1，满足单元素前提。',derivation='y[0]=1*2=2，y[1]=2*2=4，输出[2,4]不等于原序列[1,2]。',verdict='error',explanation='后面的独立边界任务遗漏元素为1的条件；一个正确[1]例子不能支持任意单元素的保持性质。此反例经直接计算核对。')
method.validate_challenge(verified,e)
out=Path('/tmp/stage3-insight-probe');out.mkdir();design={'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'method_sha256':method.METHOD_SHA256,'evidence_sha256':sha256_digest(e),'fixed_audit':original['content_audit'],'control_challenge':control,'verified_challenge':verified,'calculated_counterexample':calculated,'variants':['automatic_probe','verified_counterexample'],'repeats':3,'purpose':'Diagnostic component experiment on known convolution case; same evidence and content audit, change only condition challenge finding; not an unseen accuracy estimate','attempt_policy':'3 format/transport attempts per rating, first valid, no score-dependent retries'};(out/'design.json').write_bytes(canonical_json_bytes(design))
def run(job):
 variant,rep=job;challenge=control if variant=='automatic_probe' else verified;folder=out/f'{variant}-{rep}';folder.mkdir();provider=QualityProvider(budget_ledger=Path('/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json'),scope=f'insight-{variant}-{rep}');row={'variant':variant,'repeat':rep,'status':'in_progress'}
 def checkpoint(attempts):row['rating_attempts']=attempts;(folder/'result.json').write_bytes(canonical_json_bytes(row))
 req=method.request_for(e,original['content_audit'],challenge);rating,attempts=complete_stage(provider,req,e,method.validate_rating,f'insight-{variant}:repeat:{rep}','rating',checkpoint)
 row.update(status='complete' if rating else 'judge_error')
 if rating:row.update(rating=rating,effective_dimensions=method.effective_levels(rating,e),**method.aggregate(rating,e),**method.reconciliation_result(rating,e))
 row['result_sha256']=sha256_digest(row);(folder/'result.json').write_bytes(canonical_json_bytes(row));print(variant,rep,row['status'],row.get('score'),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p:list(p.map(run,[(v,i) for v in design['variants'] for i in range(1,4)]))
