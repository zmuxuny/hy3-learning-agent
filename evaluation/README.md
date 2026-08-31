# Learning Agent Evaluation

`evaluation/` 是腾讯犀牛鸟第三阶段的隔离评测控制平面。E0 已冻结三份 v1 契约和离线校验器；E1 在不改写这些 Schema 的前提下，增加了四轨 Runtime Mini 链路：

```text
Runtime Mini Fixture
→ 独立临时 SQLite 与冻结时钟
→ 生产 Agent Runtime / 工具协议
→ Evaluation Model Recorder
→ 生产 Guard / Notification / OutboxAction
→ Recording Delivery Sink
→ E1 最小 DecisionEpisode v1 runtime_export
→ E0 validator
```

默认模型模式是固定响应的 `stub`。它只证明工程链路、隔离性和确定性，不是 Hy3 Primary Episode、Calibration Output 或正式模型能力结果。

## 当前可用能力

- `DecisionEpisode v1`、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` 三份严格 JSON Schema；
- 单一 canonical JSON / SHA-256 实现及递归校验、稳定错误排序、引用/Split/摘要/隐私检查；
- P/I/A/R 各一个 E0 手工协议 Episode，以及各一个版本化 E1 Runtime Mini Fixture 和独立 Oracle；
- 父进程 + 独立 Worker 的 `run-agent`：每个 Episode 使用不同的系统临时目录与绝对路径 SQLite，不启动 FastAPI lifespan、Scheduler 或邮件轮询；
- 作用域冻结 UTC 时钟、确定性逻辑身份、公开合成资源 Snapshot Provider；
- 经 `AgentRuntime.client` 注入的 OpenAI-compatible Recorder，支持流式与非流式透明转发；
- 生产 Outbox 状态机末端的带类型 Delivery Adapter，以及仅用于评测的 Recording Delivery Sink；
- 仅覆盖四个 Mini Fixture 的确定性 v1 runtime 投影和原子产物发布。

## 安装与命令

源码仓库的常规安装脚本会以 editable 模式安装评测包：

```bash
./scripts/setup.sh
```

校验仓库内的 E0 契约数据：

```bash
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v1
```

运行四轨 E1 stub Mini：

```bash
E1_OUTPUT="$(mktemp -d)/e1-output"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1 \
  --manifest evaluation/datasets/decisionbench-v1/manifests/e1-mini-stub.json \
  --output "$E1_OUTPUT"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E1_OUTPUT"
```

`--episode-id P-E1-MINI-001` 可选择单例，`--track intervention` 可按轨道过滤。目标目录必须尚不存在；Runner 先在同级 staging 目录完成全部 Worker、隐私和 E0 校验，再原子发布，任何中途失败都不会留下半成品。

成功摘要稳定声明 `invocation_mode=stub formal_evaluation_result=false evaluation_status=not_a_formal_model_evaluation`。失败返回非零状态，结构化错误只包含稳定错误码、阶段、Episode ID 与公共说明，不回显消息、异常载荷、密钥或通知目标。

真实模型是明确的可选路径，同时满足下列条件才会启动：

```bash
OPENAI_API_KEY=... .venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1 \
  --manifest evaluation/datasets/decisionbench-v1/manifests/e1-mini-stub.json \
  --output <new-system-temp-output> \
  --model-mode real \
  --allow-real-model
```

Key 只从调用者环境进入 Worker，不写 Manifest、产物或错误。E1 自动化、常规验收和本次提交均未调用真实 Hy3；资源访问即使在 real 模式下也只能命中版本化快照。

## 目录规范

```text
evaluation/
├── schemas/                              # E0 三份权威 v1 Schema
├── datasets/decisionbench-v1/
│   ├── episodes/mini/                    # E0 手工协议 Episode
│   ├── fixtures/mini/                    # E1 Runtime Mini Fixture
│   ├── oracles/mini/                     # 与模型 Context 分离的 Oracle
│   ├── resources/e1-mini/snapshot.json   # 公开合成资源快照
│   └── manifests/e1-mini-stub.json       # E1 固定运行清单
├── src/learning_agent_eval/
└── tests/
```

`run-agent` 产物包含 `episodes/*.json`、内部审计用 `captures/*.json` 和 `run-manifest.json`。临时 SQLite、WAL/SHM、Worker 目录、模型私有推理与通知路由材料均不发布。

## 隔离、时间与资源边界

父进程属于纯 evaluation 控制平面，不静态导入 `app.*`。Worker 使用最小环境白名单，并在导入生产应用前设置 `EVALUATION_MODE=1`、禁用 `env_file`、绑定 Worker 内的绝对 SQLite、关闭 Scheduler 和 Email Reply Polling，再安装文件、SQLite、网络、SMTP、Web Push、IMAP 与 subprocess 陷阱。配置非法时失败关闭，不回退默认数据库。

冻结时钟通过作用域 ContextVar 提供；Runtime、Guard、通知冷却/安静时间、RunEvent、Notification、OutboxReceipt 以及 E1 工具路径共享同一冻结 UTC，退出作用域后恢复真实时钟。数据库证据摘要只对临时合成库的规范化逻辑投影计算，Runner 不打开或计算真实用户数据库摘要。

Snapshot Provider 只接受 Manifest 登记的 query/URL。未知搜索、打开或 `resource_save` URL 返回结构化失败，不回退 DuckDuckGo、Bing、`httpx` 或 DNS。stub 模式阻断全部网络；real 模式的唯一网络例外是显式模型 Provider。

## Fake Outbox 的准确语义

“假 Outbox”不是跳过通知，也不是伪造数据库行，而是：

```text
生产 notification_send
→ 生产 Notification Guard
→ 生产 Intervention / Notification
→ 生产 OutboxAction claim / fence / idempotency
→ Recording Delivery Sink
→ 临时库确定性 OutboxReceipt
```

Adapter 只在 Dispatcher 原本调用 SMTP/Web Push Provider 的最后边界注入；未传 Adapter 时保持生产发送行为。Sink 只接受 `smtp` 和 `web_push`，其他 destination 失败关闭，绝不执行 workspace file、subprocess 或未知副作用。它只记录稳定 action identity、destination、经安全投影的请求/载荷摘要和字段元数据，不保存收件地址、Push endpoint/keys、认证信息或完整载荷。

SMTP 模拟 Receipt 保持 `status=accepted`，Web Push 保持当前 `delivered` 语义；两者 response 均声明 `transport=evaluation_sink`、`emulated_destination`、`external_side_effect=false` 和稳定 Sink 版本。Agent 仍只看到生产 `notification_send` 当轮返回的 `pending_delivery`，后续模拟 Receipt 不会注入模型消息。IMAP 是入站能力，E1 仅禁用并设调用陷阱，不实现假 IMAP。

## Recorder 与隐私边界

Recorder 记录公开投影后的 visible messages/input digest、system prompt version/digest、实际 tool allowlist/schema digest、用户可见文本、Function Call ID/name/canonical arguments、模型模式和稳定 ordinal，并将原始响应对象或 Chunk 原样交还 Runtime。System message 正文不进入产物，`visible_messages` 只保留稳定占位，正文只形成公开版本号与摘要，避免安全指令中的私有推理术语污染最终产物。

请求与响应进入 Recorder 投影时会递归丢弃 `reasoning_content`、`reasoning`、Chain-of-Thought 同义字段；这些值不复制、不缓存、不记录、不散列、不计数、不导出也不评价。投影和最终产物都经现有 `privacy_issues` 对字段名与文本值失败关闭；私有推理术语/sentinel、API Key、Bearer/JWT、认证 Token、非保留域邮箱、手机号和身份字段一旦出现，整个运行失败且不发布产物。

## E1 与 E2 的边界

E1 投影只理解四个公开合成 Runtime Mini Fixture：模型回合来自 Recorder，ToolInvocation、RunEvent、Operation、Guard、Notification 和 Outbox 状态来自临时生产数据库，随机数据库身份映射为 Fixture 逻辑 ID 与稳定 ordinal。结果不会从 Oracle 或 scripted expected answer 回填。

通用 Episode Exporter、通用 Normalizer、完整 State Delta、Rules、Judge、聚合、报告、48 个 Primary Episodes、24 个 Calibration Outputs 和正式分数仍属于 E2 及之后；当前 DecisionBench v1 也仍未完成。E1 的已知限制是只覆盖四条最小生产工具路径、单 Worker 顺序批处理和 SMTP/Web Push 两种外发目的地，不构成任意产品状态的通用导出器。

## Canonical JSON 与校验

所有摘要只调用 `learning_agent_eval.canonical`：对象 Key 排序、Unicode NFC、UTF-8、整数化等值浮点、保留数组顺序，并拒绝非有限数值、非字符串 Key 和非 JSON 类型。校验器还拒绝符号链接、重复 JSON Key、未知 Schema 版本、超过 2 MB 的单文件、私有推理、凭据形态、认证字段、非保留邮箱和个人身份字段。

E1 定向验收结果为：

- `.venv/bin/pytest -q evaluation/tests`：`65 passed in 76.11s (0:01:16)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 0.82s`。

完整回归与发布门禁的最终结果以 [`../docs/STATUS.md`](../docs/STATUS.md) 为准。
