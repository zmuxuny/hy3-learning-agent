# E4 经复核候选数据

当前入口包含 48 Primary、24 Calibration、8 个来源 Case、资源、Split/Mutation 清单与摘要绑定的逐例裁决。
80 条内容裁决和 8 组三档判断见 [内容检查记录](content-review.json)，72 行索引见 [复核表](review-worksheet.csv)。
阅读 [Dataset Card](DATASET_CARD.md) 了解构造、历史运行及正式使用边界。

`primary/` 和 `calibration/` 是当前协议下独立可验证的 Case Suite；运行输出另存。
production registry 为空；当前 Primary 家族已用于探索复查，不能作为未见正式测试集。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/check_e4_candidates.py evaluation/datasets/decisionbench-v1-candidate --require-review
```

历史输入和失败见 [旧运行档案](../../artifacts/e4-candidate-20260905/README.md)，
交接时的完整输入另存于 [E4 验收档案](../../artifacts/e4-acceptance-20260906/README.md)。
