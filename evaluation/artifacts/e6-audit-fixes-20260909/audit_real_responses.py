import json,hashlib
from pathlib import Path
from copy import deepcopy
from learning_agent_eval.active_judge import _validate_payload_v3
from learning_agent_eval.blinding_v2 import build_blind_judge_input_v3
r=Path(__file__).parent;run=r/'frozen-evidence/formal113';rows=[];example=None
for p in (run/'judge-attempts.jsonl').read_text().splitlines():
 a=json.loads(p)
 if a.get('validation_error_codes') or not a.get('public_response'):continue
 eid=a['episode_id'];e=json.loads((run/'runtime/episodes'/f'{eid}.json').read_text());rule=json.loads((run/'rules/rules'/f'{eid}.json').read_text());ref=json.loads((run/'runtime/judge-references'/f'{eid}.json').read_text());blind=build_blind_judge_input_v3(e,rule,ref);payload,errors=_validate_payload_v3(a['public_response'],episode=e,blind_input=blind)
 rows.append({'episode_id':eid,'attempt':a['attempt'],'original_record_sha256':a['record_sha256'],'audit_checks':len(a['public_response']['audit_checks']),'errors':list(errors)})
 assert not errors
 if example is None:example=(e,blind,a['public_response'])
e,blind,original=example;mutations=[]
for name in ['missing','empty','invalid_path']:
 p=deepcopy(original)
 if name=='missing':p.pop('audit_checks')
 elif name=='empty':p['audit_checks']=[]
 else:p['audit_checks'][0]['evidence_paths']=['result.nonexistent_fact']
 value,errors=_validate_payload_v3(p,episode=e,blind_input=blind);assert value is None and errors;mutations.append({'mutation':name,'errors':list(errors)})
result={'scope':'seen original public responses, offline contract validation only; no rescoring or model calls','valid_responses':len(rows),'fact_checks':sum(x['audit_checks'] for x in rows),'rows':rows,'counterexamples':mutations};(r/'real-response-validation.json').write_text(json.dumps(result,indent=2));print({k:v for k,v in result.items() if k!='rows'})
