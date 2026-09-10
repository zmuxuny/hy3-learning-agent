# 应用案例的人工标注

两位标注者分别评价同一批48个应用案例；讨论后共同采用czy的标注作为最终参考，用于检验自动评分。

| 文件 | 内容 |
| --- | --- |
| [application_czy.csv](application_czy.csv) | czy的48例原始标注，保留原始文件字节 |
| [application_zyq.csv](application_zyq.csv) | zyq的48例原始标注，保留原始文件字节 |
| [application.csv](application.csv) | 协商后采用的最终参考，与czy文件完全相同 |
| [adjudication.json](adjudication.json) | 协商决定、采用文件、范围及三份CSV的SHA-256摘要 |

`id`为案例编号；`D1`—`D7`为七维等级，范围0—2；`review_score`、`review_outcome`为原表记录的参考总分及达标结论；`note`为判断说明。原始表中的`automatic_`字段保留其导出上下文，最终自动评分见[逐例对照表](../../evaluation/artifacts/decisionbench-final-method-20260910/automatic/application-human.csv)。

协商前逐维一致率为329/336＝97.92%，达标结论为48/48＝100%。[统计档案](../../evaluation/artifacts/human-confirmation-20260910/README.md)提供逐维统计、7处差异和复算命令；最终自动与人工的达标结论一致率为46/48＝95.8%。所有CSV按`id`关联。
