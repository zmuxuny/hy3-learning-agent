# E4 候选数据

输入：48 Primary、24 Calibration、8 个 Calibration 源 Case、Split/Mutation 清单与待填写复核表。
阅读 [Dataset Card](DATASET_CARD.md) 了解构造方法和使用限制。

`primary/` 和 `calibration/` 是独立可验证的 Case Suite。运行输出另行保存，输入不混入模型回答。
目前未注册 production Benchmark；所有结果均为 non-formal。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/check_e4_candidates.py evaluation/datasets/decisionbench-v1-candidate
```
