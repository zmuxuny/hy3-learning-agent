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
def save(fig,name,layout=True):
    if layout:fig.tight_layout()
    fig.savefig(OUT/(name+'.png'),dpi=180,bbox_inches='tight');svg=OUT/(name+'.svg');fig.savefig(svg,bbox_inches='tight',metadata={'Date':None});svg.write_text('\n'.join(s.rstrip() for s in svg.read_text().splitlines())+'\n');plt.close(fig)
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

tr=rows('triplets');groups=sorted({r['group'] for r in tr})
group_names=['CSV复习截止','二分查找练习','提醒冷却余时','跨夜免打扰','去重函数验收','配置解析验收','预算调整撤销','旧计划恢复']
repeat_colors=['#315f91','#81a6cd','#bd8743']
fig,axes=plt.subplots(2,4,figsize=(13,6.8),sharex=True,sharey=True)
for i,(g,ax) in enumerate(zip(groups,axes.flat)):
    for j,r in enumerate(sorted([r for r in tr if r['group']==g],key=lambda r:int(r['repeat']))):
        ys=[float(r[k]) for k in ['good','mild','severe']]
        ax.plot(np.arange(3)+(j-1)*.035,ys,'o-',color=repeat_colors[j],label=f'第{j+1}次评分',lw=1.7,ms=5,alpha=.9)
    ax.set_title(f'B{i*3+1:02d}—B{i*3+3:02d}  {group_names[i]}',fontsize=11,pad=10)
    ax.set(xticks=[0,1,2],xticklabels=['正确处理','局部缺陷','严重错误'],ylim=(-4,112),xlim=(-.22,2.22),yticks=[0,25,50,75,100])
    ax.tick_params(axis='x',labelbottom=True,labelsize=9);ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    if i%4==0:ax.set_ylabel('加权分 / 100')
    if i==3:
        ax.scatter([1.965],[100],s=110,facecolors='none',edgecolors='#ac4c4c',lw=1.7,zorder=5)
        ax.annotate('100分：未区分严重错误',xy=(1.965,100),xytext=(.25,48),fontsize=8.5,color='#934444',arrowprops=dict(arrowstyle='->',color='#934444',lw=.8))
fig.legend(*axes[0,0].get_legend_handles_labels(),loc='upper center',bbox_to_anchor=(.5,1.035),ncol=3,frameon=False)
save(fig,'discrimination')

st=[r for r in rows('stability') if r['part']=='quality']
fig,(ax,zero)=plt.subplots(2,1,figsize=(12,5.7),sharex=True,gridspec_kw={'height_ratios':[7,1],'hspace':.10})
colors={'good':'#416b9c','mild':'#c3964f','severe':'#b36c6c'}
for j,(condition,label) in enumerate([('good','正确处理'),('mild','局部缺陷'),('severe','严重错误')]):
    for i,g in enumerate(groups):
        r=next(r for r in st if r['id']==g+'-'+str(j+1));v=float(r['raw_sd']);xx=i+(j-1)*.22
        if v>0:
            ax.vlines(xx,1,v,color=colors[condition],alpha=.35,lw=1.5)
            ax.scatter(xx,v,color=colors[condition],s=44,zorder=4)
        else:zero.scatter(xx,0,color=colors[condition],s=44,zorder=4)
    ax.scatter([],[],color=colors[condition],s=44,label=label)
ax.set_yscale('log');ax.set_ylim(1,60);ax.set_yticks([1,2,5,10,20,50],[1,2,5,10,20,50]);ax.minorticks_off()
ax.set_ylabel('三次评分的标准差 / 分（对数刻度）')
ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True);ax.tick_params(axis='x',bottom=False,labelbottom=False)
ax.legend(loc='upper right',ncol=3,frameon=False)
outlier=next(float(r['raw_sd']) for r in st if r['id']=='quality-04-3')
ax.annotate(f'B12 严重错误：{outlier:.2f}分\n三次评分为100、35、22.5',xy=(3.22,outlier),xytext=(4.1,26),fontsize=11,color='#934444',arrowprops=dict(arrowstyle='->',color='#934444',lw=1))
zero.set(ylim=(-.6,.6),yticks=[0],yticklabels=['0'],xticks=range(8),xticklabels=group_names,xlim=(-.6,7.6))
zero.tick_params(axis='x',labelsize=10,length=0,pad=10);zero.set_ylabel('三次相同',rotation=0,labelpad=30,va='center',fontsize=10)
zero.set_facecolor('#f4f6f9');zero.spines['bottom'].set_visible(False);zero.spines['left'].set_visible(False)
save(fig,'repeat-stability',layout=False)

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
