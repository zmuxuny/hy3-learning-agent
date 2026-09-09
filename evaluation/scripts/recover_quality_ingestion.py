"""Recover unscored audits rejected solely by surplus descriptive finding labels.

Original request/response files are immutable. Required assertion coverage,
source paths, categories and the grading method remain unchanged. The recovered
record links to its failed original row and adds the previously missing rating.
"""
import argparse,json
from pathlib import Path
from run_learning_quality import QualityProvider,complete_stage
from learning_agent_eval.learning_quality import METHOD_SHA256,request_for,validate_rating,aggregate,schedule_facts,effective_levels,undo_version_facts,exponential_facts
from learning_agent_eval.quality_content_audit import validate as validate_audit
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from learning_agent_eval.runtime_metadata import git_worktree_clean,dependency_environment_reason_codes
from learning_agent_eval.e3_io import current_git_commit

def recover(root,ledger,shard=0,shards=4):
    if not git_worktree_clean() or dependency_environment_reason_codes():raise ValueError('clean frozen source required')
    provider=QualityProvider(budget_ledger=ledger,scope='quality-ingestion-recovery')
    for kind in ['full','validation','adversarial']:
        suite=root/kind/'suite.json'
        if not suite.exists():continue
        specs={x['id']:x for x in json.loads(suite.read_text())}
        for p in sorted(root.glob(f'{kind}-results-*/results.json')):
            data=json.loads(p.read_text());assert data['method_sha256']==METHOD_SHA256
            for old in data['rows']:
                if old['status']!='judge_error':continue
                identity=f"{kind}-{old['id']}-{old['repeat']}"
                if int(sha256_digest(identity)[:8],16)%shards!=shard:continue
                target=root/'ingestion-recovery'/identity/'results.json'
                if target.exists():continue
                spec=specs[old['id']];e=json.loads((suite.parent/spec['evidence_file']).read_text());assert sha256_digest(e)==old['evidence_sha256']
                selected=None
                for attempt in old.get('audit_attempts',[]):
                    # Only the over-strict local coverage error is eligible.
                    if attempt.get('validation_error')!='ValueError: every listed assertion must be checked, no invented identifiers':continue
                    try:audit=validate_audit(json.loads(attempt['reply']['content']),e)
                    except (ValueError,TypeError):continue
                    selected=(attempt,audit);break
                if selected is None:continue
                attempt,audit=selected
                row={k:old[k] for k in ['id','repeat','track','kind','evidence_sha256']}
                row.update(status='in_progress',method_sha256=METHOD_SHA256,source_commit=current_git_commit(),original_result_sha256=old['result_sha256'],
                    original_file=str(p.relative_to(root)),recovery='additional-finding-label-parser-fix',recovered_audit_attempt=attempt['number'],content_audit=audit)
                target.parent.mkdir(parents=True)
                def save(attempts):row['rating_attempts']=attempts;target.write_bytes(canonical_json_bytes(row))
                rating,attempts=complete_stage(provider,request_for(e,audit),e,validate_rating,f"{identity}:ingestion-recovery",'rating',save)
                if rating is None:row['status']='judge_error'
                else:row.update(status='complete',rating=rating,effective_dimensions=effective_levels(rating,e),undo_version_facts=undo_version_facts(e),exponential_facts=exponential_facts(e),schedule_facts=schedule_facts(e),**aggregate(rating,e))
                row['result_sha256']=sha256_digest(row);target.write_bytes(canonical_json_bytes(row));print(identity,row['status'],row.get('score'),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',required=True,type=Path);p.add_argument('--budget-ledger',required=True,type=Path);p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=4);a=p.parse_args();recover(a.evidence,a.budget_ledger,a.shard,a.shards)
