# 项目状态

更新时间：2026-08-18（Asia/Shanghai）
当前版本：1.1.1

下一版本：2.0.0-alpha.1（已暂停发布）。M13/M14 当前代码是实现候选，不再视为“基本完成”；在进入 M15 前必须完成 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 的 H0–H8 门禁。

## 2026-08-18 全盘审查结论

- `develop` 暂不具备 V2 Alpha 发布条件，也不应继续在当前 Evidence 投影上实现 M15 learner state。
- 审查确认了审批拒绝恢复、当前工具 checkpoint、queued Run 恢复、SQLite 长事务、Evidence undo/digest/完整投影、跨计划技能 Guard、Session 压缩、handoff、提醒回复和移动导航等 P0/P1 问题。
- 当前代码适合单进程、本机、有人观察的 Demo；不适合无人值守长期运行、无认证个人服务器或不可信代码执行。
- `main` / `v1.1.1` 仍是稳定发布基线；本轮审查没有合并、发布或改写 Git 历史。
- 修复依赖顺序、失败基线、故障注入矩阵、提交纪律和发布条件以 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 为执行事实来源。

## V2 当前实现候选（develop，尚未验收）

- `EvidenceObservation` 是追加式事实层，带来源、计划/任务/Run/Session、评分、提示/迁移等级、Rubric 快照、因果链和幂等键；没有编辑或物理删除路径。
- `submission_create`、`submission_check`、`quiz_grade` 和带证据的 `task_patch` 会双写账本；同一幂等键重试只返回原观察。
- `study_state_get` 与计划 Context 已接入同一个 `evidence-summary-v1` 投影候选；完整账本、时间往返、撤销和重复计权语义尚未通过验收。
- `scripts/rebuild-evidence.py` 已提供重建、审计、回填和派生快照命令；只读审计、迁移与备份先后顺序需按 H1 修复。
- Artifact 表和 `artifact_id + content_hash` 引用已经存在；内容耐久性、文件指纹、作用域审计和幂等冲突尚未通过验收。
- 当前仓库存在 41 个场景名称和第一版实现，但多数场景尚未形成独立领域断言，不能用“41/41”证明覆盖；M13/M14 均需按硬化计划重新验收。

## 当前结论

Learning Agent 已形成真实可运行的个人学习 Harness 原型，而不是一次问答式聊天页面。Hy3 在统一 Runtime 中读取分层上下文、调用工具、观察结果并继续决策；正常路径可以演示计划澄清、资源核验、计划采用、学习跟踪、提交验收、复习和提醒。当前审查同时证明这些路径的崩溃一致性、长期 Context 连续性和服务器安全边界尚未达到长期产品标准。

`main` 是可发布分支，`develop` 用于集成下一版本。发布前必须在 `develop` 完成测试、浏览器回归和文档同步，再合并到 `main`；不再保留“main 固定为旧归档快照”的历史约定。

## 已实现的正常路径能力

以下条目描述可运行能力，不代表其崩溃恢复、并发、迁移和长期上下文不变量已经通过 H0–H8：

- 对话式计划制定：需求不充分时由 Agent 生成结构化提问卡；充分后可委派只读子 Agent 调研，汇总成可审阅提案，用户采用后才创建正式计划。
- 计划执行与调整：Agent 能读取学习位置、计划版本、资源、事件和提交，教学下一步、修改阶段/任务、安排复习和日历；写操作受计划焦点 Guard、幂等键、预算与可撤销 Operation 约束。
- 学习证据与考核：支持文字、文件、代码和链接提交；Agent 可读取文件、运行有界代码、按标准验收、评分并安排下一轮复习。
- 主动性：单实例心跳先筛选到期复习、临近任务和长期无证据计划，再启动计划级 Hy3 Run 自主判断是否保持安静、提醒、抽查或调整。提醒首先成为连续 Session 的 Agent 消息，再投影到收件箱、浏览器和可选邮件；点击或邮件回复回到同一上下文。
- Session 管理：对话自动语义命名，支持手动改名、归档、恢复和非破坏式消息编辑。全局 Session 创建计划后通过 `SessionPlanLink + handoff_summary` 显式过渡到新的计划 Session，不静默改绑。
- Harness 可观察性：每条 Agent 消息内包含可折叠 Run；工具、审批、子 Agent、失败和预算可以逐项展开。阻塞审批支持暂停—批准/拒绝—检查点恢复，进程重启可恢复有检查点的主 Run 和子 Run。
- 产品状态一致性：同一 Session 只有一个根 Run；运行中的跟进与邮件回复进入后端耐久队列，完成后仍在原 Session 续跑。活动/待审批 Run 阻止归档，归档 Session 不能采用遗留提案，计划正式完成受任务证据与终态约束。

## Context 与 Memory 1.1

- Context 按 Global Profile、计划/Session 作用域记忆、紧凑计划索引或当前计划、相关事件、待复习/考核、资源/提交/日历和近期对话分层组装；全局对话不注入无关计划全文，计划对话不能读取其他计划私有内容。
- V2 第一批新增不可变 `EvidenceObservation` 账本：提交、提交验收、带证据的任务完成和测验评分会幂等双写；`study_state_get` 与计划 Context 读取同一份证据状态摘要，并返回稳定 digest 与明确的保守性说明。
- 长 Session 超过阈值后压缩旧消息，但原文不删除。每次压缩写入不可变 `SessionSummary` 版本，记录覆盖消息、来源消息 ID 和生成方式，当前摘要只是最新投影。
- 长期记忆先以 proposal 存在；确认后才进入检索。重复内容会强化原记录而不是复制；用户纠正会建立 `supersedes_id / superseded_by_id` 替代链，旧认识保留为历史。
- 记忆归档是可恢复软归档，不再通过 API 物理删除。短期/情节记忆超过 90 天、显式过期或关联计划消失时会写明生命周期原因。
- 检索使用 BM25 + 本地 SimHash、RRF、作用域、层级、置信度与时间衰减；结果记录使用次数/最近时间并返回 `score_breakdown`。
- 每个 Run 保存不可变 `ContextSnapshot`。在消息内展开“读取计划、近期进度与相关记忆”，可查看实际来源构成、命中记忆分数、估算 Token 和送入模型的 Markdown。
- SQLite 是事实来源，`data/context/global.md` 与 `data/context/plans/{id}.md` 是不含 Session 对话的最新可读投影，`data/context/runs/{run_id}.md` 是该轮精确输入副本；原始对话、事件、摘要版本和历史快照仍保留在数据库。

## 当前审查验证

- 隔离源码副本 `pytest -q`：126 passed，但出现一个 aiosqlite 工作线程在事件循环关闭后仍回调的警告；现有测试主要覆盖正常路径，不能关闭已复现的崩溃一致性缺陷。
- `npm run build`、`npm audit`、`pip check` 和 Python compileall：通过；npm 报告 0 vulnerabilities。
- 真实 Chrome 使用临时数据库副本生成 23 个状态截图；视觉溢出检查未失败，但最终回归因 `settings-mobile` 缺失而失败。现有脚本还会先在桌面进入状态再缩小窗口，不能证明手机冷启动导航可用。
- Evidence 探针复现了 SQLite 时间往返后 digest 改变，以及 naive/aware 时间混合可能导致投影异常。
- 当前审查未重新发送真实邮件，也未用用户正式数据库运行破坏性测试；历史 Hy3/Web/SMTP/IMAP 验证仍只代表当时的正常路径结果。
- 工作区在审查结束时保持干净，本地 `develop` 比 `origin/develop` 超前两个 V2 提交。

## 明确边界

- 产品只面向单个本地用户/个人服务器，不提供账号、组织、租户或公网开放认证。
- `code_execute` 是个人工作区内的有界进程，不是容器级不可信代码沙箱。
- 当前日历是应用内日历，不宣称与系统/Google/Outlook 双向同步。
- Service Worker 通知需要浏览器仍在运行；电脑关机或浏览器完全退出时不能被本地服务唤醒。
- SMTP/IMAP、VAPID 和模型调用依赖用户自己的供应商配置及本地进程持续运行。
- 子 Agent v1 默认只读；业务写操作回到主 Agent，避免多个执行体竞争修改计划和长期记忆。

上述边界不影响受控本机 Demo，但审批恢复、事务一致性、Evidence 正确性、Context 连续性、移动导航和服务器安全边界会阻塞 V2 Alpha、无人值守运行和对陌生用户公开推广。后续执行以 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 为准，不能在界面或文档中把待修能力写成已完成。
