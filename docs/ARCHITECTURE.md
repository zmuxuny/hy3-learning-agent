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

每次运行都有唯一 `run_id`，前端通过 SSE 订阅运行事件。进程内任务注册表也以 `run_id` 跟踪主 Run、心跳与子 Run，使取消接口可以终止真实协程；数据库中的 `cancel_requested/status` 仍是跨重启的权威状态。

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

只装配当前对话需要的近期消息。较早消息压缩为当前 Session 摘要，但不会自动提升为全局或计划长期记忆；每次压缩写入不可变 `SessionSummary`，记录版本、覆盖到的消息、来源消息 ID 和生成方式。原始消息始终保留。

每条会话拥有显式焦点：`plan_id = null` 表示全局对话，非空值表示计划对话。`currentPlan` 只代表界面正在查看的数据，不能被当作对话焦点；前端使用独立的 `focusPlanId` 组装 Run 请求。切换全局与计划焦点时建立新的会话边界，避免近期原文跨计划串入。后端同时校验已有 Session 的 `plan_id`，拒绝用同一个 Session 静默改绑其他计划，隔离不能只依赖 UI。

全局 Session 创建计划后不会被静默改绑。`SessionPlanLink` 记录 `created / discussed / focused` 关系，界面提供“打开计划”和“在计划中继续”。后者创建带 `parent_session_id` 与 `handoff_summary` 的计划 Session；原全局 Session 保持原作用域，新的计划 Session 获得可追溯的最小交接上下文。

Session 与 Plan 都支持可恢复归档。归档只改变生命周期和默认列表，不删除原始消息、计划结构、记忆、证据或事件；归档计划退出主动候选扫描，归档 Session 为只读。手动归档同样写入 `Operation` 审计记录。

`Session → ChatMessage → AgentRun` 同时承担持久化与 UI 恢复：`GET /agent/sessions` 返回以 Session 聚合的标题、消息数、Run 数和最近状态，`GET /agent/sessions/{session_id}/messages` 返回完整原文。前端选择历史 Session 后恢复整个消息流，并把最新 Run 的事件投影到对应用户消息之后。新 Run 先乐观加入用户消息，完成事件到达后再用数据库原文替换，避免网络时序造成重复或闪烁。首轮完成后由独立短请求生成语义标题，`PATCH /agent/sessions/{session_id}` 支持手动改名；自动命名只会替换未被用户修改的初始标题。

计划制定在 Session 内增加两层持久状态：`PlanningIntake` 保存目标、带来源的已确认事实、结构化待确认问题、充分性结论/置信度/理由；`PlanProposal` 保存完整 PlanCreate 负载、主 Agent 理由、子 Agent 报告和 pending/accepted/rejected 生命周期。普通会话 Run 不能再直接调用 `plan_create`；必须先将 Intake 标为 ready，再写提案。`POST /agent/plan-proposals/{id}/decision` 是显式提交边界，采用操作幂等地创建正式 Plan、Operation 与 SessionPlanLink。

用户编辑消息采用非破坏式当前分支语义：旧内容写入 `ChatMessageRevision`，旧 Run、事件、快照与 Operation 不变；目标消息之后的旧消息加 `superseded_by_edit` 标记并从 Session API、上下文组装、摘要压缩与 handoff 中排除。修订内容仍在原 Session 创建新 Run，因此不会在侧边栏产生伪对话。当前版本保留审计但不提供旧分支切换 UI。

### Working Memory

当前 Agent Run 的目标、临时计划、工具结果和未完成步骤。Run 结束后只保留事件与总结，不把临时推断直接提升为长期事实。

### Memory Proposal

模型从对话和学习结果中提取的候选长期记忆。候选包含作用域、来源、置信度和过期策略，经用户确认后才进入检索。相同内容会强化既有记录；纠正通过 `supersedes_id / superseded_by_id` 保留新旧关系。归档、到期和被替代都是可审计生命周期，不通过公开 API 物理删除。

## 3. 数据库与 Markdown 快照

数据库是事实来源；Markdown 是面向模型和用户的可读快照，不作为唯一存储。

当前生成：

```text
data/context/global.md
data/context/plans/{plan_id}.md
data/context/runs/{run_id}.md
```

`global.md` 和 `plans/{plan_id}.md` 是最新可读投影，只保存稳定画像、计划与记忆，不混入某个 Session 的 Conversation；`runs/{run_id}.md` 是该轮精确模型输入的可读副本。数据库中的 `ContextSnapshot` 才是每个 Run 的不可变事实历史，保存完整 Markdown、来源清单和 Token 估算。`GET /agent/runs/{run_id}/context` 与消息内上下文检查器用于复现本次模型输入。用户可在记忆查看器中确认、纠正、归档和恢复，并查看来源与检索使用痕迹。

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

调度器只有一个全局循环。每 `AGENT_HEARTBEAT_SECONDS` 做一次轻量确定性候选扫描，而不是为每个任务创建常驻心跳：先检查到期复习、24 小时内任务，再逐个检查活动计划的最新学习证据。扫描阶段不加载聊天全文；命中候选后才用明确的 `plan_id` 启动计划级 Run，由 ContextAssembler 注入该计划结构、证据、计划记忆和必要事件。`AGENT_PROGRESS_CHECKIN_HOURS` 默认 24 小时；`AGENT_CANDIDATE_COOLDOWN_MINUTES` 默认 180 分钟，避免计划被每轮重复交给模型。只有提交、考核、证据或任务完成等学习行为会刷新“最近活动”，计划元数据维护不再伪装成学习进展。`GET /settings/proactive` 暴露下一轮时间、最近判断和最近心跳 Run，前端每 15 秒同步状态与站内通知。

主动提醒的权威回复位置是 Session，不是收件箱。`notification_send` 优先使用来源 Session；无会话后台 Run 会复用同计划最近的活动 Session，没有时建立一个稳定的“学习跟进”Session。提醒以 `ChatMessage(role=assistant, ui_kind=proactive_notification)` 投影到该对话，同时以 `Notification` 投影到各发送渠道。收件箱、页面 Toast、Service Worker 和邮件令牌都指向同一 Session；`POST /notifications/{id}/open` 会为旧通知幂等补链并返回精确 `message_id`。输入区持有显式回复目标，发送后用户消息保存 `reply_to_notification_id`；ContextAssembler 将目标提醒单独注入且从普通 Conversation 投影中去重，因此即使同一 Session 有多条提醒也不靠相邻顺序猜测。计划焦点继续提供完整状态与分层记忆。

`Notification.archived_at` 只提供可恢复的收件箱生命周期。一个逻辑提醒可以拥有站内、邮件和 Push 多条投递记录，但收件箱只展示站内权威行，每日上限也按逻辑提醒计数；打开、已读、归档和恢复会同步同组投递。`GET /notifications` 默认返回活动消息，`?archived=true` 返回归档列表；单条归档/恢复和批量归档已读均保留通知事实与对话消息。ContextAssembler 排除已归档通知，也排除已经投影到当前 Session 的通知；后者由 Conversation 区只注入一次，避免同一提醒重复占用上下文。

同一 Session 同时只允许一个根 Run。用户在运行期间发送的跟进、后台提醒回复和邮件回复都先进入耐久 `QueuedMessage`；队列保存触发来源、原始用户正文与消息元数据。当前 Run 完成或失败后由后端在同一事务边界创建下一条 `ChatMessage` 和根 Run，再由前端跟随显示；浏览器关闭不会让队列失去消费者。用户主动停止 Run 时队列仍保留并等待手动发送，系统不会在明确停止后擅自启动下一项。等待阻塞审批时普通回复进入队列，审批回答只通过专用 approval 接口恢复检查点，避免两种语义互相覆盖。

IMAP 采用持久化后确认：邮件 UID 先写入消息或队列元数据并提交数据库，随后才标记为已读；进程在两步之间失败时，下轮以 UID 去重，不会重复创建用户消息。邮件回复仍恢复原提醒的 Session 和 `reply_to_notification_id`，不会生成独立邮件对话。

## 5.1 Harness 运行事件

前端展示可审计过程，不展示模型私有思维链。每个历史或实时 Run 都在所属消息中显示可折叠摘要；展开后可查看工具与子 Agent 事件，每项工具还可继续展开有界的结构化输入/结果。右侧处理记录面板提供同一事件流的全局审计视图：

```text
run.started
assistant.status
tool.started
tool.completed
subagent.started
subagent.completed
approval.required
approval.resolved
operation.committed
notification.sent
run.budget_exceeded
run.completed
run.failed
run.cancelled
```

`assistant.delta` / `assistant.reasoning` 只作为当前进程内的瞬时流事件，不写入事件表；断线重连后，下一次累计 delta 或持久化的 `assistant.message` 会恢复可见答案。持久化事件按 Run 串行分配 sequence，转向、取消与 Runtime 并发写入不会争用同一序号。

用户可以即时停止 Run。写操作完成后，界面显示影响范围和撤销入口。

## 6. Intervention Guard

模型决策不能直接触达用户，必须经过确定性规则：

- 免打扰时间
- 单日通知上限
- 同类提醒冷却时间
- 已完成或已删除任务过滤
- 高风险计划修改必须由用户确认
- API 失败时不重复轰炸

工具运行还维护每个 Run 独立的失败熔断器：同一工具连续失败两次后，本轮不再把该工具暴露给 Hy3，避免网络或依赖故障耗尽全部工具轮次；其他能力仍可继续使用，下一次 Run 会重新尝试。

Web 工具对初始 URL 和每一次重定向都执行 SSRF 校验。localhost、IP 字面量、`.local`、RFC 私网和链路本地地址始终拒绝；在显式开启本地代理兼容时，只允许公网域名经 Clash/Mihomo 一类代理解析到 `198.18.0.0/15` 或 `2001::/32` 的 Fake-IP，直接请求这些地址仍被拒绝。搜索提供商通过统一接口选择，默认主源为 DuckDuckGo HTML、备选源为 Bing HTML，可通过环境变量关闭备选。

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

当前注册 `planning_delegate`，可一次把最多三个规划调查分给独立 `AgentRun(trigger=subagent, parent_run_id=...)`；通用 `subagent_spawn/status/join/cancel` 使用同一只读执行器。子 Run 只接收父 Run 的只读上下文快照和单一任务；工具白名单限于画像/记忆/文件/日历读取及 `web_search/web_open`，并强制拒绝保存搜索结果和全部业务写工具。通用子 Run 在安全边界保存模型消息、轮次和待执行工具；异常进入 `failed`，重启按 `checkpoint.kind=subagent` 路由到专用恢复器。子 Run 不写主 Session 消息，返回报告后由主 Agent解决冲突和提交写操作。父事件流记录 `subagent.started/completed`，侧边栏与最近 Run 查询只投影根 Run，不把子 Run 冒充新对话。

这是以只读调查为边界的通用子 Agent v1；计划共创只是它的一种调用方式。其长期约束保持为：

- 只继承最小必要上下文和工具。
- 默认不能直接修改计划或长期记忆。
- 返回结果与证据给主 Agent，由主 Agent决定后续动作。
- 产生独立 `run_id`，并在父 Run 的事件流中可见。

通用 spawn/status/join/cancel、只读工具白名单、独立轮次/工具上限和崩溃检查点已经实现。SQLite 部署使用跨 Run 的事件单写者锁并对短暂 `locked/busy` 退避重试；工具观察和事件结果有界压缩，研究轮次耗尽后额外执行一次禁用工具的最终总结。`subagent_join` 的外层工具超时会在请求等待时长上增加收尾余量，不会先于子 Run 自身超时。规划子 Run 的成功报告或失败说明同时写入 `AgentRun.output` 并投影回父事件，主 Agent 可据此解决冲突或降级完成。

应用启动时会扫描遗留的 `queued/running` Run：有 `checkpoint` 的恢复为 `queued` 并从断点续跑；没有检查点的标记为 `failed(process_interrupted)` 并追加可见事件，保留原消息、工具结果和操作记录，同时解除 Session 的假占用。心跳等无会话 Run 恢复时会按当前数据库重建上下文（不重放旧快照），指向已删除计划的待恢复 Run 直接安全收口。阻塞型审批在 `waiting_approval` 状态下持久化待批工具与参数，批准后从检查点恢复，拒绝后把拒绝结果回填给模型继续调整。
