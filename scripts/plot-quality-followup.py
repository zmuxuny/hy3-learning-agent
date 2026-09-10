"""Plot all repeated scores in the new-family validation, including missing slots."""
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
ROOT=Path(__file__).resolve().parents[1]
FONT=FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',size=13)
rows=list(csv.DictReader((ROOT/'evaluation/artifacts/decisionbench-severe-validation-20260910/automatic/cases.csv').open()))
labels=['备份计划','参数练习','表单目标','订阅取消','每日上限','邮件渠道','字节统计','分页复测','图片签名','批注保留','跨项目调整','版本冲突']
fig,ax=plt.subplots(figsize=(11,4.8));fig.patch.set_facecolor('#fafcf9');ax.set_facecolor('#fafcf9')
for condition,color,label in [('good','#327a66','正常对照'),('severe','#ad6044','严重缺陷')]:
    xs=[];ys=[]
    for r in rows:
        if r['condition']!=condition:continue
        index=int(r['id'].split('-')[1]);x=index+(-.13 if condition=='good' else .13)+(int(r['repeat'])-2)*.055
        if r['score']=='':ax.text(x,-8,'无分',fontproperties=FONT,rotation=90,ha='center',fontsize=8)
        else:xs.append(x);ys.append(float(r['score']))
    ax.scatter(xs,ys,s=27,color=color,label=label,alpha=.82)
ax.axhline(70,color='#8b958f',linestyle='--',linewidth=.9)
ax.text(12.45,71,'通过线',ha='right',fontproperties=FONT,fontsize=9,color='#67766d')
ax.set_xticks(range(1,13),labels,rotation=35,ha='right')
for tick in ax.get_xticklabels():tick.set_fontproperties(FONT);tick.set_rotation(35);tick.set_ha('right')
ax.set_ylim(-12,109);ax.set_xlim(.5,12.5);ax.set_yticks([0,20,40,60,80,100])
ax.set_ylabel('最终分',fontproperties=FONT);ax.set_title('12个新场景族：正常与严重缺陷输出各评分3次',fontproperties=FONT,pad=16,fontsize=16)
ax.legend(prop=FONT,loc='center right',frameon=False);ax.spines[['top','right']].set_visible(False)
fig.tight_layout()
for ext in ['png','svg']:fig.savefig(ROOT/f'assets/stage3/severe-validation.{ext}',dpi=180)
