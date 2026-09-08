# Learning Agent Evaluation

`evaluation/` 是腾讯犀牛鸟第三阶段的隔离评测控制平面。当前活动协议为
**Evaluation Protocol Release 1.6（可配置Hy3接口的未登记候选）**。最近已登记并运行的正式批次是
Protocol1.4 / `decisionbench-v1.4-e6-test-release`，必须用冻结源码`1f1f35f`复现旧批次。
Protocol 1.0 的旧 E6 Release 与结果原字节保留；1.1–1.3 为本轮另版开发验证，
不把它们的已见样本称为正式测试。`DecisionEpisode v4` 仍是活动 Episode Schema，
Schema、方法协议和 Benchmark 各自独立版本化。完整过程见
[E6新版修复与验证记录](../docs/E6新版修复与验证记录.md)。

当前活动链路是：

```text
CaseSpec v2（控制面，含私有标注）
→ 被测 Agent 不可见的运行投影
→ 独立 Worker / 临时 SQLite / 冻结时钟 / 生产 Runtime
→ 精确脱敏的模型可见消息与工具 Schema、父子调用轨迹
→ 前后 Snapshot / 语义身份绑定 / State Delta / Operation 对齐
→ DecisionEpisode v4 或 RuntimeFailure v2
→ CaseSpec 派生的无标签 JudgeReference v2
→ rule-result-v3
→ 路径级盲化 / 七维 Rubric / 四轨锚点
→ judge-result-v3（最多一次修复）
→ Rule-first aggregate-result-v3
→ 注册 Benchmark Release / 完整 Runtime 终态清单 / formal 三层重算
→ 全链 canonical / digest / privacy / Evidence Path 校验
→ staging 后原子发布
```

默认 Runtime 和 Judge 都使用调用者提供的固定 `stub`。它只证明工程协议、隔离、
确定性、失败分类和 Hard Gate/cap，不是 Hy3 能力结果。E3.1.2 原始收口只运行 stub。
2026-09-05 经授权完成审计修复及 E4 候选运行：48 Primary 真实执行得到 45 Episode +
3 RuntimeFailure，24 Calibration 为受控输出。2026-09-06 已完成 48 Primary、24 Calibration
和 8 来源的 AI 内容裁决、候选重建和最终回归。S06 机制修复与 E5 必需实验已完成：
V2 固定 `40fddb8`，24 例判别力和 16×5 重复共 88 个有效评估，最低预设方法目标达标。
真实浏览器流程及 AI 审计完成，具体漏检/波动和费用见 [E5 验收记录](../docs/E5验收工作记录.md)。
E6首批48/46/2保留；修复轮完成多版方法验证、新G家族冻结登记及48 Episode/33有效Judge/15失败，formal=false。
1.5候选修复完整请求编码和输入超限分类，仓外45 passed、79请求证据等价，仅离线，真实复测0次。
当时余额0.216953元，已停止该轮付费；接口切换后新授权约50元已登记，当前候选1.6完成45项回归和单例真实链路验证，详见[接口档案](artifacts/e6-provider-20260908/README.md)。历史版本、费用和剩余问题见[新版档案](artifacts/e6-repair-20260908/README.md)及
[新版工作记录](../docs/E6新版修复与验证记录.md)。另见 [候选数据卡](datasets/decisionbench-v1-candidate/DATASET_CARD.md)、
[原始运行记录](artifacts/e4-candidate-20260905/README.md)及 [方案复核](../docs/E4候选数据收口与方案复核.md)。

## API与计费配置

产品、评测Agent和Judge使用同一组 `OPENAI_API_BASE`、`OPENAI_API_KEY`、`MODEL_NAME=hy3`。
真实评测CLI在父进程读取项目 `.env`（显式环境变量优先）；Worker只接收最小环境，不读或复制 `.env`。
直接调用Python库时由调用者传入环境配置。接口需使用无内嵌凭据的HTTPS地址；网络隔离仅放行本次配置的模型主机。
Judge按完整base路径追加 `/chat/completions`，不再固定腾讯地址。评测结果记录实际 `api_base`、origin及服务类别；
第三方记作 `openai-compatible`，不冒充腾讯官方。离线复核使用制品中的接口记录，不依赖复核机器当前配置。

费用使用唯一账本的 `input_rate` / `output_rate`（元/百万token，数值等于微元/token，支持小数）；
每次预留冻结费率、计价依据和接口，结算不受后来费率修改影响，未知usage仍保留预留。
用户已授权新接口约50元可用等价额度。站点公开倍率已留档，实际人民币单价未确认，当前以旧官方1/4费率作等价估算，
不声称是新站实际扣款；账本 `pricing_basis` 标明这一口径。额度已登记一次，后续直接读取账本，不再加50元。
接口修改无需重新设计Rubric；本轮1.6记录实际实现变化，旧1.0–1.5制品和既有正式批次保持原字节。

## 核心语义

- Validator 只判断证据结构是否完整，不判断动作是否正确。错误动作仍是有效 Episode，
  由 Rules/Judge 低分；摘要错误、引用断裂、轨迹/Delta 不完整才是 invalid。
- `case-spec-v2` 是案例控制面的唯一事实源。Agent 看不到 CaseSpec 或 JudgeReference；
  `judge-reference-v2` 只能由 CaseSpec 确定性派生，含无质量标签的公共约束文本、
  `path/operator/expected_value` 谓词和可接受变体。
- `constraint-proposition-v1` 固定谓词语义：同一约束内 predicates 全部合取；
  `must_satisfy` 要求命题为真，`must_not` 要求命题为假。两类约束都必须存在。
- Calibration 的 Good/Mild/Severe 标签、变异来源、作者/复核者和裁决说明只留在
  CaseSpec 私有区，永不进入 Agent 或 Judge 输入。
- v4 保存每次调用实际收到的、严格脱敏后的有序消息和工具 Schema，并记录
  `run_id/parent_run_id/parent_call_id/depth/call_purpose`。主 Agent、子 Agent和汇总调用
  都可重建；并发调用在开始时原子保留 ordinal，标题和记忆压缩等辅助调用明确标成
  非决策调用。
- `model-action-declaration-v2` 以严格首个非空 JSON 帧或模型原生工具参数数组
  `evaluation_action_classes` 记录14类动作；主决策调用非流式，原始参数完整记录后
  仅剥离该协议字段交给生产工具。两种声明冲突或缺失仍失败，不自动补造。协议固定
  每类行动的轨道、语义、字段、边界、正反例与组合政策。工具和状态只
  校验实际效果，不用关键词、track 或 Case ID 猜测意图；跨轨、组合、缺失声明和无法归类
  状态仍形成可评分 Episode，由 Rules 失败关闭。
- Judge 不获得 `state_before` 的额外全知投影；决策前事实只能来自
  `observable_trace.model_calls[*].visible_context`。`state_after/state_delta` 只证明结果。
- 行为失败正常进入评分；Provider/框架/隔离失败写 `runtime-failure-v2`，不冒充分数。
  每个所选 Case 恰有一个 Episode 或 Failure，单例失败不撤销同批成功制品。
- Failure-only 分区也能贯穿 Rules/Judge/Aggregate，Judge Provider 调用为 0；formal 状态
  单调继承上游，任何事后过滤都不能恢复资格。
- E1 `capture.json` 仍只是历史脱敏工程附件，不是 v4、Rules、Judge 或聚合的事实源。

## 活动契约

| Schema | 作用 |
|---|---|
| `case-spec-v2` | 运行输入、身份绑定、Judge 标准、predicate 语义与私有标注控制面 |
| `judge-reference-v2` | CaseSpec 的确定性无标签 Judge 投影 |
| `provider-attestation-v1` | Agent/Judge Provider 的可审计归因 |
| `environment-manifest-v2` | 冻结配置、提交、依赖锁、隔离与 Provider 关联 |
| `decision-episode-v4` | 14 类动作声明、可重建并发层级轨迹、实际效果与完整证据 |
| `runtime-failure-v2` | 不评分的基础设施失败终态，可保存 v4 调用轨迹 |
| `runtime-run-manifest-v3` | 所选 Case 的 Episode/Failure 闭合分区与执行前过滤状态 |
| `rule-result-v3` / `rule-run-manifest-v3` | v4 确定性规则、源码 bundle 摘要与 Hard Gate |
| `judge-result-v3` / `judge-run-manifest-v3` | 盲化结构化 Judge、一次修复与 formal 继承 |
| `aggregate-result-v3` / Track/Manifest v3 | 纯确定性 Rule-first 聚合与源码摘要 |
| `evaluation-protocol-release-v1` | 当前候选 Protocol1.6 及历史版本分别绑定组件 |
| `benchmark-release-manifest-v1` | 固定 Benchmark Case/资源/partition/协议摘要 |
| `trusted-benchmark-registry-v1` | 仓库固定的生产信任根；旧E6与新版E6分项登记 |
| `source-bundle-manifest-v1` | 四个活动执行组件的保守来源包摘要 |
| `schema-lock-manifest-v1` | 全部历史与活动 Schema 的独立原始字节锁 |

所有契约使用 strict Pydantic、`extra=forbid`、JSON Schema 2020-12、唯一 canonical
JSON/SHA-256 和自摘要。独立 Schema Lock 覆盖全部生成 Schema；即使模型和生成文件同步
修改，历史锁仍会失败。已登记协议禁止原地刷新。另版开发使用新的协议身份和复制绑定的数据集，
每批记录具体摘要与 Git commit；旧输出不能改写为新身份。所有历史 Schema 字节保留。

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

历史 Runtime/Rules/Judge/Aggregate Python 入口和历史 Manifest 的执行请求会在创建输出、
调用 Provider、网络、数据库、通知、subprocess 或 Capture 前返回
`legacy_execution_disabled`。包级公共 API 只暴露当前 `run_active_runtime`、
`evaluate_active_rules`、`evaluate_active_judges` 与 `aggregate_active_results`。

当前 `decisionbench-v1.6-regression/engineering` 将旧v4工程Case原内容重新绑定到1.5，
包含故意的错误动作和Provider Failure，预期保留失败终态及非零退出码；不作为模型能力实验。
以下命令在干净仓外副本和项目锁定依赖中执行，输出使用临时目录。旧v4绑定只供其历史源码执行：

```bash
E31_ROOT="$(mktemp -d)"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v1.6-regression/engineering \
  --manifest evaluation/datasets/decisionbench-v1.6-regression/engineering/manifest.json \
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
  --stub-response evaluation/fixtures/e311-fixed-judge-responses-v3.json
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

Judge 盲化投影、Judge 展开 Prompt、Provider 原始响应/异常、API Key、Capture、SQLite/WAL/SHM、
Worker/staging 目录、私有推理和通知路由材料不发布。

## Snapshot、轨迹与身份

Collector 只读取闭合的实体/字段 allowlist。CaseSpec 用公开业务字段声明
`identity_bindings`；注册器以语义字段匹配实际数据库对象，不能按排序位置把声明 ID
静默分配给错误的 Plan/Stage/Task/Submission。未绑定的新对象仍使用稳定生成 ID；
根 AgentRun 和全部后代 AgentRun、RunEvent、ToolInvocation 与 Operation 一并采集。

只读工具通过实际 durable RunEvent 对齐观察；模型可见消息保留产品原始公开结果。
审批后未执行的工具意图必须与 durable 队列匹配，不能虚构为执行成功。已声明的公开逻辑 ID
与缺失 intake 的空标识有明确转换规则，未知数据库引用仍失败关闭。

Delta 只由真实前后 Snapshot 比较得到，明确区分 missing/null、added/removed/changed、
零变化和 typed source。Operation forward/inverse patch 证明归因而不代替后态。
`submission.check` 现在完整枚举 Submission/Task/Stage/Plan 派生变化，因此 Assessment
ACCEPT 和 REVISION_REQUIRED 都能对齐；无法归因的变化仍失败关闭。

## Rules、Judge 与聚合

`e311-rule-pack-v3` 把结构无效与决策错误分开。结构化谓词和动作边界由 Rules 权威
执行；时间、阈值、存在性、隐私、完整性和实际 Hard Gate 不交给 Judge 重算。
四阶段 Manifest 都记录 source bundle digest；Rules/Judge/Aggregate 使用整个 evaluation
包的保守超集，Runtime 另覆盖生产 `backend/app` seam。它是可审计来源包摘要而非动态调用
闭包的数学证明。工作树、Git commit 与 bundle 同时校验，formal 不能由未提交实现生成后
再恢复。

`blind-judge-input-v3` 使用路径级删除和 opaque ID，不按字段名子串或自由文本全局替换。
所以业务中的 `baseline`、`candidate`、`Good` 和 `candidate_key` 保留，而 Case ID、tag、
质量标签、变异源、作者裁决、生成模型、Provider/endpoint、凭据和路由材料不可见。
JudgeReference 的公共 `must_satisfy/must_not` 语义及结构化谓词可见。

公共 `decision-rubric-v1` 固定 D1–D7 权重 `15/15/20/20/15/5/10`，档位映射为
`0/1/2 → 0%/50%/100%`；`decision-track-anchors-v1` 独立定义 Planning、Intervention、
Assessment、Revision 四轨锚点。配置、Prompt 和响应 Schema 都有稳定版本与摘要。

`judge-result-v3` 要求固定 D1–D7、每维至少一个同时对 Judge 可见且能回到同一原始
v4 Episode 的 Evidence Path、稳定 reason code，以及非满分的具体公共问题。第一次
Schema/Evidence/隐私失败只把稳定错误码送回修复一次；第二次失败得到无维度、无分数的
`judge_error`。固定响应不按 ID、track、Oracle 或关键词生成答案。

`deterministic-aggregator-v3` 的基础分为 `sum(weight × level / 2)`。Critical Rule Fail
强制 Fail 且 cap=39，任一 Major Fail cap=69，多个 cap 取最严，Minor 无额外 cap。
Judge 高分不能抵消 Rule Gate，suggested gate 不自动升级。活动 `aggregate-results` 另生成
`reviewed-results.csv`，按 `semantic-critical-review-v1` 校验内容裁决：confirmed Critical
强制 fail/cap 39；没有确定 Gate 时未决建议为 review_required；dismissed 不撤销 Rule Gate。
CSV 保留原始分、Rule-only 结果、裁决后结果、每条裁决及绑定摘要，由 Validator 和读取端重算。
invalid/judge_error/Runtime Failure 单列且不按 0 混入均值；四轨分别发布。

## E5 实验与内容裁决

[E5 档案](artifacts/e5-acceptance-20260906/README.md)保存完整 V1/V2，而非只保存通过版。
V2 严格排序 6/8、Good>Severe 8/8、Critical 联合召回 8/8，结论一致 96.25%、平均总体
标准差 2.243425。Assessment 两个 Mild 漏检，Planning 个别样本波动较大；宏平均达标
不能代替分轨/逐例解释。所有复核明确标记 AI。

实验编排入口是 `evaluation/scripts/run_e5_experiments.py`，复用活动 Runtime/Rules/Judge/
Aggregate API；通用 `validate-method/report/compare` CLI 仍未实现，不能将该脚本当作
全部 E5–E8 工具均已完成。结果需在源码目录外生成，真实执行要求干净的设计绑定提交。
以下以已经生成、版本匹配的 Runtime/Rules 和已有共享账本为输入；`$E5_*` 均由调用者设置，
API Key 只从授权环境内存读取。这些是重跑命令，不会把已有结果覆盖或续跑成新成功批次。

```bash
export PYTHONPATH=evaluation/src
.venv/bin/python evaluation/scripts/run_e5_experiments.py prepare \
  --runtime "$E5_RUNTIME" --rules "$E5_RULES" --output "$E5_OUTPUT"
.venv/bin/python evaluation/scripts/run_e5_experiments.py discrimination \
  --runtime "$E5_RUNTIME" --rules "$E5_RULES" --output "$E5_OUTPUT" \
  --budget-ledger "$E5_LEDGER"
.venv/bin/python evaluation/scripts/run_e5_experiments.py repeat \
  --runtime "$E5_RUNTIME" --rules "$E5_RULES" --output "$E5_OUTPUT" \
  --budget-ledger "$E5_LEDGER"
.venv/bin/python evaluation/scripts/run_e5_experiments.py summarize --output "$E5_OUTPUT"
```

`prepare` 固定全部 24 例及 Good/Mild 16 例，首轮计入各例第一次，追加 64 次；失败保留
固定分母。`summarize` 验证上游与 CSV 后生成 `metrics.json` 和 `per-evaluation.json`，
缺陷定位/召回需要另外做内容裁决，不能从扣分自动推断。本轮的 `defect-content-review.json`
和 `method-validation.json` 绑定实际文件摘要，分别报告实验完成与最低方法指标是否达到。

语义裁决通过唯一活动入口，`--semantic-reviews` 的参数是 **JSON 列表文件路径**：

```bash
.venv/bin/python -m learning_agent_eval aggregate-results \
  --episodes "$E5_RUNTIME" --rules "$E5_RULES" --judges "$E5_JUDGES" \
  --semantic-reviews "$E5_REVIEWS_JSON" --output "$E5_REVIEWED_OUTPUT"
```

每条裁决严格包含 `episode_id/episode_sha256/judge_result_sha256/candidate_id/verdict/`
`reviewer_role/rationale/evidence_paths`。`candidate_id` 引用 `issue:` 或 `gate:` 提名；
复核补充的遗漏 Critical 使用 `reviewer:` 并绑定同例证据。角色为 primary/delegated AI
或 human reviewer，不能混淆；本轮为主 AI 内容裁决及同一子 agent AI 复核。已生成目录
不覆盖；先保留原 Aggregate，再新建裁决输出。44 个冻结 Schema 未因此改写。

## Provider、隔离与正式性

正式性分为三层：单制品的 `protocol_eligible/provider_eligible`、完整批次的
`trusted_benchmark_run`、最终 Aggregate Manifest 的 `formal_capability_result`。单个
Episode/Rule/Judge/Aggregate Result 与所有中间 Run Manifest 都固定
`formal_evaluation_result=false`。最终能力状态必须从仓库固定 production registry、注册
Release、完整 Case/终态库存、上游摘要、四轨结果和错误分类重算；调用者自带 Release、修改
预期数量、删除 Failure 或事后过滤都不能建立信任。

`protocol_eligible` 只描述当前制品/执行是否遵循活动协议，因此一个前置选择的单 Episode
或单 track 分区仍可为 true；`selection_mode=adhoc_filter` 会独立、永久地阻断
`trusted_benchmark_run` 和 `formal_capability_result`，下游不能把两层状态混为一谈。

当前 production registry 已分别登记 `decisionbench-v1-e6-test-release` 和
`decisionbench-v1.4-e6-test-release`；活动 engineering Benchmark Release 仍为
`engineering` 且未注册，所以工程制品即使结构通过，也始终 non-formal。RuntimeFailure、
invalid_input、judge_error 保留审计但阻止能力结论，不按 0 分混入均值。

Provider Attestation 是可审计归因，不是密码学证明。real 资格要求固定 TokenHub HTTPS
allowlist、请求模型 `hy3`、响应模型 `hy3`、Provider request ID、请求/响应时间、安全
配置摘要、当前 Git commit、干净工作树和匹配的 `evaluation-runtime-lock-v1`。Agent/Judge
real 模式都必须传入同一个已有 `--budget-ledger`，每次调用（含结构修复）先预留、按完整
usage 结算，未知费用保留预留。当前 Judge 固定输入上界196608、输出上限12288、n=1、HTTP timeout180秒，
HTTP不自动重试，最多一次结构修复；Agent输出上限沿用16000。证据路径限定在
本次实际可见Episode路径；公开失败仅记录安全类别和HTTP状态，未知usage保留预留。E5 的全部真实调用和
浏览器产品调用均计入唯一共享账本，E5结束占用11.606238元；首批E6结束占用17.609982元。
用户明确授权当时可用17元，累计上限一次更正为34.609982元；本次修复与另版实验新增417请求、16.783047元，
最终907请求/34.393029元，剩余0.216953元。历史7条未知usage预留1.767676元保留，未核对云实扣，
不得重复加17或重置账本。Agent 和
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

v4 Collector/Exporter 对未登记生产事实继续保留明确分类问题并由 Rules 失败关闭；真正
缺失、损坏或无法重建的证据仍由 Validator 拒绝。Provider 响应归因能审计声明与响应字段，
不能证明远端服务的密码学身份；正式运行仍需要组织侧凭据、网络和运行审批。

E4 已完成 48 Primary、24 Calibration 及 8 来源的 AI 内容裁决、绑定重建和最终回归；E5 必需实验已完成且最低方法目标达标。
历史真实批次仍为 45 Episode + 3 Failure，候选保持探索身份，复核记录不计作独立人类一致性。
E6首批与新版G测试均完成冻结登记及全批运行，分别保留46/2和33/15有效Judge/失败，formal=false；
修复/验证和完整分母见[新版档案](artifacts/e6-repair-20260908/README.md)。1.5仅离线候选，G现在已见。
原人工审核由主AI执行并计入完成；扩展AI对齐统计、精确反事实、完整对抗安全扩展及E7/E8按实际进度记录，不等待真人。
固定响应和 engineering 聚合不证明 Judge 标签正确或 Hy3 能力。已授权协议试跑只证明有限样例可执行，
尚未统计完整 14 类真实遵循率；下一轮调用继续复用原预算，不能自动把探索记录升级为正式数据。发布治理细节见
[`../docs/E3.1.2正式评测准入与版本治理.md`](../docs/E3.1.2正式评测准入与版本治理.md)，最终验收命令和精确结果
记录在 [`../docs/STATUS.md`](../docs/STATUS.md)。
