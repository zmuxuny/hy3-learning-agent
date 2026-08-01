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
- `email_reply`：IMAP 轮询从带回复令牌的邮件生成。

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

全局 Session 创建计划后不会被静默改绑。`SessionPlanLink` 记录 `created / discussed / focused` 关系，界面提供“打开计划”和“在计划中继续”。后者创建带 `parent_session_id` 与 `handoff_summary` 的计划 Session；原全局 Session 保持原作用域，新的计划 Session 获得可追溯的最小交接上下文。

Session 与 Plan 都支持可恢复归档。归档只改变生命周期和默认列表，不删除原始消息、计划结构、记忆、证据或事件；归档计划退出主动候选扫描，归档 Session 为只读。手动归档同样写入 `Operation` 审计记录。

`Session → ChatMessage → AgentRun` 同时承担持久化与 UI 恢复：`GET /agent/sessions` 返回以 Session 聚合的标题、消息数、Run 数和最近状态，`GET /agent/sessions/{session_id}/messages` 返回完整原文。前端选择历史 Session 后恢复整个消息流，并把最新 Run 的事件投影到对应用户消息之后。新 Run 先乐观加入用户消息，完成事件到达后再用数据库原文替换，避免网络时序造成重复或闪烁。首轮完成后由独立短请求生成语义标题，`PATCH /agent/sessions/{session_id}` 支持手动改名；自动命名只会替换未被用户修改的初始标题。

计划制定在 Session 内增加两层持久状态：`PlanningIntake` 保存目标、带来源的已确认事实、结构化待确认问题、充分性结论/置信度/理由；`PlanProposal` 保存完整 PlanCreate 负载、主 Agent 理由、子 Agent 报告和 pending/accepted/rejected 生命周期。普通会话 Run 不能再直接调用 `plan_create`；必须先将 Intake 标为 ready，再写提案。`POST /agent/plan-proposals/{id}/decision` 是显式提交边界，采用操作幂等地创建正式 Plan、Operation 与 SessionPlanLink。

用户编辑消息采用非破坏式当前分支语义：旧内容写入 `ChatMessageRevision`，旧 Run、事件、快照与 Operation 不变；目标消息之后的旧消息加 `superseded_by_edit` 标记并从 Session API、上下文组装、摘要压缩与 handoff 中排除。修订内容仍在原 Session 创建新 Run，因此不会在侧边栏产生伪对话。当前版本保留审计但不提供旧分支切换 UI。

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
3. 当前焦点计划快照；全局对话只注入紧凑计划索引和 Session 关联计划，不批量注入跨计划资源与提交
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

调度器只有一个全局循环。每 `AGENT_HEARTBEAT_SECONDS` 做一次确定性候选扫描，而不是为每个任务创建常驻心跳：先检查到期复习、24 小时内任务，再检查最久未产生学习证据的活动计划。`AGENT_PROGRESS_CHECKIN_HOURS` 默认 24 小时；近期已经发过站内消息时不会重复产生进度询问候选。`GET /settings/proactive` 暴露下一轮时间、最近判断和最近心跳 Run，前端每 15 秒同步状态与站内通知。

## 5.1 Harness 运行事件

前端展示可审计过程，不展示模型私有思维链。最新 Run 在消息下方显示一行可折叠活动摘要，展开后查看状态、工具与子 Agent 事件；完整载荷仍进入右侧轨迹抽屉：

```text
run.started
assistant.status
tool.started
tool.completed
subagent.started
subagent.completed
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

工具运行还维护每个 Run 独立的失败熔断器：同一工具连续失败两次后，本轮不再把该工具暴露给 Hy3，避免网络或依赖故障耗尽全部工具轮次；其他能力仍可继续使用，下一次 Run 会重新尝试。

Web 工具对初始 URL 和每一次重定向都执行 SSRF 校验。localhost、IP 字面量、`.local`、RFC 私网和链路本地地址始终拒绝；在显式开启本地代理兼容时，只允许公网域名经 Clash/Mihomo 一类代理解析到 `198.18.0.0/15` 或 `2001::/32` 的 Fake-IP，直接请求这些地址仍被拒绝。搜索提供商通过统一接口选择，当前默认实现为 DuckDuckGo HTML。

学习资源采用两阶段协议：`web_search / web_open` 负责发现与正文核验，`resource_save` 才把 Agent 明确选择的课程、教程、实验、学习路径或参考资料写入计划。保存项包含平台、类型、难度、语言、核验摘要和适配理由，并生成可撤销 `Operation`；原始搜索结果不等同于课程资源。

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
- 整栏滚动的 Sidebar 与置顶计划归档入口
- 结构化提问卡提交（不复制成普通消息气泡）
- CommonMark 标题、列表、引用、表格、链接和代码块
- 可收起的完整运行轨迹抽屉
- 主动抽查卡片
- 记忆查看器和上下文来源

视觉重点是信息可解释、状态明确、对齐精确和快速响应。计划卡、边框、间距与状态色使用统一视觉 Token，不依赖游戏化特效制造完成感。

## 8.1 Harness 的 UI 投影

```text
Sidebar                  Conversation Canvas               Run Drawer
计划 / 记忆 / Session     Session 多轮原始消息               完整事件序列
学习 Agent 在线状态        关键上下文与工具摘要               参数 / 结果 / 失败
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

## 9. 个人部署边界

系统固定使用本地 Owner，不提供登录、注册、团队或租户能力。`owner_id` 只作为个人数据的稳定命名空间，便于导入、备份和防止工具漏写作用域，不代表多用户产品路线。

## 10. 子 Agent 边界

当前注册 `planning_delegate`，可一次把最多三个规划调查分给独立 `AgentRun(trigger=subagent, parent_run_id=...)`。子 Run 只接收父 Run 的只读上下文快照和单一任务；工具白名单限于画像/记忆/文件/日历读取及 `web_search/web_open`，并强制拒绝保存搜索结果和全部业务写工具。子 Run 不写主 Session 消息，返回简短报告后由主 Agent join、解决冲突并生成提案。父事件流记录 `subagent.started/completed`，侧边栏与最近 Run 查询只投影根 Run，不把子 Run 冒充新对话。

这是针对计划共创的受限委员会，不等于通用 Agent 编排。后续通用能力仍必须：

- 只继承最小必要上下文和工具。
- 默认不能直接修改计划或长期记忆。
- 返回结果与证据给主 Agent，由主 Agent决定后续动作。
- 产生独立 `run_id`，并在父 Run 的事件流中可见。

通用 spawn/join/cancel、每类子 Agent 工具白名单、费用预算和崩溃检查点按 `ROADMAP.md` 的后续顺序实现；当前规划子 Run 的失败会作为报告返回，主 Agent 可降级完成。

应用启动时会把上一个进程遗留的 `queued/running` Run 标记为 `failed(process_interrupted)` 并追加可见事件，保留原消息、工具结果和操作记录，同时解除 Session 的假占用。这是安全收口，不是检查点续跑；真正的进程恢复仍在 Roadmap 中。
