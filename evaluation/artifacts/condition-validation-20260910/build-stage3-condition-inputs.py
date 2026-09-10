import json,hashlib,datetime
from pathlib import Path
root=Path('/root/workspace/tencent_rhinobird2026/learning_travel');out=root/'evaluation/datasets/decisionbench-learning-v1/extensions/condition-validation-20260910';(out/'evidence').mkdir(parents=True)
examples=[
('平方后开方的实数范围','planning',{'goal':'准备一份实数平方与开方关系的待审练习，包含正负数。','approval':'仅草案','confirmed_level':'会实数四则运算'},
'草案：对实数x，sqrt(x*x)=abs(x)；在x≥0时才可写成x。先算x=3得到3，再算x=-3得到3，比较两者。提交推演后再审阅，不采用计划。',
'草案：对实数x，sqrt(x*x)=x。先算x=3得到3，验证这个恒等式。后续练习将它作为正负实数都成立的不变量，提交推演后再审阅，不采用计划。',
'x=-3满足实数前提，sqrt(9)=3不等于-3；需要绝对值或非负条件。'),
('有序列表拼接的范围条件','planning',{'goal':'设计两个升序非空列表直接拼接后的性质练习，明确何时仍升序。','approval':'仅草案','confirmed_level':'会列表与大小比较'},
'草案：两个升序非空列表A、B只有在max(A)≤min(B)时，A+B才仍升序。用[1,2]和[3,4]验证成立，再用[1,4]和[2,3]验证缺少条件时不成立。提交结果后等待审阅。',
'草案：两个列表A、B各自升序且非空，A+B就一定仍升序。用[1,2]和[3,4]验证，得到[1,2,3,4]。后续把这项性质作为全部升序非空列表的验收规则，提交结果后等待审阅。',
'[1,4]+[2,3]=[1,4,2,3]，4>2；各自有序不能推出拼接有序。'),
('整数除法的分配条件','planning',{'goal':'给非负整数设计整除2的待审练习，解释先求和再整除与分别整除的关系。','approval':'仅草案','confirmed_level':'会整数加法与向下取整'},
'草案：若a、b均为偶数，则(a+b)//2=a//2+b//2；不能推广到任意非负整数。先代入2、4验证，再代入1、1观察左边1与右边0。提交计算后等待审阅。',
'草案：任意非负整数a、b都有(a+b)//2=a//2+b//2。用2、4计算两边均为3，后续练习把这一恒等式作为验收依据。提交计算后等待审阅。',
'a=b=1，(1+1)//2=1，1//2+1//2=0，整数向下取整不能无条件分配。'),
('合并正确率的权重条件','planning',{'goal':'设计两组练习合并正确率的待审计算任务，组内题数可以不同。','approval':'仅草案','confirmed_level':'会百分数和加权平均'},
'草案：合并正确率等于总答对数除以总题数。两组题数相同时才可直接平均组内正确率。例：1题答对1题与9题答对0题，合并为1/10=10%，不是(100%+0%)/2。提交推演后等待审阅。',
'草案：两组练习的合并正确率总等于两组正确率的算术平均。例：每组10题，分别答对8题和6题，平均为70%，与总正确率相同。即使组内题数不同，后续也统一用这种平均作为验收标准。提交推演后等待审阅。',
'1/1与0/9合并为1/10=10%，未加权平均为50%；需要题数权重。'),
('缩短单段而保持工作量','revision',{'goal':'把40分钟任务拆到每段最多10分钟，总工作量保持40分钟，本次仅提案。','approval':'仅提案','weekly_minutes':120,'tasks':[{'title':'练习','estimated_minutes':40}]},
'建议把原40分钟练习拆成4段，每段10分钟，合计40分钟，最长单段10分钟；原成果保留，等你批准后再修改。',
'建议把原40分钟练习拆成10分钟和30分钟两段，合计40分钟，所以单段已缩短到最多10分钟；原成果保留，等你批准后再修改。',
'总量40正确，但最长单段30>10，等量拆分不代表满足单段上限。'),
('保留任务与新增练习的合计','revision',{'goal':'保持总量40分钟，将两项各20分钟任务的后一项拆成两个10分钟段，仅给建议。','approval':'仅提案','weekly_minutes':100,'tasks':[{'title':'第一项','estimated_minutes':20},{'title':'第二项','estimated_minutes':20}]},
'建议保留第一项20分钟，把第二项20分钟替换成两个10分钟小段；20+10+10=40分钟，原有成果保留。等待批准后执行。',
'建议保留第一项和第二项各20分钟，再新增两个10分钟小段，这是原40分钟的等量重新分配，总负荷不变。原有成果保留，等待批准后执行。',
'保留40再新增20实际60分钟，不是原40的等量重新分配，未超周上限100也不能消除目标违背。')]
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
suite=[];labels=[]
for i,(name,track,state,good,bad,reason) in enumerate(examples,1):
 for j,(condition,text) in enumerate([('good',good),('defect',bad)],1):
  identity=f'condition-{i:02}-{j}';e={'track':track,'trigger':{'objective':state['goal']},'state_before':state,'state_after':{},'observable_trace':{'model_calls':[],'tool_invocations':[],'guard_decisions':[]},'result':{'user_visible_output':text}}
  (out/'evidence'/f'{identity}.json').write_bytes(canonical(e));suite.append(dict(id=identity,kind='constructed-condition-validation',track=track,evidence_file=f'evidence/{identity}.json',evidence_sha256=hashlib.sha256(canonical(e)).hexdigest()));labels.append(dict(id=identity,name=name,condition=condition,expected_outcome='pass' if condition=='good' else 'fail',rationale=reason))
(out/'suite.json').write_bytes(canonical(suite));(out/'private-labels.json').write_bytes(canonical(labels));(out/'design.json').write_text(json.dumps({'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'method_source_commit':'7636e12','method':'learning-quality-10','outputs':12,'repeats':3,'total_slots':36,'suite_sha256':hashlib.sha256(canonical(suite)).hexdigest(),'labels_sha256':hashlib.sha256(canonical(labels)).hexdigest(),'scope':'Constructed after method freeze; not used for prompt development; four teaching condition pairs and two workload condition pairs','success_metrics':'Defect output failed and evidence identifies actual counterexample; normal output passed; all 3 independent repeats retained. Fixed full denominator.','attempt_policy':'max3 per stage, first valid; one recovery cycle for invalid only'},ensure_ascii=False,indent=2)+'\n')
print('12 fixed unseen inputs; 36 planned ratings')
