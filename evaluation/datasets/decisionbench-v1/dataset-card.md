# DecisionBench v1 Dataset Card

## 当前状态

本目录包含两个明确分层的 Mini 集合：

| 集合 | Episode | 用途 | 是否为正式结果 |
| --- | --- | --- | --- |
| E0 手工协议夹具 | `P/I/A/R-MINI-001` | 验证 v1 Schema、Action Envelope、引用、摘要与隐私 | 否；未执行 Runtime |
| E1 Runtime Mini Fixture | `P/I/A/R-E1-MINI-001` | 验证隔离 Worker、生产 Runtime/工具、Recorder、Outbox 与最小导出链路 | 否；固定 stub 响应 |

E1 Fixture 的执行结果按需输出到系统临时目录，不作为数据集静态 Episode 提交。它们不属于规划中的 48 个 Primary Episodes 或 24 个 Calibration Outputs，也不代表真实 Hy3 表现。当前目录仍不能称为完整 DecisionBench v1，不能生成正式分数、排名或能力结论。

## 来源与构造

- 所有学习者、计划、任务、证据、通知内容和资源均为公开合成事实，不来自真实用户；
- E0 Episode 为 `hand_authored`、`manual_protocol_fixture`，未运行模型、数据库、工具或 Guard；
- E1 Fixture 固定 frozen time、`Asia/Shanghai`、逻辑 ID、初始状态和 scripted tool-call ID；默认 `invocation_mode=stub`、`formal_evaluation_result=false`；
- E1 固定响应仍经过生产 `AgentRuntime` 的消息组装、工具协议、数据库提交和终态收口；
- Fixture 只引用独立 `oracles/mini/` 文件；Oracle 不进入模型可见 Context，也不用于回填 runtime result；
- `resources/e1-mini/snapshot.json` 只包含完全合成文本和 `.test` 保留域 URL，并由现有 canonical JSON / SHA-256 固定；
- 作者与复核者只使用角色名，不记录个人身份。

## E1 四轨覆盖

| Fixture | 生产路径 | 最小可观察结果 |
| --- | --- | --- |
| `P-E1-MINI-001` | `plan_proposal_create` | 创建待审阅 `PlanProposal`，不直接采纳 |
| `I-E1-MINI-001` | `notification_send` + Guard + Outbox | Guard allowed，持久化 Intervention/Notification/SMTP Outbox/accepted Receipt |
| `A-E1-MINI-001` | `submission_check` | 基于合成证据持久化 revision verdict |
| `R-E1-MINI-001` | `plan_patch` | 产生可逆 `Operation` 和实际计划变更 |

Intervention 的 blocked、SMTP、Web Push、replay/idempotency 和未知 destination 路径由定向测试覆盖，不用额外 Fixture 冒充第五个轨道样本。

## 划分与泄漏策略

全部 Mini 均属于 `dev`，并使用互不相同的 `scenario_family_id`。校验器按场景族强制 Dev/Test 不相交；未来 Calibration 变体必须继承种子场景的 Split。正式规划仍是 `S01–S04` 属于 Dev、`S05–S12` 属于 Test；当前没有创建这些 Primary 场景，也没有用 Test 标签调整 Prompt、Oracle、规则或 scripted response。

## 隔离与外部副作用

每个 E1 Episode 在独立 Worker 和系统临时目录中执行。Worker 在导入 `app.*` 前禁用 `.env`、绑定绝对临时 SQLite、关闭 Scheduler 和 Email Reply Polling，并安装文件、数据库、网络、SMTP/Web Push/IMAP 与 subprocess 陷阱。stub 模式网络调用为零，未知资源失败关闭，不回退实时搜索、HTTP 或 DNS。

E1 的 `fake_outbox` 指“完整生产 Outbox 协议 + Recording Delivery Sink”：生产 `notification_send`、Notification Guard、Intervention/Notification、OutboxAction claim/fence/idempotency 和 OutboxReceipt 都真实运行，只在 Dispatcher 的最终 SMTP/Web Push Provider 边界替换 Transport。SMTP Receipt 为 `accepted`，Web Push 保持 `delivered`；Agent 只观察原始 `pending_delivery`，不会收到模拟 Receipt。

## 隐私与个人信息

数据和运行产物不得包含真实姓名、邮箱、电话、通知地址、Push endpoint/keys、账号、用户数据库行、`.env`、API Key、认证 Token 或模型私有推理。Recorder 在投影前递归丢弃 `reasoning_content`、`reasoning`、Chain-of-Thought 同义字段，不复制、缓存、散列、计数、导出或评价它们；system message 正文也不发布，只保留稳定占位与独立 version/digest。最终产物再次通过 `privacy_issues` 对字段名和文本值失败关闭。

Recording Sink 仅保留稳定 action identity、destination、安全请求/载荷摘要和字段元数据；不保存完整载荷或路由材料。数据库摘要只针对 Worker 内合成数据的规范化逻辑投影，不读取或散列真实数据库。

## 适用范围与限制

当前适合：E0 契约回归、E1 隔离工程回归、生产 Runtime/工具/Outbox seam 验证和确定性检查。

当前不覆盖：通用 Episode Exporter、通用 Normalizer、完整 State Delta、Rule Evaluator、Hy3 Judge、人工盲标、Calibration Mutation、有效性实验、结果聚合、正式模型调用和完整 DecisionBench。E1 最小投影只支持上述四个 Mini Fixture，不能用于导出任意用户 Run。

## 校验与运行

```bash
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v1

E1_OUTPUT="$(mktemp -d)/e1-output"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1 \
  --manifest evaluation/datasets/decisionbench-v1/manifests/e1-mini-stub.json \
  --output "$E1_OUTPUT"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E1_OUTPUT"
```

入口、过滤参数与真实模型双重 opt-in 说明见 [`../../README.md`](../../README.md)，精确回归结果见 [`../../../docs/STATUS.md`](../../../docs/STATUS.md)。本目录随仓库使用 MIT License。
