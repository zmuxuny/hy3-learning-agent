# Learning Agent Evaluation

`evaluation/` 是腾讯犀牛鸟第三阶段的隔离评测控制平面。工程里程碑、Episode Schema 和 Benchmark 发布名是三个独立版本维度：E0/E1/E2 是工程阶段，`DecisionEpisode v1/v2` 是单个 Episode 契约，`DecisionBench v1` 是尚未完成的 Benchmark 发布名。

E2 复用 E1 的隔离 Runtime Harness，并把新 Runtime 输出直接切换为 `DecisionEpisode v2`：

```text
Runtime Mini Fixture
→ 独立临时 SQLite、冻结时钟和生产 Runtime/工具/Guard/Outbox
→ Runtime 前后 allowlist Snapshot
→ 通用 Normalizer 与稳定逻辑身份
→ 完整 State Delta 与 Operation 对齐
→ 单一权威 DecisionEpisode v2
→ 完整性检查与 e2-rule-pack-v1
→ rule-result-v1 / Hard Gate
→ canonical、privacy、Schema 校验
→ 原子发布
```

`DecisionEpisode v1`、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` 保持冻结。历史 E0 手工 Episode 仍可读取和校验；新的 Runtime 不再生成 v1，也不会一次运行同时输出 v1/v2。E1 `capture.json` 只保留为脱敏工程审计附件，Rules 和后续 Judge 均不得从 `capture.*` 取证。

默认模型模式是固定响应的 `stub`。它只证明工程链路、隔离性、规则确定性和 Hard Gate 语义，不是 Hy3 Primary Episode、Calibration Output 或正式模型能力结果。本次没有运行真实 Hy3。

## 当前可用能力

- 严格的 `DecisionEpisode v1/v2`、`rule-result-v1`、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` JSON Schema；
- 唯一 canonical JSON / SHA-256 实现，以及版本分派的 Schema、摘要、引用、Evidence Path、Split、隐私和完整性校验；
- P/I/A/R 各一个 E0 手工协议 Episode，以及各一个版本化 E1 Runtime Mini Fixture 和独立 Oracle；
- 父进程加独立 Worker 的 `run-agent`：每个 Episode 使用不同的系统临时目录和绝对路径 SQLite，不启动 FastAPI lifespan、Scheduler 或邮件轮询；
- 作用域冻结 UTC 时钟、公开合成资源 Snapshot Provider、Evaluation Model Recorder，以及完整生产 Outbox 协议末端的 Recording Delivery Sink；
- 生产数据库前后态的通用 allowlist Snapshot Collector、严格 JSON Normalizer、稳定逻辑身份映射、递归 State Delta 与 Operation patch 对齐；
- 从 Recorder、Snapshot、ToolInvocation、RunEvent、Operation、Guard、Notification 和 Outbox 事实导出的通用 v2 Episode；
- `model_attempt → tool_execution → guard_decision → final_effect → durable_status` 一等分层；
- `common/planning/intervention/assessment/revision/trace/isolation` 七个确定性规则包、完整性结果、不可抵消的 Critical Hard Gate 和原子规则发布。

## 安装与命令

源码仓库的常规安装脚本会以 editable 模式安装评测包：

```bash
./scripts/setup.sh
```

校验仓库内的历史 E0 v1 数据：

```bash
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v1
```

运行四轨 stub Runtime，校验 v2 Episode，再执行完整性和 Rules：

```bash
E2_ROOT="$(mktemp -d)"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1 \
  --manifest evaluation/datasets/decisionbench-v1/manifests/e1-mini-stub.json \
  --output "$E2_ROOT/runtime"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E2_ROOT/runtime"
.venv/bin/python -m learning_agent_eval evaluate-rules \
  --input "$E2_ROOT/runtime" \
  --output "$E2_ROOT/rules"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E2_ROOT/rules"
```

两个运行入口均支持 `--episode-id P-E1-MINI-001` 和 `--track intervention`。目标目录必须尚不存在；控制平面先在同级 staging 目录完成全部 Worker、校验或规则计算，再原子发布，任何中途失败都不会留下半成品。`evaluate-rules` 在输入无效、完整性失败或 `invalid_input` 时返回 1，出现 Critical Fail/Hard Gate 时返回 2，仅有非 Critical Fail 时返回 3。

成功摘要稳定声明 stub 产物的 `formal_evaluation_result=false`。失败只输出稳定错误码、阶段、Episode ID、公共说明和安全 Evidence Path，不回显完整消息、数据库行、异常载荷、密钥或通知目标。

真实模型仍是显式双重 opt-in 路径：

```bash
OPENAI_API_KEY=... .venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1 \
  --manifest evaluation/datasets/decisionbench-v1/manifests/e1-mini-stub.json \
  --output <new-system-temp-output> \
  --model-mode real \
  --allow-real-model
```

Key 只从调用者环境进入 Worker，不写 Manifest、产物或错误。资源即使在 real 模式下也只能命中版本化快照。当前四个 Mini 仍是 engineering-only，因此 real opt-in 本身不会把它们变成正式 Primary 结果。

## 产物与版本边界

```text
evaluation/
├── schemas/                              # 冻结 v1 + DecisionEpisode v2 + Rule Result v1
├── datasets/decisionbench-v1/
│   ├── episodes/mini/                    # E0 历史 v1 手工协议 Episode
│   ├── fixtures/mini/                    # E1 Runtime Mini Fixture，E2 导出为 v2
│   ├── oracles/mini/                     # 与模型 Context 分离的 Oracle
│   ├── resources/e1-mini/snapshot.json   # 公开合成资源快照
│   └── manifests/e1-mini-stub.json       # 固定工程运行清单
├── src/learning_agent_eval/
└── tests/
```

`run-agent` 只生成 v2 `episodes/*.json`、工程审计用 `captures/*.json` 和 `e2-run-output-manifest-v1`。`evaluate-rules` 生成 `rules/*.json`、`integrity/*.json` 和 `rule-run-manifest-v1`。临时 SQLite、WAL/SHM、Worker 目录、模型私有推理与通知路由材料均不发布。

## Snapshot、身份与 State Delta

Snapshot Collector 使用闭合实体类型和逐实体字段 allowlist，覆盖当前四轨所需的 AgentRun、ContextSnapshot、ToolInvocation、RunApproval、RunEvent、Operation、Plan/Stage/Task、PlanProposal、TaskSubmission、Artifact/Evidence/LearningEvent/Achievement/ActivityDay、Intervention、ProactiveDecision、Notification、OutboxAction/Receipt 及公开资源事实。未登记 ORM 列不会被读取；已登记实体若出现不支持的字段形状、引用、状态、非 JSON 类型或非有限数值则失败关闭。Collector 绝不序列化 ORM `__dict__`，也不使用 `str(unknown)` 吞错。

时间统一为确定性 UTC，JSON 递归规范化；语义数组保序，集合稳定排序。逻辑身份优先使用 Fixture 声明 ID，否则以实体类型、作用域和按规范化语义排序后的 ordinal 生成，同一注册表跨前后态复用。数据库自增 ID、随机 UUID、claim/reply token、endpoint、Push key、认证字段和地址不会进入 Episode。

Delta 由前后 Snapshot 实际比较得到，不从工具名、Oracle 或 scripted expected answer 推断。变化条目显式记录 `added/removed/changed`、before/after 的 `missing/present` 与值、可解析路径、typed source refs、零到多个有序 Operation refs 和对齐状态；`compared_entity_refs` 与 `unchanged_entity_refs` 一等证明已比较且未变化的实体。`null` 与字段缺失不同；新实体只在后态，删除实体只在前态；确认无变化使用完整 Snapshot 和稳定空变化集合，不把未采集误写成空 Delta。

Operation 的 forward/inverse patch 用于证明归因，但不能代替真实后态。字段更新逐值对齐正反 patch；Operation 驱动的实体新增/删除还必须出现配对的显式创建/删除标记，例如顶层 `created_ref/delete_ref` 或已登记的 typed award refs。一个 Operation 可影响多个字段或实体，多个 Operation 可按时间顺序共同影响一路径；Notification、Outbox、Receipt、pending approval 和运行终态等合法无 Operation 事实使用 typed source，不伪造 `operation_ref`。无法对齐的持久化变化使完整性失败。

## 决策分层与 Rules

v2 独立表达模型尝试、工具执行、Guard 决定、最终效果、耐久状态和正式评测资格。Guard blocked/deferred 可以保留模型 Tool Call 和 ToolInvocation，同时必须证明禁止的最终副作用为零；计划提案的待采纳边界引用 `PlanProposal(status=pending)`，暂停执行的高风险工具批准引用耐久 `RunApproval`。WAIT/NO_OP 可以没有 ToolInvocation 和状态变化，但仍携带一个已确认完整的空 Delta；只要 Recorder、终态和完整性证据齐全就是有效 Episode。

`rule-result-v1` 使用 `deterministic-rule-evaluator-v1` 和 `e2-rule-pack-v1`。每个稳定顺序的 check 输出 `pass/fail/not_applicable/invalid_input`、`minor/major/critical`、可解析到 v2 Episode 的 Evidence Path、observed、expected 和公共 reason code。缺字段是 `invalid_input`，不是业务 Fail；`hard_gates` 只引用实际 failed Critical checks，其他 Pass 不可抵消它。`dimension_signals` 仅保存确定性结构化信号，不产生 0/1/2 档位、总分或 Judge 结论。

## 隔离、Outbox 与隐私边界

父进程不静态导入 `app.*`。Worker 在导入生产应用前使用最小环境白名单、禁用 `env_file`、绑定 Worker 内绝对 SQLite、关闭 Scheduler/Email Reply Polling，并安装文件、数据库、网络、SMTP、Web Push、IMAP 与 subprocess 陷阱。配置非法时失败关闭，不回退默认数据库；数据库摘要只针对临时合成库的规范化投影。

“假 Outbox”仍严格表示：

```text
生产 notification_send
→ 生产 Notification Guard
→ 生产 Intervention / Notification
→ 生产 OutboxAction claim / fence / idempotency
→ Recording Delivery Sink
→ 临时库确定性 OutboxReceipt
```

Adapter 只在 SMTP/Web Push Provider 最后边界注入。Sink 只接受 `smtp` 和 `web_push`，其他 destination 失败关闭；SMTP Receipt 为 `accepted`，Web Push 为 `delivered`。Agent 仍只观察生产工具当轮的 `pending_delivery`，不会看到模拟 Receipt。

Recorder 在 Worker 内只投影公开模型输入/输出、system/tool digest 和 Function Call；发布 Capture 时进一步移除完整 visible messages，只保留输入摘要。System message 正文不发布；`reasoning_content`、`reasoning` 与 Chain-of-Thought 同义字段在投影前递归丢弃，不复制、缓存、散列、计数、导出或评价。最终产物继续由现有 `privacy_issues` 对私有推理、凭据、认证 Token、非保留域邮箱、手机号、身份与路由字段失败关闭。

## 已知限制与后续边界

通用 Collector/Exporter 当前覆盖已登记的生产实体与四轨关键路径；新评测事实若需要未登记 ORM 实体或列，必须先显式加入 allowlist、身份和 Delta 语义，否则失败关闭。当前生产 `submission.check` 的 revision-required 路径可完整导出；accepted 路径还会产生 Operation patch 未枚举的派生 Plan/Stage 更新，因此 Exporter 明确以 `delta.operation_unattributed.plan` 失败关闭，不能伪装成完整 ACCEPT Episode。Mini Harness 仍是单 Worker 顺序批处理，只在最终 Transport 层模拟 SMTP/Web Push；没有访问真实用户数据或外部 Provider。

E3 的 Hy3 Judge、标签盲化、聚合和报告仍未实现；48 个 Primary Episodes、24 个 Calibration Outputs、有效性实验、正式分数和 DecisionBench v1 正式发布也均未完成。E2 stub Rules 不能被写成 Hy3 能力结论。

当前定向验收结果为：

- `.venv/bin/pytest -q evaluation/tests`：`104 passed in 118.16s (0:01:58)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 0.89s`；
- `.venv/bin/pytest -q`：`1179 passed, 2 warnings in 2115.27s (0:35:15)`。

完整回归与发布门禁的最终结果以 [`../docs/STATUS.md`](../docs/STATUS.md) 为准。
