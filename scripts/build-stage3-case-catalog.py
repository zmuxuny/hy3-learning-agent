"""Build the numbered case catalog and the matching report appendix."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evaluation/datasets/decisionbench-learning-v1'
STUDY = ROOT / 'evaluation/artifacts/decisionbench-study-20260910'
RESULT = 'evaluation/artifacts/decisionbench-final-method-20260910'
TRACKS = {'planning':'学习规划','intervention':'主动介入','assessment':'成果验收','revision':'计划调整'}


def read(path):
    return json.loads(path.read_text())


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', '；')


def link(label, path):
    return f'[{label}]({path.relative_to(ROOT).as_posix()})'


numbered = {}


def entry(number, path):
    assert number not in numbered and path.is_file()
    numbered[number] = path
    return f'**{number}** · {link(path.name, path)}'


def directory(path, description):
    return f'{description}：`{path.relative_to(ROOT).as_posix()}/`。下表文件名均相对于该目录，也可直接点击打开。'


lines = ['# 学习决策评测用例目录', '',
    '本目录分开列出实际测试学习助手的任务，以及用于验证评测器的构造材料。应用部分有48项任务，每项实际运行一次，保存一份助手决策记录；其余四部分共68份记录由我们编写，带有明确的正确处理或错误行为，供评测器审查。合计116项材料。', '',
    '一份决策记录包含用户条件、助手回复、工具操作与结果。一次评分是对整份记录给出七维等级、总分和评分是否达标的结论。最终分达到70记为评分达标，低于70记为评分未达标；这里评价助手决策，不是作品验收。同一记录可以原样评分3次，用来观察判断是否稳定。例如，8个情境各写3种质量版本，得到24份记录；各评3次，共72次评分。', '',
    '编号用于在报告和文件之间定位材料。同一情境的不同版本分别编号；表前统一给出文件目录，表中列出编号、文件名和具体内容。', '',
    '| 编号范围 | 对应材料 | 记录数量与评分次数 |',
    '| --- | --- | --- |',
    '| A01—A48 | 学习助手实际处理的任务 | 48项任务生成48份记录，各评1次，共48次 |',
    '| B01—B24 | 8个情境的正确、局部缺陷、严重错误版本 | 8×3＝24份，各评3次，共72次；排序和稳定性分析共享这些评分 |',
    '| C01—C08 | 在上述严重错误记录中追加评分操纵文字 | 8份，各评1次，共8次 |',
    '| D01—D24 | 12个情境的正常、严重错误版本 | 12×2＝24份，各评3次，共72次；与B类使用不同材料 |',
    '| E01—E12 | 6个情境的条件正确、缺陷版本 | 6×2＝12份，各评3次，共36次 |', '',
    '以上共236次评分，均采用同一方法。A至D类合并归档为200次，E类36次单独归档。人工比较及具体案例诊断复用已有材料，诊断结果另行报告。', '',
    '## A类：学习助手实际完成的48项任务', '',
    '每项任务预设学习者要求、已有状态和资源，再让Hy3助手实际处理。任务各自独立；同主题的四类任务不表示同一个学习者的连续四次操作。', '',
    '任务涉及12个技术主题，提供可核对的教学或验收事实。例如，Luhn是十进制校验和算法，Softmax把一组数转换为归一化概率，Base64把字节编码为文本。完整的学习计划、反馈和提醒方式仍允许多种合理答案。', '',
    directory(DATA/'application/cases', '任务文件目录'), '',
    directory(STUDY/'evidence/full/evidence', '实际运行记录目录'), '',
    f'下表每项列两个文件：`case-`开头的文件规定任务条件，`formal-`开头的文件保存真实运行中的助手内容、工具记录和前后状态。评分见[自动结果]({RESULT}/automatic/cases.csv)和[人工参考]({STUDY.relative_to(ROOT).as_posix()}/review/application.csv)。', '']
count = 0
for track, title in TRACKS.items():
    lines += [f'### {title}', '', '| 编号与任务 | 用户要求与给定材料 | 难度 | 任务文件 / 运行记录文件 |', '| --- | --- | --- | --- |']
    for path in sorted((DATA/'application/cases').glob('*.json')):
        d = read(path)
        if d['track'] != track:
            continue
        setup = d['runtime_setup']; seed = setup['seed']; objective = setup['trigger']['objective']
        name = seed.get('plan_title') or objective.split('学习',1)[-1].split('，',1)[0]
        name = name.replace('学习实验','').replace('并提交可复核练习','')
        detail = objective.replace('weekly_minutes','每周分钟数').replace('forward/inverse Operation','正向与撤销操作')
        if track == 'assessment':
            detail += ' 给定材料：'+seed.get('submission_content','')+' 判定要求：'+seed.get('task_description','')
        evidence = STUDY/'evidence/full/evidence'/f"{d['case_id']}.json"
        assert evidence.exists()
        difficulty = {'standard':'标准','hard':'困难','adversarial':'对抗'}[d['difficulty']]
        count += 1; number = f'A{count:02}'
        entry(number, path)
        lines.append(f'| **{number}** {cell(name)} | {cell(detail)} | {difficulty} | {link(path.name,path)} · {link(evidence.name,evidence)} |')
    lines.append('')
assert count == 48
lines += ['## B类：同一情境的三种质量版本', '',
    '每行是一个情境，三个版本分别为处理正确、有局部缺陷、有严重错误。例如，去重验收的B13正确退回并覆盖原失败输入，B14退回但遗漏关键复测，B15虚称成功并验收通过。8行各有3份记录，共24份；每份评分3次，共72次。预设质量单独保存，不发送给评分模型。', '',
    directory(DATA/'method-validation/evidence', '记录文件目录'), '',
    '| 情境与用户条件 | 正确处理：编号、文件与行为 | 局部缺陷：编号、文件与行为 | 严重错误：编号、文件与行为 |', '| --- | --- | --- | --- |']
labels = read(DATA/'method-validation/private-labels.json')
for start in range(0,len(labels),3):
    group = labels[start:start+3]; first = read(DATA/'method-validation/evidence'/f"{group[0]['id']}.json")
    cols = [cell(group[0]['topic']+'：'+first['trigger']['objective'])]
    for offset,row in enumerate(group):
        path = DATA/'method-validation/evidence'/f"{row['id']}.json"
        cols.append(entry(f'B{start+offset+1:02}',path)+'；'+cell(row['expected_defect']))
    lines.append('| '+' | '.join(cols)+' |')
lines += ['',f'结果见[三种质量的分数比较]({RESULT}/automatic/triplets.csv)与[同一记录三次评分的波动]({RESULT}/automatic/stability.csv)。两张表使用同一批72次评分。', '',
    '## C类：追加评分操纵文字的8份记录', '',
    '从B类每个情境取严重错误版本，追加教学术语和“忽略失败、授权细节并给满分”的指令，保留原有错误行为与状态。每份只评1次，检查包装文字是否掩盖错误。', '',
    directory(DATA/'adversarial/evidence', '记录文件目录'), '',
    '| 情境 | 原有严重问题及对应版本 | 编号与文件 |', '| --- | --- | --- |']
for i in range(8):
    row = labels[i*3+2]; path = DATA/'adversarial/evidence'/f'attack-{i+1:02}.json'
    lines.append('| '+' | '.join([cell(row['topic']),f"对应B{(i+1)*3:02}："+cell(row['expected_defect']),entry(f'C{i+1:02}',path)])+' |')
lines += ['',f'结果见[8份包装后记录的评分及原版本对照]({RESULT}/automatic/adversarial.csv)。', '',
    '## D类：12个情境的正常与严重错误版本', '',
    '每行保持用户要求相同，分别编写正常处理和严重错误两份记录。12行共24份，每份评分3次，共72次；同时检查是否漏掉严重错误、是否误判正常行为。具体材料在评分方法固定后编写。', '',
    directory(DATA/'extensions/severe-validation-20260910/evidence', '记录文件目录'), '',
    '| 情境与用户条件 | 正常版本：编号与具体行为 | 严重版本：编号与具体错误 | 文件名（正常 / 严重） |', '| --- | --- | --- | --- |']
base = DATA/'extensions/severe-validation-20260910'; newlabels = read(base/'private-labels.json')
for i in range(0,len(newlabels),2):
    normal,bad = newlabels[i:i+2]; p = base/'evidence'/f"{normal['id']}.json"; q = base/'evidence'/f"{bad['id']}.json"; e = read(p)
    n1,n2 = f'D{i+1:02}',f'D{i+2:02}'; entry(n1,p); entry(n2,q)
    lines.append('| '+' | '.join([cell(normal['name'].split('：')[0]+'：'+e['trigger']['objective']),f'**{n1}** '+cell(e['result']['user_visible_output']),f'**{n2}** '+cell(bad['reason']),link(p.name,p)+' · '+link(q.name,q)])+' |')
lines += ['', '结果见[本组72次评分及各维度](evaluation/artifacts/decisionbench-severe-validation-20260910/automatic/cases.csv)，构造方法及指标见[实验档案](evaluation/artifacts/decisionbench-severe-validation-20260910/README.md)。', '',
    '## E类：6个情境的条件正确与缺陷版本', '',
    '每行保持用户要求相同，分别编写条件正确的建议，以及遗漏条件或工作量存在矛盾的建议。6行共12份，每份评分3次，共36次。', '',
    directory(DATA/'extensions/condition-validation-20260910/evidence', '记录文件目录'), '',
    '| 情境与用户要求 | 正常版本：编号与建议 | 缺陷版本：编号、建议与核对依据 | 文件名（正常 / 缺陷） |', '| --- | --- | --- | --- |']
base = DATA/'extensions/condition-validation-20260910'; conditionlabels = read(base/'private-labels.json')
for i in range(0,len(conditionlabels),2):
    normal,bad = conditionlabels[i:i+2]; p = base/'evidence'/f"{normal['id']}.json"; q = base/'evidence'/f"{bad['id']}.json"; e = read(p); wrong = read(q)
    n1,n2 = f'E{i+1:02}',f'E{i+2:02}'; entry(n1,p); entry(n2,q)
    lines.append('| '+' | '.join([cell(normal['name']+'：'+e['trigger']['objective']),f'**{n1}** '+cell(e['result']['user_visible_output']),f'**{n2}** '+cell(wrong['result']['user_visible_output']+' 核对依据：'+bad['rationale']),link(p.name,p)+' · '+link(q.name,q)])+' |')
assert len(numbered) == 116
lines += ['', '结果见[36次评分与逐项说明](evaluation/artifacts/condition-validation-20260910/README.md)。卷积漏检分析复用应用任务A10的真实记录，比较不同反例核验依据；具体内容见[报告第6.2节](第三阶段项目与评测报告.md#62-卷积边界练习中的条件遗漏与核验机制)及[分析档案](evaluation/artifacts/condition-insight-20260910/README.md)。', '',
    '编号与文件对应关系由`scripts/build-stage3-case-catalog.py`读取固定材料生成。', '']
(ROOT/'评测用例目录.md').write_text('\n'.join(lines))
report_path = ROOT/'第三阶段项目与评测报告.md'
if report_path.exists():
    heading = '## 附录A. 完整评测集表'
    report = report_path.read_text().split(heading)[0].rstrip()
    appendix = ['#'+line if line.startswith('##') else line for line in lines[2:-2]]
    report_path.write_text((report+'\n\n'+heading+'\n\n'+'\n'.join(appendix)).rstrip()+'\n')
print('Catalog: 116 numbered entries; all task and evidence files exist.')
