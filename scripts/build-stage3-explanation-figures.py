"""Build the report's method flow and experiment-count diagrams as editable SVG."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'assets/stage3'
NAVY = '#203d63'
GRAY = '#536174'
BLUE = '#eaf0f8'
WHITE = '#ffffff'
STROKE = '#c5d0de'


class Figure:
    def __init__(self, title, height):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1180" height="{height}" viewBox="0 0 1180 {height}" role="img" aria-label="{escape(title)}">',
            '<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9" fill="#71839b"/></marker></defs>',
            f'<rect width="1180" height="{height}" fill="white"/>',
            '<g font-family="Noto Sans CJK SC, Noto Sans CJK JP, sans-serif">']

    def text(self, x, y, label, size=24, bold=False, color=GRAY, anchor='start'):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}" text-anchor="{anchor}">{escape(label)}</text>')

    def box(self, x, y, width, height, title, lines, fill=BLUE):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{fill}" stroke="{STROKE}" stroke-width="1.5"/>')
        self.text(x+width/2,y+37,title,25,True,NAVY,'middle')
        for i,line in enumerate(lines):
            self.text(x+width/2,y+77+i*33,line,22,False,GRAY,'middle')

    def arrow(self, points):
        self.parts.append(f'<polyline points="{points}" fill="none" stroke="#71839b" stroke-width="2" marker-end="url(#arrow)"/>')

    def save(self, name):
        (OUT/name).write_text('\n'.join(self.parts+['</g></svg>'])+'\n')


f = Figure('关键决策自动评估流程', 690)
f.text(30,44,'关键决策自动评估流程',32,True,NAVY)
f.text(30,83,'评测对象：Decision Episode    自动评估：规则程序 + 两次Hy3评审调用',23)
f.box(30,120,300,170,'① 关键决策片段',['状态、环境','可观察轨迹与行动边界'])
f.box(430,120,300,170,'② 证据整理与规则预检',['保留原文与来源位置','提取断言与可计算证据'])
f.box(830,120,300,170,'③ Hy3内容核验',['核对具体断言与条件','输出发现及证据引用'])
f.arrow('330,205 425,205');f.arrow('730,205 825,205')
f.arrow('980,290 980,350 180,350 180,420')
f.text(295,331,'原始Episode + 核验发现 + 七维判据',24,False,GRAY)
f.box(30,425,300,170,'④ Hy3七维评审',['按任务影响给0/1/2等级','给出理由与问题严重度'])
f.box(430,425,300,170,'⑤ 规则核对与分数汇总',['核对等级，按权重求和','应用69 / 39分严重度上限'])
f.box(830,425,300,170,'⑥ 自动评估结果',['七维等级、总分与严重度','证据、理由与达标结论'])
f.arrow('330,510 425,510');f.arrow('730,510 825,510')
f.text(30,652,'方法验证：双人人工标注与自动结果对照；人工标注不参与自动分数的计算。',24,True,NAVY)
f.save('evaluation-method-flow.svg')

f = Figure('评测材料与实验安排', 855)
f.text(30,44,'评测材料与实验安排',32,True,NAVY)
f.text(30,84,'材料来源',24,True,NAVY);f.text(330,84,'材料构造与重复安排',24,True,NAVY);f.text(920,84,'完整评分次数',24,True,NAVY)
rows=[
 ('实际应用任务','检查应用表现','48项任务各实际运行一次','形成48份真实决策记录','48 × 1 = 48次'),
 ('三种质量对照','验证评测方法','8个情境，各编写3种质量版本','24份固定记录，每份评分3次','24 × 3 = 72次'),
 ('评分操纵','验证评测方法','8份严重错误记录追加评分指令','保留实际错误，每份评分1次','8 × 1 = 8次'),
 ('正常与严重对照','验证评测方法','12个情境，各编写正常 / 严重版本','24份固定记录，每份评分3次','24 × 3 = 72次'),
 ('条件与工作量','验证评测方法','6个情境，各编写正确 / 缺陷版本','12份固定记录，每份评分3次','12 × 3 = 36次')]
for i,(title,purpose,line1,line2,count) in enumerate(rows):
 y=112+i*126
 f.box(30,y,225,108,title,[purpose],WHITE)
 f.box(335,y,490,108,line1,[line2])
 f.box(905,y,245,108,count,['每份材料保持不变'])
 f.arrow(f'255,{y+54} 330,{y+54}');f.arrow(f'825,{y+54} 900,{y+54}')
f.text(30,792,'共116份不同材料、236次评分。',27,True,NAVY)
f.text(30,834,'三种质量排序与重复稳定性共用同一批72次评分；人工比较复用已有材料。',24)
f.save('evaluation-experiment-map.svg')
print('Built episode evaluation flow and experiment lanes.')
