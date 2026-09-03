# Learning Agent Evaluation

`evaluation/` 是腾讯犀牛鸟第三阶段的隔离评测控制平面。E3.1 完成了正式评测前的
干净切换：`DecisionEpisode v3` 是唯一可能获得 formal 资格的活动契约；v1/v2 及其
Rules/Judge/聚合只保留为 engineering-only 历史回归，不再扩展成正式双栈。
`DecisionBench v1` 仍只是未来 Benchmark 发布名，不能与 Episode Schema 版本混淆。

当前活动链路是：

```text
CaseSpec（控制面，含私有标注）
→ 被测 Agent 不可见的运行投影
→ 独立 Worker / 临时 SQLite / 冻结时钟 / 生产 Runtime
→ 精确脱敏的模型可见消息与工具 Schema、父子调用轨迹
→ 前后 Snapshot / 语义身份绑定 / State Delta / Operation 对齐
→ DecisionEpisode v3 或 RuntimeFailure v1
→ CaseSpec 派生的无标签 JudgeReference
→ rule-result-v2
→ 路径级盲化 / 七维 Rubric / 四轨锚点
→ judge-result-v2（最多一次修复）
→ Rule-first aggregate-result-v2
→ 全链 canonical / digest / privacy / Evidence Path 校验
→ staging 后原子发布
```

默认 Runtime 和 Judge 都使用调用者提供的固定 `stub`。它只证明工程协议、隔离、
确定性、失败分类和 Hard Gate/cap，不是 Hy3 能力结果。E3.1 没有调用真实 Hy3、
公网、真实数据库、`.env`、邮箱或 Push endpoint，也没有创建 E4 数据。

## 核心语义

- Validator 只判断证据结构是否完整，不判断动作是否正确。错误动作仍是有效 Episode，
  由 Rules/Judge 低分；摘要错误、引用断裂、轨迹/Delta 不完整才是 invalid。
- `case-spec-v1` 是案例控制面的唯一事实源。Agent 看不到 CaseSpec 或 JudgeReference；
  `judge-reference-v1` 只能由 CaseSpec 确定性派生，含无质量标签的公共约束文本、
  `path/operator/expected_value` 谓词和可接受变体。
- Calibration 的 Good/Mild/Severe 标签、变异来源、作者/复核者和裁决说明只留在
  CaseSpec 私有区，永不进入 Agent 或 Judge 输入。
- v3 保存每次调用实际收到的、严格脱敏后的有序消息和工具 Schema，并记录
  `run_id/parent_run_id/parent_call_id/depth/call_purpose`。主 Agent、子 Agent和汇总调用
  都可重建；标题和记忆压缩等辅助调用明确标成非决策调用。
- Judge 不获得 `state_before` 的额外全知投影；决策前事实只能来自
  `observable_trace.model_calls[*].visible_context`。`state_after/state_delta` 只证明结果。
- 行为失败正常进入评分；Provider/框架/隔离失败写 `runtime-failure-v1`，不冒充分数。
  每个所选 Case 恰有一个 Episode 或 Failure，单例失败不撤销同批成功制品。
- E1 `capture.json` 仍只是历史脱敏工程附件，不是 v3、Rules、Judge 或聚合的事实源。

## 活动契约

| Schema | 作用 |
|---|---|
| `case-spec-v1` | 运行输入、身份绑定、Judge 标准与私有标注的控制面 |
| `judge-reference-v1` | CaseSpec 的确定性无标签 Judge 投影 |
| `provider-attestation-v1` | Agent/Judge Provider 的可审计归因 |
| `environment-manifest-v2` | 冻结配置、提交、依赖锁、隔离与 Provider 关联 |
| `decision-episode-v3` | 可重建层级轨迹、结果与完整证据 |
| `runtime-failure-v1` | 不评分的基础设施失败终态 |
| `runtime-run-manifest-v2` | 所选 Case 的 Episode/Failure 闭合分区 |
| `rule-result-v2` / `rule-run-manifest-v2` | v3 确定性规则与 Hard Gate |
| `judge-result-v2` / `judge-run-manifest-v2` | 盲化结构化 Judge、一次修复与归因 |
| `aggregate-result-v2` / Track/Manifest v2 | 纯确定性 Rule-first 聚合 |

所有契约使用 strict Pydantic、`extra=forbid`、JSON Schema 2020-12、唯一 canonical
JSON/SHA-256 和自摘要。已提交 Schema 由漂移测试与源码模型逐项比较；v1/v2 冻结
Schema 的字节摘要继续锁定。

## 安装与命令

常规安装脚本会以 editable 模式安装评测包：

```bash
./scripts/setup.sh
```

历史 v1/v2 数据仍可只读校验：

```bash
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v1
```

活动 v3 engineering suite 包含四轨正例、错误动作、Guard 阻断、真实生产子 Agent
委派和一个故意的 Provider Failure。完整批次预期 `run-agent`/`evaluate-rules`/
`aggregate-results` 分别因 Failure、Critical Fail、Fail outcome 返回 `2/2/3`；这不是
控制平面异常：

```bash
E31_ROOT="$(mktemp -d)"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v3-engineering \
  --manifest evaluation/datasets/decisionbench-v3-engineering/manifest.json \
  --output "$E31_ROOT/runtime"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E31_ROOT/runtime"
.venv/bin/python -m learning_agent_eval evaluate-rules \
  --input "$E31_ROOT/runtime" --output "$E31_ROOT/rules"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E31_ROOT/rules"
.venv/bin/python -m learning_agent_eval evaluate-judge \
  --episodes "$E31_ROOT/runtime" --rules "$E31_ROOT/rules" \
  --output "$E31_ROOT/judges" --judge-mode stub \
  --stub-response evaluation/fixtures/e31-fixed-judge-responses-v2.json
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E31_ROOT/judges"
.venv/bin/python -m learning_agent_eval aggregate-results \
  --episodes "$E31_ROOT/runtime" --rules "$E31_ROOT/rules" \
  --judges "$E31_ROOT/judges" --output "$E31_ROOT/aggregates"
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset "$E31_ROOT/aggregates"
```

四个执行入口都支持可重复的 `--episode-id` 和 `--track`。目标目录必须不存在；全部
内容先写同级 staging，完整校验后用原子 rename 发布。输入、Schema、清单或 digest
错误返回 1；Judge 二次无效返回 2 且没有伪造维度；任何错误只输出稳定错误码、阶段、
Artifact ID 和公共说明。

未完成的 `evaluate`、`validate-method`、`report`、`compare` 不提供占位成功。

## 输出目录

```text
<runtime>/
├── episodes/<episode-id>.json
├── judge-references/<episode-id>.json
├── failures/<failure-id>.json
└── run-manifest.json

<rules>/
├── rules/<episode-id>.json
└── rule-manifest.json

<judges>/
├── judge-results/<episode-id>.json
└── run-manifest.json

<aggregates>/
├── episodes/<episode-id>.json
├── tracks/<track>.json
└── run-manifest.json
```

盲化投影、展开 Prompt、Provider 原始响应/异常、API Key、Capture、SQLite/WAL/SHM、
Worker/staging 目录、私有推理和通知路由材料不发布。

## Snapshot、轨迹与身份

Collector 只读取闭合的实体/字段 allowlist。CaseSpec 用公开业务字段声明
`identity_bindings`；注册器以语义字段匹配实际数据库对象，不能按排序位置把声明 ID
静默分配给错误的 Plan/Stage/Task/Submission。未绑定的新对象仍使用稳定生成 ID；
根 AgentRun 和全部后代 AgentRun、RunEvent、ToolInvocation 与 Operation 一并采集。

Delta 只由真实前后 Snapshot 比较得到，明确区分 missing/null、added/removed/changed、
零变化和 typed source。Operation forward/inverse patch 证明归因而不代替后态。
`submission.check` 现在完整枚举 Submission/Task/Stage/Plan 派生变化，因此 Assessment
ACCEPT 和 REVISION_REQUIRED 都能对齐；无法归因的变化仍失败关闭。

## Rules、Judge 与聚合

`e31-rule-pack-v2` 把结构无效与决策错误分开。结构化谓词和动作边界由 Rules 权威
执行；时间、阈值、存在性、隐私、完整性和实际 Hard Gate 不交给 Judge 重算。

`blind-judge-input-v2` 使用路径级删除和 opaque ID，不按字段名子串或自由文本全局替换。
所以业务中的 `baseline`、`candidate`、`Good` 和 `candidate_key` 保留，而 Case ID、tag、
质量标签、变异源、作者裁决、生成模型、Provider/endpoint、凭据和路由材料不可见。
JudgeReference 的公共 `must_satisfy/must_not` 语义及结构化谓词可见。

公共 `decision-rubric-v1` 固定 D1–D7 权重 `15/15/20/20/15/5/10`，档位映射为
`0/1/2 → 0%/50%/100%`；`decision-track-anchors-v1` 独立定义 Planning、Intervention、
Assessment、Revision 四轨锚点。配置、Prompt 和响应 Schema 都有稳定版本与摘要。

`judge-result-v2` 要求固定 D1–D7、每维至少一个同时对 Judge 可见且能回到同一原始
v3 Episode 的 Evidence Path、稳定 reason code，以及非满分的具体公共问题。第一次
Schema/Evidence/隐私失败只把稳定错误码送回修复一次；第二次失败得到无维度、无分数的
`judge_error`。固定响应不按 ID、track、Oracle 或关键词生成答案。

`deterministic-aggregator-v2` 的基础分为 `sum(weight × level / 2)`。Critical Rule Fail
强制 Fail 且 cap=39，任一 Major Fail cap=69，多个 cap 取最严，Minor 无额外 cap。
Judge 高分不能抵消 Rule Gate，suggested gate 不自动升级。invalid/judge_error/Runtime
Failure 单列且不按 0 混入均值；四轨分别发布，不生成掩盖弱轨的 overall。

## Provider、隔离与正式性

Provider Attestation 是可审计归因，不是密码学证明。real 资格要求固定 TokenHub HTTPS
allowlist、请求模型 `hy3`、响应模型 `hy3`、Provider request ID、请求/响应时间、安全
配置摘要、当前 Git commit、干净工作树和匹配的 `evaluation-runtime-lock-v1`。Agent 和
Judge 的 real 模式还分别要求 `--allow-real-model` / `--allow-real-judge`；Key 只从调用者
环境读取，CLI 参数、Manifest、日志和错误均不保存 Key 或 endpoint。

Worker 在任何 `app.*` 导入前安装最小环境与审计陷阱，关闭 Scheduler/Email Polling，
并阻断仓库 `.env`、`data/...`（含相对路径）、Worker 外 SQLite、非允许网络、SMTP、
SMTP_SSL、Web Push、IMAP、IMAP_SSL 和 Worker subprocess。生产通知/Outbox 协议保持
原样，只在最后 Transport 使用 Recording Delivery Sink。

普通课程文本中的 `reasoning`、`推理过程`、`Good`、`baseline`、`candidate` 不因词面
被拒绝；字段名为私有推理/凭据/PII 或值具有秘密/真实身份形状时仍失败关闭。私有推理
在投影前按字段删除，不保存、不散列、不计数、不输出也不评价。

## 边界

v3 Collector 对未登记生产事实继续失败关闭。Provider 响应归因能审计声明与响应字段，
不能证明远端服务的密码学身份；正式运行仍需要组织侧凭据、网络和运行审批。

E4–E8 仍未实现：没有 48 个 Primary、24 个 Calibration、有效性实验、人工盲标、正式
Hy3 运行、Baseline/Candidate 回归、Case、最终报告、Demo 或 Release。当前固定响应和
engineering 聚合不得用作 Calibration 标签或 Hy3 能力结论。最终验收命令和精确结果
记录在 [`../docs/STATUS.md`](../docs/STATUS.md)。
