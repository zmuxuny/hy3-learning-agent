"""Render the published study tables into PNG/SVG figures without API calls."""
import csv,json,argparse
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
def render(archive,output):
    output.mkdir(parents=True,exist_ok=True)
    font_manager.fontManager.addfont('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    plt.rcParams.update({'font.family':'Noto Sans CJK JP','font.size':11,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none','figure.facecolor':'white'})
    def read(p):return list(csv.DictReader((archive/p).open()))
    def save(fig,name):
        fig.savefig(output/f'{name}.png',dpi=180,bbox_inches='tight');fig.savefig(output/f'{name}.svg',bbox_inches='tight');plt.close(fig)
    auto=json.loads((archive/'automatic/summary.json').read_text());rev=json.loads((archive/'review/summary.json').read_text())
    labels=['学习规划','主动介入','成果验收','计划调整'];a=auto['full']['tracks'];r=rev['application']['tracks']
    fig,ax=plt.subplots(figsize=(10,4.5));x=np.arange(4);ax.bar(x-.18,[q['mean_score'] for q in a],.36,label='自动评分',color='#9badb4');ax.bar(x+.18,[q['mean_score'] for q in r],.36,label='逐例复核',color='#34766c')
    for i,q in enumerate(r):ax.text(i+.18,q['mean_score']+1,f"{q['mean_score']:.1f}",ha='center')
    ax.set(xticks=x,xticklabels=labels,ylim=(0,112),ylabel='最终分均值 / 100',title='48份真实应用输出：自动评分与逐例复核');ax.legend(loc='lower left');ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);fig.tight_layout();save(fig,'application-results')
    ds=['D1','D2','D3','D4','D5','D6','D7'];data=np.array([[q[d] for d in ds] for q in r]);fig,ax=plt.subplots(figsize=(11,3.5));im=ax.imshow(data,vmin=0,vmax=2,cmap='YlGnBu',aspect='auto')
    ax.set(xticks=range(7),xticklabels=['事实内容','目标需求','行动时机','约束控制','结果可用','行动适度','解释下一步'],yticks=range(4),yticklabels=labels,title='逐例复核后的七维等级均值（0—2，越高越好）')
    for i in range(4):
        for j in range(7):ax.text(j,i,f'{data[i,j]:.2f}',ha='center',va='center',color='white' if data[i,j]>1.5 else '#182522')
    fig.colorbar(im,ax=ax,pad=.02);fig.tight_layout();save(fig,'dimension-results')
    rows=read('automatic/triplets.csv');groups=sorted({q['group'] for q in rows});fig,ax=plt.subplots(figsize=(12,5));x=np.arange(8)
    for shift,key,label,color in [(-.24,'good','优质','#34766c'),(0,'mild','局部缺陷','#d8ac63'),(.24,'severe','严重缺陷','#b36c6c')]:
        vals=[[float(q[key]) for q in rows if q['group']==g] for g in groups];means=[np.mean(v) for v in vals];ax.bar(x+shift,means,.23,label=label,color=color)
        for i,v in enumerate(vals):ax.scatter([i+shift]*3,v,s=20,facecolors='white',edgecolors='#273833',zorder=4)
    ax.set(xticks=x,xticklabels=['CSV计划','二分查找','冷却剩余','跨夜免打扰','去重验收','非法配置','预算调整','旧稿恢复'],ylim=(0,115),ylabel='封顶前的加权分 / 100',title='8组三档输出 × 3次独立评分：严格排序22/24组');ax.legend(ncol=3,loc='upper right',fontsize=10);ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);fig.tight_layout();save(fig,'discrimination')
    rows=read('automatic/stability.csv');fig,ax=plt.subplots(figsize=(11,4));x=np.arange(24)
    ax.bar(x,[float(q['raw_sd']) for q in rows],color=['#34766c','#d8ac63','#b36c6c']*8);ax.set(xticks=np.arange(1,24,3),xticklabels=['CSV计划','二分查找','冷却剩余','跨夜免打扰','去重验收','非法配置','预算调整','旧稿恢复'],ylabel='3次加权分的总体标准差',title='重复评分波动：每组三柱依次为优质、局部缺陷、严重缺陷');ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);fig.tight_layout();save(fig,'repeat-stability')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,default=ROOT/'evaluation/artifacts/decisionbench-study-20260910');p.add_argument('--output',type=Path,default=ROOT/'assets/stage3');a=p.parse_args();render(a.archive,a.output)
