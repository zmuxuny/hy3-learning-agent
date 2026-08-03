# 项目状态

更新时间：2026-08-03（Asia/Shanghai）

## 当前阶段

个人学习场景已经形成完整的端到端 Harness：计划从对话式需求澄清、受限子 Run 分工和可审阅提案开始，用户采用后才成为正式计划；随后 Hy3 可以跟踪/教学、搜索核验资源、修改计划、读取文件、检查提交、运行代码、安排复习与日历，并由后台候选扫描决定是否发送站内提醒或邮件。

分支约定：`main` 固定为归档快照（`6e29e55`，0.6.0 之前的产品版本），`develop` 是持续开发线；所有新功能与修复先进入 `develop`。

## 本轮已验证

- 混合语义检索已接入：纯 Python BM25 关键词 + 本地 SimHash 向量（零新增依赖），RRF 融合并叠加作用域/层级/置信度/时间衰减；`memory_search` 返回每条结果的 `score_breakdown`，无向量或无检索词时自动回退到权重排序。
- 确认记忆在 `memory_maintain` 时持久化 64 位本地向量（`memories.embedding` / `embedding_provider`），旧库通过增量迁移自动补列。
- Run 状态机已支持真实 `waiting_approval` 暂停：带 `blocking: true` 的工具审批会持久化待批调用并停止本轮，`POST /agent/runs/{id}/approval` 批准后从检查点恢复执行、拒绝后把拒绝结果回填给模型继续调整；`memory_propose` 等候选式确认不中断 Run。
- 每个工具轮次前持久化 `checkpoint`（消息、步骤、剩余工具调用）；应用重启时，有检查点的 `queued/running` Run 恢复为 `queued` 并自动续跑，无检查点的仍安全标 `failed(process_interrupted)`。前端在 Run 内联区显示批准/拒绝卡片。
- 写工具幂等已落地：`ToolInvocation` 以 `run_id + 工具名 + 参数哈希` 生成 `idempotency_key`，同一 Run 内重复调用返回原结果并带 `replayed` 标记；阻塞审批期间记录为 `pending_approval`，批准执行后转为 `committed`。工具契约接口暴露 `idempotent` 标记。
- Run 预算已接入：`budget_usage` 记录模型调用次数、Token、工具调用、网络请求、耗时与估算费用；`AGENT_MAX_MODEL_CALLS / AGENT_MAX_TOOL_CALLS / AGENT_MAX_ELAPSED_SECONDS / AGENT_MAX_ESTIMATED_COST_USD` 超限时发 `run.budget_exceeded` 事件并安全停止，前端运行轨迹显示预算用量。
- Service Worker 已注册：通知通过 `showNotification` 展示（标签页关闭但浏览器运行时仍可显示），点击通知路由回收件箱；配置 `VAPID_*` 后可选 Web Push 推送，未配置时回退页面内通知。失效订阅（404/410）自动清理。电脑关机或浏览器完全退出无法唤醒，文档如实标注。
- 设置页已上线：侧栏新增“设置”，覆盖模型（地址/名称/API Key 状态/温度）、邮件（SMTP/IMAP 字段、连接测试、删除凭据）与通知策略（免打扰、每日上限、冷却时间）。保存直接写 `.env`（0600 权限、原子替换），API 永不回传密码；模型与邮箱参数重启后生效，通知策略即时生效。
- Web 搜索增加 Bing HTML 备选源：主源失败、超时或返回空结果时自动降级，`web_search` 结果带 `fallback_used` 标记；`WEB_SEARCH_FALLBACK_PROVIDER` 可设为 `none` 关闭降级。
- 邮件渠道纳入每日上限与冷却统计（此前只统计站内消息）；邮件发送失败的 `error` 详情完整回填给模型。真实 Hy3 冒烟通过：`plan_list` 工具调用成功，Run 正常完成。
- 连续天数真实计算：以 `ActivityDay` 为准，任务完成/测验通过事件落库后刷新 `streak_days`（今天未学习时保留到昨天为止的连续记录）。
- 规则成就引擎上线：首次建计划、首次完成任务、首次测验通过、3/7 天连续、100/500 XP 共 7 条规则，幂等解锁并写入 `Achievement`；首页欢迎态展示成就墙与徽章。

- System Prompt 与 41 个真实 Function Calling 输入 Schema 进入同一个 Hy3 多轮 Runtime；41 个输出 Schema 由契约接口公开并在普通成功结果回填前校验。
- `PlanningIntake` 持久保存已确认事实、结构化问题和 AI 的充分性理由；`PlanProposal` 在用户采用前不创建正式计划，重复采用保持幂等。
- `planning_delegate` 与通用子 Agent 共用只读执行器；`subagent_spawn / status / join / cancel` 已开放，v1 强制只读白名单、独立轮次上限和结构化报告，父 Agent 仍是唯一写入口。
- `study_state_get` 为计划跟踪/教学提供统一版本化快照，新计划会选择首个 pending 任务而不是返回“当前无任务”。
- 用户消息支持复制和非破坏式编辑；旧版本进入 `ChatMessageRevision`，旧下游退出当前上下文，原 Run/快照/Operation 保留，同一 Session 重新运行。
- 计划焦点由后端 Guard 强制；计划内 Run 不能操作其他计划私有数据。
- 自动化闭环通过：计划修改 → 文件写/读 → Python 执行 → 核心任务提交 → 验收 → 进度/XP → 日历。
- 分层记忆通过：全局/计划/Session 作用域、相关性排序、过期、归档、计划摘要和 28 条长会话压缩；原始消息全部保留。
- 主动调度以单实例全局心跳筛选到期复习、24 小时内任务和默认 24 小时无学习证据，再启动 Hy3；最近/下次检查和静默原因可见，站内通知受免打扰、频率和冷却 Guard。
- SMTP 发送和 IMAP 回复路由已实现；没有邮箱配置时安全回退为站内通知。
- 真实 Hy3 Run 成功完成 `web_search → plan_get → web_open → web_search(save) → plan_patch → resource_list`，搜索结果、计划版本和可撤销操作均真实落库。
- 真实连续 Session 验收通过：同一 Session 两轮消息只生成一个侧栏条目，语义标题保持稳定；修复网络策略后真实完成 `web_search → web_open` 并核验 Python 官方 asyncio 页面。
- Session 和计划支持手动归档、独立归档列表、恢复与操作审计；归档内容不删除，归档计划退出主动扫描。
- 全局对话创建计划后显示显式承接卡片；`SessionPlanLink`、父 Session 和交接摘要把新计划对话连接到来源，同时保持焦点隔离。
- 全局上下文改为紧凑计划索引和关联计划优先，不再默认批量注入跨计划资源与提交。
- 邮件通知保存来源 Session，IMAP 回复作为用户消息回到原 Session；收件箱显示脱敏配置诊断并提供 SMTP 真实发送与 IMAP 登录测试。
- 学习资源使用 `web_search → web_open → resource_save` 两阶段策展：具体课程、教程、实验和参考资料记录平台、难度、语言、核验摘要与推荐理由，在计划页集中列出并可撤销。
- 计划页“教我下一步”会先调用统一学习位置快照，再依据任务、提交、事件、复习和资源确定当前进度，只推进一个任务和一个小练习。
- Web 安全校验兼容 WSL 代理的 IPv4/IPv6 Fake-IP DNS，同时继续拒绝 localhost、私网和非公网 IP 字面量；真实搜索返回 3 条结果并成功打开 Python 官方页面。
- 同一工具在单个 Run 内连续失败两次会熔断并从后续模型轮次移除，避免 Web 或外部依赖故障触发无效重试风暴；运行摘要中的失败工具不再显示成“已完成”。
- 工具改用独立数据库事务，参数/工具失败不再因主 Runtime ORM 对象失效而升级成 `MissingGreenlet`；模型超时有一次可观察重试，工具回填有体积上限。
- 前端对话页取消 Header；最新 Run 以 Codex 式单行工作记录折叠/展开，完整轨迹仍可审计；Markdown 改用禁用原始 HTML 的 CommonMark 解析器，覆盖标题、列表、引用、链接、表格与代码块。
- 侧边栏由整栏负责滚动，对话不再嵌套独立滚动区；置顶计划提供直接归档入口。计划焦点只保留在输入框下方，移除远离正文的重复卡片。
- 提问卡提交走结构化 Session 端点并立即退出待答状态，不再把卡片答案复制成普通消息气泡。
- 站内主动消息无需邮箱；页面每 15 秒同步，并在新消息到达时显示应用内提示。SMTP/IMAP 仅用于离站发送与邮件回复。
- 站内消息支持单条归档、批量归档全部已读、独立归档列表与恢复；归档不删除消息，并退出 Agent 的近期通知上下文。
- 计划详情只有一个标题（计划名称），摘要和操作降为正文层级，任务保持纵向时间线。
- 浏览器回归覆盖 375、768、1280、1440 和 2560×1440；没有文档横向溢出、重复 Header 或横向裁切按钮。
- `pytest -q`：70 passed。
- 前端生产构建通过：JS 284.72 kB（gzip 106.02 kB），CSS 68.33 kB（gzip 13.45 kB）；生产依赖审计 0 vulnerabilities。
- 发布验收已冻结：版本 0.6.0、`CHANGELOG.md`、CI（pytest + 前端构建 + 生产依赖审计）、`scripts/reset-data.sh`（备份并清空）与 `scripts/seed-fixture.sh`（恢复浏览器回归夹具）均已就绪。
- 0.6.0 稳定性与体验验收通过：邮件冷却统计、成就/连续天数、首页成就墙；真实 Hy3 冒烟与多视口浏览器回归通过。真实验证中 DuckDuckGo 主源连接失败、Bing 备选源成功返回 3 条结果（python.org 等），自动降级设计按预期生效。
- 0.7.0 对话体验验收通过：计划澄清问答提交后可展开、运行记录展示记忆引用与子 Agent 活动、运行中可停止/排队/打断、代码块与回答复制、上下文占用显示、记忆页搜索与首页记忆概览；多视口浏览器回归无溢出。
- 数据滞后提醒已定位并加固：根因是重置前仍有后端进程持有已移走的旧 SQLite 文件（进程继续读旧 inode）。`/settings` 现返回实际数据库路径与数据量，设置页展示数据状态；`demo-data.sh reset` 同时清理根目录/`backend/` 遗留数据库文件。

## 已验证的学习闭环

1. 比较具体课程、教程、动手实验与必要参考，打开正文核验并保存为计划资源清单。
2. 从模糊目标开始，经结构化提问、规划子 Run 与提案确认创建完整计划。
3. 可撤销修改计划、阶段、任务和日历。
4. 提交文字、文件、代码或链接；读取证据并运行 Python/Bash 检查。
5. 保存验收结果，完成核心任务，更新计划进度与 XP。
6. 创建测验、评分、安排复习。
7. 后台发现候选事件并自主决定沉默或触达。
8. 站内收件箱为默认渠道；SMTP/IMAP 构成可选邮箱往返。

## 明确边界

- 这是个人应用，不做登录、团队和多租户。
- `code_execute` 有时间/输出/环境限制，但不是容器级不可信代码沙箱。
- 阻塞型审批的 Run 暂停—批准—恢复与检查点续跑已实现；无检查点的遗留 Run 仍安全标记为 `process_interrupted`。
- 通用子 Agent v1 为只读委员会（spawn/status/join/cancel 已开放）；子 Agent 写工具由架构拒绝，写操作始终交回主 Agent。
- SMTP/IMAP 协议、连续 Session 路由和连接测试已实现；当前本机实例已完成 QQ SMTP 真实发送、IMAP 登录和回复回原 Session 验收。凭据只存在被 Git 忽略的 `.env`，公开仓库不包含邮箱地址或授权码。
