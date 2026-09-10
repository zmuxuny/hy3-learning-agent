"""Render the final method's consolidated tables into the public report figures."""
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
A=ROOT/'evaluation/artifacts/decisionbench-final-method-20260910/automatic'
OUT=ROOT/'assets/stage3'
font_manager.fontManager.addfont('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
plt.rcParams.update({'font.family':'Noto Sans CJK JP','font.size':11,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none','svg.hashsalt':'learning-agent-final-method','figure.facecolor':'white'})
def rows(name):return list(csv.DictReader((A/(name+'.csv')).open()))
def save(fig,name):
    fig.tight_layout();fig.savefig(OUT/(name+'.png'),dpi=180,bbox_inches='tight');svg=OUT/(name+'.svg');fig.savefig(svg,bbox_inches='tight',metadata={'Date':None});svg.write_text('\n'.join(s.rstrip() for s in svg.read_text().splitlines())+'\n');plt.close(fig)
tracks=['planning','intervention','assessment','revision'];names=['学习规划','主动介入','成果验收','计划调整'];dims=['事实内容','学习目标','行动时机','用户控制','结果可用','行动适度','解释步骤']
app=rows('application-human');x=np.arange(4);fig,ax=plt.subplots(figsize=(10,4.5))
automatic=[np.mean([float(r['automatic_score']) for r in app if r['track']==t]) for t in tracks];human=[np.mean([float(r['human_score']) for r in app if r['track']==t]) for t in tracks]
ax.bar(x-.18,automatic,.36,label='自动评分',color='#a7b2c2');ax.bar(x+.18,human,.36,label='固定人工参考',color='#416b9c')
for i,v in enumerate(human):ax.text(i+.18,v+1,f'{v:.1f}',ha='center')
ax.set(xticks=x,xticklabels=names,ylim=(0,112),ylabel='最终分均值 / 100',title='48份真实应用输出：自动评分与人工参考');ax.legend(loc='lower left');ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);save(fig,'application-results')

data=np.array([[np.mean([int(r['human_D'+str(i)]) for r in app if r['track']==t]) for i in range(1,8)] for t in tracks]);fig,ax=plt.subplots(figsize=(11,3.5));im=ax.imshow(data,vmin=0,vmax=2,cmap='Blues',aspect='auto')
ax.set(xticks=range(7),xticklabels=dims,yticks=range(4),yticklabels=names,title='固定人工参考的七维等级均值（0—2）')
for i in range(4):
    for j in range(7):ax.text(j,i,f'{data[i,j]:.2f}',ha='center',va='center',color='white' if data[i,j]>1.5 else '#283343')
fig.colorbar(im,ax=ax,pad=.02);save(fig,'dimension-results')

tr=rows('triplets');groups=sorted({r['group'] for r in tr});group_names=['CSV复习','二分查找','提醒冷却','跨夜免打扰','去重验收','配置验收','预算调整','旧稿恢复'];fig,ax=plt.subplots(figsize=(12,5));x=np.arange(8)
for shift,key,label,color in [(-.24,'good','优质','#416b9c'),(0,'mild','局部缺陷','#d8ac63'),(.24,'severe','严重缺陷','#b36c6c')]:
    vals=[[float(r[key]) for r in tr if r['group']==g and r[key]!=''] for g in groups];ax.bar(x+shift,[np.mean(v) if v else np.nan for v in vals],.23,label=label,color=color)
    for i,v in enumerate(vals):ax.scatter([i+shift]*len(v),v,s=20,facecolors='white',edgecolors='#283343',zorder=4)
correct=sum(r['strict_raw']=='True' for r in tr);ax.set(xticks=x,xticklabels=group_names,ylim=(0,115),ylabel='加权分 / 100（严重度限制前）',title=f'三档判别：8个场景各评3次，严格排序{correct}/24组');ax.legend(ncol=3,loc='upper right',fontsize=10);ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);save(fig,'discrimination')

st=[r for r in rows('stability') if r['part']=='quality'];fig,ax=plt.subplots(figsize=(11,4));ax.bar(np.arange(24),[float(r['raw_sd']) if r['raw_sd'] else np.nan for r in st],color=[{'good':'#416b9c','mild':'#d8ac63','severe':'#b36c6c'}[r['condition']] for r in st]);ax.set(xticks=np.arange(1,24,3),xticklabels=group_names,ylabel='三次加权分的总体标准差',title='相同输出的评分波动：每组三柱为优质、局部缺陷、严重缺陷');ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);save(fig,'repeat-stability')

alignment=rows('human-alignment');fig,ax=plt.subplots(figsize=(10,4.5));vals=[float(r['agreement'])*100 for r in alignment];ax.barh(dims[::-1],vals[::-1],color='#416b9c',height=.6)
for i,v in enumerate(vals[::-1]):ax.text(v+1,i,f'{v:.1f}%',va='center',fontsize=12)
ax.set_xlim(0,108);ax.set_xticks([0,25,50,75,100],['0%','25%','50%','75%','100%']);ax.set_title('48份应用输出：自动评分与人工等级的一致率',pad=15);ax.spines['left'].set_visible(False);ax.tick_params(axis='y',length=0);save(fig,'human-alignment')

rs=[r for r in rows('cases') if r['part']=='new_scenarios'];fig,ax=plt.subplots(figsize=(11,4.8));family_names=['备份计划','参数练习','表单目标','订阅取消','每日上限','邮件渠道','字节统计','分页复测','图片签名','批注保留','跨项目调整','版本冲突']
for condition,color,label in [('good','#416b9c','正常对照'),('severe','#ad6044','严重缺陷')]:
    selected=[r for r in rs if r['condition']==condition and r['score']!=''];xs=[int(r['id'].split('-')[1])+(-.13 if condition=='good' else .13)+(int(r['repeat'])-2)*.055 for r in selected];ax.scatter(xs,[float(r['score']) for r in selected],s=27,color=color,label=label,alpha=.82)
ax.axhline(70,color='#8d97a5',linestyle='--',linewidth=.9);ax.text(12.45,71,'通过线',ha='right',fontsize=9,color='#647185');ax.set_xticks(range(1,13),family_names,rotation=35,ha='right');ax.set(ylim=(-12,109),xlim=(.5,12.5),ylabel='最终分',title='12组正常与严重对照：每份输出评分3次');ax.legend(loc='center right',frameon=False);save(fig,'severe-validation')
insight=ROOT/'evaluation/artifacts/condition-insight-20260910/automatic/cases.csv'
if insight.exists():
    ds=[r for r in csv.DictReader(insight.open()) if r['part']=='rating-probe']
    fig,ax=plt.subplots(figsize=(9,4.2))
    for i,(key,label,color) in enumerate([('automatic_probe','自动探测：仍使用[1]','#a7b2c2'),('verified_counterexample','已核对反例：使用[2]','#416b9c')]):
        rr=[r for r in ds if r['id']==key]
        for r in rr:
            xx=i+(int(r['repeat'])-2)*.08;yy=float(r['score']);ax.scatter(xx,yy,s=50,color=color,zorder=3)
            ax.text(xx,yy+2.5,r['score'],ha='center',fontsize=10)
    ax.axhline(70,color='#8d97a5',linestyle='--',linewidth=1);ax.text(1.35,71,'通过线',ha='right',color='#647185',fontsize=10)
    ax.set(xticks=[0,1],xticklabels=['自动探测：仍使用[1]','已核对反例：使用[2]'],xlim=(-.4,1.4),ylim=(40,112),ylabel='最终分 / 100',title='同一卷积计划：只替换边界任务的核验依据，各评分3次')
    ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);save(fig,'condition-insight')
conditions=ROOT/'evaluation/artifacts/condition-validation-20260910/automatic/cases.csv'
if conditions.exists():
    ds=list(csv.DictReader(conditions.open()));fig,ax=plt.subplots(figsize=(10.5,4.5))
    for kind,color,label in [('good','#416b9c','条件正确'),('defect','#ad6044','条件遗漏或负荷矛盾')]:
        rr=[r for r in ds if r['condition']==kind]
        for i,r in enumerate(rr):
            x=int(r['id'].split('-')[1])+(-.13 if kind=='good' else .13)+(int(r['repeat'])-2)*.045
            ax.scatter(x,float(r['score']) if r['score'] else -7,s=27,color=color,marker='o' if r['score'] else 'x',label=label if i==0 else None)
    ax.axhline(70,color='#8d97a5',linestyle='--',linewidth=1);ax.set(xticks=range(1,7),xticklabels=['平方与开方','升序拼接','整除分配','合并准确率','单段长度','保留与新增'],ylim=(-12,110),ylabel='最终分 / 100',title='6组成对条件验证：每份输出评分3次')
    ax.legend(loc='lower right',frameon=False);save(fig,'condition-validation')
print('Report figures rendered from verified tables.')
