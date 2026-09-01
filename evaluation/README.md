# Learning Agent Evaluation

`evaluation/` 是腾讯犀牛鸟第三阶段的隔离评测控制平面。工程里程碑、Episode Schema 和 Benchmark 发布名是三个独立版本维度：E0–E3 是工程阶段，`DecisionEpisode v1/v2` 是单个 Episode 契约，`DecisionBench v1` 是尚未完成的 Benchmark 发布名。

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
→ 标签与生成者信息盲化
→ decision-rubric-v1 / decision-track-anchors-v1
→ 结构化 Judge（最多一次修复）/ judge-result-v1
→ Rule Hard Gate 优先的确定性聚合
→ canonical、privacy、Schema 校验
→ 原子发布
```

`DecisionEpisode v1/v2`、`rule-result-v1`、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` 均保持冻结。历史 E0 手工 Episode 仍可读取和校验；新的 Runtime 不再生成 v1，也不会一次运行同时输出 v1/v2。E1 `capture.json` 只保留为脱敏工程审计附件，Rules、Judge 和聚合均不得从 `capture.*` 取证。

默认模型与 Judge 模式都是固定响应的 `stub`。它只证明工程链路、隔离性、结构化 Judge 协议、规则确定性和 Hard Gate/cap 语义，不是 Hy3 Primary Episode、Calibration Output 或正式模型能力结果。本次 E3 开发和验收没有运行真实 Hy3，也没有访问公网。

## 当前可用能力

- 严格的 `DecisionEpisode v1/v2`、`rule-result-v1`、`judge-result-v1`、Judge/聚合 Manifest、Episode/轨道聚合、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` JSON Schema；
- 唯一 canonical JSON / SHA-256 实现，以及版本分派的 Schema、摘要、引用、Evidence Path、Split、隐私和完整性校验；
- P/I/A/R 各一个 E0 手工协议 Episode，以及各一个版本化 E1 Runtime Mini Fixture 和独立 Oracle；
- 父进程加独立 Worker 的 `run-agent`：每个 Episode 使用不同的系统临时目录和绝对路径 SQLite，不启动 FastAPI lifespan、Scheduler 或邮件轮询；
- 作用域冻结 UTC 时钟、公开合成资源 Snapshot Provider、Evaluation Model Recorder，以及完整生产 Outbox 协议末端的 Recording Delivery Sink；
- 生产数据库前后态的通用 allowlist Snapshot Collector、严格 JSON Normalizer、稳定逻辑身份映射、递归 State Delta 与 Operation patch 对齐；
- 从 Recorder、Snapshot、ToolInvocation、RunEvent、Operation、Guard、Notification 和 Outbox 事实导出的通用 v2 Episode；
- `model_attempt → tool_execution → guard_decision → final_effect → durable_status` 一等分层；
- `common/planning/intervention/assessment/revision/trace/isolation` 七个确定性规则包、完整性结果、不可抵消的 Critical Hard Gate 和原子规则发布。
- 版本化七维公共 Rubric（权重 `15/15/20/20/15/5/10`）与 Planning/Intervention/Assessment/Revision 四轨独立 0/1/2 锚点；
- 稳定 opaque Judge ID、质量标签/生成者/作者/source ref 盲化、严格输出 Schema、原始 v2 Evidence Path 回映射与一次修复；
- Rule Critical Hard Gate `Fail + cap=39`、Major Fail `cap=69`、Minor 无额外 cap、suggested gate 不自动升级、无效输入单列和逐轨汇总的纯确定性聚合。

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
E3_ROOT="$(mktemp -d)"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1 \
  --manifest evaluation/datasets/decisionbench-v1/manifests/e1-mini-stub.json \
  --output "$E3_ROOT/runtime"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E3_ROOT/runtime"
.venv/bin/python -m learning_agent_eval evaluate-rules \
  --input "$E3_ROOT/runtime" \
  --output "$E3_ROOT/rules"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E3_ROOT/rules"
.venv/bin/python -m learning_agent_eval evaluate-judge \
  --episodes "$E3_ROOT/runtime" \
  --rules "$E3_ROOT/rules" \
  --output "$E3_ROOT/judges" \
  --judge-mode stub \
  --stub-response evaluation/fixtures/e3-fixed-judge-responses-v1.json
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E3_ROOT/judges"
.venv/bin/python -m learning_agent_eval aggregate-results \
  --episodes "$E3_ROOT/runtime" \
  --rules "$E3_ROOT/rules" \
  --judges "$E3_ROOT/judges" \
  --output "$E3_ROOT/aggregates"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E3_ROOT/aggregates"
```

四个运行入口均支持 `--episode-id P-E1-MINI-001` 和 `--track intervention`。目标目录必须尚不存在；控制平面先在同级 staging 目录完成计算与全部校验，再原子发布，任何中途失败都不会留下半成品。`evaluate-rules` 在输入无效、完整性失败或 `invalid_input` 时返回 1，出现 Critical Fail/Hard Gate 时返回 2，仅有非 Critical Fail 时返回 3。`evaluate-judge` 对 invalid 输入返回 1、稳定 `judge_error` 返回 2；`aggregate-results` 对 invalid 返回 1、Judge Error 返回 2、实际 Hard Gate Fail 返回 3。

固定响应文件明确声明 `judge_mode=stub`、`formal_evaluation_result=false` 和 `not_a_formal_model_evaluation`，只重放调用者提供的同一结构化响应，不按 Episode ID、track、Oracle action class、scripted answer 或关键词生成评分。真实 Judge 必须同时指定 `--judge-mode real --allow-real-judge`，并只从调用者环境读取 `OPENAI_API_KEY`；Key、endpoint、展开 Prompt、Provider 原始响应或异常均不写入产物与错误。本次没有使用该 real 路径。

成功摘要稳定声明 mode、complete/invalid/error 数和 `formal_evaluation_result`。失败只输出稳定错误码、阶段、Episode ID 和公共说明，不回显完整 Episode、展开 Prompt、Provider 载荷/异常、数据库行、密钥、endpoint 或通知目标。

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
├── schemas/                              # 冻结 E0–E2 契约 + E3 Judge/聚合契约
├── fixtures/e3-fixed-judge-responses-v1.json # engineering-only 固定响应
├── datasets/decisionbench-v1/
│   ├── episodes/mini/                    # E0 历史 v1 手工协议 Episode
│   ├── fixtures/mini/                    # E1 Runtime Mini Fixture，E2 导出为 v2
│   ├── oracles/mini/                     # 与模型 Context 分离的 Oracle
│   ├── resources/e1-mini/snapshot.json   # 公开合成资源快照
│   └── manifests/e1-mini-stub.json       # 固定工程运行清单
├── src/learning_agent_eval/
└── tests/
```

`run-agent` 只生成 v2 `episodes/*.json`、工程审计用 `captures/*.json` 和 `e2-run-output-manifest-v1`。`evaluate-rules` 生成 `rules/*.json`、`integrity/*.json` 和 `rule-run-manifest-v1`。`evaluate-judge` 只发布 `judge-results/<episode-id>.json` 与 `run-manifest.json`；盲化投影和展开 Prompt 默认不发布。`aggregate-results` 发布 `episodes/<episode-id>.json`、`tracks/<track>.json` 与 `run-manifest.json`，不生成四轨 overall、HTML、CSV 或 Markdown 报告。临时 SQLite、WAL/SHM、Worker/staging 目录、Capture 副本、模型私有推理与通知路由材料均不发布。

## Snapshot、身份与 State Delta

Snapshot Collector 使用闭合实体类型和逐实体字段 allowlist，覆盖当前四轨所需的 AgentRun、ContextSnapshot、ToolInvocation、RunApproval、RunEvent、Operation、Plan/Stage/Task、PlanProposal、TaskSubmission、Artifact/Evidence/LearningEvent/Achievement/ActivityDay、Intervention、ProactiveDecision、Notification、OutboxAction/Receipt 及公开资源事实。未登记 ORM 列不会被读取；已登记实体若出现不支持的字段形状、引用、状态、非 JSON 类型或非有限数值则失败关闭。Collector 绝不序列化 ORM `__dict__`，也不使用 `str(unknown)` 吞错。

时间统一为确定性 UTC，JSON 递归规范化；语义数组保序，集合稳定排序。逻辑身份优先使用 Fixture 声明 ID，否则以实体类型、作用域和按规范化语义排序后的 ordinal 生成，同一注册表跨前后态复用。数据库自增 ID、随机 UUID、claim/reply token、endpoint、Push key、认证字段和地址不会进入 Episode。

Delta 由前后 Snapshot 实际比较得到，不从工具名、Oracle 或 scripted expected answer 推断。变化条目显式记录 `added/removed/changed`、before/after 的 `missing/present` 与值、可解析路径、typed source refs、零到多个有序 Operation refs 和对齐状态；`compared_entity_refs` 与 `unchanged_entity_refs` 一等证明已比较且未变化的实体。`null` 与字段缺失不同；新实体只在后态，删除实体只在前态；确认无变化使用完整 Snapshot 和稳定空变化集合，不把未采集误写成空 Delta。

Operation 的 forward/inverse patch 用于证明归因，但不能代替真实后态。字段更新逐值对齐正反 patch；Operation 驱动的实体新增/删除还必须出现配对的显式创建/删除标记，例如顶层 `created_ref/delete_ref` 或已登记的 typed award refs。一个 Operation 可影响多个字段或实体，多个 Operation 可按时间顺序共同影响一路径；Notification、Outbox、Receipt、pending approval 和运行终态等合法无 Operation 事实使用 typed source，不伪造 `operation_ref`。无法对齐的持久化变化使完整性失败。

## 决策分层与 Rules

v2 独立表达模型尝试、工具执行、Guard 决定、最终效果、耐久状态和正式评测资格。Guard blocked/deferred 可以保留模型 Tool Call 和 ToolInvocation，同时必须证明禁止的最终副作用为零；计划提案的待采纳边界引用 `PlanProposal(status=pending)`，暂停执行的高风险工具批准引用耐久 `RunApproval`。WAIT/NO_OP 可以没有 ToolInvocation 和状态变化，但仍携带一个已确认完整的空 Delta；只要 Recorder、终态和完整性证据齐全就是有效 Episode。

`rule-result-v1` 使用 `deterministic-rule-evaluator-v1` 和 `e2-rule-pack-v1`。每个稳定顺序的 check 输出 `pass/fail/not_applicable/invalid_input`、`minor/major/critical`、可解析到 v2 Episode 的 Evidence Path、observed、expected 和公共 reason code。缺字段是 `invalid_input`，不是业务 Fail；`hard_gates` 只引用实际 failed Critical checks，其他 Pass 不可抵消它。`dimension_signals` 仅保存确定性结构化信号，不产生 0/1/2 档位、总分或 Judge 结论。

## Judge 输入、Rubric 与结果校验

Judge 控制平面先完整校验 Runtime/Rule 目录、闭合清单、Episode/Rule 自摘要与交叉 digest，再构建 `blind-judge-input-v1`。投影保留公开 v2 事实、Rule 已确认状态、当前轨道锚点和严格输出 Schema；删除或 opaque 化 Good/Mild/Severe、Baseline/Candidate、Episode/family/tag/source ref、生成模型/Prompt/Invocation 身份、Oracle 作者/复核/说明、凭据、endpoint 与路由材料。Oracle 只保留结构化 allowed action、约束引用/Evidence Path、criticality 与 expected effects。原 Episode 不改写，opaque 映射不进入 Prompt 或产物，投影也不是第二事实源。

公共 `decision-rubric-v1` 将 D1–D7 与权重固定为 `15/15/20/20/15/5/10`，`decision-track-anchors-v1` 为四轨分别给出完整 0/1/2 锚点；两份配置分离、可摘要且不读取网页。Judge 被明确要求不重算 Rules 已确定的时间、阈值、存在性、完整性、隐私与 Hard Gate。

`judge-result-v1` 固定 D1–D7 顺序与 `level=0|1|2`，每维至少一个原始 v2 Evidence Path、稳定 reason code 和公共说明，非满分必须有具体问题。所有维度/semantic issue/suggested gate 路径既要存在于 Judge 实际可见投影，也要回到同一原始 Episode 解析；`capture.*`、provenance 作者路径和投影内部路径均拒绝。结果还绑定 Episode/Rule、Prompt、Rubric、轨道锚点与 blind input digest，并校验隐私、formal/mode/status 和自摘要。

Provider 输出首次不满足 Schema、Evidence 或隐私约束时，只携带稳定错误码修复一次，不回传或保存首次原始响应；第二次仍失败生成无维度、无默认分的稳定 `judge_error`。Provider 原始异常同样只折叠为公共错误码。stub 永远 `formal_evaluation_result=false`；real 也只有在 Episode、Rules 和 v2 formal eligibility 全部正式时才可标 formal，因此四个 E1 Mini 即使走 real seam 仍不正式。

## 确定性聚合

`deterministic-aggregator-v1` 不调用模型、网络、数据库、`.env` 或 subprocess。基础分严格为 `sum(weight × level / 2)`。Rule `invalid_input`、Judge `invalid_input/judge_error`、缺维度或路径失效均不评分；它们按类别进入轨道清单，绝不按 0 分混入均值。failed Critical check 的实际 Hard Gate 直接令 Episode `Fail` 且 cap=39；任一 Major Fail cap=69；多个 cap 取最小值；Minor 不新增 cap。Judge 高分无法删除 Rule Gate，`suggested_hard_gates` 只保留为建议，不成为实际 Gate 或 cap。

聚合产物保存七维原始档位、逐维 weighted signal、Rule Fail、实际/建议 Gate、所用 cap、原始/最终分数、Episode outcome、三段 digest 关联、formal 状态和自摘要。轨道文件只平均正常 scored Episode，四轨分别发布，不生成单一总分。

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

E3 Judge/盲化/聚合工程闭环已经实现，但本次只运行 fixed-response stub，未调用真实 Hy3，因此没有正式 Judge 能力结论。48 个 Primary Episodes、24 个 Calibration Outputs、有效性实验、正式评测、版本回归、Case/最终报告、Demo 和 DecisionBench v1 正式发布属于 E4–E8，均未完成。固定响应与其 100 分工程聚合只验证协议和 cap 计算，不能写成 Hy3 表现。

E3 pre-commit 验收结果如下；完整门禁与 post-commit 说明见 [`../docs/STATUS.md`](../docs/STATUS.md)：

- `.venv/bin/pytest -q evaluation/tests/test_e3_judge_aggregate.py`：`25 passed in 29.53s`；
- `.venv/bin/pytest -q evaluation/tests/test_protocol_schemas.py`：`5 passed in 0.35s`；
- `.venv/bin/pytest -q evaluation/tests`：`130 passed in 169.79s (0:02:49)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 1.32s`；
- `.venv/bin/pytest -q`：`1205 passed, 2 warnings in 2119.04s (0:35:19)`。

文档链接、lint/typecheck、secret scan、依赖检查、`git diff --check` 与四轨系统临时目录 smoke 均通过；该 smoke 包括两次逐文件比较、Episode/track 过滤、Manifest commit、隐私/标签/路由、零外部调用和 SQLite/WAL/SHM 扫描，临时输出已清理。
