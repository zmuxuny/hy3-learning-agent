# 双人人工标注与自动评分对齐

两位标注者各自独立标注128条评价记录：48份应用记录各1次，24份质量对照材料各3次，共72次，以及8份评分操纵材料各1次。两人的七维等级及评分是否达标的结论逐项一致，人工一致率为100%。人工参考结果用于评价自动评分的可靠性。

`confirmation.json`保存作者对标注范围与结果的确认，以及对应标签文件的摘要。报告中的人工统计与这些已确认标签对应。

## 自动评分与人工参考的比较

48份应用记录具有完整的七维数值，用于计算自动评分与人工参考的一致程度。最终方法的达标判断有46/48相同；逐维一致率及二次加权Kappa见[最终对齐表](../decisionbench-final-method-20260910/automatic/human-alignment.csv)。Kappa考虑双方等级分布带来的偶然一致，对相差两档的判断给予更大差异权重。其余质量对照和操纵材料的标注用于逐项判断核对。

本目录`application-alignment.csv`保留对应评分记录的数值比较。最终报告采用上面链接的统一方法结果。以下命令复算本目录的对齐记录：

```bash
python evaluation/scripts/summarize_human_confirmation.py \
  --archive evaluation/artifacts/decisionbench-study-20260910 \
  --output /tmp/human-confirmation-recompute
```
