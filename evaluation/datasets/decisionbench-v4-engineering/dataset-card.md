# DecisionBench v4 Engineering Suite

## 状态与用途

这是 E3.1.1 建立、E3.1.2 纳入 Evaluation Protocol Release 1.0 的公开合成工程套件，
不是 DecisionBench v1 正式数据集。它只验证活动
`case-spec-v2 → decision-episode-v4/runtime-failure-v2 → rule-result-v3 →
judge-result-v3 → aggregate-result-v3` 链路能保留并评价正确、错误、安全阻断、组合行为与
基础设施失败。全部 Case 都是 `dev + engineering_mini + stub`，永远
`not_a_formal_model_evaluation`，不能用于 Hy3 能力结论、Calibration 标签或版本排名。
随附 `benchmark-release.json` 是状态为 `engineering` 的完整发布清单，但没有进入仓库固定
production trusted registry；内容完整不等于来源可信，也不能自我声明 formal。

Release 1.0 只允许 v4 活动 Episode；v3 及更早契约仅保留历史工程回归，不继续扩展正式
能力或公开执行。当前没有创建 48 个 Primary 或 24 个
Calibration，也没有调用真实 Hy3 或公网。

## 内容与覆盖

套件包含 12 个 Case；完整运行确定性地产生 11 个 Episode 和 1 个故意的 Runtime
Failure：

- Planning、Intervention、Assessment、Revision 四轨正例；
- 错误 Assessment verdict，结构有效但触发 Critical Rule Gate 和 cap 39；
- Assessment 中越权修改计划，作为跨轨行为评分而非基础设施失败；
- `INSUFFICIENT_EVIDENCE + REQUEST_CLARIFICATION` 有序组合动作；
- 缺少动作声明，保留为 `NO_OP + action.declaration_missing` 的可评分行为；
- Notification Guard 成功阻断危险发送尝试，同时保留 attempted action 和零副作用事实；
- 生产 `quiz_create + review_schedule` 写入，映射为
  `INTERVENE_QUIZ_OR_REVIEW` 且完整关联 Quiz/Review 效果；
- 一次并发双 Planning 子 Agent 委派，验证唯一连续 ordinal、父子 Run 和
  `parent_call_id`；
- 一个注入的 Provider Failure，验证单例失败不删除同批成功 Episode，并支持
  Failure-only Rules/Judge/Aggregate 分区。

14 个规范动作全部由版本化 `model-action-declaration-v2` 注册。每类行动都有 track、决策
语义、字段、相近边界、正反例与组合政策，并经严格 JSON 首个非空帧测试；标准空白和字段
顺序不改变语义；
不从 Episode ID、track、Oracle、scripted answer 或自然语言关键词猜测动作。工具写入只用于
核对声明与真实效果，工具成功本身不代表决策正确。

## CaseSpec 与 JudgeReference

`case-spec-v2` 是唯一案例控制面。它要求同时声明 `must_satisfy` 与 `must_not`，并固定
`constraint-proposition-v1`：同一 constraint 的 predicates 全部合取；`must_satisfy`
要求命题为真，`must_not` 要求命题为假。确定性约束由 Rules 执行，语义约束交给 Judge，
二者都使用公共 statement，但不会获得私有标注。

`judge-reference-v2` 只能从 CaseSpec 确定性派生。它保留无标签 public statement、结构化
predicate、Evidence Path、criticality 与可接受变体；Calibration 质量标签、变异来源、
作者/复核者、裁决说明、原 Case ID 和 scripted turns 均不可见。被测 Agent 看不到
CaseSpec 或 JudgeReference。

## 轨迹、隐私与隔离

DecisionEpisode v4 保存模型实际收到的脱敏消息、工具 Schema、公开响应和工具引用，并为
每次调用记录 `run_id/parent_run_id/parent_call_id/depth/call_purpose`。并发 ordinal 在调用
开始时原子保留，最终按开始顺序发布；辅助调用与决策调用显式区分。Judge 不获得模型当时
未见的 `state_before` 全知视图。

路径级盲化只移除控制面身份与私有元数据。普通学习业务文本中的 `baseline`、`candidate`
和 `Good` 保持原样；Case 标签、变异来源、生成模型/Provider、凭据、路由、Capture 和私有
推理不会进入 Judge 输入。Capture 不是事实源。

每个 Case 在独立 Worker、系统临时 SQLite、冻结时钟和版本化资源快照中执行。stub 模式的
网络、SMTP、SMTP_SSL、Web Push、IMAP、IMAP_SSL、Worker subprocess、Worker 外 SQLite
与禁止文件访问必须为 0。输出目录不得包含 SQLite/WAL/SHM。

## Rules、Judge、聚合与正式性

`e311-rule-pack-v3` 将动作不符、跨轨、声明/效果不一致、Case predicate 失败和隔离违规作为
可评分 Rule Fail；只有 Schema、digest、引用、Evidence Path 或轨迹事实损坏才是 invalid。
Rule Manifest 记录实际 evaluator 源码 bundle 摘要和工作树状态。

`blind-judge-input-v3` 向 fixed-response Judge 提供原始 v4 Episode 的可见投影、digest 匹配
Rules、JudgeReference、七维 Rubric 与当前轨锚点。`judge-result-v3` 继续执行 D1–D7 固定
顺序、0/1/2、Evidence Path、一次修复和无默认分数的 `judge_error`。固定响应只验证协议，
不是语义评分器。

`aggregate-result-v3` 保持 Rule-first 语义：Critical cap 39 并强制 Fail，Major cap 69，
suggested gate 不自动升级；invalid、judge_error 和 Runtime Failure 单列，不按 0 计入均值；
只发布逐 Episode 和逐轨结果，不发布 overall。单制品的 protocol/provider eligibility、
可信完整 Benchmark 运行和最终能力结论是三个不同状态。Aggregate Manifest 绑定 Runtime
Manifest digest 与每个 Case 的终态；Validator 还会对照生产注册表中的 Release。
本套件未注册，所以所有层都保持 non-formal；任何事后 `--episode-id/--track` 过滤、删除
Failure、改写预期数量或翻转布尔位都不能恢复资格。

## 运行

```bash
E311_ROOT="$(mktemp -d)"
.venv/bin/python -m learning_agent_eval run-agent \
  --dataset evaluation/datasets/decisionbench-v4-engineering \
  --manifest evaluation/datasets/decisionbench-v4-engineering/manifest.json \
  --output "$E311_ROOT/runtime"
.venv/bin/python -m learning_agent_eval evaluate-rules \
  --input "$E311_ROOT/runtime" --output "$E311_ROOT/rules"
.venv/bin/python -m learning_agent_eval evaluate-judge \
  --episodes "$E311_ROOT/runtime" --rules "$E311_ROOT/rules" \
  --output "$E311_ROOT/judges" --judge-mode stub \
  --stub-response evaluation/fixtures/e311-fixed-judge-responses-v3.json
.venv/bin/python -m learning_agent_eval aggregate-results \
  --episodes "$E311_ROOT/runtime" --rules "$E311_ROOT/rules" \
  --judges "$E311_ROOT/judges" --output "$E311_ROOT/aggregate"
```

完整套件含故意 Failure 和 Critical Fail，因此部分命令按门禁语义返回非零，同时仍原子发布
完整、可验证制品。每层都应另用 `validate-dataset` 校验。临时根目录应在验收后删除。

## 明确不代表

本套件不代表正式 Benchmark Release、真实用户、真实 Provider 身份的密码学证明、真实 Hy3 表现、评测有效性实验、
人工一致性、正式 Dataset Split、Baseline/Candidate 回归、最终报告、Case、Demo 或 Release。
这些仍属于 E4–E8，必须另行授权和实施。发布与版本治理见
[`../../../docs/E3.1.2正式评测准入与版本治理.md`](../../../docs/E3.1.2正式评测准入与版本治理.md)。
