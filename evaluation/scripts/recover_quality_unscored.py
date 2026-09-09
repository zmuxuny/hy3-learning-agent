"""One declared operational recovery cycle for still-unscored final study slots."""
import argparse,json
from pathlib import Path
from run_learning_quality import QualityProvider,complete_stage
from learning_agent_eval.learning_quality import METHOD_SHA256,request_for,validate_rating,aggregate,schedule_facts,effective_levels,undo_version_facts,exponential_facts,reading_view,catalog
from learning_agent_eval.quality_content_audit import request as audit_request,validate as validate_audit
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from learning_agent_eval.runtime_metadata import git_worktree_clean,dependency_environment_reason_codes
from learning_agent_eval.e3_io import current_git_commit

def recover(root,ledger,shard=0,shards=4):
    if not git_worktree_clean() or dependency_environment_reason_codes():raise ValueError('clean frozen source required')
    policy=json.loads((root/'operational-recovery-design.json').read_text())
    assert policy['max_cycles_per_slot']==1 and policy['method_sha256']==METHOD_SHA256
    provider=QualityProvider(budget_ledger=ledger,scope='quality-unscored-recovery')
    for kind in ['full','validation','adversarial']:
        specs={x['id']:x for x in json.loads((root/kind/'suite.json').read_text())}
        for p in sorted(root.glob(f'{kind}-results-*/results.json')):
            for old in json.loads(p.read_text())['rows']:
                if old['status']!='judge_error':continue
                identity=f"{kind}-{old['id']}-{old['repeat']}"
                if int(sha256_digest(identity)[:8],16)%shards!=shard:continue
                parser=root/'ingestion-recovery'/identity/'results.json'
                if parser.exists():continue
                target=root/'operational-recovery'/identity/'results.json'
                if target.exists():continue
                spec=specs[old['id']];e=json.loads((root/kind/spec['evidence_file']).read_text())
                assert sha256_digest(e)==old['evidence_sha256']
                # A correctable parser-only failure belongs to the prior recovery.
                eligible=False
                for a in old['audit_attempts']:
                    if a.get('validation_error')!='ValueError: every listed assertion must be checked, no invented identifiers':continue
                    try:validate_audit(json.loads(a['reply']['content']),e)
                    except (ValueError,TypeError):continue
                    eligible=True
                if eligible:continue
                row={k:old[k] for k in ['id','repeat','track','kind','evidence_sha256']}
                row.update(status='in_progress',method_sha256=METHOD_SHA256,source_commit=current_git_commit(),
                    original_result_sha256=old['result_sha256'],original_file=str(p.relative_to(root)),recovery='one-unscored-operational-cycle')
                target.parent.mkdir(parents=True)
                def save(stage,attempts):
                    row[stage+'_attempts']=attempts;tmp=target.with_suffix('.pending');tmp.write_bytes(canonical_json_bytes(row));tmp.replace(target)
                audit=old.get('content_audit')
                if audit is None:
                    audit,_=complete_stage(provider,audit_request(e,reading_view,catalog),e,validate_audit,identity+':operational-recovery','audit',lambda a:save('audit',a))
                else:row['reused_original_audit']=True
                if audit is not None:
                    row['content_audit']=audit
                    rating,_=complete_stage(provider,request_for(e,audit),e,validate_rating,identity+':operational-recovery','rating',lambda a:save('rating',a))
                    if rating is not None:row.update(status='complete',rating=rating,effective_dimensions=effective_levels(rating,e),undo_version_facts=undo_version_facts(e),exponential_facts=exponential_facts(e),schedule_facts=schedule_facts(e),**aggregate(rating,e))
                if row['status']=='in_progress':row['status']='judge_error'
                row['result_sha256']=sha256_digest(row);target.write_bytes(canonical_json_bytes(row));print(identity,row['status'],row.get('score'),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',required=True,type=Path);p.add_argument('--budget-ledger',required=True,type=Path);p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=4);a=p.parse_args();recover(a.evidence,a.budget_ledger,a.shard,a.shards)
