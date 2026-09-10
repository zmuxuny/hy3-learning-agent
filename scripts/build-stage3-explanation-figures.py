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


f = Figure('学习助手记录如何得到评分和复核结果', 650)
f.text(30,43,'从助手行为到质量评分',32,True,NAVY)
f.text(30,83,'待评的是助手决策；核验和评分由另外两次Hy3调用完成。',24)
xs = [30,260,490,720,950]
f.box(xs[0],130,200,198,'完整决策记录',['用户要求与状态','助手回复与操作','工具与状态结果'])
f.box(xs[1],130,200,198,'Hy3：内容核验',['第一次模型调用','逐条核对原句','列出发现与证据'])
f.box(xs[2],130,200,198,'Hy3：质量评分',['第二次模型调用','按七维给0/1/2','说明理由与严重度'])
f.box(xs[3],130,200,198,'程序：核对计算',['执行明确证据规则','按权重计算分数','应用严重度上限'])
f.box(xs[4],130,200,198,'自动评分结果',['七维等级与理由','最终分：0—100','≥70记为评分达标'])
for x in xs[:-1]:
    f.arrow(f'{x+200},229 {x+227},229')
f.text(260,367,'核验与评分都读取原始记录；程序计算结果也作为核验依据。',23)
f.arrow('1050,328 1050,420 870,420 870,457')
f.box(665,460,485,151,'开发助手（AI）：逐次复核',['回看原始记录，核对自动理由','保存同意或修订意见；原始自动评分保留'],WHITE)
f.box(30,460,580,151,'两位人工：独立参考标注',['按同一判据标注指定材料','与自动结果比较；覆盖范围见报告第5.4节'],WHITE)
f.save('evaluation-method-flow.svg')

f = Figure('实验材料来源、记录份数和评分次数',660)
f.text(30,43,'实验材料与评分次数',32,True,NAVY)
f.text(30,83,'一次评分＝完整审查一份记录；重复评分时，记录内容不变。',24)
f.box(30,118,290,130,'测试学习助手',['预设任务 → 应用实际运行','观察生成的回复和操作'])
f.box(365,118,785,130,'48项任务 → 48份真实记录 → 各评1次 → 48次评分',['分析应用表现，并与人工参考比较'])
f.arrow('320,183 362,183')
f.box(30,290,290,267,'验证评测器',['编写质量差异明确的记录','把要求、回复和操作','一起交给评测器评分'])
rows=[('三种质量','8个情境 × 3种版本 × 3次评分 ＝ 72次'),
      ('评分操纵','8份严重记录追加操纵文字 × 1次评分 ＝ 8次'),
      ('正常 / 严重','12个情境 × 2种版本 × 3次评分 ＝ 72次'),
      ('条件 / 工作量','6个情境 × 2种版本 × 3次评分 ＝ 36次')]
for i,(title,content) in enumerate(rows):
    y=290+i*68
    f.parts.append(f'<rect x="365" y="{y}" width="785" height="59" rx="5" fill="{BLUE}" stroke="{STROKE}"/>')
    f.text(383,y+38,title,23,True,NAVY)
    f.text(558,y+38,content,21)
f.arrow('320,423 344,423 344,319 362,319')
for y in [387,455,523]:
    f.arrow(f'344,423 344,{y} 362,{y}')
f.text(30,609,'共116份不同记录、236次评分。三种质量的排序和重复稳定性共用72次评分。',24,True,NAVY)
f.text(30,643,'人工比较复用已有材料；具体案例诊断单列分析，不增加新的应用任务。',23)
f.save('evaluation-experiment-map.svg')
print('Built method flow and experiment map SVGs.')
