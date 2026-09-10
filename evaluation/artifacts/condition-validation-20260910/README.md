# 条件适用范围与任务负荷验证

使用与主体实验相同的冻结方法，对6组成对材料、12份输出各评分3次，共36个位置。实验检查一般性结论是否遗漏条件，以及满足总量或周预算的任务建议是否同时满足单段长度与等量拆分要求。

## 结果

| 指标 | 固定分母与结果 | 含义 |
| --- | --- | --- |
| 正常对照通过 | 18 / 18 | 6份条件正确输出各评分3次，均达到70分通过线 |
| 缺陷输出未通过 | 18 / 18 | 6份条件错误或负荷矛盾输出各评分3次，均低于70分 |
| 明确识别目标问题 | 18 / 18 | 复核确认核验或评分提供了有效反例、算式或单段长度矛盾 |
| 有效评分 / 无分 | 36 / 0 | 全部预定位置有有效结果，逐例核对完成 |

输入、具体正常与缺陷原句及数学依据见[数据说明](../../datasets/decisionbench-learning-v1/extensions/condition-validation-20260910/README.md)。这是条件明确的受控短材料；真实卷积长计划中，前后任务间的条件借用和错误影响判断另见[案例分析](../condition-insight-20260910/README.md)。

重复中的通过结论一致，逐维归因仍有差异。例如“整数除法分配”的一次评分正确识别数学反例，却把“提交计算后等待审阅”误读为已提交，额外扣了行动与控制维度；这些自动判断与复核意见完整保留。

## 文件与数据字典

| 文件 | 内容 |
| --- | --- |
| [automatic/cases.csv](automatic/cases.csv) | 36行，具体案例名、重复索引、状态、七维、加权分、最终分、通过结论、证据与复核 |
| [automatic/repetition.csv](automatic/repetition.csv) | 每份输出三次评分的有效数、均分与总体标准差 |
| [automatic/summary.json](automatic/summary.json) | 固定分母、通过/未通过、缺陷识别数与复核覆盖 |
| [review.json](review.json) | 开发助手对36个位置逐项核对，绑定证据和评分摘要 |
| [design.json](design.json) | 冻结方法、源码提交、输入摘要、重复次数及最初运行规则 |
| [recovery-amendment.json](recovery-amendment.json) | 作者追加的仅无效位置恢复授权，规定输入、方法与索引不变，保留首个有效 |
| `runs/`、`recovery/`、`recovery-2/` | 全部原始请求摘要、模型响应、解析判断和状态；原始无效记录与有效结果分别保存 |
| [budget.json](budget.json) | 从全部原始响应凭证对应唯一费用账本的请求记录 |

`score`为最终分，`raw_score`为七维加权分，`D1`—`D7`对应[方法说明](../../../学习决策评测方法.md)顺序。`reviewed_condition_error`记录核验或评分是否明确识别目标问题，与`outcome`是否通过分别统计；`original_status`保存原始有效性。20个原始无效位置及恢复响应可从日志逐项核对。

## 离线复算

在仓库根目录执行，无模型调用：

```bash
condition_output=$(mktemp -d /tmp/condition-recompute-XXXXXX)
PYTHONPATH=evaluation/src:evaluation/scripts:backend .venv/bin/python evaluation/scripts/summarize_condition_validation.py \
  --archive evaluation/artifacts/condition-validation-20260910 --output "$condition_output/automatic"
diff -r evaluation/artifacts/condition-validation-20260910/automatic "$condition_output/automatic"
```

脚本校验输入与标签摘要、方法与源码绑定、所有原始请求和响应、有效结果选择、恢复编号、评分公式及复核绑定。输入源代码提交为`1295254e2f8f792da30eb331cb2d0ea12437fa0b`，方法摘要与主体200位置相同。`build-stage3-condition-inputs.py`保存构造规则，`run-*.py`与`recover-*.py`保存实际执行脚本。新的真实评分可使用[运行脚本](../../scripts/run_learning_quality.py)，显式指定本组`suite.json`、`learning-quality-9`及3次重复，另存结果。
