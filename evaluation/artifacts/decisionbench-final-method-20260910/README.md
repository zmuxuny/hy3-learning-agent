# 学习决策评测：统一方法的完整结果

本档案汇总同一冻结评测方法对104份输入的200次评分：48份真实应用决策各评1次，24份三档输出各评3次，8份评分操纵输出各评1次，24份正常/严重对照各评3次。模型输入、原始响应、逐维判断、错误与复核均保留。

## 实验结论

| 实验 | 固定分母与结果 | 可核对的数据 |
| --- | --- | --- |
| 三档判别 | 8组各比较3次；严格排序23/24，优质高于严重23/24；最终分排序亦23/24 | [triplets.csv](automatic/triplets.csv)：三档各次加权分与排序结论 |
| 重复评分 | 24份三档输出各评3次；加权分标准差均值3.6963，最终分3.3681；通过结论多数占比97.22%，七维向量75.00% | [stability.csv](automatic/stability.csv)：逐输出波动与一致比例 |
| 评分操纵 | 8个严重输出变体；1个误通过、7个未通过 | [adversarial.csv](automatic/adversarial.csv)：与未操纵输出的差值 |
| 正常/严重对照 | 12组各2份、各评3次；正常36/36通过，严重36/36识别为严重且未通过 | [cases.csv](automatic/cases.csv)：筛选`part=new_scenarios` |
| 应用决策 | 48份自动均通过；固定人工参考46通过、2未通过；通过一致46/48 | [application-human.csv](automatic/application-human.csv)：逐例、逐维双列 |
| 全量复核 | 200/200个评分位置核对完成 | [review.json](review.json)：原始证据及结果摘要绑定、七维检查和具体分歧 |

三档未正确排序的一组来自“跨夜免打扰”：助手实际关闭免打扰并发通知，一次自动评分把介入目标误当成修改设置的授权，给100分。对应操纵输出同样被低估为局部问题，给72.5分。其他重复中的严重失败结论、这些误判及复核意见同时保存。

## 文件与字段

| 文件 | 元信息与用途 |
| --- | --- |
| [cases.csv](automatic/cases.csv) | 200行；每行一个案例的一次评分。`part`为应用、三档、操纵或正常/严重对照；`repeat`为固定重复索引；`D1`—`D7`对应方法说明中的七维；同时保留加权分、最终分、严重度、通过结论及来源路径 |
| [tracks.csv](automatic/tracks.csv) | 按数据部分和四类决策汇总，列有效、通过、未通过、无分与均分 |
| [dimensions.csv](automatic/dimensions.csv) | 每个数据部分×决策类别×维度的等级均值，等级范围0—2 |
| [human-alignment.csv](automatic/human-alignment.csv) | 48个应用案例的逐维直接一致率与二次加权Kappa；无定义记空值 |
| [summary.json](automatic/summary.json) | 所有固定分母、错误恢复数、复核覆盖及主要指标 |
| [design.json](design.json) | 方法、源码提交、输入摘要、重复次数与失败恢复规则 |
| `runs/`、`recovery/` | 128次评分的完整原始请求摘要、响应、解析判断及仅无分恢复记录 |
| [budget.json](budget.json) | 由原始响应中的费用凭证逐一对应唯一账本，保存本次请求计费记录 |

`raw_score`是七维加权分；`score`是应用严重度上限后的最终分；`outcome`为通过、未通过或无分。`original_status`保存补评前的状态。原始响应解析失败不会转成零分，也不会删除该次尝试。无效评分共3个位置，其中本档案1个、正常/严重对照原档案2个，均按预定规则恢复，最终无分0。

## 输入与来源

应用输入与其48份真实生成记录分别保存在[数据集](../../datasets/decisionbench-learning-v1/README.md)和[原始应用证据](../decisionbench-study-20260910/evidence/full/)。此处读取固定的完整决策记录重新评分，不改变产品输出。

全部结果的方法摘要为`b473189f42564de0401fe93aa1a9d2db30757ed002ebdde2322b932fbf8248b3`。本档案128次评分的源码提交为`1295254e2f8f792da30eb331cb2d0ea12437fa0b`；其余72次已经使用相同方法、输入及请求结构，保留在[正常/严重对照原始档案](../decisionbench-severe-validation-20260910/README.md)，不重复调用。复算逐次验证方法一致及原始证据绑定。

固定人工参考的来源见[人工确认档案](../human-confirmation-20260910/README.md)。两位人工的确认覆盖128个原始评分位置；本次新自动响应由开发助手逐项复核，人工身份不延伸到新响应。

## 离线复算

已在干净Git副本中复算主体、条件验证、案例诊断及正常/严重对照档案，18个结果文件均与归档逐字节一致。[交付验证记录](verification.json)列出源码提交、文件摘要、相关测试和PDF、视频检查结果。

在仓库根目录安装项目环境后执行，不调用模型：

```bash
study_output=$(mktemp -d /tmp/decisionbench-final-recompute-XXXXXX)
PYTHONPATH=evaluation/src:evaluation/scripts:backend .venv/bin/python evaluation/scripts/summarize_final_method.py \
  --archive evaluation/artifacts/decisionbench-final-method-20260910 \
  --output "$study_output/automatic"
diff -r evaluation/artifacts/decisionbench-final-method-20260910/automatic "$study_output/automatic"
```

脚本检查原始应用运行与导出证据、输入摘要、方法摘要、请求摘要、原始响应解析、每个有效结果的首个有效选择、恢复索引、分数计算和复核绑定。图表由`scripts/plot-final-method-results.py`从这些表重建。

## 新的真实评分

通过环境变量配置自己的接口密钥、接口地址和模型，并使用授权费用账本。以下命令对24份三档输入各评3次，输出另存仓外：

```bash
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/run_learning_quality.py \
  --suite evaluation/datasets/decisionbench-learning-v1/method-validation/suite.json \
  --method learning-quality-9 --repeats 3 \
  --output /tmp/decisionbench-new-quality-results \
  --budget-ledger /absolute/path/to/authorized-budget.json
```

应用生成在仓外副本与临时库运行，完整命令见[原始应用复现说明](../decisionbench-study-20260910/REPRODUCE.md)。`run-batch.py`、`recover-batch.py`和执行日志保存本次批量运行的原始操作；上面的相对路径命令用于新的环境。
