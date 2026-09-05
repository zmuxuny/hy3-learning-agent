# E4 输入设计与真实运行

`primary-blueprint-v1.csv` 是 12 个家族 × 4 轨的设计索引，已与实际输入和seed逐条对齐。
用户授权的 AI 逐例内容裁决已完成，身份为 `delegated_ai_reviewer`。48 个可执行输入及24个受控 Calibration 在
[候选数据包](../datasets/decisionbench-v1-candidate/README.md)中构建。

## 已执行与内容复核

48 Primary 在干净 `63be8da` 由真实 Hy3 执行，得到 45 Episode + 3 RuntimeFailure；
24 Calibration 使用独立 C01–C08 家族的受控响应，得到 24 Episode + 0 Failure。
原始终态和失败均在 [运行档案](../artifacts/e4-candidate-20260905/README.md)，没有挑选成功重跑替换。
`bd89530` 离线修复其中两类读引用导出错误；原批次仍维持原有身份与失败记录。
`df8e8e7` 又修复模型可见画像中的控制 ID 泄漏并重建 24 个 Calibration；旧批次仅供探索复查。

80 条 AI 内容记录和 8 组三档裁决绑定最终 Case/资源摘要；作者与复核者可能共享模型及上下文，未做独立人类标注。当前修订 Primary 未新增真实执行；24 条受控 Calibration 已在当前绑定下重新导出，见 [E4 验收证据](../artifacts/e4-acceptance-20260906/README.md)。
production registry 为空。E4 内容与工程验收已完成并暂停；原 DoD 中的正式冻结/登记明确移至 E5 方法稳定后、E6 前，承诺保留。

## 后续数据流程

1. 核对每 Case 的目标、实际临时状态、时间、资源、授权、作业附件与判定边界。
2. 逐例内容裁决已由AI承担并保留；独立人类一致性仍是另外的实验，不能以本次记录替代。
3. 仅用 Development/Calibration 验证与调整评测方法，再冻结正式协议和 Benchmark。
4. 以固定配置执行正式 Primary，保留每 Case 唯一 Episode/Failure 终态及四轨分母。

当前 S01–S12 已标为探索输入；正式 Primary 测试不得参与 Agent、Prompt、Rules 或 Judge 调优。
根据测试输出修改决策语义时，该轮转为探索记录，并另建未参与调优的测试家族。
基础设施修复也必须留下原提交、失败和新协议摘要，不能给旧制品换身份。

真实用户轨迹只能在获得许可和脱敏复核后作为案例来源；不能直接复制生产数据库。
当前数据是作者构造的独立快照与合成验收材料，不证明真实用户学习效果。
人工准备环境与公开作业、让 Hy3 真实验收是可行路径；人工编造 Hy3 的决策不能成为 Primary。

## 可执行命令

以下命令只准备并检查输入，不调用 Provider。输出目录必须不存在：

```bash
E4_ROOT=$(mktemp -d /tmp/learning-agent-authoring-XXXXXXXX)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/build_e4_candidates.py --output "$E4_ROOT/inputs" --review-records evaluation/datasets/decisionbench-v1-candidate/content-review.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/check_e4_candidates.py "$E4_ROOT/inputs" --require-review
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/prepare_protocol_pilot.py --output "$E4_ROOT/pilot"
```

四个 `protocol_pilot` 输入来自工程 Case，明确标注来源与待复核状态；该角色永远不能登记为
released Benchmark。它们只检验协议与运行工程。真实执行通过 `run-agent --model-mode real
--allow-real-model --budget-ledger <既有共享账本>` 显式启用。S06 修复与 E5 接续已获授权，
使用同一预算，真实 Judge 的计费接入及实验顺序见 [E5 开发交接](../../docs/E5开发交接.md)。

## 费用与限制

用户授权本轮试跑及 Primary 生产共享 **14 元**。早期 16 请求/4.169728 元子预算方案已经被后续
经本轮总限额约束的工程试跑与 E4 运行取代，不是实际总请求次数。
本轮实际 190 次请求：公开 usage 估算 4.355213 元，4 次未知费用保留 1.042432 元，合计占用 5.397645 元。
这不是提供商实际账单，也不能据此声称账户余额。

持久账本位于 `/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json`，权限 0600。
后续恢复必须复用它，不能重建账本重置额度。每次请求先按输入最多 196,608 Token、输出最多
16,000 Token 预留 0.260608 元；完整一致的 usage 才结算退款，未知费用不按零处理。
共享文件锁和 fsync 保证并发记账；`n=1`，SDK 自动重试关闭，推理与回答共用输出限额。

真实主 Runtime 上限为 8 轮/8 次模型请求/16 次工具调用；pilot 的统一 Recorder 另有每 Case
24 次上限，包含子 Agent 和辅助调用。所有调用仍受同一个资金账本约束，不能靠子调用绕过。
计价依据：[腾讯 Hy3 价格](https://cloud.tencent.com/document/product/1823/130055)、
[调用限制](https://cloud.tencent.com/document/product/1823/132252)；恢复前应再次核对，不能套用于其他模型或路由。

Worker 只允许既定 Hy3 端点；数据库、资源和通知投递仍隔离。公开轨迹保存消息、工具 Schema、
请求参数与 usage，不保存密钥或私有思维链。当前不运行真实 Judge，也不把固定响应测试当作校准实验。
