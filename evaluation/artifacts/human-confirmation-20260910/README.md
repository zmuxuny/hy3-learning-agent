# 双人人工标注与自动评分对齐

两位标注者分别评价48个应用案例，给出七维等级和决策评分是否达标的结论。两份评分表为[application_czy.csv](../../../交付材料/人工标注/application_czy.csv)和[application_zyq.csv](../../../交付材料/人工标注/application_zyq.csv)。讨论后，两人共同认可czy的标注，最终参考为[application.csv](../../../交付材料/人工标注/application.csv)，协商决定见[adjudication.json](../../../交付材料/人工标注/adjudication.json)。

总分由七维等级及严重度上限计算。zyq上传表中的7行计算误差已更正，维度等级与达标结论未变；[计算对照](../../../交付材料/人工标注/score-corrections.csv)与[上传原件](../../../交付材料/人工标注/原始上传/application_zyq.csv)分别保存。

## 协商前的一致性

| 比较对象 | 相同数量 | 一致率 |
| --- | --- | --- |
| 七维等级逐项比较 | 329 / 336（48例×7维） | 97.92% |
| 每例七维全部相同 | 41 / 48 | 85.42% |
| 决策评分是否达标 | 48 / 48 | 100% |

7处等级分歧分别涉及学习目标2处、行动时机3处、行动适度2处，均相差一级。逐维统计见[human-human-alignment.csv](human-human-alignment.csv)，逐项分歧见[annotation-differences.csv](annotation-differences.csv)，其中`left`表示czy，`right`表示zyq。

## 自动评分与协商后参考的比较

自动评分与最终参考在48例中的46例给出相同达标结论，一致率95.8%。逐维直接一致率分别为：事实与内容正确性83.3%、学习目标与需求95.8%、行动选择与时机97.9%、约束与用户控制100%、结果可用性93.8%、行动适度与副作用100%、解释与下一步93.8%。[application-alignment.csv](application-alignment.csv)列出最终方法的七维直接一致率；[application-human.csv](../decisionbench-final-method-20260910/automatic/application-human.csv)并列双方每例的等级、总分和结论。[confirmation.json](confirmation.json)保存两份标注、最终参考的文件摘要及全部比较结果。

## 离线复算

从仓库根目录执行，不调用模型。脚本按案例`id`对齐两份独立标注，核对协商决定与文件摘要，再分别计算协商前一致性及自动与最终参考的一致性。

```bash
python evaluation/scripts/summarize_human_confirmation.py \
  --annotations 交付材料/人工标注 \
  --archive evaluation/artifacts/decisionbench-final-method-20260910 \
  --output /tmp/human-confirmation-recompute
```
