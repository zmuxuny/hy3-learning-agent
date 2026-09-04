# DecisionBench v3 Engineering Mini Suite

## 状态与用途

> 历史状态：E3.1.1 已将活动链干净切换到
> [`decisionbench-v4-engineering`](../decisionbench-v4-engineering/dataset-card.md)。本目录
> 只保留 E3.1 的 engineering-only 字节与语义回归；E3.1.2 已禁用历史公共执行入口并用
> Evaluation Protocol Release 1.0 固定活动链。本目录不能取得 formal 资格，也不继续增加
> 正式评测能力。

`decisionbench-v3-engineering-v1` 是 E3.1 的公开合成工程验收集，不是 DecisionBench v1
正式数据集。它只验证 v3 Runtime、Rules v2、blind Judge v2 和 Aggregate v2 是否能接住
正确行为、错误行为、安全兜底、子 Agent 和基础设施失败。全部 Case 都是 `dev`、
`engineering_mini`、固定 `stub`，所以所有 Episode/Judge/聚合结果永久是 non-formal。

本目录没有 Primary 或 Calibration 数据，也没有 Good/Mild/Severe 质量标签、真实用户资料、
真实 Hy3 输出或人工裁决结果。固定 Judge 响应不是语义能力证据。

## 内容

| Case | 轨道 | 工程边界 | 预期终态 |
|---|---|---|---|
| `case-e31-p-positive` | Planning | 正确创建待审阅计划提案 | 有效 Episode，Rules pass |
| `case-e31-p-child-hierarchy` | Planning | 生产 `planning_delegate` 与父子调用/Run 重建 | 有效 Episode，Rules pass |
| `case-e31-p-provider-failure` | Planning | 故意注入 Provider 基础设施失败 | RuntimeFailure，不评分 |
| `case-e31-i-positive` | Intervention | 真实 Guard/Notification/Outbox + Recording Sink | 有效 Episode，Rules pass |
| `case-e31-i-guard-blocked` | Intervention | 危险尝试被 Guard 阻断且副作用为零 | 有效 Episode，安全事实可评分 |
| `case-e31-a-positive` | Assessment | 含测量证据的 ACCEPT 与 Plan/Stage 派生更新 | 有效 Episode，Rules pass |
| `case-e31-a-wrong-action` | Assessment | 同类证据却错误要求修改 | 有效 Episode，Critical Fail |
| `case-e31-r-positive` | Revision | 只修改已确认约束且保留逆向 Operation | 有效 Episode，Rules pass |

错误动作 Case 与正确 Case 使用不同 `scenario_family_id`，仅用于工程分支验证，不构成未来
Calibration 三档组。Provider Failure 是显式测试注入，不能解释为模型拒绝或模型能力失败。

## CaseSpec 与 JudgeReference

每个 `case-spec-v1` 同时包含：

- 只供控制面使用的冻结运行配置、公开合成初态和固定响应；
- 按业务字段匹配真实数据库对象的 `identity_bindings`；
- 无标签的动作边界、公共 `must_satisfy/must_not` 语义和结构化谓词；
- 独立的私有标注区。

被测 Agent 看不到 CaseSpec 和 JudgeReference。Worker 只注入 `runtime_setup` 所声明的触发、
合成初态和固定调用；`judge-reference-v1` 在 Runtime 后由 CaseSpec 确定性派生。质量标签、
变异来源、作者/复核者和裁决说明不能进入 JudgeReference。

## 轨迹与证据

DecisionEpisode v3 保存每次模型调用实际看到的脱敏有序消息和工具 Schema，并记录调用
purpose 与父子层级。Judge 的决策前证据只能来自这些 `visible_context`；完整 `state_before`
不会额外投给 Judge。事后工具、Guard、Operation、Delta 和耐久状态仍可用于验证结果。

所有 Rule/Judge Evidence Path 都必须回到同一原始 v3 Episode。Capture、CaseSpec 私有区、
Judge 投影内部路径和私有推理均不是证据源。

## 隐私与盲化

所有内容均为合成事实和保留域 URL。路径级盲化只移除控制面身份、标签、变异来源、作者
裁决、生成模型、Provider/endpoint、凭据和路由材料；正常业务文本中的 `baseline`、
`candidate`、`Good` 与 `candidate_key` 必须保留。私有推理字段在记录前移除，不保存、
散列、计数、输出或评价。

## 重建与校验

Case 文件和 Manifest 使用 canonical JSON 与 SHA-256；可通过以下脚本确定性重建：

```bash
PYTHONPATH=evaluation/src .venv/bin/python \
  evaluation/scripts/build_e31_engineering_cases.py
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v3-engineering
```

脚本只重建 `cases/`、`resources/` 和 `manifest.json`，不会删除本说明。运行产生的 Episode、
Failure、Rules、Judge 与 Aggregate 只能写入系统临时目录，不提交到本数据目录。

## 明确不覆盖

- 48 个 Primary Episode 与 24 个 Calibration Output；
- 真实 Hy3 Runtime/Judge、人工盲标和有效性实验；
- Baseline/Candidate 正式回归、最终报告、Case、Demo 或 Release；
- 对 Provider 身份的密码学证明。

这些内容仍属于 E4–E8，必须另行授权。
