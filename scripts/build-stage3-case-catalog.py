"""Build a readable catalog with direct links to every published evaluation input."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evaluation/datasets/decisionbench-learning-v1'
STUDY = 'evaluation/artifacts/decisionbench-study-20260910'
RESULT = 'evaluation/artifacts/decisionbench-final-method-20260910'
TRACKS = {'planning':'学习规划','intervention':'主动介入','assessment':'成果验收','revision':'计划调整'}

def read(path):
    return json.loads(path.read_text())

def cell(value):
    return str(value).replace('|', '\\|').replace('\n', '；')

def link(label, path):
    return f'[{label}]({path.relative_to(ROOT).as_posix()})'

lines = ['# 学习决策评测用例目录', '',
    '本目录逐项列出48个应用输入、24份三档输出、8份评分操纵输出、24份正常与严重对照及12份条件核验对照，共116份输入材料。主体四部分共200次评分，条件对照另评36次，全部采用同一冻结方法。另有6次卷积定点诊断复用同一份应用输出，不增加用例数量。每条链接指向实际JSON文件；用例名称用于阅读，文件内标识用于连接脚本和结果表。', '',
    '应用输入由作者构造，随后真实运行Hy3助手；其余输出连同观察结果由作者构造，用来验证评测方法。各用例独立运行，同主题的四类决策不表示同一个学习者的连续四次操作。', '',
    '技术主题提供可核对的教学或验收事实。例如，Luhn是十进制校验和算法，Softmax把一组数转换为归一化概率，Base64把字节编码为文本。这些局部事实帮助判断建议是否正确，完整的学习计划、反馈与介入仍允许多种合理答案。', '',
    '## 应用用例：48个真实运行的输入', '',
    f'输入总表见[应用清单](evaluation/datasets/decisionbench-learning-v1/application/manifest.json)，评分见[自动结果]({RESULT}/automatic/cases.csv)和[人工确认对应的复核表]({STUDY}/review/application.csv)。表中“原始输出”包括完整助手内容、工具记录和前后状态。', '']
count = 0
for track, title in TRACKS.items():
    lines += [f'### {title}', '', '| 用例 | 用户要求与给定材料 | 难度 | 仓库位置 |', '| --- | --- | --- | --- |']
    for path in sorted((DATA/'application/cases').glob('*.json')):
        d=read(path)
        if d['track'] != track: continue
        setup=d['runtime_setup'];seed=setup['seed'];objective=setup['trigger']['objective']
        name=seed.get('plan_title') or objective.split('学习',1)[-1].split('，',1)[0]
        name=name.replace('学习实验','').replace('并提交可复核练习','')+'：'+title
        detail=objective.replace('weekly_minutes','每周分钟数').replace('forward/inverse Operation','正向与撤销操作')
        if track=='assessment': detail+=' 给定材料：'+seed.get('submission_content','')+' 判定要求：'+seed.get('task_description','')
        evidence=ROOT/STUDY/'evidence/full/evidence'/f"{d['case_id']}.json"
        assert evidence.exists()
        difficulty={'standard':'标准','hard':'困难','adversarial':'对抗'}[d['difficulty']]
        lines.append(f'| {cell(name)} | {cell(detail)} | {difficulty} | {link("用例JSON",path)} · {link("原始输出",evidence)} |')
        count+=1
    lines.append('')
assert count==48
lines += ['## 三档判别用例：8组、24份输出', '',
    '每组固定同一用户条件，分别构造优质、局部缺陷与严重缺陷输出。三列链接各对应一份完整证据；括号内给出预设差异。质量标签单独保存，不发送给评分模型。', '',
    '| 用例与用户条件 | 优质输出 | 局部缺陷输出 | 严重缺陷输出 |', '| --- | --- | --- | --- |']
labels=read(DATA/'method-validation/private-labels.json')
for start in range(0,len(labels),3):
    group=labels[start:start+3];first=read(DATA/'method-validation/evidence'/f"{group[0]['id']}.json")
    cols=[cell(group[0]['topic']+'：'+first['trigger']['objective'])]
    for row in group:
        p=DATA/'method-validation/evidence'/f"{row['id']}.json"
        cols.append(link('原始证据',p)+'（'+cell(row['expected_defect'])+'）')
    lines.append('| '+' | '.join(cols)+' |')
lines += ['',f'结果见[三档排序表]({RESULT}/automatic/triplets.csv)与[逐输出重复波动]({RESULT}/automatic/stability.csv)。', '',
    '## 评分操纵用例：8份输出', '',
    '在上述每组严重缺陷输出后附加教学术语、要求忽略失败或授权细节并给满分的文字，原有行为与状态保持不变。下表逐项对应其原始严重缺陷。', '',
    '| 用例 | 原有严重问题 | 仓库位置 |', '| --- | --- | --- |']
for i in range(8):
    row=labels[i*3+2];p=DATA/'adversarial/evidence'/f'attack-{i+1:02}.json';assert p.exists()
    lines.append(f'| {cell(row["topic"])}：评分操纵 | {cell(row["expected_defect"])} | {link("操纵后完整证据",p)} |')
lines += ['',f'结果见[对抗结果表]({RESULT}/automatic/adversarial.csv)。', '',
    '## 正常与严重错误对照：12组、24份输出', '',
    '具体场景在方法固定后构造，每组包含正常和严重缺陷两份输出，分别评价3次。正常对照要求行动有依据且符合授权，七维同时检查解释等局部内容。', '',
    '| 用例与用户条件 | 正常对照 | 严重缺陷 | 仓库位置 |', '| --- | --- | --- | --- |']
base=DATA/'extensions/severe-validation-20260910';newlabels=read(base/'private-labels.json')
for i in range(0,len(newlabels),2):
    normal,bad=newlabels[i:i+2];p=base/'evidence'/f"{normal['id']}.json";q=base/'evidence'/f"{bad['id']}.json";e=read(p)
    lines.append('| '+' | '.join([cell(normal['name'].split('：')[0]+'：'+e['trigger']['objective']),cell(e['result']['user_visible_output']),cell(bad['reason']),link('正常证据',p)+' · '+link('严重证据',q)])+' |')
lines += ['', '结果见[72次评分及全部维度](evaluation/artifacts/decisionbench-final-method-20260910/automatic/cases.csv)，来源与构造步骤见[实验档案](evaluation/artifacts/decisionbench-severe-validation-20260910/README.md)。', '',
    ]
lines += ['## 条件核验对照：6组、12份输出', '', '每组保持用户要求相同，比较正确限定条件的建议与遗漏条件或错误声称等量的建议。每份评分3次，共36次。具体内容及原始文件如下。', '', '| 用例与用户要求 | 正常建议 | 错误建议及核对依据 | 仓库位置 |', '| --- | --- | --- | --- |']
base=DATA/'extensions/condition-validation-20260910';conditionlabels=read(base/'private-labels.json')
for i in range(0,len(conditionlabels),2):
    normal,bad=conditionlabels[i:i+2];p=base/'evidence'/f"{normal['id']}.json";q=base/'evidence'/f"{bad['id']}.json";e=read(p);wrong=read(q)
    lines.append('| '+' | '.join([cell(normal['name']+'：'+e['trigger']['objective']),cell(e['result']['user_visible_output']),cell(wrong['result']['user_visible_output']+' 核对依据：'+bad['rationale']),link('正常证据',p)+' · '+link('错误证据',q)])+' |')
lines += ['', '结果与逐维检查见[条件核验专项档案](evaluation/artifacts/condition-validation-20260910/README.md)。第6.2节的卷积定点诊断复用上述应用卷积记录，固定原文和内容核验后比较不同反例证据，见[定点诊断档案](evaluation/artifacts/condition-insight-20260910/README.md)。', '', '本目录由`scripts/build-stage3-case-catalog.py`读取固定输入与独立标签生成。', '']
(ROOT/'评测用例目录.md').write_text('\n'.join(lines))
report_path=ROOT/'第三阶段项目与评测报告.md'
if report_path.exists():
    heading='## 附录A. 完整评测集表'
    report=report_path.read_text().split(heading)[0].rstrip()
    appendix=[]
    for line in lines[2:-2]:
        appendix.append('#'+line if line.startswith('##') else line)
    report_path.write_text((report+'\n\n'+heading+'\n\n'+'\n'.join(appendix)).rstrip()+'\n')
print('Catalog: 48 application + 24 quality + 8 adversarial + 24 severe pairs + 12 condition pairs = 116 inputs.')
