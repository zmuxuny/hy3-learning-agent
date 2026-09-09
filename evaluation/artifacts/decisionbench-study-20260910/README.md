# 学习决策评测：完整实验与复核

2026-09-10归档。应用由Hy3真实生成48份输出；方法验证24份作者构造输出各评3次；8份评分操纵对抗输出各评1次。评分使用同一冻结方法。128个评分位置均有有效结果，并已逐例复核。

| 结果 | 完整数据 |
| --- | --- |
| 应用自动评分：四类场景各12例 | [逐例](automatic/cases.csv)、[逐维](automatic/dimensions.csv)、[逐类](automatic/tracks.csv) |
| 应用逐例复核：46例通过、2例未通过；14例存在缺陷 | [逐例](review/application.csv)、[逐维](review/dimensions.csv)、[逐类](review/tracks.csv) |
| 判别力：22/24组三档严格排序，优质高于严重缺陷23/24组 | [三档结果](automatic/triplets.csv) |
| 重复一致性：加权分标准差均值4.56，最终分3.28 | [逐输出波动](automatic/stability.csv) |
| 对抗：8份严重缺陷输出追加评分操纵文字后，0份误判通过 | [配对结果](automatic/adversarial.csv) |
| 全部128次复核 | [意见表](review/reviews.csv)、[证据绑定的原始意见](evidence/review-notes.json) |

应用自动评分48例均通过；复核改变8例的维度等级，其中2例从通过变为未通过。自动分不被复核覆盖。方法验证的判别与一致性指标始终按原自动结果计算。复核者为开发助手AI，未报告独立双人人工一致性。

## 原始证据与运行故障

[evidence/design.json](evidence/design.json)保存运行前固定的方法、输入摘要、分片与重复次数。`evidence/full`包含公开评分材料与原始运行、规则、参考的来源链；`validation`和`adversarial`包含公开输入与未发送给评分模型的构造标签。`*-results-*`保留原始请求摘要、响应、解析结果与费用凭证号。

应用评分最初34例有效、14例无分；12例通过解析错误修复恢复，2例使用预先固定的一次无分补充周期恢复。方法验证最初69次有效、3次无分，3次均通过同一解析修复恢复。附加标签曾被错误拒绝；修复不改变提示、量表、公式或原字节。已有有效分数没有重跑。所有原失败与恢复链接均保留。

[application-runs.tar.gz](application-runs.tar.gz)保存应用各次运行及失败清单；`evidence/full/selection.json`列出首次成功轨迹的选择。一个场景在两次运行故障后进行了补充运行，原失败没有删去。[development-records.tar.gz](development-records.tar.gz)保留方法开发阶段原始实验和调度记录，不能混入最终验证统计。

## 使用

[在浏览器查看案例与图表](viewer.html)；这是归档结果浏览器，展示已完成实验。

- [复算与重跑](REPRODUCE.md)
- [分析报告](../../../docs/第三阶段项目与评测报告.md)
- [评测方法](../../../docs/学习决策评测方法.md)
- [数据集](../../datasets/decisionbench-learning-v1/README.md)

金额以唯一授权账本为准。`budget-at-experiment-close.json`保存实验结束时的累计摘要，含既有历史调用及未知用量的预留费用，不能作为本轮新增消费额。
