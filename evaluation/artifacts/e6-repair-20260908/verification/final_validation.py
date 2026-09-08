import hashlib,json,shutil,subprocess
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
r=Path('/tmp/learning-e6-repair-hoxki28l');repo=Path('/root/workspace/tencent_rhinobird2026/learning_travel');o=repo/'evaluation/artifacts/e6-repair-20260908';sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();load=lambda p:json.loads(p.read_text());checks=[]
def check(name,ok,details=None):
 checks.append({'check':name,'passed':bool(ok),'details':details});assert ok,name
m=load(r/'archive-extracted/evidence-manifest.json');check('archive_manifest_all_files',all(sha(r/'archive-extracted'/x['relative_path'])==x['sha256'] for x in m['files']),{'count':len(m['files'])})
for f in ['summary.json','cases.csv']:check('frozen_1_4_archive_report_reproduction_'+f,(r/'report-from-archive'/f).read_bytes()==(o/f).read_bytes(),{'sha256':sha(o/f)})
readiness=load(o/'method-readiness.json')
for f,digest in readiness['evidence_files'].items():check('preformal_method_decision_binding_'+f,sha(r/'archive-extracted'/f)==digest)
s=load(o/'summary.json');check('formal_false_and_original_blockers',s['formal_capability_result'] is False and set(s['capability_blockers'])=={'capability.judge_error','capability.run_not_trusted'})
rows=s['cases'];check('case_denominators',len(rows)==48 and Counter(x['track'] for x in rows)==dict.fromkeys(['planning','intervention','assessment','revision'],12));check('all_outcomes_preserved',Counter(x['reviewed_outcome'] for x in rows)=={'pass':15,'fail':18,'judge_error':15});check('null_scores_preserved',all(x['reviewed_score'] is None for x in rows if x['status']=='judge_error'))
judges=[load(p) for p in (r/'formal/judge/judge-results').glob('*.json')];print('Judge errors',Counter(x.get('error_code') for x in judges if x['status']=='judge_error'))
check('actual_judge_attempt_denominator',len((r/'formal/judge-attempts.jsonl').read_text().splitlines())==48)
b=load(o/'budget-audit.json');actual=Path(b['ledger_path']);check('ledger_matches_final_snapshot',sha(actual)==b['ledger_sha256']);check('all_417_paid_calls_mapped',b['artifact_matched_requests']==417 and not b['errors'] and not b['unmatched_requests']);check('budget_history_authorization_preserved',b['historical_490_unchanged'] and b['authorization_unchanged']);check('actual_budget_limit_and_remaining',b['limit_micro_cny']==34609982 and b['occupied_micro_cny']==34393029 and b['remaining_micro_cny']==216953)
cv=load(o/'candidate-v5-verification.json');check('79_lossless_candidate_requests',cv['requests']==79 and cv['all_equivalent'] and cv['frozen_input_rejections']==8 and cv['candidate_input_rejections']==0);check('candidate_real_runs_not_claimed',cv['real_candidate_judge_calls']==0 and cv['formal'] is False);check('next_corrected_request_reservation_unaffordable',cv['smallest_corrected_failed_request_reservation_micro_cny']>cv['remaining_micro_cny'])
ex=load(r/'logs/offline-v5-regression-fixed.execution.json');check('candidate_external_regression',ex['exit_code']==0 and ex['source_commit'].startswith('3d20c8b') and '45 passed' in (r/'logs/offline-v5-regression-fixed.log').read_text())
check('candidate_protocol_registry_readonly_verify',load(r/'logs/candidate-final-release-verify.execution.json')['exit_code']==0)
p=load(o/'public-evidence-review.json');check('public_artifacts_privacy_review',p['completed'] and p['unresolved_sensitive_findings']==0 and p['exact_synthetic_matches_reviewed']==35)
pi=load(r/'formal-ai-review/pointer-verification.json');check('content_audit_paths_and_sources',pi['passed'] and pi['checked_paths']==914 and pi['checked_source_files']==144 and sha(o/'content-audit.json')==pi['audit_sha256'])
h=load(o/'historical-preservation.json');check('historical_artifacts_and_releases_unchanged',all(x['all_git_blob_bytes_identical'] for x in h['checks']),h['checks'])
(o/'verification').mkdir(exist_ok=True)
for f in ['formal-report-from-archive.log','formal-report-from-archive.execution.json','candidate-final-release-verify.log','candidate-final-release-verify.execution.json']:shutil.copy2(r/'logs'/f,o/'verification'/f)
report={'schema':'e6-repair-final-validation-v1','created_at':datetime.now(timezone.utc).isoformat(),'reviewer_role':'primary_ai_reviewer','scope':'Artifact integrity, frozen-version report reproducibility, denominators, budget and specific regression evidence. Not independent human review or capability success.','checks':checks,'checks_passed':len(checks),'checks_failed':0,'archive':load(o/'archive-verification.json'),'formal_capability_result':False,'candidate_v5_real_validation_completed':False,'paid_calls_in_this_verification':0,'cleanup_receipt':'cleanup.json'}
(o/'validation-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print({'checks_passed':len(checks),'checks_failed':0})
