# 第三阶段评测交付材料

本目录集中提供人工标注、实验结果和完整评测数据。项目图文报告见[第三阶段项目与评测报告](../第三阶段项目与评测报告.md)，产品与评测演示见[108秒Demo](../第三阶段Demo.mp4)。

## 文件入口

| 材料 | 内容与文件 |
| --- | --- |
| 完整评测数据包 | [评测数据.zip](评测数据.zip)：测试输入、原始决策证据、模型请求与响应、人工标注、统计结果及复算代码；解压后的README给出独立复算命令 |
| 两位标注者的评分表 | [application_czy.csv](人工标注/application_czy.csv) · [application_zyq.csv](人工标注/application_zyq.csv)：各48例，七维标签保留，总分经过计算核对；详细字段见[人工标注说明](人工标注/README.md) |
| 协商后参考 | [application.csv](人工标注/application.csv) · [adjudication.json](人工标注/adjudication.json)：两人讨论后采用czy标签，保存决定与摘要 |
| 协商前一致性 | [human-human-alignment.csv](实验结果/人工一致性/human-human-alignment.csv)：逐维等级一致率及Kappa；[annotation-differences.csv](实验结果/人工一致性/annotation-differences.csv)：7处等级差异 |
| 自动与最终参考逐例对照 | [application-human.csv](实验结果/主体实验/application-human.csv)：48例双方七维等级、总分、达标结论；[human-alignment.csv](实验结果/主体实验/human-alignment.csv)：七维汇总 |
| 主体实验全部结果 | [cases.csv](实验结果/主体实验/cases.csv) · [summary.json](实验结果/主体实验/summary.json)：应用48次、质量对照72次、评分操纵8次、正常与严重对照72次，共200次 |
| 质量区分与重复稳定性 | [triplets.csv](实验结果/主体实验/triplets.csv) · [stability.csv](实验结果/主体实验/stability.csv)：24组排序及24份材料各3次评分的波动 |
| 条件验证 | [cases.csv](实验结果/条件验证/cases.csv) · [summary.json](实验结果/条件验证/summary.json)：12份建议各评3次，共36次 |
| 卷积案例诊断 | [结果目录](实验结果/卷积诊断/)：既有案例的核验与评分对照，单列分析 |
| 文件完整性 | [SHA256SUMS.json](SHA256SUMS.json)：本目录标注、结果和ZIP的逐文件摘要；ZIP内部另有原始数据摘要 |

人工原始标注的逐维一致率为329/336＝97.92%，达标结论48/48相同。自动评分与协商后最终参考的达标结论46/48相同。两类一致性分别计算。

## 用例与结果对应

[完整评测用例目录](../评测用例目录.md)列出116份材料的内容与编号。每个结果文件的`id`对应具体案例；重复实验结合`part`和`repeat`定位一次评分。主体`cases.csv`中的`source`与`evidence`分别指向评分响应和被评决策证据，这些路径在解压后的数据包中保持不变。

`D1`—`D7`为0—2级维度等级。`score`为0—100的最终分，`outcome=pass`表示助手决策评分达到70，`fail`表示低于70。自动与人工对照表用`automatic_`、`human_`前缀区分双方。

## 材料生成与复算

从仓库根目录执行`python scripts/build-evaluation-delivery.py`，可根据归档数据重建本目录的结果副本、ZIP与摘要。原始标注直接保留；生成程序不改变标签或实验分数。ZIP内附独立复算命令，仓库中的[运行说明](../evaluation/README.md)还提供再次调用模型的实验命令。

独立复算检查见[verification.json](verification.json)：数据包解压至临时目录后，核对全部文件摘要，并重建主体、条件、诊断和人工一致性四组结果。
