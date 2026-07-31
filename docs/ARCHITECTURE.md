# 主动 Agent 与上下文架构

## 1. 总体架构

```text
用户消息 ─┐
后台心跳 ─┼─▶ AgentRuntime ─▶ ContextAssembler ─▶ Hy3
任务事件 ─┘        ▲                                  │
                  └──── 观察结果 ◀──── ToolRegistry ◀─┘
                                           │
                  SQLite / Markdown ◀──────┼──────▶ 通知渠道
                                           │
                                   Operation / RunEvent
                                           │ SSE
                                           ▼
                                      Harness 工作台
```

后台 Worker 持续运行；Hy3 按事件调用。用户消息和主动事件共享同一个可观察、可停止、有轮次预算的工具循环，确定性 Guard 只约束权限、触达频率和安全边界。

用户对话和后台心跳都进入同一个 `AgentRuntime`，只改变触发源：

- `user_message`：用户明确提出目标或指令。
- `heartbeat`：调度器周期性检查状态。
- `task_event`：任务完成、延期或提交发生变化。
- `review_due`：到达计划的复习时间。
- `email_reply`：后续由邮件接收器生成。

每次运行都有唯一 `run_id`，前端通过 SSE 订阅运行事件。

## 2. 分层上下文

### Global Learner Profile

跨计划共享且相对稳定的信息：

- 学习目标和长期方向
- 每日可用时间、免打扰时间
- 偏好的解释方式和学习节奏
- 已确认的优势、薄弱点和约束

### Plan Memory

每个计划独立维护：

- 计划目标和完成标准
- 当前阶段与进度摘要
- 已掌握内容和未解决阻塞
- 最近一次干预及结果
- 下一次复习或检查时间

### Event Ledger

不可变的学习事件流：

- 任务创建、开始、完成和延期
- 对话、提交、评分和抽查结果
- Agent 的提醒决策，包括选择保持安静的决策
- 用户对记忆或计划建议的确认与拒绝

### Conversation Window

只保存当前对话需要的近期消息。较早对话经过总结后进入全局或计划记忆，不无限堆叠原始消息。

每条会话拥有显式焦点：`plan_id = null` 表示全局对话，非空值表示计划对话。`currentPlan` 只代表界面正在查看的数据，不能被当作对话焦点；前端使用独立的 `focusPlanId` 组装 Run 请求。切换全局与计划焦点时建立新的会话边界，避免近期原文跨计划串入。后端同时校验已有 Session 的 `plan_id`，拒绝用同一个 Session 静默改绑其他计划，隔离不能只依赖 UI。

`Session → ChatMessage → AgentRun` 同时承担持久化与 UI 恢复：`GET /agent/sessions/{session_id}/messages` 按时间和 ID 返回原始消息；前端选择任一历史 Run 时先恢复整个 Session，再把该 Run 的事件投影到对应用户消息之后。新 Run 先乐观加入用户消息，完成事件到达后再用数据库原文替换，避免网络时序造成重复或闪烁。

### Working Memory

当前 Agent Run 的目标、临时计划、工具结果和未完成步骤。Run 结束后只保留事件与总结，不把临时推断直接提升为长期事实。

### Memory Proposal

模型从对话和学习结果中提取的候选长期记忆。候选包含作用域、来源、置信度和过期策略，经用户确认后才进入全局长期记忆。

## 3. 数据库与 Markdown 快照

数据库是事实来源；Markdown 是面向模型和用户的可读快照，不作为唯一存储。

建议生成：

```text
data/context/global.md
data/context/plans/{plan_id}.md
data/context/decisions/{date}.md
```

快照由结构化记录生成，包含更新时间和来源事件 ID。用户可以在“记忆查看器”中纠正内容，纠正本身也记录为新事件。

## 4. 上下文组装顺序

每次调用 Hy3 时，`ContextAssembler` 按预算组装：

1. 系统角色、权限和输出 Schema
2. 全局用户画像摘要
3. 当前焦点计划快照；全局对话不注入某一计划的私有快照
4. 与候选事件相关的历史事件
5. 最近一次干预及用户反应
6. 必要的近期对话

Hy3 支持长上下文，但系统仍需选择、分层和压缩。长上下文能力用于保留更多相关证据，不用于无差别塞入全部历史。

## 5. 主动决策协议

主动心跳与用户对话使用同一个 Tool Calling 循环，不维护一套独立的固定 JSON 工作流。Hy3 可以先调用 `plan_list`、`plan_get`、画像或事件工具收集证据，再自主选择：

- 不调用写工具，并在 Run 结论中记录保持安静；
- 调用通知、测验或复习工具进行干预；
- 调用可撤销的计划工具完成低风险调整；
- 创建记忆或高风险变更候选，等待用户确认。

每次模型轮次、工具开始/完成、最终结论和失败都归入同一个 `run_id`。`silent` 同样必须形成完成事件，证明 Agent 做过判断，而不是只有通知结果。

## 5.1 Harness 运行事件

前端展示可审计过程，不展示模型私有思维链：

```text
run.started
assistant.status
tool.started
tool.completed
approval.required
operation.committed
notification.sent
run.completed
run.failed
```

用户可以停止 Run。写操作完成后，界面显示影响范围和撤销入口。

## 6. Intervention Guard

模型决策不能直接触达用户，必须经过确定性规则：

- 免打扰时间
- 单日通知上限
- 同类提醒冷却时间
- 已完成或已删除任务过滤
- 高风险计划修改必须由用户确认
- API 失败时不重复轰炸

## 7. 计划内存隔离

每个计划只读取自己的 `Plan Memory` 和相关事件。全局画像可以被所有计划引用，但一个计划的私有对话不会自动泄漏到另一个计划。

当跨计划信息确实有价值时，Hy3 只能提出一条“提升为全局记忆”的候选，用户确认后写入 Global Learner Profile。

## 8. 第一阶段模块

后端计划拆分为：

```text
backend/app/
├── api/             # HTTP and SSE endpoints
├── context/         # assembly, snapshots and consolidation
├── core/            # configuration and scheduling
├── db/              # engine and repositories
├── models/          # persistent entities
├── notifications/   # inbox/browser/email adapters
├── runtime/         # run loop, event stream and sub-agent boundary
├── services/        # plans, quizzes and learning events
└── tools/           # atomic agent capabilities
```

前端第一阶段实现：

- 今日状态与通知收件箱
- 计划、阶段和任务
- 对话主画布与内联 Agent 行动摘要
- 可收起的完整运行轨迹抽屉
- 主动抽查卡片
- 记忆查看器和上下文来源

视觉重点是信息可解释、状态明确、对齐精确和快速响应。计划卡、边框、间距与状态色使用统一视觉 Token，不依赖游戏化特效制造完成感。

## 8.1 Harness 的 UI 投影

```text
Sidebar                  Conversation Canvas               Run Drawer
计划 / 记忆 / 最近 Run    Session 多轮原始消息               完整事件序列
主动教练在线状态          关键上下文与工具摘要               参数 / 结果 / 失败
                         固定输入框                         审计与撤销确认

Plan Index
计划卡列表：目标 / 状态 / 进度 / 期限 / 阶段与任务概况
└─ 点击单一计划
   ▼
Plan Workspace
当前计划：版本 / 纵向阶段时间线 / 任务行 / 证据 / 复习 / Agent 操作痕迹
├─ Agent 输入框与计划内容共享整个内容区的视觉中轴
├─ 输入框明确显示“计划焦点”或“全局对话”
└─ 任何“让 Agent 检查”请求都返回统一 AgentRuntime，不直接写数据库
```

页面层级与对话焦点是两组状态：`planScreen` 控制列表或详情，`currentPlan` 承载详情数据，`focusPlanId` 决定下一次 Run 的上下文。导航到“学习计划”只打开列表，不自动加载第一份计划；Run 完成后的数据刷新只更新数据，不强制切换页面。进入具体计划时才选择详情并建立计划焦点，“新对话”则清空会话与计划焦点。

## 9. 多用户预留

首版没有登录，但所有用户数据实体保留 `owner_id`。服务层不能依赖硬编码用户；本地模式通过配置注入默认 Owner。未来加入认证后，数据库隔离和工具权限不需要整体重写。

## 10. 子 Agent 边界

统一 Agent 可以为资源调研、代码作业评测或计划冲突分析启动子 Agent。子 Agent：

- 只继承最小必要上下文和工具。
- 默认不能直接修改计划或长期记忆。
- 返回结果与证据给主 Agent，由主 Agent决定后续动作。
- 产生独立 `run_id`，并在父 Run 的事件流中可见。

参赛 MVP 先实现协议与事件类型，不以多 Agent 作为关键依赖。
