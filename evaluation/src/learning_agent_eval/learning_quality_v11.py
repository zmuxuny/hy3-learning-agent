"""Independent condition challenge plus full-evidence content and decision scoring."""
import json
from copy import deepcopy
from . import learning_quality_v10 as previous
from . import condition_challenge as challenge
from .canonical import canonical_json,sha256_digest

INSTRUCTIONS='''independent_condition_checks是独立命题核验，不是事实根。逐项核对其原句、明确前提和实际推演：成立的反例必须反映到相关维度，不能以待审、预算合规或其他示例正确掩盖错误教学/验收要求；不成立的反例应说明其违反哪项已声明前提。checks引用其中附带的原始evidence_paths。若缺失条件使教学任务或验收要求错误，按内容与可用性评估其影响；任务待审不豁免内容缺陷。保留独立核验与评分的分歧，不因结论不同丢弃核验结果。'''
METHOD=deepcopy(previous.METHOD)
METHOD.update(version='learning-quality-11',condition_challenge=challenge.CONFIG)
METHOD['instructions']+='\n'+INSTRUCTIONS
METHOD_SHA256=sha256_digest(METHOD)

def request_for(e,content_audit=None,condition_challenge=None):
    req=previous.request_for(e,content_audit)
    req['messages'][0]['content']+='\n'+INSTRUCTIONS
    payload=json.loads(req['messages'][1]['content'])
    payload['independent_condition_checks']=challenge.with_sources(condition_challenge,e) if condition_challenge is not None else []
    req['messages'][1]['content']=canonical_json(payload)
    return req

audit_request=previous.audit_request
challenge_request=challenge.request
validate_challenge=challenge.validate
validate_rating=previous.validate_rating
effective_levels=previous.effective_levels
aggregate=previous.aggregate
reconciliation_result=previous.reconciliation_result
