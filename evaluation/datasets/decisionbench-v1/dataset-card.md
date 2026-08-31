# DecisionBench v1 Dataset Card

## 当前状态

本目录在 E0 只包含四个 **手工协议 Mini Episode**，每条轨道一个：

| Episode | 轨道 | 场景作用 | 当前身份 |
| --- | --- | --- | --- |
| `P-MINI-001` | Planning | 时间预算、最终产物与提案边界 | 手工协议夹具 |
| `I-MINI-001` | Intervention | 近期有效证据下的 `WAIT`/克制边界 | 手工协议夹具 |
| `A-MINI-001` | Assessment | 必需 Artifact 缺失时的 False Accept 边界 | 手工协议夹具 |
| `R-MINI-001` | Revision | 时间约束变化、目标保留与审批边界 | 手工协议夹具 |

它们用于证明 Schema、Action Envelope、Evidence Path、摘要、Split 与隐私校验，**不属于**规划中的 48 个 Primary Episodes 或 24 个 Calibration Outputs，也不代表真实 Hy3 表现。当前目录不能称为已完成的 DecisionBench v1，不能生成正式分数、排名或结果表。

## 来源与构造

- 数据来源：依据第三阶段评测实施方案手工构造的合成学习状态；
- 构造方式：`hand_authored`，未运行生产 Runtime、模型、工具或 Guard；
- 作者与复核者：只记录 `evaluation_protocol_author` / `evaluation_protocol_reviewer` 角色，不记录个人身份；
- 用途：协议与离线校验回归；
- 环境：固定时间和时区，无模型调用、无数据库、无网络、无通知通道；
- 资源：不包含外部网页、用户文件或受版权保护的数据快照。

每个样本提供可接受行动类别、必须满足的条件、禁止行为、预期效果、可接受变化和 Critical Failure。Oracle 约束决策及后果，不提供唯一标准文本。

## 划分与泄漏策略

四个 Mini Episode 都位于 `dev`，并使用互不相同的 `scenario_family_id`。数据集校验器按场景族强制 Dev/Test 不相交；未来 Calibration 变体必须继承种子场景的 Split。

正式规划仍是：`S01–S04` 属于 Dev，`S05–S12` 属于 Test。E0 没有创建这些正式场景，也没有用 Test 标签调整 Rubric、规则或 Prompt。

## 覆盖范围

当前覆盖：

- 四轨顶层结构与动作枚举；
- 合成状态逻辑 ID、引用和公共 Context 摘要；
- 离线 Environment Manifest；
- 空的手工轨迹与构造结果边界；
- 多动作/多变化的 Acceptable Action Envelope；
- canonical JSON、嵌套摘要和 Episode 摘要；
- 隐私、秘密、Dev/Test 泄漏和未知字段失败关闭。

当前不覆盖：真实 Runtime 轨迹、Recorder、临时数据库 Fixture、资源快照、假 Outbox、Exporter、规则评分、Hy3 Judge、人工盲标、Calibration Mutation、有效性实验和正式结果。

## 隐私、个人信息与安全

所有学习者、目标、任务、证据和约束均为合成事实，不来自真实用户。数据不包含真实姓名、邮箱、电话、通知地址、账号、数据库行、`.env`、API Key、认证 Token 或模型私有思维链。

校验会拒绝 `reasoning_content` 及同义私有推理字段、凭据字段/疑似值、非保留域邮箱和个人身份字段。错误输出不回显载荷。评测 CLI 不导入生产应用模块，不创建 SQLite，不调用网络或外部通知 Provider。

## 许可与预期用途

本目录随仓库使用 MIT License。Mini Episode 仅适合协议实现、验证器开发和 CI 回归；不得用于声称模型质量、用户采用效果、教育有效性或真实世界安全性。

## 校验

完成源码安装后，在仓库根目录运行：

```bash
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v1
```

预期结果为四个样本通过，且 Planning / Intervention / Assessment / Revision 各一个。精确回归结果以 [`../../../docs/STATUS.md`](../../../docs/STATUS.md) 为准。
