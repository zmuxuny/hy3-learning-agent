# E6 · 2026-09-08 冻结批次证据

全批执行、逐轨报告与主AI自审已完成；**正式能力资格未通过（formal=false）**。48个新测试输入均生成Episode，Judge为46有效、2失败，最终6通过/40不通过/2无分。数据只作本批诊断，不从旧探索结果补齐，不删除失败或改写冻结分数。详细内容判断和后续方法计划见[E6工作记录](https://github.com/zmuxuny/hy3-learning-agent/blob/1295254e2f8f792da30eb331cb2d0ea12437fa0b/docs/E6%E9%AA%8C%E6%94%B6%E5%B7%A5%E4%BD%9C%E8%AE%B0%E5%BD%95.md)。

| 轨道 | 完整分母 | 有效Judge / 失败 | 原始均分 | Rule封顶均分 | 裁决均分 | 通过数 |
| --- | --- | --- | --- | --- | --- | --- |
| Planning | 12 | 12 / 0 | 65.4167 | 38.7500 | 38.7500 | 0 |
| Intervention | 12 | 12 / 0 | 82.2917 | 54.2500 | 54.2500 | 3 |
| Assessment | 12 | 11 / 1 | 52.9545 | 36.7727 | 36.7727 | 0 |
| Revision | 12 | 11 / 1 | 97.2727 | 55.6364 | 55.6364 | 3 |

均值仅使用有效Judge；失败为null，仍留在每轨12的通过率分母内。浏览用CSV仅将CRLF换行规范为LF，48行字段与档案原始CSV完全相同。完整精度、七维和48行分别见[summary.json](summary.json)与[cases.csv](cases.csv)。42个Rule Gate中包含行动声明、审批等待及规则适用性问题，不能解释为42次实际危险操作。[case-analysis.json](case-analysis.json)给出48例摘要绑定的AI内容归因，保留工具返回草案与实际持久化状态的区别。

运行源码固定干净`b0b0d02ced10c3c6387a6541656a7b693571e312`；测试与注册冻结提交为`c62cb62b93c65436f733c51aec6365abc9cfb9c8`。E5方法实验仍固定`40fddb8`，产品含`d8fa199`修复。本轮只新增只读报告工具，未调整Agent/评分方法或测试输入。

- Protocol SHA256：`4798b02f9dabf570ae3f7fda60119349f1b0bfde03ef8cabbfd582868d5cdc8c`
- Benchmark SHA256：`a254dea7195cc9991df54a11d2efe13a546225099bd71a95a5d353902c3e53b8`
- Suite SHA256：`e4d985ce2df21d113a20e412fea111f8d4596bbf8cad3e0228f094dc9eed498a`
- Registry SHA256：`638b82de6973d02e761460503d2094db5657761e7dbd6fd3668134b7d9e2e606`

## 失败、AI裁决和费用

F05-A为Provider错误，没有usage或响应归因；约120.18秒的请求时长不足以确认底层原因。F05-R首轮与一次结构修复均引用Rule ID作为Episode证据路径，最终无效。Judge共54次真实请求（48首轮、6结构修复），5例修复成功；保留8个不合格尝试及全部费用，见[judge-attempt-summary.json](judge-attempt-summary.json)。4次截断只保存文本摘要、字节数、结束原因，没有完整公开JSON，不重造响应。整批blockers为`capability.judge_error`和`capability.run_not_trusted`。

[semantic-reviews.json](semantic-reviews.json)含84个有效Judge候选及1个AI新增项，3确认、82驳回，0待定；身份为`primary_ai_reviewer`，不是独立人类盲标。确认项F02/F04/F11-P已被Rule封顶，裁决分数未再变化。Rule和原始Judge维度分保持不变。档案同时保留AI初稿误判及两次纠正：补读未执行工具返回草案；按完整身份而非列表顺序核对Runtime/聚合终态。

用户追加5元后原账本累计上限19元，没有以云账户17.90元重置预算。[预算汇总](budget-summary.json)：本批160请求，占用**6.003744元**（Agent2.538239、Judge3.465505），其中未知usage预留0.204028元；累计490请求、占用**17.609982元**，剩余**1.390018元**。原330条请求原样保留，无活动reserved；未知usage继续保留。未核对云账户实扣，不将占用全部称作已确认消费。最终原始账本见[budget-final.json](budget-final.json)，此前5个授权/预设文件保持调用前提交字节。

## 证据内容与离线重建

[e6-evidence.tar.gz](e6-evidence.tar.gz)保留公开runtime/rules/judge、原始aggregate及adjudicated目录、54次Judge尝试、语义裁决/草稿纠正、报告、逐例归因、费用和执行/回归日志、报告工具快照及自审脚本。包内`evidence-manifest.json`绑定每个文件的原始SHA256；目录[artifact-manifest.json](artifact-manifest.json)绑定顶层文件和压缩包（不自引用）。没有`.env`、产品数据/SQLite、Capture、私有推理、展开Judge Prompt或完整Provider包。历史run_stage.py仅作为执行命令证据，不用于离线重建，也不应再次触发付费。

在仓外干净`b0b0d02`副本、锁定依赖Python环境中解压，设`evidence`为解压目录、`rebuilt`为另一个不存在的临时输出目录，可离线执行：

```bash
PYTHONPATH=evaluation/src python "$evidence/summarize_e6_run.py" \
  --dataset evaluation/datasets/decisionbench-v1-e6-test \
  --run "$evidence" --output "$rebuilt"
cmp "$rebuilt/summary.json" "$evidence/report/summary.json"
cmp "$rebuilt/cases.csv" "$evidence/report/cases.csv"
PYTHONPATH=evaluation/src python "$evidence/audit_run.py"
```

上述命令不调用模型、不打开产品库。最后一条重写解压副本中的派生自审JSON，原档案不变。详细执行退出码与测试范围见[validation-summary.json](validation-summary.json)：36个仓外定向测试通过，两个实际CSV篡改被拒绝，冻结/来源绑定只读重建通过，制品/算术/费用自审580项通过。这些检查不赋予方法或正式能力资格。归档重建、历史保全和临时环境清理见[closure-checks.json](closure-checks.json)。本轮仅本地提交，未推送、未发布。
