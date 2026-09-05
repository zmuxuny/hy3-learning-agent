# E4 输入设计与协议试跑

`primary-blueprint-v1.csv` 包含 12 个家族 × 4 轨的 48 个设计位置，全部是未复核草案。
它不是 48 个已完成 CaseSpec，更不是 48 个真实 Hy3 Episode。场景和 Oracle 仍需逐项细化。
Calibration 的 8 个种子应使用独立的 C01—C08 家族，不能从 Primary 复制后用于调优。

## 当前工作顺序

1. 先完成四轨各一例协议试跑：检查声明解析、工具对应、Recorder 覆盖与终态，保留失败。
2. 按工作单细化 CaseSpec：人工提供目标、初态、资源和判定边界；真实 Agent 产生决策与结果。
3. 将关键事实映射为可执行 seed，并比较实际前置 Snapshot。只存在于摘要、无法进入模型上下文的事实不能作为 Oracle 依据。
4. 作者自检后，由另一位复核者检查资源、隐私、允许行动、正反约束、难度及来源；争议保留裁决记录。草案不能伪造复核身份。
5. 使用独立 Development/Calibration 调整规则与 Judge，完成一致性与区分度实验，然后冻结协议和 48 个 Primary 输入。
6. 真实运行 Primary，逐 Case 验收 Episode/RuntimeFailure、调用上下文和补丁；人工复核内容后才准备正式 Benchmark 注册审查。

真实用户轨迹可作为获得许可并脱敏后的案例来源；不要直接复制生产数据库。
人工准备公开作业并在隔离环境触发 Assessment 是有效来源，不能人工编造 Hy3 的决策输出。
任何需新增的 seed 字段，应先通过实际状态及模型可见上下文的反例测试，再批量写入 Case。

## 四例试跑

```bash
PILOT_ROOT=$(mktemp -d /tmp/learning-agent-pilot-XXXXXXXX)
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/prepare_protocol_pilot.py --output "$PILOT_ROOT/suite"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m learning_agent_eval validate-dataset --dataset "$PILOT_ROOT/suite"
# 配置经授权的 OPENAI_API_KEY 进程环境后，才执行下面一条。
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m learning_agent_eval run-agent --dataset "$PILOT_ROOT/suite" --manifest "$PILOT_ROOT/suite/manifest.json" --output "$PILOT_ROOT/runtime" --model-mode real --allow-real-model --budget-ledger "$PILOT_ROOT/budget.json"
```

真实运行前通过 `ModelBudget.create` 在仓库外创建限额为 `14_000_000` 微元的账本，并将同一路径传给所有后续运行。每次请求先预留最坏金额，仅在公开 usage 数字完整且一致时结算退款；超时或 usage 缺失保留全部预留款。并发请求共享文件锁，模型自动重试关闭。

准备脚本无 Provider 调用；固定四个 `protocol_pilot` Case，各轨一个。输入来自现有工程 Case，保留来源，独立内容复核仍为 pending。试跑输出不纳入 Primary、Calibration 或能力结果。该角色不能发布为 released Benchmark。

预算方案（2026-09-05 核对）：Planning 最多 5 次、Intervention 3 次、Assessment 4 次、Revision 4 次，共不超过 16 次请求，包括子 Agent 和辅助调用。Recorder 在调用前原子扣减请求额度；SDK 自动重试关闭，单次 `n=1`、`max_tokens=16000`。达到上限保留失败，不扩大 Case 数或自动重跑。

按腾讯公布的 Hy3 最大输入 192k（保守按 196,608 Token）和输入/输出每百万 Token 1 元/4 元计算，最坏总额为 `16 × (196608 × 1 + 16000 × 4) / 1000000 = 4.169728 元`，这是试跑子预算；本轮试跑和 E4 数据生产共用 14 元总预算。推理和回答共用该输出限额。计价来源：[官方价格](https://cloud.tencent.com/document/product/1823/130055)、[Hy3 调用限制](https://cloud.tencent.com/document/product/1823/132252)。未来试跑须重新核对计价；本方案不适用于其他模型或路由。扩大到 E4 Suite 时继续使用同一个外部账本，不能重新创建账本以重置余额。

Worker 仅允许既定 Hy3 端点，数据库、资源、通知投递仍使用隔离环境。保留 Runtime 制品和请求参数；有公开 usage 时记录 Token 数，缺失时披露未知，不能按零费用处理。不保存私有思维链内容。
