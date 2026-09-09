"""Frozen scenario-quality experiment runner; replays evidence, never runs products."""
from __future__ import annotations
import argparse, dataclasses, json
from pathlib import Path
from learning_agent_eval.learning_quality import METHOD,METHOD_SHA256,request_for,validate_rating,aggregate
from learning_agent_eval.active_judge import OpenAICompatibleHy3JudgeProviderV3
from learning_agent_eval.canonical import canonical_json_bytes,sha256_digest
from learning_agent_eval.e3_io import current_git_commit
from learning_agent_eval.runtime_metadata import git_worktree_clean,dependency_environment_reason_codes

class QualityProvider(OpenAICompatibleHy3JudgeProviderV3):
    config_document={**OpenAICompatibleHy3JudgeProviderV3.config_document,'max_tokens':8192}

def run(suite,output,ledger,ids=None,repeats=1):
    if output.exists():raise FileExistsError(output)
    if not git_worktree_clean() or dependency_environment_reason_codes():
        raise ValueError('frozen clean source and locked dependencies required')
    specs=json.loads(suite.read_text())
    output.mkdir(parents=True)
    provider=QualityProvider(budget_ledger=ledger,scope=output.name)
    manifest={'method':METHOD,'method_sha256':METHOD_SHA256,'source_commit':current_git_commit(),
              'suite_sha256':sha256_digest(specs),'rows':[]}
    for rep in range(1,repeats+1):
        for spec in specs:
            if ids and spec['id'] not in ids:continue
            row=dict(id=spec['id'],repeat=rep,track=spec['track'],kind=spec['kind'],status='runtime_failure')
            if spec.get('evidence_file'):
                e=json.loads((suite.parent/spec['evidence_file']).read_text())
                if sha256_digest(e)!=spec['evidence_sha256']:raise ValueError('input changed')
                req=request_for(e);req['_budget_call_id']=f"{spec['id']}:repeat:{rep}"
                row.update(evidence_sha256=spec['evidence_sha256'],request_sha256=sha256_digest(req))
                # Exactly one provider attempt per fixed slot, no hidden retries.
                reply=provider.complete(req)
                payload=None
                row.update(status='judge_error',reply=dataclasses.asdict(reply))
                if reply.status=='completed':
                    try:
                        payload=validate_rating(json.loads(reply.content),e)
                    except (ValueError,TypeError) as exc:
                        row['validation_error']=type(exc).__name__+': '+str(exc)[:300]
                    else:row.update(status='complete',rating=payload,**aggregate(payload))
                row['result_sha256']=sha256_digest(row)
            manifest['rows'].append(row)
            (output/'results.json').write_bytes(canonical_json_bytes(manifest))
            print(row['id'],rep,row['status'],row.get('score'),flush=True)
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suite',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--budget-ledger',type=Path,required=True);p.add_argument('--case-id',action='append')
    p.add_argument('--repeats',type=int,default=1);a=p.parse_args()
    if a.repeats<1:p.error('repeats must be positive')
    run(a.suite,a.output,a.budget_ledger,a.case_id,a.repeats)
