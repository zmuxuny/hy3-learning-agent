# 学习决策评测运行说明

本目录提供学习助手的离线评测代码、样本及实验记录。评价对象为Decision Episode（关键决策片段），由状态、环境、可观察轨迹和可接受行动范围共同界定，覆盖助手建议内容、工具效果及状态变化。

## 材料入口

| 材料 | 内容与位置 |
| --- | --- |
| 方法说明 | [学习决策评测方法](../学习决策评测方法.md)：评价单位、七维等级、核验、评分与复核 |
| 完整用例目录 | [评测用例目录](../评测用例目录.md)：116份输入的具体条件、构造差异及精确文件链接 |
| 数据集 | [DecisionBench](datasets/decisionbench-learning-v1/README.md)：应用场景、三档输出、评分操纵及正常/严重对照 |
| 完整结果 | [统一方法实验档案](artifacts/decisionbench-final-method-20260910/README.md)：48份真实记录各1次、24份三种质量记录各3次、8份操纵记录各1次、24份正常/严重记录各3次，共200次评分及逐例七维、分组统计、原始响应与复算 |
| 条件与案例诊断 | [36次条件验证](artifacts/condition-validation-20260910/README.md) · [卷积案例分析](artifacts/condition-insight-20260910/README.md)：输入条件、逐次核验与评分采纳 |
| 人工参考 | [双人人工确认](artifacts/human-confirmation-20260910/README.md)：128个位置的覆盖确认；最终自动—人工数值见[对齐表](artifacts/decisionbench-final-method-20260910/automatic/human-alignment.csv) |
| 分析报告 | [项目与评测报告](../第三阶段项目与评测报告.md)：产品、方法、实验、典型案例与完整评测集附录 |

核对48例应用结果时，直接使用[参考标签 application.csv](artifacts/decisionbench-study-20260910/review/application.csv)和[自动与人工逐例对照 application-human.csv](artifacts/decisionbench-final-method-20260910/automatic/application-human.csv)，两表按`id`关联。参考表的`D1`—`D7`为0—2级参考值，`review_score`与`review_outcome`为参考总分及决策评分达标结论；对照表以`automatic_`和`human_`前缀并列最终自动值与参考值。全部关键数据的完整路径、规模和证据查找方式见[交付数据索引](../第三阶段交付说明.md#关键数据文件索引)。

## 评测流程

1. 在仓外源码副本、临时数据库与固定资源中运行应用场景，保存用户条件、完整输出、工具调用和前后状态。
2. 整理带原始位置的评分输入，由Hy3核验具体内容，再按七个维度给出等级、理由与证据引用。
3. 程序核对已覆盖的日期、数值、版本与精确测试规则，计算加权分、严重度限制后的最终分及通过结论。
4. 保存逐例、逐维、逐类结果；在方法验证中与独立人工标注比较，分析一致程度和具体分歧。

以上200次与条件检查的36次评分均采用同一固定方法，其执行参数为`learning-quality-9`；精确方法摘要与输入摘要保存在实验设计中。

| 脚本 | 作用 |
| --- | --- |
| [prepare_learning_quality.py](scripts/prepare_learning_quality.py) | 将真实应用运行整理为完整评分证据 |
| [run_learning_quality.py](scripts/run_learning_quality.py) | 调用内容核验与七维评分，保存全部响应、失败与费用 |
| [summarize_final_method.py](scripts/summarize_final_method.py) | 核验所有最终原始结果、重复次数、证据与复核绑定，复算200次评分及完整统计 |
| [summarize_condition_validation.py](scripts/summarize_condition_validation.py) | 核验36个条件位置、各恢复响应及具体缺陷复核 |
| [summarize_condition_insight.py](scripts/summarize_condition_insight.py) | 核验卷积等既有案例的诊断响应及固定反例比较 |
| [summarize_decisionbench_review.py](scripts/summarize_decisionbench_review.py) | 核对原始应用参考标签与证据并汇总 |

## 离线复算

安装环境见[项目README](../README.md)。以下命令在仓库根目录运行，不调用模型：

```bash
study_output=$(mktemp -d /tmp/decisionbench-recompute-XXXXXX)
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/summarize_final_method.py \
  --archive evaluation/artifacts/decisionbench-final-method-20260910 \
  --output "$study_output/automatic"
diff -r evaluation/artifacts/decisionbench-final-method-20260910/automatic "$study_output/automatic"
```

复算核对输入、原始请求、响应、解析结果、恢复位置及评分计算。逐项数据字典、应用原始运行来源和新实验步骤见[完整档案](artifacts/decisionbench-final-method-20260910/README.md)。

## 新的模型评分

通过环境变量提供`OPENAI_API_KEY`、`OPENAI_API_BASE`和`MODEL_NAME`，并使用自己的授权费用账本。输出写入新的仓外目录。以下命令对24份正常/严重对照各评分3次：

```bash
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/run_learning_quality.py \
  --suite evaluation/datasets/decisionbench-learning-v1/extensions/severe-validation-20260910/suite.json \
  --output /tmp/decisionbench-new-scores \
  --method learning-quality-9 --repeats 3 \
  --budget-ledger /absolute/path/to/authorized-budget.json
```

版本通过上述参数显式固定。新的模型调用产生新的结果，另行保存；接口与格式故障单列。数据集预设的质量标签不发送给评分模型。

## 目录结构

`datasets/`保存输入与独立标签；`src/learning_agent_eval/`保存评测实现；`scripts/`保存运行与复算脚本；`artifacts/`保存实验记录；`tests/`保存工程验证。此前实验按原始数据来源保留，最终验收以本页链接的统一结果为入口。[数据集版本索引](datasets/README.md)说明其他目录的用途。
