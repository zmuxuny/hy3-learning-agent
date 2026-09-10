# 卷积条件遗漏：反例寻找与评分采纳

本档案已通过干净Git副本复算，2个结果文件逐字节一致，见[交付验证记录](../decisionbench-final-method-20260910/verification.json)。

本档案分析一份真实的学习规划输出：前面正确演示与[1]卷积保持原序列，后面的独立边界任务却概括成“单元素卷积保持原序列”。[1,2]与[2]卷积实际得到[2,4]，表明后一结论缺少元素为1的条件。原始内容见[卷积计划完整记录](../decisionbench-study-20260910/evidence/full/evidence/formal-i10-p.json)，分析正文见[项目与评测报告第6.2节](../../../第三阶段项目与评测报告.md)。

## 固定案例上的核验尝试

| 处理方式 | 材料与重复 | 结果 |
| --- | --- | --- |
| 内容提示中要求寻找反例 | 卷积规划、任务拆分、概率规划、重复提醒，4份既有输出各3次 | 卷积三次均100分；其余完整七维与误报见逐次表 |
| 单独提取断言、条件、输入和推演 | 卷积规划与任务拆分，2份既有输出各3次 | 卷积100、69、69；任务拆分92.5、100、100 |
| 固定其余核验，只替换边界任务的依据 | 同一卷积计划，自动[1]探测与经计算确认的[2]反例各送评分3次 | 自动探测100、100、100；正确反例69、85、85 |

正确反例组的三次评分均明确认出条件遗漏；两个85分结果把问题视为局部内容错误，理由是整体框架、核心验收和待批流程正确。69分结果强调该练习要求验证错误性质，影响任务可用性。因此，反例生成和影响判断需要分别检验。

这是已知案例的机制分析：固定评分实验的6次调用只改变一条独立核验记录，其余原始输出、内容核验和评分提示不变。提示尝试的18个位置用于开发诊断，均有有效结果；完整核验与评分记录保留。上述尝试不改变主体200次实验使用的冻结评测方法，也不计为新的未见场景。

## 文件与元信息

| 位置 | 内容 |
| --- | --- |
| [automatic/cases.csv](automatic/cases.csv) | 24行，含18次核验尝试和6次固定依据诊断；状态、分数、七维、具体复核意见 |
| [automatic/summary.json](automatic/summary.json) | 24个位置均有效；固定依据诊断的分数、认出错误数、未通过数 |
| [review.json](review.json) | 开发助手对全部24个位置的证据检查；原始状态与最终判断分别保存 |
| `prompt-check/` | 提示核验的设计、12份原始评分及日志；方法源码提交7636e12 |
| `independent-check/` | 独立核验的设计、6个位置的原始评分及日志；方法源码提交ed73ce1，包含原始响应与仅无效位置恢复 |
| [rating-probe/design.json](rating-probe/design.json) | 原始证据摘要、固定内容核验、两种独立核验依据、直接计算结果、重复次数 |
| `rating-probe/*/result.json` | 6次评分的原始响应、请求摘要、等级和分数 |
| `setup-error/` | 开始前因工作树Git引用定位失败而中止的日志；没有产生模型评分 |
| `run-*.py` | 真实执行时使用的批处理脚本，保留当时外部工作树路径 |

`condition_error_identified`表示复核确认核验或评分明确指出目标条件/负荷问题，和最终是否未通过分别保存。结果中的内部文件标识仅用于定位；报告使用具名案例解释内容。

## 离线复算

在仓库根目录执行，无需模型接口：

```bash
insight_output=$(mktemp -d /tmp/condition-insight-recompute-XXXXXX)
PYTHONPATH=evaluation/src:evaluation/scripts:backend .venv/bin/python evaluation/scripts/summarize_condition_insight.py \
  --archive evaluation/artifacts/condition-insight-20260910 --output "$insight_output/automatic"
diff -r evaluation/artifacts/condition-insight-20260910/automatic "$insight_output/automatic"
```

脚本逐次核对方法与证据摘要、请求摘要、原始响应解析、评分公式及复核绑定；检查固定依据诊断确实只改变同一任务的一条核验记录，并直接复算[1,2]与[2]的卷积。[图表脚本](../../../scripts/plot-final-method-results.py)读取复算表生成报告图10。
