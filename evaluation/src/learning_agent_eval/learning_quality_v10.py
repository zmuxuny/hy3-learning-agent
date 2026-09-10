"""Condition-aware content verification; prior methods retain their frozen identity."""
from copy import deepcopy
from . import learning_quality_v9 as previous
from .canonical import sha256_digest

CONDITIONS = '''结论适用条件核验：逐份任务检查正文、验收条件和总结语句，不用前面的正确示例代替对后面一般结论的验证。对性质、不变量、等价、保持、全部、总是等断言，按以下顺序在verification中写出实际推演：
1. 原句与量词范围：该句指特定例子，还是所有满足某条件的输入。先从同一任务的上下文收集明确限定，不凭空补上能使断言成立的前提。
2. 前提与结论：列出已经声明的输入范围及声称的性质。构造至少一个仍满足这些已声明前提、但不同于原示例的最小输入，实际推演公式或操作，检验结论。需要时再检查零、负数、空输入、重复项、单位元、边界或顺序；只检查本断言适用的类型。
3. 判定依据：有反例时记录输入、算式/步骤、实际结果与所声称性质的矛盾，并标error；未找到反例不等于证明所有情况正确，写明实际检查的范围。已明确限定到成立条件的结论、仅要求探索性质的练习、条件式建议不可按无条件断言误扣。
4. 范围一致性：前面的窄条件不能自动扩展到后面独立任务，也不能用一个正确示例替代整份任务中的不变量或验收规则；局部错误按它对学习与验收的实际影响评分。
负荷与目标核验：对拆分、缩短、等量、保持等调整，列出原任务时长、保留时长、新增任务时长、修改后合计及最长单段。周预算合规只证明上限未超，不能证明任务变短或等量。说明了新总数不等于说明新增负担；核对“重新分配/拆分”等措辞与这些数值是否一致。
归因核验：最终评分需要明确采用或驳回已找到的具体反例；不可仅因待批、预算合规、其他示例正确而判所有内容无缺陷。教学步骤只提出准备知识时，不等于断言学习者从未学过；没有调用证据的虚假完成不等于真实违规尝试；脱敏前后引用号形式不同不等于事实矛盾。'''

METHOD=deepcopy(previous.METHOD)
METHOD['version']='learning-quality-10'
METHOD['condition_verification']=CONDITIONS
METHOD['instructions']+='\n'+CONDITIONS
METHOD['content_audit']['instructions']+='\n'+CONDITIONS
METHOD['content_audit']['version']='focused-content-audit-5'
METHOD_SHA256=sha256_digest(METHOD)

def _extend(request):
    request['messages'][0]['content']+='\n'+CONDITIONS
    return request

def request_for(e,content_audit=None):return _extend(previous.request_for(e,content_audit))
def audit_request(e,reading_view,catalog):return _extend(previous.audit_request(e,reading_view,catalog))
validate_rating=previous.validate_rating
effective_levels=previous.effective_levels
aggregate=previous.aggregate
reconciliation_result=previous.reconciliation_result
