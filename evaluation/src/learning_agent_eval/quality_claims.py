"""Locate factual assertions and check the product's monotonic undo contract."""
import re
from .judge_request_projection import draft_reading_aid

# The operation endpoint restores business values and increments the current
# plan version. This contract is exercised by the product invariant tests.
UNDO_CONTRACT={'source':'backend/app/api/operations.py:undo_operation','rule':'undo restores prior business values and increments current plan version by one'}

def assertions(e):
    rows=[]
    for draft in draft_reading_aid(e):
        plan=draft['draft_plan']
        for field in ('current_level','background','prerequisites','description','goal','expected_outcome'):
            if plan.get(field):rows.append(dict(kind='draft_'+field,claim=plan[field],evidence_path=draft['evidence_path']))
        for stage in plan.get('stages',[]):
            for task in stage.get('tasks',[]):
                rows.append(dict(kind='task_content_and_acceptance',claim={'title':task.get('title'),'description':task.get('description'),'task_metadata':task.get('task_metadata',task.get('metadata'))},evidence_path=draft['evidence_path']))
    for i,inv in enumerate(e['observable_trace']['tool_invocations']):
        args=inv.get('canonical_args',{})
        if 'confirmed_facts' in args:rows.append(dict(kind='assistant_written_facts',claim=args['confirmed_facts'],evidence_path=f'observable_trace.tool_invocations[{i}].canonical_args'))
    for i,call in enumerate(e['observable_trace']['model_calls']):
        text=call.get('assistant_text') or ''
        for line in text.splitlines():
            if any(key in line for key in ('撤销','回退','回到','拆成','未系统','没学过','没有学过','零基础','还剩','静默','复测')):
                rows.append(dict(kind='specific_output_claim',claim=line,evidence_path=f'observable_trace.model_calls[{i}].assistant_text'))
    for i,row in enumerate(rows):row['claim_id']=f'claim-{i+1:03d}'
    return rows

def undo_version_facts(e):
    versions=[]
    for i,inv in enumerate(e['observable_trace']['tool_invocations']):
        result=inv.get('result',{})
        if isinstance(result,dict) and inv.get('tool_name') in ('plan.get','plan.patch') and isinstance(result.get('version'),int):
            versions.append((result['version'],f'observable_trace.tool_invocations[{i}].result'))
    if not versions:return []
    current,path=versions[-1];facts=[]
    pattern=r'(?:版本(?:号)?\s*(?:将|会|可)?\s*(?:回退|回到|恢复|退回|变回)(?:到|至|为)?\s*[vV]?\s*(\d+)|逆向操作回到\s*[vV](\d+))'
    for i,call in enumerate(e['observable_trace']['model_calls']):
        text=call.get('assistant_text') or ''
        for line in text.splitlines():
            for match in re.finditer(pattern,line):
                claimed=int(match.group(1) or match.group(2))
                facts.append(dict(claim=line,claimed_version=claimed,current_version=current,expected_undo_version=current+1,
                    incorrect=claimed!=current+1,contract=UNDO_CONTRACT,evidence_paths=[f'observable_trace.model_calls[{i}].assistant_text',path]))
    return facts

NUMERIC_CONTRACT={'arithmetic':'Python math.exp using ordinary double precision','scope':'explicitly requires naive and stable exponential normalization to agree without overflow on supplied large-number input; unspecified arbitrary precision is not supplied by the task'}

def exponential_facts(e):
    import math
    facts=[]
    for draft in draft_reading_aid(e):
        for stage in draft['draft_plan'].get('stages',[]):
            for task in stage.get('tasks',[]):
                text=task.get('description') or ''
                requires=re.search(r'稳定[^；。]*?与朴素[^；。]*?一致且不(?:发生)?溢出',text)
                if not requires:continue
                for match in re.finditer(r'\[\s*(-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)+)\s*\]',text):
                    values=[float(x) for x in match.group(1).split(',')]
                    results=[]
                    for v in values:
                        try:results.append({'input':v,'output':math.exp(v),'overflow':False})
                        except OverflowError:results.append({'input':v,'output':None,'overflow':True})
                    maximum=max(values);numerators=[math.exp(v-maximum) for v in values];total=sum(numerators)
                    facts.append({'claim':text,'input':values,'naive_exp':results,'stable_result':[n/total for n in numerators],
                        'incorrect':any(x['overflow'] for x in results),'contract':NUMERIC_CONTRACT,'evidence_paths':[draft['evidence_path']]})
    return facts
