"""Run source-bound quality scoring with a content audit and recorded recovery."""
from __future__ import annotations
import argparse,dataclasses,json
from pathlib import Path
from learning_agent_eval.learning_quality import METHOD,METHOD_SHA256,request_for,validate_rating,aggregate,schedule_facts,reading_view,catalog,effective_levels,undo_version_facts,exponential_facts
from learning_agent_eval.quality_content_audit import request as audit_request,validate as validate_audit
from learning_agent_eval.active_judge import OpenAICompatibleHy3JudgeProviderV3
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from learning_agent_eval.e3_io import current_git_commit
from learning_agent_eval.runtime_metadata import git_worktree_clean,dependency_environment_reason_codes

MAX_ATTEMPTS=3
class QualityProvider(OpenAICompatibleHy3JudgeProviderV3):
    config_document={**OpenAICompatibleHy3JudgeProviderV3.config_document,'max_tokens':16000,'timeout_seconds':300}

def complete_stage(provider,request,e,validate,identity,stage,checkpoint):
    attempts=[]
    for number in range(1,MAX_ATTEMPTS+1):
        req={**request,'_budget_call_id':f'{identity}:{stage}:{number}'}
        attempt=dict(number=number,request_sha256=sha256_digest(req),status='in_flight')
        attempts.append(attempt);checkpoint(attempts)
        reply=provider.complete(req)
        attempt.update(status='invalid',reply=dataclasses.asdict(reply))
        if reply.finish_reason=='length':
            provider.budget.record_outcome(reply.budget_ticket,outcome='output_truncated')
            attempt['validation_error']='output_truncated'
        elif reply.status=='completed':
            try:payload=validate(json.loads(reply.content),e)
            except (ValueError,TypeError) as exc:attempt['validation_error']=type(exc).__name__+': '+str(exc)[:500]
            else:
                attempt.update(status='complete',payload=payload)
                checkpoint(attempts);return payload,attempts
        checkpoint(attempts)
    return None,attempts

def run(suite,output,ledger,ids=None,repeats=1,method_version='learning-quality-8',repeat_indices=None):
    from learning_agent_eval import learning_quality as method
    stage_audit_request = audit_request
    if method_version == 'learning-quality-9':
        from learning_agent_eval import learning_quality_v9 as method
        stage_audit_request = method.audit_request
    elif method_version == 'learning-quality-10':
        from learning_agent_eval import learning_quality_v10 as method
        stage_audit_request = method.audit_request
    elif method_version == 'learning-quality-11':
        from learning_agent_eval import learning_quality_v11 as method
        stage_audit_request = method.audit_request
    elif method_version != 'learning-quality-8':
        raise ValueError('unsupported quality method')
    if output.exists():raise FileExistsError(output)
    if not git_worktree_clean() or dependency_environment_reason_codes():raise ValueError('frozen clean source and locked dependencies required')
    specs=json.loads(suite.read_text());output.mkdir(parents=True)
    provider=QualityProvider(budget_ledger=ledger,scope=output.name)
    manifest={'method':method.METHOD,'method_sha256':method.METHOD_SHA256,'source_commit':current_git_commit(),
        'suite_sha256':sha256_digest(specs),'attempt_policy':{'max_attempts_per_stage':MAX_ATTEMPTS,'selection':'first valid; retry only transport/format failure; no score-dependent retry'},'rows':[]}
    def save():
        p=output/'results.json';tmp=output/'results.pending';tmp.write_bytes(canonical_json_bytes(manifest));tmp.replace(p)
    for rep in range(1,repeats+1):
        if repeat_indices is not None and rep not in repeat_indices:continue
        for spec in specs:
            if ids and spec['id'] not in ids:continue
            row=dict(id=spec['id'],repeat=rep,track=spec['track'],kind=spec['kind'],status='runtime_failure')
            manifest['rows'].append(row)
            if spec.get('evidence_file'):
                e=json.loads((suite.parent/spec['evidence_file']).read_text())
                if sha256_digest(e)!=spec['evidence_sha256']:raise ValueError('input changed')
                row.update(status='in_progress',evidence_sha256=spec['evidence_sha256'])
                def checkpoint(stage,attempts):row[stage+'_attempts']=attempts;save()
                identity=f"{spec['id']}:repeat:{rep}"
                audit,attempts=complete_stage(provider,stage_audit_request(e,reading_view,catalog),e,validate_audit,identity,'audit',lambda a:checkpoint('audit',a))
                row['status']='in_progress'
                if audit is not None:
                    row['content_audit']=audit
                    challenge=None
                    if hasattr(method,'challenge_request'):
                        challenge,attempts=complete_stage(provider,method.challenge_request(e),e,method.validate_challenge,identity,'challenge',lambda a:checkpoint('challenge',a))
                        if challenge is not None:row['condition_challenge']=challenge
                    rating=None
                    if not hasattr(method,'challenge_request') or challenge is not None:
                        request=method.request_for(e,audit,challenge) if hasattr(method,'challenge_request') else method.request_for(e,audit)
                        rating,attempts=complete_stage(provider,request,e,method.validate_rating,identity,'rating',lambda a:checkpoint('rating',a))
                    if rating is not None:
                        row.update(status='complete',rating=rating,effective_dimensions=method.effective_levels(rating,e),undo_version_facts=undo_version_facts(e),exponential_facts=exponential_facts(e),schedule_facts=schedule_facts(e),**method.aggregate(rating,e))
                        if hasattr(method, 'reconciliation_result'):
                            row.update(method.reconciliation_result(rating,e))
                if row['status']=='in_progress':row['status']='judge_error'
                row['result_sha256']=sha256_digest(row)
            save();print(row['id'],rep,row['status'],row.get('score'),flush=True)
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--suite',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--budget-ledger',type=Path,required=True);p.add_argument('--case-id',action='append');p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--method',choices=['learning-quality-8','learning-quality-9','learning-quality-10','learning-quality-11'],default='learning-quality-8')
    p.add_argument('--repeat-index',type=int,action='append',help='Restrict to declared repeat indices for failure-only recovery');a=p.parse_args()
    if a.repeats<1:p.error('repeats must be positive')
    if a.repeat_index and any(i<1 or i>a.repeats for i in a.repeat_index):p.error('repeat index outside declared range')
    run(a.suite,a.output,a.budget_ledger,a.case_id,a.repeats,a.method,a.repeat_index)
