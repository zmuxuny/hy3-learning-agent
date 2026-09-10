# 学习决策评测集

**DecisionBench** 是本项目“学习决策评测集”的名称：用用户条件、系统行为和实际结果来评价学习助手的规划、介入、验收与调整。

当前交付入口是 [decisionbench-learning-v1](decisionbench-learning-v1/README.md)。阅读报告、运行实验和查看完整结果均从该入口开始。

| 数据 | 用途 | 如何理解 |
| --- | --- | --- |
| 当前应用样本 | [48个学习决策输入](decisionbench-learning-v1/application/manifest.json) | 四类场景各12个，交给真实Hy3应用执行；样本输入和模型输出分开保存 |
| 内容核验开发样本 | [三档构造输出](learning-quality-validation-v1/README.md)及历史真实轨迹 | 已用于发现评测漏检，属于开发证据 |
| 最终方法验证样本 | [24份三档输出](decisionbench-learning-v1/method-validation/README.md)与[8份对抗输出](decisionbench-learning-v1/adversarial/README.md) | 检验好中差排序、重复评分和对抗输出；构造输出不计作真实应用运行 |

## 历史目录与用途

此前将软件协议版本与实验批次写进了数据目录名。这些名字表示不同用途和冻结时间，不能按版本数字大小挑选“最好的数据集”。历史文件被原实验清单按路径和摘要引用，继续保留供复算。

| 旧名称 | 实际含义 | 当前用途 |
| --- | --- | --- |
| decisionbench-v1、v1-candidate | 最初的48个决策输入与24个质量校准设计 | 历史设计及来源 |
| decisionbench-v3-engineering、v4-engineering | 用于检查隔离、记录与规则的工程测试 | 软件回归，不是产品能力榜单 |
| decisionbench-v1.*-regression | 对应某版评测协议的校准和工程回归 | 旧实验复算；1.17是当前工程协议候选 |
| decisionbench-*-e6-test、*-e6-final-test | 此前冻结的真实应用实验输入 | 历史运行来源；1.13的48个输入被当前应用集显式复用 |
| decisionbench-v1.15-validation | 工程补修后的定向验证输入 | 历史验证 |
| e6-repair-development-* | 开发时的错误复现与边界用例 | 历史开发 |
| e7-development、e7-method-controls、e7-test-* | 暂停的产品版本比较及其开发对照 | 历史保留，当前报告不使用产品比较结论 |
| learning-quality-validation-v1 | 内容质量方法的开发样本 | 已见开发样本，不再称独立测试 |

历史结果入口位于 [artifacts](../artifacts)。每个实验以自己的输入清单、源码提交和方法摘要确定身份。当前交付采用有明确用途的目录名，不继续把工程协议版本当作数据集名称。

## 正常与严重错误对照

[新场景中的严重失败识别](decisionbench-learning-v1/extensions/severe-validation-20260910/README.md)位于统一数据入口的extensions中：12个场景各有正常与严重输出，共24份，评分72次；与48份应用输入、24份三档输出、8份操纵输出共同构成104份输入、200次评分的最终实验。具体场景在方法冻结后构造。

## 条件适用范围验证

[条件核验对照](decisionbench-learning-v1/extensions/condition-validation-20260910/README.md)包含6组成对材料、12份输出，各评3次，检验平方与开方、升序拼接、整除、准确率合并、任务单段长度及负荷变化。与主体104份输入合计116份；主体200个评分位置与条件专项36个位置分别汇总，方法相同。
