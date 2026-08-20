# 主动 Agent 与上下文架构

> 状态说明（2026-08-20）：本文描述目标架构，并明确 develop 上已经验收的边界。H1–H6 已完成迁移/UTC/备份、事务/幂等/outbox、耐久 Runtime/Queue/child、Evidence/Competency、Context/Memory/Intervention 与应用安全边界；累计关闭 77 个缺陷 ID，矩阵剩余 10 个 open ID，下一门禁为 H7，M15–M20 继续冻结。完整前端与发布工程仍未验收；当前真实状态见 [`STATUS.md`](STATUS.md)，逐项缺陷见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)，修复顺序见 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md)。

阅读规则：本文件中的“必须 / 只 / 不会 / 权威 / 严格”等表述是后端和前端最终要共同强制的**目标契约**，不能据此推断全部门禁已经满足。H0 建立的失败基线会在对应门禁修复后删除 xfail；当前边界为：

- 事务与副作用：H2-TXN-001–009 已关闭；统一 UoW、physical outer transaction、短 CAS、request digest 和 outbox/receipt 已验收。
- Runtime：H3-RUN-001–011 已关闭；主/子 Run 统一使用版本化 checkpoint、lease/version fence、审批事实、耐久 retry/预算和原子终态/Queue/父投影。
- Evidence / Competency：H1-TIME-001/002 与 H4-EVID-001–008、H4-COMP-001–006、H4-SCHEMA-001/002 已关闭；完整账本、追加式控制事实、Artifact snapshot、scope-aware graph 与严格工具 Schema 已验收。
- Context / Intervention：H5-CTX-001–012、H5-INT-001–003、H5-MAIL-001/002、H5-PRO-001–003 已关闭；来源图、generation fence、typed blocks、逻辑 Intervention、mail job 与 ProactiveDecision 已验收。
- 安全：H6-AUTH/CODE/TRUST/WEB/ENV/CONFIG/REDACT 均已关闭；local 默认 loopback，server API 必须认证，外部来源不能自行获得写权限，没有 sandbox Provider 时 `code_execute` 不可用。
- UI：H7-UI-001–006 仍 open；浏览器登录体验、移动导航和发布资产尚未验收。

## 1. 总体架构

```text
用户消息 ─┐
后台心跳 ─┼─▶ AgentRuntime ─▶ ContextAssembler ─▶ Hy3
任务事件 ─┘        ▲                                  │
                  └──── 观察结果 ◀──── ToolRegistry ◀─┘
                                           │ effect_kind
                         ┌─────────────────┴──────────────────┐
                         │                                    │
                    短 CAS / UoW                         durable outbox
                         │                                    │
             SQLite / Operation / RunEvent          独立 dispatcher → 外部渠道
                         │                                    │
                         └──────── receipt / reconciliation ──┘
                                           │ SSE
                                           ▼
                                      Harness 工作台
```

后台 Worker 持续运行；Hy3 按事件调用。用户消息和主动事件共享同一个可观察、可停止、有轮次预算的工具循环，确定性 Guard 约束权限、触达频率和安全边界。H2 的 effect coordinator 进一步根据 `pure_read / database_write / external_read / external_write` 选择执行协议：数据库写在一个 UoW 完成，外部等待不持有 writer，外部写先提交 durable intent 再由 dispatcher 执行。

用户对话和后台心跳都进入同一个 `AgentRuntime`，只改变触发源：

- `user_message`：用户明确提出目标或指令。
- `heartbeat`：调度器周期性检查状态。
- `task_event`：任务完成、延期或提交发生变化。
- `review_due`：到达计划的复习时间。
- `email_reply`：IMAP 轮询从带回复令牌的邮件生成。

每次运行都有唯一 `run_id`，前端通过 SSE 订阅运行事件。进程内任务注册表以 `run_id` 跟踪主 Run、心跳与子 Run，stable wake key 在暂停 task 收尾期间只交接一个 successor；数据库状态机中的 `status/phase/state_version/lease/checkpoint/approval` 是跨重启权威状态。

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

H4 已把 V2 Evidence 固定为追加式 fact ledger。提交、验收、测验评分和带证据的任务完成写入结构化来源、评分、提示/迁移等级和幂等键；修改、撤销与重做通过 amendment/invalidation/reinstatement 事实表达，原观察由数据库 trigger 禁止更新和删除。`study_state_get`、计划 Context、tool、HTTP API 与 CLI 读取同一个无截断投影，增量 watermark 持续和 full oracle 对比；0/1/500/501/10,000、关闭重开和备份恢复后的 digest 已一致。

每条可计权证据先登记不可变 `Artifact` canonical envelope、耐久 bytes snapshot、size/hash、作用域和元数据，观察通过受约束关联表引用 Artifact 与一个或多个 Competency。task 的 `assesses` 映射在 Evidence 写入的同一 UoW 快照；SQL 在分页前过滤技能。Competency global/plan key 由 partial unique 区分，edge/link 的两端 owner/plan 从数据库解析；真实 graph mutation 原子推进 owner revision，Operation dependency 与 `RESTRICT` FK 阻止撤销静默级联。

### Conversation Window

当前实现只装配当前对话需要的近期消息。较早消息通过 `SessionCompressionState` 短 claim 完整分块读取，模型链成功且 source version/generation CAS 仍成立时才生成 `SessionSummary` 并推进连续 coverage；失败、并发冲突或编辑不会推进 cursor。原始消息始终保留，摘要不会自动提升为长期记忆。

每条会话拥有显式焦点，后端校验 Session/Plan scope，`currentPlan` 与 `focusPlanId` 分离，隔离不依赖 UI。ContextAssembler 只把 SessionPlanLink 用于关系和排序；global Session 最多读取紧凑计划索引，不会因 discussed/created/focused link 读取计划私有事实。前端归档后的 stale focus 仍由 H7-UI-003 处理。

全局 Session 创建计划后不静默改绑；新计划 Session 保存一次性冻结、版本化且可追溯的 `SessionHandoff`。handoff 记录来源事实、内容 hash 与 Context generation，精确重试复用同一事实，来源 Session 后续消息不能改写既有 child。

Session 与 Plan 都支持可恢复归档。归档只改变生命周期和默认列表，不删除原始消息、计划结构、记忆、证据或事件；归档计划退出主动候选扫描，归档 Session 为只读。手动归档同样写入 `Operation` 审计记录。

`Session → ChatMessage → AgentRun` 同时承担持久化与 UI 恢复：`GET /agent/sessions` 返回以 Session 聚合的标题、消息数、Run 数和最近状态，`GET /agent/sessions/{session_id}/messages` 返回完整原文。前端选择历史 Session 后恢复整个消息流，并把最新 Run 的事件投影到对应用户消息之后。新 Run 先乐观加入用户消息，完成事件到达后再用数据库原文替换，避免网络时序造成重复或闪烁。首轮完成后由独立短请求生成语义标题，`PATCH /agent/sessions/{session_id}` 支持手动改名；自动命名只会替换未被用户修改的初始标题。

计划制定在 Session 内增加两层持久状态：`PlanningIntake` 保存目标、带来源的已确认事实、结构化待确认问题、充分性结论/置信度/理由；`PlanProposal` 保存完整 PlanCreate 负载、主 Agent 理由、子 Agent 报告和 pending/accepted/rejected 生命周期。普通会话 Run 不能再直接调用 `plan_create`；必须先将 Intake 标为 ready，再写提案。`POST /agent/plan-proposals/{id}/decision` 是显式提交边界，采用操作幂等地创建正式 Plan、Operation 与 SessionPlanLink。

用户编辑消息采用非破坏式当前分支语义，保留带 version/content hash 的 Revision、Run/Event/Operation 审计，并沿 verified provenance closure 失效所有派生 Memory、Summary、Snapshot 与 handoff。无法证明来源的 legacy 事实只做保守失效，不在编辑时猜测或提升来源图；同一 edit action 与唯一 rerun 在 SIGKILL 后幂等收敛。

### Working Memory

当前 Agent Run 的目标、临时计划、工具结果和未完成步骤。Run 结束后只保留事件与总结，不把临时推断直接提升为长期事实。

### Memory Proposal

模型从对话和学习结果中提取候选长期记忆。候选包含作用域、typed provenance、置信度和过期策略，经用户确认后才进入检索。相同内容在原 Memory 的下一 lifecycle version 增加独立 source edge；纠正通过 `supersedes_id / superseded_by_id` 保留新旧关系。归档、恢复、到期、失效和替代均写 append-only lifecycle event，不通过公开 API 物理删除。状态、版本、pointer、digest 与事件在同一 CAS 中推进；未经来源验证的旧行不会参与检索、强化或维护。

## 3. 数据库与 Markdown 快照

数据库是事实来源；Markdown 是面向模型和用户的可读快照，不作为唯一存储。

H1 已建立冻结 migration registry/history、规范 fresh/upgrade schema、全局 UTC 类型和协调维护协议。迁移发布、恢复和回滚会固定并复核路径、目录描述符/inode 与内容摘要，只从复验通过的 trusted snapshot 回退；restore 在写入前拒绝把目标数据库或安全备份根目录放在 source backup 本身或其子路径中，source backup 位于安全备份根目录下仍是正常布局。共享 lexical normalization 消除 `.`/`..` 别名但不跟随 symlink，SQLite URI 对空格、`%`、`#`、`?` 安全；legacy `_write_probe` 只接受精确的空单列旧残留，其他近似 schema 失败关闭。该协议覆盖受控 lifecycle lease，不承诺协调绕过 Runtime/maintenance 的原始 SQLite writer。

H2 在此基础上追加 schema revision 2：ToolInvocation 保存 canonical args、request digest、effect kind 与 fenced claim；OutboxAction/Receipt 保存外部 intent、destination、claim、receipt 和不确定状态。H1 遗留 running invocation 或无 receipt 的外部 queued notification 无法证明是否执行，因此迁移时 fail closed 到 `needs_reconciliation`，不会从旧 args hash 猜测新身份。

H3 追加 schema revision 3：AgentRun 保存受约束的 phase/state version、checkpoint schema version、lease、retry deadline/reason 和审批引用；RunApproval/Steer/Queue 增加 shape/CAS/position 约束与 dequeue index。fresh 与 frozen H2 upgrade 的规范 schema 完全等价；legacy current-tool 与审批只在 request identity 可证明时恢复，否则进入 `needs_reconciliation`。session/stateless Queue position 由 partial unique index 强制，运行时使用两阶段临时位置完成无冲突重排。

H4 追加 schema revision 4：Evidence、Artifact、关联和 Competency graph mutation 组成 append-only 事实层；full/incremental projection 共享 watermark 与 digest。

H5 追加 schema revision 5：`ContextState` 为所有 Context 相关语义变化提供 owner generation fence；`ProvenanceNode/Edge` 连接消息版本、摘要、handoff、Memory 与 Snapshot block；`SessionCompressionState` 协调完整分块和连续 coverage；`ProactiveDecision`、`Intervention` 与 `InboundMailJob` 分别保存主动决策、逻辑提醒和邮件入口身份。不可证明的 revision 4 来源保留为 `legacy_unverified`，迁移不按文本、时间或 Run 猜测关联。

应用事务统一由 `app.db.uow` 协调。SQLite 写路径在创建嵌套 savepoint 前显式建立 physical outer transaction，避免 release 最外层 savepoint 时提前提交；service/tool handler 默认只 flush，API/Runtime coordinator 提交完整原子集。短事务退避只包住可安全重放的 claim/CAS、事件和 receipt callback，不重跑隐藏任意业务工作的 ORM session。

H3 定向为 40 passed，前端状态契约为 9 passed；真实 Hy3 临时库已完成双 SIGKILL 恢复。完整验证结果统一见 [`STATUS.md`](STATUS.md)。外部安装、连续学习闭环和 7 日留存仍须在 H8 由真人记录验证。

当前生成：

```text
data/context/global.md
data/context/plans/{plan_id}.md
data/context/runs/{run_id}.md
```

`global.md` 和 `plans/{plan_id}.md` 是最新可读投影，只保存稳定画像、计划与记忆，不混入某个 Session 的 Conversation；`runs/{run_id}.md` 是该轮精确模型输入的可读副本。数据库中的 `ContextSnapshot` 内容与 normalized blocks 是每个 Run 的 append-only 审计事实，同时具有 `building/valid/invalid/legacy_unverified` 生命周期；编辑不会改写旧内容，而是显式使旧快照失效。Snapshot 保存 retained/dropped 分区、来源版本/digest、reason 与 PromptEnvelope budget。`GET /agent/runs/{run_id}/context` 与消息内上下文检查器用于复现本次模型输入。

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

每个 proactive Run 在模型前冻结候选 key/kind/payload、Evidence watermark 与 projection digest，终态写入 `ProactiveDecision`。只有 `success_wait/success_intervention` 消耗长期冷却；quiet hours 保存精确 `next_eligible_at`，Guard/model/runtime failure 使用短退避。晚写入的旧 Evidence 与已失效事实不会伪装成近期学习活动。

主动提醒的权威回复位置是 Session。一次逻辑 Intervention 拥有稳定 ID、canonical assistant message 与 reply token，所有 delivery 和回复都引用它；活动 Run 的 Queue、Run 和 ChatMessage 耐久保存 target 与 execution mode。H5 已关闭后端身份与恢复协议；前端显式保留 target 的门禁仍由 H7-UI-004 跟踪。

收件箱归档是可恢复生命周期；一个逻辑 Intervention 的多渠道 Notification 只计数和注入一次。同文提醒保持不同 Intervention 身份，打开任一 delivery 会更新同一逻辑组，不再按 run/title/body/thread 猜测合并。

同一 Session/计划 scope 同时只允许一个非终态根 Run，包括 `needs_reconciliation`。Queue item、ChatMessage、新 Run、终态和 successor 由短事务/CAS 仲裁；queued/no-checkpoint 是可 claim 的未开始意图，late steer 必须消费或原子排队，lease heartbeat 与 token/version fence 阻止双执行者提交。

IMAP 用 `BODY.PEEK[]` 读取；UIDVALIDITY/UID 和解析后的 reply job 先耐久提交，再由独立 ACK claim 标 Seen。重启和双 poller 复用同一 job，不重复 Run。归档计划回复以 read-only execution mode 进入原 Intervention/Session，并生成明确只读答复或失败回执。

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

Web 工具对初始 URL 和每次重定向先解析全部地址并拒绝任一 non-global 结果，再用选定数值 IP 发包、保留逻辑 Host/TLS SNI 并复核实际连接 peer。响应按 raw stream 独立限制 wire/decoded bytes，只接受 identity/gzip、总 deadline 和精确 MIME allowlist；搜索 Provider 在解析前进一步要求精确 HTML MIME。Web/File 返回会写入耐久 `external_untrusted` marker，后续写操作不能把模型看到的内容当作授权。

部署边界由 `DeploymentPolicy + DeploymentBoundaryMiddleware` 强制。local 模式的启动参数与 ASGI server scope 都必须是 loopback；server 模式需要强 bearer token、精确 HTTPS public origin 和一致 CORS。全部 `/api/v1` 需要 bearer 或签名会话，Cookie 写请求还需 Origin 与双提交 CSRF。高风险能力是否可用由 `execution_policy` 决定，而不是由 System Prompt 或前端隐藏；当前没有 sandbox Provider，因此 `code_execute` 在模型、registry 和 outbox 三层关闭。统一 redaction 在 RunEvent、checkpoint/审批投影、模型观察、Context/磁盘投影、诊断错误和日志边界执行。

学习资源采用两阶段协议：`web_search / web_open` 负责发现与正文核验，`resource_save` 才把 Agent 明确选择的课程、教程、实验、学习路径或参考资料写入计划。保存项包含平台、类型、难度、语言、核验摘要和适配理由，并生成可撤销 `Operation`；原始搜索结果不等同于课程资源。

## 7. 计划内存隔离

每个计划只读取自己的 `Plan Memory` 和相关事件；全局画像可以被所有计划引用，私有对话不会自动泄漏。作用域由数据库实体与 provenance edge 校验，Session relation 本身不是授权，固定隔离问题集已通过 H5。

当跨计划信息确实有价值时，Hy3 只能提出一条“提升为全局记忆”的候选，用户确认后写入 Global Learner Profile。

## 8. 第一阶段模块

后端计划拆分为：

```text
backend/app/
├── api/             # HTTP and SSE endpoints
├── context/         # assembly, snapshots and consolidation
├── core/            # configuration and scheduling
├── db/              # engine, migration and Unit of Work
├── models/          # persistent entities
├── notifications/   # inbox/browser/email adapters
├── outbox.py        # external-write intent, dispatch, receipt and reconciliation
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

当前实现注册了 `planning_delegate` 与通用 `subagent_spawn/status/join/cancel`，并在执行层再次收窄为只读工具白名单。planning child 与通用 child 使用同一 claim/lease/checkpoint/retry/finalize 状态机；model/tool/network/elapsed/token/cost 预算与主 Run 共享语义，finalizing checkpoint 冻结报告，child 终态与父 `subagent.completed` 同事务投影或幂等修复。

这是以只读调查为边界的通用子 Agent v1；计划共创只是它的一种调用方式。其长期约束保持为：

- 只继承最小必要上下文和工具。
- 默认不能直接修改计划或长期记忆。
- 返回结果与证据给主 Agent，由主 Agent决定后续动作。
- 产生独立 `run_id`，并在父 Run 的事件流中可见。

H3 的崩溃一致性证据包括真实 SIGKILL、双进程 claim、SQLite busy、连续三次恢复、多工具副作用 replay、父子终态修复与固定种子 100 轮 baseline/oracle 对照。父终态取消会覆盖并等待所有非终态 child；scope-invalid child 在一次启动协调中完成终态清理和父投影。

启动协调扫描全部非终态 Run：queued/no-checkpoint 重新 claim；有可信 checkpoint 的 running/retry Run 重新排队；未决审批保持等待；缺失 running checkpoint 或无法证明的 legacy 身份进入 `needs_reconciliation`，不会默认批准或猜测外部结果。SQLite lease 是共享单库上的执行 fence，不是多节点调度或分布式数据库协议。
