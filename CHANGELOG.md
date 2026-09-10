# Changelog

## 报告读图与Demo呈现 · 2026-09-11

- 加强应用实验、方法验证、工程测试的用途说明，补充质量排序和评分稳定性图的阅读方法，以及跨夜免打扰的授权判断分析。
- 质量排序改为分情境折线图，稳定性采用对数刻度并单列零波动记录，实验分数保持不变。
- 新增书页与学习路径Logo，更新项目入口和浏览器图标；108秒无声Demo采用统一画框、完整产品视口和更长的案例展示。

## 第三阶段交付文档 · 2026-09-10

- 补充每维0—2级尺度及换算示例，列明规则检查范围；区分产品运行与评分方法验证，列全8个情境并说明各组实验目的、材料来源与评分次数。报告中的文件链接显示下划线和可核对的文件名。
- 按产品定位、技术架构、学习流程与实际案例重写产品介绍，说明基于持续学习状态的主动支持。
- 明确关键决策片段的组成、七维判据、规则与Hy3评审的完整流程，以及双人人工标注的验证用途。
- 同步方法说明、报告HTML/PDF与Demo，重绘评估流程和实验安排图，保留116项用例索引及原始实验结果。

## Unreleased · H8 engineering freeze

### First run and release engineering

- Added a three-step TokenHub/Hy3 onboarding flow for fresh, keyless, zero-Session installations. Connection checks use a minimal request, never persist model output or echo credentials, and successful settings apply without restarting before the first Run.
- Added a worktree-aware deterministic release builder with a per-file manifest and SHA-256 sidecar. Release archives contain built frontend assets and exclude local env files, databases, Context/workspace data, Node dependencies, caches and logs.
- Release startup never invokes npm; missing frontend assets fail with `missing_release_asset`. Packaged setup installs only Python runtime dependencies when the built frontend is present.
- Split runtime and development requirements so packaged setup does not install pytest, Ruff or dependency-audit tooling.
- Added eight mandatory executable release gates for static checks, Python dependency audit, historical migration, Evidence, Context isolation, real-browser matrices, secret scanning and frontend tests/build/audit.
- Closed all four H8 defect IDs. External user installation, continuous-use and seven-day retention validation remain explicitly unperformed; M15–M20 and new features are frozen pending a separate third-stage competition plan.

## Unreleased · 2.0 M13

### 学习证据账本第一条纵向切片

- 新增追加式 `EvidenceObservation` 事实表：记录来源、计划/任务/Run/Session、评分、提示/迁移等级、Rubric 快照、因果链和幂等键。
- 提交、提交验收、测验评分和带证据的任务完成会幂等双写；重复重试不会覆盖或复制原始观察。
- `study_state_get` 与计划 Context 使用确定性证据摘要，返回稳定 digest、任务级证据阶段和明确的保守性说明。
- 新增 `Artifact` 不可变来源表，提交、测验回答和任务证据都保留 `artifact_id`、内容哈希和可审计来源引用；审计会校验引用存在、URI 和哈希。
- 新增 `scripts/rebuild-evidence.py`，可从 SQLite 重建投影、审计账本、保守回填明确的 v1 证据并原子写入派生 JSON，不修改事实层。
- 新增 41 个网络无关的固定基线场景，支持重复运行并比较 digest；全量 pytest 当前为 126 项。

## Unreleased · 2.0 M14 foundation

### 显式技能图基础

- 新增 `Competency`、技能关系、计划/任务/资源映射表；技能 key 不会根据标题相似度静默合并。
- 新增 `competency_create/link/edge/get/graph` 与 `evidence_list` 工具，关系写入受计划作用域、审批、幂等和 prerequisite/part_of 环检测约束。

## 1.1.1 (2026-08-13)

### v2 前产品状态审计

- 把排队消息的续跑权收回后端：队列项保存来源、用户正文和元数据，当前 Run 完成或失败后在同一 Session 中持久化下一条用户消息并启动下一 Run；邮件回复也走同一耐久队列，不再依赖某个浏览器标签页存活。用户主动停止时队列保留，等待检查或手动发送，不擅自继续。
- 统一 Session、Plan、Run 与 Proposal 生命周期：存在活动/待审批 Run 或未处理队列时禁止归档；归档计划对任务、提交、测验、复习、资源和日历写入只读；已归档 Session 不能采用提案；正式计划必须有目标、产出、阶段和任务；完成计划必须满足核心任务证据与非核心任务终态。
- 主动检查只把真实学习证据视为用户活动，候选扫描加入计划级冷却；站内/邮件投递按同一逻辑提醒计数并同步已读/归档，提醒仍以连续 Session 消息作为权威回复位置。
- 提交与测验结果改为不可覆盖的学习事实；复习新增完成、延后和取消工具；可撤销操作保留 `operation.undone` 审计事实，不再删除历史考核事件。
- 全局上下文只注入紧凑计划索引；精确 Run Markdown 快照与全局/计划最新投影分离，后者不再混入 Session 对话。消息编辑会重建摘要覆盖范围，避免旧摘要继续影响新分支。
- 修复子 Agent `join` 外层工具超时短于自身等待时间、崩溃后不确定写操作可能重放，以及规划子 Agent SQLite 会话竞争等恢复边界。
- 设置页不再把脱敏占位符写回 `.env`；主动检查支持显式暂停。移动端底部导航新增“对话”，避免从计划、收件箱或设置页进入后失去连续 Session 入口。
- 自动化测试增至 117 项；生产构建与 23 个真实浏览器状态回归通过。

## 1.1.0 (2026-08-10)

### 可解释的 Context 与 Memory 生命周期

- 长期记忆新增去重强化、检索访问计数、最近使用时间、可恢复归档，以及“旧认识 → 新认识”的纠正/替代链；API 不再物理删除用户记忆。
- 检索访问计数与事实更新时间分离，读取记忆不会错误延长短期/情节记忆的生命周期或抬高时间相关性。
- Session 长上下文压缩新增不可变 `SessionSummary` 版本，记录覆盖到的消息、来源消息 ID 与模型/回退生成方式；原始消息继续完整保留。
- 每个 Run 的 `context.built` 事件携带实际命中的记忆和混合检索分解；对话内展开后可查看不可变上下文快照、来源构成、估算 Token 与真正送入模型的 Markdown。
- 记忆页新增当前/历史生命周期视图、纠正提案、确认、归档与恢复操作，并展示每条认识的作用域、来源、置信度和使用痕迹。
- SQLite 增量迁移覆盖记忆生命周期列与 `session_summaries` 表；旧个人安装无需重建数据库。

### 发布硬化

- 修复前端请求客户端缺少 `PUT` 方法导致模型、邮件和主动策略设置无法保存的问题。
- 修复多轮 Run 完成时 `budget_usage.elapsed_ms` 停留在上一轮检查点、低估真实总耗时的问题。
- 自动化测试增至 101 项；生产构建、生产依赖审计与多尺寸真实浏览器回归通过。

## 1.0.2 (2026-08-10)

### 主动提醒与连续对话闭环

- 站内提醒不再是脱离聊天的孤立记录：后台 Run 发出提醒前会复用同计划最近的活动 Session，没有时只建立一个稳定的“学习跟进”Session；提醒正文作为带来源元数据的 Agent 消息写入对话，收件箱仅作为聚合、已读和归档入口。
- 收件箱卡片、页面内新提醒与 Service Worker 通知均可直接打开原对话并定位提醒；输入区保留显式回复目标，用户消息持久化 `reply_to_notification_id`。历史无 `session_id` 的通知首次打开时自动补建关联且幂等，不重复生成消息。
- 用户在该对话回复时同时获得提醒正文、触发标题、完整计划状态、计划/Session 记忆和近期学习证据；ContextAssembler 排除同 Session 已投影通知，避免重复注入。邮件回复也先恢复同一提醒线程，再追加用户消息。
- 全局心跳保持轻量候选扫描，候选命中后以 `plan_id` 启动计划级 Run；无进展扫描会遍历活动计划，不再因第一份计划最近有活动而漏掉其他停滞计划。
- 验收：完整 pytest、前端生产构建与依赖审计通过；真实历史提醒完成“收件箱 → 对话定位”修复，浏览器覆盖 375/768/1280/1440/2560，无横向溢出或按钮裁切。

## 1.0.1 (2026-08-10)

### 子 Agent 稳定性与学习场景文案

- SQLite 下的 RunEvent 写入改为跨 Run 单写者串行，并对 `locked/busy` 做有界退避重试；多个规划子 Agent 并行搜索时不再因一次数据库写冲突直接进入 `OperationalError`。
- 子 Agent 工具结果进入模型前做体积压缩，事件中保留有界可审计预览；独立工具预算阻止单个调查无限搜索。若调查轮次全部用于工具调用，Runtime 额外保留一次禁用工具的总结轮，确保成功子 Run 一定形成报告。
- `planning_delegate` 现在把成功报告和失败说明写入 `AgentRun.output`。子 Agent 标签加入明确展开箭头、加载/失败/重试状态；展开后可查看任务、每项搜索/网页读取、结构化输入结果和最终结论，历史失败 Run 也能复盘已有工作。
- 侧栏和输入区改用学习产品语义：`工作/已安排/记忆` 调整为产品名、`收件箱/学习记忆`，主动入口明确为“检查学习进度”，输入框和上下文范围不再沿用 Codex 的通用工作区命名；移除没有菜单或点击行为的伪下拉、伪按钮。
- 验收：94 项 pytest、生产构建和依赖审计通过；真实 Hy3 子 Agent 完成 `plan_list → 最终报告`；23 个浏览器状态覆盖子 Agent 展开及 375/768/1280/1440/2560，无溢出、裁切或过小正文。

## 1.0.0 (2026-08-10)

### Codex 式工作台协议与可恢复 Harness

- 对照本机当前 Codex 桌面端的实际渲染资源重构会话主轴：48rem Thread、24px composer overhang、16px 消息节奏、轻量 Workspace Header 与整栏滚动侧栏；移除重复欢迎面板、头像式机器人消息和 composer 上方重复状态卡。
- Agent Run 改为 `已处理/处理中` disclosure 与低对比 Run rail；整轮记录和每个工具操作都可展开，结构化输入/结果、失败、预算、审批和子 Agent 报告留在原消息位置，历史 Run 切换后按需恢复事件。
- 计划与计划提案改为 Writing Block 式 Artifact：文档工具栏、完整标题与阶段预览、渐隐裁切、圆形展开按钮、复制/打开/采用操作；提问卡提交保留为紧凑交互回执。
- 输入区统一为一个大 composer，保留文件提交、全局/计划焦点、上下文占用、运行中转向/排队/打断与停止；新增可用的侧栏搜索和会话操作菜单。
- 工具事件新增有界参数快照；工具契约公开输入/输出 Schema、`idempotent` 和 `blocking`。写工具幂等键加入 provider `tool_call_id`，同参数的不同意图不再被错误去重。
- 主 Run 检查点补齐 ContextSnapshot 版本；通用子 Agent 增加逐轮消息/待调用工具检查点、异常终态、父 Run 失败投影和专用重启恢复器，避免子 Run 永久卡在 `queued/running` 或被主 Runtime 错误恢复。
- 后台修改计划目标/归档状态现在产生真实阻塞审批；批准、拒绝或回答后从同一 Run 检查点继续。
- 删除不再使用的旧 Agent/进度/子 Agent 面板组件和约 9KB 遗留 CSS，避免并行设计体系继续累积。
- 验收：91 项 pytest 通过；前端生产构建与 `npm audit --omit=dev` 通过；真实 Hy3 完成 `plan_list → profile_get` 两轮工具冒烟；375/768/1280/1440/2560 浏览器回归无溢出、裁切或过小正文。

## 0.8.1 (2026-08-09)

### 会话运行硬化与视觉复查

- 停止操作现在会按 `run_id` 取消实际的模型/子 Agent 协程，并立即持久化 `cancelled`；子 Agent 取消结果同步回主 Run，不再让活动面板残留。
- 同一 Run 的并发事件写入按 Run 串行分配 sequence，避免转向、取消与 Runtime 同时发事件时触发唯一键冲突；SSE 瞬时断线交给浏览器自动重连并按 sequence 去重。
- 修复同一 Run 的转向消息重复渲染完整 Agent 回答、完成后 `currentRun` 未同步导致计划卡缺失、全局排队 Run 新建 Session 后前端不跟随、审批恢复丢失提问/提案卡快照等状态问题。
- 队列增加 Session/计划焦点与归档状态校验；运行期间不再展示必然返回 409 的“立即发送”按钮，Tab 只在确实排队时拦截，普通键盘导航保持可用。
- 子 Agent 详情改为读取真实 Run，详情输入区明确只读；预算超限与子 Agent 取消事件纳入实时订阅。
- 完成 375/768/1280/1440/2560 真实浏览器复查，统一抬升过小字号、扩展 2K 工作区尺度、修复设置表单栅格并补 `prefers-reduced-motion`，未发现横向溢出或裁切。
- 将 Vite 间接依赖 `nanoid` 升至 3.3.18，发布依赖审计恢复为 0 vulnerabilities。

## 0.8.0 (2026-08-04)

### Codex 1:1 对话体验

- **流式渲染**：模型调用改为 `stream=True`，SSE 实时推送 `assistant.delta` 与 `assistant.reasoning`；前端逐 token 追加文本、增量渲染 Markdown、显示闪烁光标与“正在思考”状态，运行中发送按钮变为停止按钮。
- **队列 v2**：排队消息持久化为多条（`queued_messages`），显示在 composer 上方，支持编辑、上移/下移、删除、立即发送；当前运行结束后自动逐条发送。Enter 按默认交互行为（转向/排队），Tab 直接排队，设置页可配置默认行为。
- **中途转向（Steer）**：运行中发送的消息注入当前 Run 的模型上下文（`run_steer_messages` + 检查点），Agent 不停止、下一轮立即采纳；转向消息作为用户消息实时出现在当前对话内。
- **子 Agent 面板**：活跃子 Agent 显示在 composer 上方，可展开查看角色/目标、停止单个或全部、打开子线程。
- **进度行**：composer 上方显示当前目标、运行状态（运行中/等待确认/空闲）与下次主动检查时间，可一键暂停/恢复后台检查。
- **审批回答**：审批卡支持输入回答并继续，回答作为工具结果回填给模型（`approval="answered"`），Agent 据此调整。
- **会话状态感知**：侧栏会话显示运行中/等待确认/待处理消息状态点。
- **稳定性修复**：SQLite 切 WAL + busy_timeout，SSE 事件改为进程内队列订阅（修复运行中连接被 `wait_for` 超时杀死、事件流断线导致前端卡“运行中”的问题）；事件流对瞬时数据库锁容错并按 sequence 去重。

## 0.7.6 (2026-08-04)

### 卡片归属消息流（Codex 式渲染）

- 提问卡与提案卡不再作为独立区域渲染在消息流末尾：运行时把卡片快照写入产生它的 assistant 消息 metadata，前端只在对应 AI 消息内部渲染。
- 回答提交后，旧提问卡保留在原 AI 消息内并保持可点击展开（只读快照）；回答摘要作为独立用户消息排在下方；后续用户消息始终排在其下方，不再出现“旧卡片漂到新回复下面”。
- 仍未回答的提问卡保持可交互；被后续运行取代后自动转为只读快照。
- 运行中通过 SSE 同步 planning 状态，提问/提案卡在运行结束前即可出现在当前 AI 消息内。
- “计划澄清已提交”改为用户侧可展开摘要卡片，不再渲染在 AI 消息区域。

## 0.7.5 (2026-08-03)

### 计划卡片与消息流绑定

- `AgentRun.created_plan_id` 持久化：计划创建（工具路径与提案采用路径）会把计划绑定到产生它的 Run。
- 前端按 Run 定位计划卡：卡片渲染在创建它的那条 Agent 消息内，之后发送的新消息排在卡片下面；刷新/切换会话后历史消息里的卡片仍然保留。
- 已采纳的提案面板不再重复显示在对话末尾（由内联计划卡承担）。

## 0.7.4 (2026-08-03)

### 运行健壮性

- 启动时校验 SQLite 可写，数据库异常时立即给出明确报错，不再运行到一半才失败。
- `start.sh` 检测到 8000 端口已有实例时拒绝启动，避免两个后端进程共享同一数据库造成数据异常。
- Run 失败处理改用独立数据库会话：主会话因写入失败处于回滚状态时，不再二次崩溃（`PendingRollbackError`），失败状态与 `run.failed` 事件仍尽力落库。
- `seed-fixture.sh` 恢复数据库后强制可写（644），避免从备份恢复出只读文件。

## 0.7.3 (2026-08-03)

### 计划卡片渲染

- 对话中计划建立后以内联计划卡渲染（标题、进度、阶段/任务、截止时间与“打开计划/在计划中继续”），随对话滚动，不再使用单独的居中小卡片。
- 计划详情页从“内层固定滚动”改为整页自然滚动，工具栏吸顶、输入栏固定在视口底部，大屏下不再出现“卡片固定在屏幕中间”的观感。

## 0.7.2 (2026-08-03)

### 后台上下文防陈旧

- 心跳等无会话 Run 从检查点恢复时，不再重放旧快照：按当前数据库重建上下文，并发出带 `refreshed_on_resume` 标记的 `context.built` 事件。
- 应用重启时，指向已不存在计划的待恢复 Run 直接安全收口，不再续跑。
- 通知发送前校验计划仍存在且未归档，避免模型引用已删除计划时仍产生提醒。
- 计划被删除后，其计划级记忆在 `memory_maintain` 中自动归档，防止旧计划记忆继续进入上下文。

## 0.7.1 (2026-08-03)

### 数据可见性与重置健壮性

- `/settings` 返回实际数据库文件路径与当前数据量（计划/会话/站内消息/确认记忆），设置页新增“数据状态”说明，方便确认服务正在读取哪个库。
- `demo-data.sh reset` 现在同时清理遗留路径（根目录与 `backend/` 下的 `learning_companion.db` 及 WAL/SHM 文件），避免“数据已清空但旧进程仍读旧库”造成滞后提醒。
- 清空数据前请先停止服务；重置后可在设置页核对数据量为 0。

## 0.7.0 (2026-08-03)

### 对话体验与上下文可视化

- Codex 式可展开记录：计划澄清问答提交后可点击展开查看回答；运行工作记录内展示“本次使用记忆”与子 Agent 活动（角色、状态、工具、结论）。
- 输入框对标基础 Chatbot：运行中显示停止按钮；运行中发送可选择“排队等待”或“打断当前运行并发送”；排队提示可取消。
- 复制增强：代码块一键复制，助手回答可整体复制。
- 上下文占用可视化：新增 `MODEL_CONTEXT_WINDOW` 配置并在设置接口暴露；输入栏与运行轨迹显示当前上下文占用（k / 模型窗口）。
- 记忆亮点：`context.built` 事件携带本次使用记忆 ID；对话内展示记忆引用，首页新增记忆概览，记忆页支持搜索与作用域/状态计数。

## 0.6.0 (2026-08-03)

### 稳定性与体验

- Web 搜索新增 Bing HTML 备选源与自动降级：主源失败、超时或返回空结果时切换，结果带 `fallback_used` 标记；`WEB_SEARCH_FALLBACK_PROVIDER` 可设为 `none` 关闭。
- 邮件渠道纳入每日上限与冷却统计；邮件发送失败的错误详情完整回填给模型。
- 连续天数真实计算：以 `ActivityDay` 为准，任务完成/测验通过事件驱动 `streak_days` 刷新。
- 规则成就引擎：首次建计划、首次完成任务、首次测验通过、3/7 天连续、100/500 XP 共 7 条规则，幂等解锁并展示在首页成就墙。
- 设置页冷却时间预填；浏览器回归脚本增加成就墙检查。
- 文档事实修正：M6 版本标签/CHANGELOG 标记完成，明确 `main` 存档 / `develop` 开发线分支约定。

## 0.5.0 (2026-08-03)

### 硬化队列（M7）

- 混合语义记忆检索：纯 Python BM25 + 本地 SimHash 向量，RRF 融合并输出可解释的 `score_breakdown`；无向量/无检索词时回退关键词排序。
- 可恢复 Run 状态机：工具轮次检查点、阻塞审批的 `waiting_approval` 暂停—批准—恢复、拒绝结果回填模型、启动时自动续跑有检查点的 Run。
- 写工具幂等：`ToolInvocation` 持久化 `idempotency_key`，重复调用返回原结果并带 `replayed` 标记。
- Run 预算：模型调用、Token、工具调用、网络请求、耗时与估算费用记录在 `budget_usage`，超限产生 `run.budget_exceeded` 事件并安全停止。
- Service Worker 通知：通知经 `showNotification` 展示并路由回收件箱；可选 VAPID Web Push，失效订阅自动清理。
- 通用受限子 Agent：`subagent_spawn/status/join/cancel`，v1 强制只读白名单、独立轮次上限与结构化报告。
- 设置页：模型连接、SMTP/IMAP 凭据与测试、通知策略（免打扰/每日上限/冷却时间）；凭据只写本地 `.env`（0600），API 不回传密码。
- CI：pytest + 前端构建 + `npm audit --omit=dev`；工具契约从 37 个扩展到 41 个（输入/输出 Schema 双向校验）。

### 文档与工程

- STATUS/ROADMAP/HARNESS/TOOL_PROTOCOL/EMAIL/PRODUCT/README 同步更新。
- 新增 `scripts/seed-fixture.sh`（从备份恢复浏览器回归夹具）与 `scripts/reset-data.sh`（备份并清空本地运行数据）。
