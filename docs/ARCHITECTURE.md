# 主动 Agent 与上下文架构

> 状态说明（2026-08-19）：本文描述目标架构，并在“旧实现候选”段落记录 develop 上已有的正常路径。H1 的迁移/UTC/备份基础已完成，关闭 13 个缺陷 ID、15 个基线节点；矩阵剩余 74 个 open ID，下一门禁为 H2，M15–M20 继续冻结。Runtime 恢复、事务、Evidence、Context/Memory、Intervention、安全与移动端仍未验收；当前真实状态见 [`STATUS.md`](STATUS.md)，逐项缺陷见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)，修复顺序见 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md)。

阅读规则：本文件中的“必须 / 只 / 不会 / 权威 / 严格”等表述是后端和前端最终要共同强制的**目标契约**，不能据此推断当前实现已经满足。develop 的旧实现候选只证明正常路径可运行；H0 已用 strict xfail 证明以下关键差距：

- 事务与副作用：H2-TXN-001–009；当前 handler 分散提交，并可能跨模型、HTTP、SMTP 或子 Run await 持有写锁。
- Runtime：H3-RUN-001–011；审批拒绝、current tool、queued/no-checkpoint、二次中断、finalization、lease 和两套 child runtime 尚不耐久。
- Evidence / Competency：H1-TIME-001/002 已关闭；H4-EVID-001–008、H4-COMP-001–006、H4-SCHEMA-001/002 仍待修复。
- Context / Intervention：H5-CTX-001–012、H5-INT-001–003、H5-MAIL-001/002、H5-PRO-001–003。
- 安全与 UI：H6-*、H7-UI-001–006。当前只允许受控 loopback Demo，不能作为无认证服务器或不可信代码沙箱。

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

目标契约：V2 从这些运行事件中维护追加式 `EvidenceObservation` 账本。提交、提交验收、测验评分和带证据的任务完成写入结构化来源、评分、提示/迁移等级和幂等键；观察本身不编辑、不物理删除，修订通过后续观察的替代/失效关系表达。`study_state_get` 和计划 Context 必须读取同一个完整、确定性证据摘要。H1-TIME-001/002 已使 UTC instant 与 digest 在 SQLite 往返后稳定；旧在线投影仍会截断 500 条之后的账本，撤销后保留 active success，并重复计权（H4-EVID-001–003）。

目标契约：每条实际证据先登记可重验的不可变 `Artifact`（规范内容指纹、耐久内容、作用域和元数据），观察只保存受限引用。M14 的 `Competency` 只通过显式 key 和可审计映射关联计划、任务、资源，所有 endpoint 由后端校验 owner/plan。旧 Artifact 不覆盖完整 envelope/文件 bytes，旧 Competency unique/edge/link 可破坏计划隔离（H4-EVID-005、H4-COMP-001–004）。

### Conversation Window

目标契约：只装配当前对话需要的近期消息。较早消息压缩为当前 Session 摘要，但不会自动提升为长期记忆；coverage 只能推进到摘要器真实读取且成功提交的消息。旧压缩会遗漏长输入、把未读消息标为 covered，并在模型失败后推进 coverage（H5-CTX-002–004）。原始消息仍保留。

目标契约：每条会话拥有显式焦点，后端校验 Session/Plan scope，`currentPlan` 与 `focusPlanId` 分离，隔离不依赖 UI。旧 ContextAssembler 会把 global Session 的 discussed link 当作读取私有计划状态的权限，前端归档又可能留下 stale focus（H5-CTX-001、H7-UI-003）。

目标契约：全局 Session 创建计划后不静默改绑；新计划 Session 保存一次性冻结、可追溯的最小 handoff。旧重复 handoff 会用来源 Session 的后续消息改写既有 child 摘要（H5-CTX-005）。

Session 与 Plan 都支持可恢复归档。归档只改变生命周期和默认列表，不删除原始消息、计划结构、记忆、证据或事件；归档计划退出主动候选扫描，归档 Session 为只读。手动归档同样写入 `Operation` 审计记录。

`Session → ChatMessage → AgentRun` 同时承担持久化与 UI 恢复：`GET /agent/sessions` 返回以 Session 聚合的标题、消息数、Run 数和最近状态，`GET /agent/sessions/{session_id}/messages` 返回完整原文。前端选择历史 Session 后恢复整个消息流，并把最新 Run 的事件投影到对应用户消息之后。新 Run 先乐观加入用户消息，完成事件到达后再用数据库原文替换，避免网络时序造成重复或闪烁。首轮完成后由独立短请求生成语义标题，`PATCH /agent/sessions/{session_id}` 支持手动改名；自动命名只会替换未被用户修改的初始标题。

计划制定在 Session 内增加两层持久状态：`PlanningIntake` 保存目标、带来源的已确认事实、结构化待确认问题、充分性结论/置信度/理由；`PlanProposal` 保存完整 PlanCreate 负载、主 Agent 理由、子 Agent 报告和 pending/accepted/rejected 生命周期。普通会话 Run 不能再直接调用 `plan_create`；必须先将 Intake 标为 ready，再写提案。`POST /agent/plan-proposals/{id}/decision` 是显式提交边界，采用操作幂等地创建正式 Plan、Operation 与 SessionPlanLink。

目标契约：用户编辑消息采用非破坏式当前分支语义，保留 Revision/Run/Event/Operation 审计，同时显式失效所有派生 Memory、Summary、Snapshot 与 handoff。旧实现只失效部分 Session/Run 来源，Plan/Global 与直接 Message 派生物可继续进入 Context（H5-CTX-006/007）。

### Working Memory

当前 Agent Run 的目标、临时计划、工具结果和未完成步骤。Run 结束后只保留事件与总结，不把临时推断直接提升为长期事实。

### Memory Proposal

模型从对话和学习结果中提取的候选长期记忆。候选包含作用域、来源、置信度和过期策略，经用户确认后才进入检索。相同内容会强化既有记录；纠正通过 `supersedes_id / superseded_by_id` 保留新旧关系。归档、到期和被替代都是可审计生命周期，不通过公开 API 物理删除。

## 3. 数据库与 Markdown 快照

数据库是事实来源；Markdown 是面向模型和用户的可读快照，不作为唯一存储。

H1 已建立冻结 migration registry/history、规范 fresh/upgrade schema、全局 UTC 类型和协调维护协议。迁移发布、恢复和回滚会固定并复核路径、目录描述符/inode 与内容摘要，只从复验通过的 trusted snapshot 回退；restore 在写入前拒绝把目标数据库或安全备份根目录放在 source backup 本身或其子路径中，source backup 位于安全备份根目录下仍是正常布局。共享 lexical normalization 消除 `.`/`..` 别名但不跟随 symlink，SQLite URI 对空格、`%`、`#`、`?` 安全；legacy `_write_probe` 只接受精确的空单列旧残留，其他近似 schema 失败关闭。该协议覆盖受控 lifecycle lease，不承诺协调绕过 Runtime/maintenance 的原始 SQLite writer。

H1 定向套件为 281 passed，H0 跨阶段回归为 27 passed / 17 strict xfailed，全仓为 441 passed / 98 strict xfailed。外部安装、连续学习闭环和 7 日留存仍须在 H8 由真人记录验证。

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
4. 当前计划的证据状态摘要（观察数量、任务证据阶段、最近观察和 digest）
5. 与候选事件相关的历史事件
6. 最近一次干预及用户反应
7. 必要的近期对话

Hy3 支持长上下文，但系统仍需选择、分层和压缩。长上下文能力用于保留更多相关证据，不用于无差别塞入全部历史。

## 5. 主动决策协议

主动心跳与用户对话使用同一个 Tool Calling 循环，不维护一套独立的固定 JSON 工作流。Hy3 可以先调用 `plan_list`、`plan_get`、画像或事件工具收集证据，再自主选择：

- 不调用写工具，并在 Run 结论中记录保持安静；
- 调用通知、测验或复习工具进行干预；
- 调用可撤销的计划工具完成低风险调整；
- 创建记忆或高风险变更候选，等待用户确认。

每次模型轮次、工具开始/完成、最终结论和失败都归入同一个 `run_id`。`silent` 同样必须形成完成事件，证明 Agent 做过判断，而不是只有通知结果。

调度器只有一个全局循环。每 `AGENT_HEARTBEAT_SECONDS` 做一次轻量确定性候选扫描，而不是为每个任务创建常驻心跳：先检查到期复习、24 小时内任务，再逐个检查活动计划的最新学习证据。扫描阶段不加载聊天全文；命中候选后才用明确的 `plan_id` 启动计划级 Run，由 ContextAssembler 注入该计划结构、证据、计划记忆和必要事件。`AGENT_PROGRESS_CHECKIN_HOURS` 默认 24 小时；`AGENT_CANDIDATE_COOLDOWN_MINUTES` 默认 180 分钟，避免计划被每轮重复交给模型。只有提交、考核、证据或任务完成等学习行为会刷新“最近活动”，计划元数据维护不再伪装成学习进展。`GET /settings/proactive` 暴露下一轮时间、最近判断和最近心跳 Run，前端每 15 秒同步状态与站内通知。

目标契约：主动提醒的权威回复位置是 Session，一次逻辑 Intervention 拥有稳定 ID，所有 delivery 和回复都引用它；活动 Run queue/steer 必须耐久保存 target。旧实现依赖 run/title/body/thread 启发式，活动 Run 回复会丢 target（H5-INT-001–003、H7-UI-004）。

目标契约：收件箱归档是可恢复生命周期，一个逻辑 Intervention 的多渠道 delivery 只计数和注入一次。旧数据模型仍以 Notification delivery 为主，会重复 Context 或错误合并同文提醒（H5-INT-002/003）。

目标契约：同一 Session 同时只允许一个根 Run；queue item、ChatMessage 和新 Run 由短事务/CAS 仲裁，进程中断后仍 claimable。旧 queued/no-checkpoint 会被启动恢复误判失败，late steer 可悬空，且没有多 worker lease（H3-RUN-004/006/007）。

目标契约：IMAP 先用 `BODY.PEEK[]` 读取，UID/reply job 提交后才标 Seen，并以稳定 Intervention/Session ID 路由。旧 `(RFC822)` fetch 可在 commit 前置 Seen，归档计划回复也缺少明确只读回执（H5-MAIL-001/002）。

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

目标契约：Web 工具对初始 URL、每次重定向和实际连接 peer 执行 SSRF 校验，并限制 wire/decompressed bytes 与精确 Content-Type。旧实现未 pin/复核 peer，接受部分 non-global 地址且无响应上限（H6-WEB-001）；修复前不能把当前校验描述为完整安全边界。

学习资源采用两阶段协议：`web_search / web_open` 负责发现与正文核验，`resource_save` 才把 Agent 明确选择的课程、教程、实验、学习路径或参考资料写入计划。保存项包含平台、类型、难度、语言、核验摘要和适配理由，并生成可撤销 `Operation`；原始搜索结果不等同于课程资源。

## 7. 计划内存隔离

目标契约：每个计划只读取自己的 `Plan Memory` 和相关事件；全局画像可以被所有计划引用，私有对话不能自动泄漏。H5-CTX-001 的旧实现失败证明这项隔离尚未验收。

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

旧实现候选注册了 `planning_delegate` 与通用 `subagent_spawn/status/join/cancel`，并以只读工具白名单约束正常路径。但 planning delegate 仍是无 checkpoint 的第二套 child runtime，终态与父 completion 事件分次提交，transient error 无耐久重试，预算协议也未统一（H3-RUN-008–011）。

这是以只读调查为边界的通用子 Agent v1；计划共创只是它的一种调用方式。其长期约束保持为：

- 只继承最小必要上下文和工具。
- 默认不能直接修改计划或长期记忆。
- 返回结果与证据给主 Agent，由主 Agent决定后续动作。
- 产生独立 `run_id`，并在父 Run 的事件流中可见。

上述 spawn/status/join/cancel、白名单、轮次限制和部分 checkpoint 是正常路径候选，不构成崩溃一致性证明。H2-TXN-009 已复现主/子/心跳 SQLite 竞争，H3-RUN-009 已复现 child 终态提交后父 completion 永久缺失。

旧启动逻辑会扫描 `queued/running` Run，但它把合法 queued/no-checkpoint 直接标记 `failed(process_interrupted)`；resume 又会先清旧 checkpoint，审批 decision 未耐久保存时默认 approve。这里记录的是 known-bad baseline（H3-RUN-001/002/004），不是目标恢复机制。H3 必须改为带 phase/version/lease 的统一状态机后再更新本节。
