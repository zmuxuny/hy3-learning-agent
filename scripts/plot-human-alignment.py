"""Plot author-confirmed human reference alignment from reproducible statistics."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
ROOT=Path(__file__).resolve().parents[1]
font=FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',size=13)
summary=json.loads((ROOT/'evaluation/artifacts/human-confirmation-20260910/confirmation.json').read_text())
labels=['事实内容','学习目标','行动时机','用户控制','结果可用','行动适度','解释步骤']
values=[r['automatic_human_agreement']*100 for r in summary['application']['dimensions']]
fig,ax=plt.subplots(figsize=(10,4.5));fig.patch.set_facecolor('#fafcf9');ax.set_facecolor('#fafcf9')
ax.barh(labels[::-1],values[::-1],color='#377c68',height=.6)
for tick in ax.get_yticklabels():tick.set_fontproperties(font)
ax.set_xlim(0,108);ax.set_xticks([0,25,50,75,100],['0%','25%','50%','75%','100%'])
for i,v in enumerate(values[::-1]):ax.text(v+1,i,f'{v:.1f}%',va='center',fontsize=12)
ax.set_title('48个应用案例：自动评分与人工标签逐维一致率',fontproperties=font,pad=15,fontsize=16)
ax.spines[['top','right','left']].set_visible(False);ax.tick_params(axis='y',length=0)
fig.tight_layout();target=ROOT/'assets/stage3'
for ext in ['png','svg']:fig.savefig(target/f'human-alignment.{ext}',dpi=180)
