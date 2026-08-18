# 项目状态

更新时间：2026-08-19（Asia/Shanghai）
当前版本：1.1.1

下一版本：2.0.0-alpha.1（已暂停发布）。M13/M14 当前代码是实现候选，不再视为“基本完成”；在进入 M15 前必须完成 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 的 H0–H8 门禁。

## 2026-08-18 全盘审查结论

- `develop` 暂不具备 V2 Alpha 发布条件，也不应继续在当前 Evidence 投影上实现 M15 learner state。
- 审查确认了审批拒绝恢复、当前工具 checkpoint、queued Run 恢复、SQLite 长事务、Evidence undo/digest/完整投影、跨计划技能 Guard、Session 压缩、handoff、提醒回复和移动导航等 P0/P1 问题。
- 当前代码适合单进程、本机、有人观察的 Demo；不适合无人值守长期运行、无认证个人服务器或不可信代码执行。
- `main` / `v1.1.1` 仍是稳定发布基线；本轮审查没有合并、发布或改写 Git 历史。
- 修复依赖顺序、失败基线、故障注入矩阵、提交纪律和发布条件以 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 为执行事实来源；逐项状态见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)。

## H0：失败基线与范围冻结

- 已在基线 `baf2564` 上建立 87 个稳定缺陷 ID；原审查没有保存逐项 P0/P1 编号，矩阵明确标记 `priority_source=unrecorded`，不伪造历史分级。
- 已新增 113 个 strict-xfail 测试节点以及迁移、夹具安全、0/1/500 条 Evidence、IMAP commit-before-Seen 等 20 个 passing gate；`xfail_strict=true`，实现修复后必须删除对应 xfail。
- 全局 pytest 数据库已从仓库内固定文件迁到进程独占的系统临时目录；大型迁移、Evidence、并发和故障注入用例进一步使用每用例 `tmp_path`。三类 SQL 夹具均为公开合成数据并由 manifest 固定 hash、schema digest 和行数。
- 测试现会把 Context、workspace、upload 与配置写入重定向到每用例临时目录，并在 session 前后用不输出内容的单向指纹守卫正式 `.env`、SQLite 与运行目录。保护夹具落地前的基线试跑曾触发旧测试对 Git 忽略的 `data/context` / `data/workspace` 写路径；为避免误删预存数据，H0 没有自动清理或恢复这些歧义文件，后续测试已被禁止再次写入。
- 原 41 个场景已逐项登记独立输入、字面期望、reason code、不变量和 mutant，但仍标为 `pending_rewrite`。旧执行器只有 27 个输入指纹和 3 个场景专属 oracle；在 H4 逐条变成可执行领域断言前，不得宣称“41 项覆盖”。
- H0 阶段没有修改生产代码，当时 87 项缺陷全部 open。H1 现已关闭其中 13 个 ID、15 个基线节点；当前剩余 74 个 open ID，M15–M20 继续冻结，下一实现门禁为 H2。

## H1：迁移、时间与备份基础（已完成）

- 已实现：
  - `schema_migrations` 冻结历史表、显式 schema version、不可变 revision checksum 与 fresh/install upgrade 等价校验。
  - 全局 UTC canonicalization：新增 `UTCDateTime`、`core/time.py`、固定精度 digest 时间串，以及历史 naive 时间按 UTC 解释的兼容策略。
  - crash-safe SQLite 迁移协议：`dry-run → verified backup → candidate publish → post-publish verify/rollback`，覆盖 writer gap、new inode、absent target creator、WAL/rollback journal、killpoint 和安全恢复。
  - 备份发布、迁移交接与回滚会固定并复核路径、目录描述符/inode 和内容摘要；候选库、备份 payload 与工作快照只有通过边界复验才能作为 trusted snapshot，晚到篡改会回滚且不会暴露无效恢复引用。
  - legacy `_write_probe` 仅接受精确的空单列旧残留；任何额外列、约束、行、index、trigger 或 view 均按未知 schema 失败关闭，规范 schema 不保留探针表。
  - 新的维护入口 `scripts/data-maintenance.py` 与 `app.db.maintenance`：`preflight / backup / verify / restore / recover / migration-backup-verify / migration-backup-restore`。
  - `scripts/rebuild-evidence.py`、`reset-data.sh`、`seed-fixture.sh`、`demo-data.sh` 均切到 H1 维护协议，避免在备份前修改原库。restore 在任何锁或写入前拒绝把目标数据库或安全备份根目录放在 source backup 本身或其子路径中；source backup 位于安全备份根目录下仍是正常布局。共享 lexical path normalization 消除 `.`/`..` 别名但不跟随 symlink，SQLite URI 对空格、`%`、`#`、`?` 安全。

- 已验证：
  - H1 定向套件共 `281 passed`：`tests/hardening/test_h1_time_contracts.py` 10、`test_h1_migration_protocol.py` 183、`test_h1_maintenance.py` 79、`test_h1_rebuild_coordinator.py` 5、`tests/test_migrations.py` 4。
  - H0 跨阶段回归为 `27 passed, 17 strict xfailed`；剩余均属于后续门禁的已登记预期失败。
  - 全量回归为 `441 passed, 98 strict xfailed`，0 unexpected failure、0 XPASS；`python -m compileall backend tests scripts`、`pip check`、前端生产构建、完整与 production-only `npm audit` 以及 `git diff --check` 均通过，npm audit 为 0 vulnerabilities。

- 尚未实现：
  - H2 Unit of Work / outbox / claim 短事务、H3 durable run queue/checkpoint、H4 Evidence/Competency 事实层、H5 Context/Intervention、H6 安全边界、H7 前端状态架构、H8 首启与发布工程。

- 已知限制：
  - 历史 naive 时间会按 UTC 解释；旧数据若原本丢失本地偏移，H1 不会伪造恢复不存在的时区信息。
  - 迁移与维护协议只支持受控 lifecycle lease；绕过 Runtime/maintenance 直接写 SQLite 的外部进程不在兼容承诺内。
  - `frontend` 目前没有独立自动化 `test` 脚本；当前工程门禁只有生产构建与 `npm audit`，这一缺口仍待后续里程碑补齐。
  - 外部用户安装完成率、连续学习闭环和 7 日留存仍是**待真实用户验证**，不能由自动化测试替代。

- 下一门禁：
  - 按计划进入 H2：事务、幂等与 Outbox；在 H2–H8 全部关闭及真人门槛完成前，不恢复 M15–M20。

## V2 当前实现候选（develop，尚未验收）

- `EvidenceObservation` 是追加式事实层，带来源、计划/任务/Run/Session、评分、提示/迁移等级、Rubric 快照、因果链和幂等键；没有编辑或物理删除路径。
- `submission_create`、`submission_check`、`quiz_grade` 和带证据的 `task_patch` 会双写账本；同一幂等键重试只返回原观察。
- `study_state_get` 与计划 Context 已接入同一个 `evidence-summary-v1` 投影候选；H1 已关闭 UTC/SQLite 往返稳定性，完整账本、撤销和重复计权语义仍待 H4 验收。
- `scripts/rebuild-evidence.py` 已提供重建、审计、回填和派生快照命令；H1 已证明纯 audit 不建表、不迁移、不写入，任何回填/重建写操作都会先取得协调 lease 并完成全量验证备份。
- Artifact 表和 `artifact_id + content_hash` 引用已经存在；内容耐久性、文件指纹、作用域审计和幂等冲突尚未通过验收。
- 当前仓库存在 41 个场景名称和第一版实现，但多数场景尚未形成独立领域断言，不能用“41/41”证明覆盖；M13/M14 均需按硬化计划重新验收。

## 当前结论

Learning Agent 已形成真实可运行的个人学习 Harness 原型，而不是一次问答式聊天页面。Hy3 在统一 Runtime 中读取分层上下文、调用工具、观察结果并继续决策；正常路径可以演示计划澄清、资源核验、计划采用、学习跟踪、提交验收、复习和提醒。当前审查同时证明这些路径的崩溃一致性、长期 Context 连续性和服务器安全边界尚未达到长期产品标准。

`main` 是可发布分支，`develop` 用于集成下一版本。发布前必须在 `develop` 完成测试、浏览器回归和文档同步，再合并到 `main`；不再保留“main 固定为旧归档快照”的历史约定。

## 已实现的正常路径能力

以下条目描述可运行能力，不代表其崩溃恢复、并发、迁移和长期上下文不变量已经通过 H0–H8：

- 对话式计划制定：需求不充分时由 Agent 生成结构化提问卡；充分后可委派只读子 Agent 调研，汇总成可审阅提案，用户采用后才创建正式计划。
- 计划执行与调整：Agent 能读取学习位置、计划版本、资源、事件和提交，教学下一步、修改阶段/任务、安排复习和日历；作用域 Guard、幂等、统一 UoW 与撤销完整性仍由 H2/H4 重新验收。
- 学习证据与考核：支持文字、文件、代码和链接提交；Agent 可读取文件、运行有界代码、按标准验收、评分并安排下一轮复习。
- 主动性：正常路径中，单实例心跳先筛选候选，再启动计划级 Hy3 Run 决定是否干预；提醒可投影到收件箱、浏览器和可选邮件。活动 Run 回复目标、多渠道唯一 Intervention、IMAP ack 与冷却语义仍由 H5-INT/MAIL/PRO 缺陷阻塞。
- Session 管理：对话自动语义命名，支持手动改名、归档、恢复和非破坏式消息编辑。全局 Session 创建计划后通过 `SessionPlanLink + handoff_summary` 显式过渡到新的计划 Session，不静默改绑。
- Harness 可观察性：每条 Agent 消息内包含可折叠 Run；工具、审批、子 Agent、失败和预算可以逐项展开。暂停、恢复、子 Run 和重启路径已有正常路径实现，但审批拒绝、current tool、二次中断、finalization、lease 和 child durability 仍由 H3-RUN-001–011 阻塞。
- 产品状态一致性：单进程正常路径会限制同一 Session 根 Run，并将部分跟进放入后端队列。commit 后 kill、late steer、提醒 target 与多执行者仲裁尚未耐久闭环（H3-RUN-004/006/007、H5-INT-001）；归档与证据终态约束也需在后续门禁复验。

## Context 与 Memory 1.1

- Context 有 Global/Profile/Plan/Session 分层组装候选，但 H5-CTX-001 已证明 global Session 的 discussed link 会解锁计划私有事件、测验、提醒、复习和日历；当前不能宣称严格隔离。
- V2 第一批新增 `EvidenceObservation` 账本与统一投影候选；H1-TIME-001/002 已关闭 SQLite 时间往返差异，但 H4-EVID-001 仍证明 501/10,000 条账本会被在线路径截断，因此当前仍不能宣称完整账本 digest 稳定。
- 长 Session 会保留原文并写 `SessionSummary` 候选；H5-CTX-002–004 已证明压缩会遗漏输入或在失败后错误推进 coverage，尚不具备长期连续性保证。
- 长期记忆先以 proposal 存在；确认后才进入检索。重复内容会强化原记录而不是复制；用户纠正会建立 `supersedes_id / superseded_by_id` 替代链，旧认识保留为历史。
- 记忆归档已有软归档/恢复候选；H5-CTX-011 已证明恢复会清空未来 `expires_at`，生命周期语义尚未通过。
- 检索使用 BM25 + 本地 SimHash、RRF、作用域、层级、置信度与时间衰减；结果记录使用次数/最近时间并返回 `score_breakdown`。
- 每个 Run 会保存 `ContextSnapshot` 候选；H5-CTX-007 已证明编辑后缺少可靠的 stale/valid 标记，H5-CTX-008/009 也证明 manifest 与 Token 预算不能完整解释实际模型输入。
- SQLite 是事实来源，`data/context/global.md` 与 `data/context/plans/{id}.md` 是不含 Session 对话的最新可读投影，`data/context/runs/{run_id}.md` 是该轮精确输入副本；原始对话、事件、摘要版本和历史快照仍保留在数据库。

## H0 验证

- 修改前基线：`pytest -q` 为 126 passed；该数字只证明旧正常路径测试通过，不是领域正确性证明。
- H0 定向：`pytest -q tests/hardening -rxX` 为 20 passed、113 xfailed、0 XPASS、0 failed。节点覆盖拒绝/崩溃/二次恢复、真实 SQLite 锁、重复请求、外部副作用不确定、跨计划隔离、编辑/归档/恢复、安全与前端状态契约。
- 解除登记验证：`pytest -q tests/hardening --runxfail --tb=no` 为 20 passed、113 failed；113 个登记节点全部在旧实现失败，没有被夹具错误伪装成通过。
- 全量回归：`pytest -q -rxX` 为 146 passed、113 xfailed、0 XPASS、0 failed；唯一警告是现有 FastAPI TestClient 的 `StarletteDeprecationWarning`。
- 静态与依赖门禁：Python `compileall`、`pip check`、前端生产构建、完整 `npm audit` 和 `npm audit --omit=dev` 均通过；两次 npm audit 均为 0 vulnerabilities。当前前端尚无独立 Vitest/Playwright 脚本，这一发布门禁缺口由 H8-CI-001 保持 open。
- 数据库夹具 passing gate 校验 manifest/hash、无个人数据扫描、`integrity_check`、`foreign_key_check`、schema digest 与边界行数；所有物化副本都在 pytest 临时目录。
- 真实 Chrome 冷启动：使用独立临时 SQLite、临时代码副本和精确双 Session 合成令牌；375、768、1280、1440、2560 各自新 browser context、首次导航前设 viewport。五个尺寸均无根级溢出、加载残留、页面错误或 API 写请求；H7-UI-001 的 375/768 Session 和 375 设置共 3 项 strict expected failure，桌面三尺寸无非预期失败。报告不保存 Session ID/标题，验收后临时目录已移入系统 Trash。
- H0 没有调用真实模型、SMTP、IMAP 或公网，也没有用 SQLite 打开、查询、迁移、复制或修改正式用户数据库，没有输出、复制或修改 `.env`/个人数据。应用配置导入仍可能按生产启动方式读取本地 `.env`，但测试会先用合成环境值覆盖外部凭据且不打印内容；保护夹具另在本机计算不输出的单向完整性指纹。

## 明确边界

- 产品只面向单个本地用户；当前未认证服务只能绑定 loopback。个人服务器认证仍由 H6-AUTH-001 阻塞，不能对外开放。
- `code_execute` 目前只是宿主进程限制，不是安全沙箱；H6-CODE-001 修复前不得用于不可信代码，server 模式必须默认关闭。
- 当前日历是应用内日历，不宣称与系统/Google/Outlook 双向同步。
- Service Worker 通知需要浏览器仍在运行；电脑关机或浏览器完全退出时不能被本地服务唤醒。
- SMTP/IMAP、VAPID 和模型调用依赖用户自己的供应商配置及本地进程持续运行。
- 子 Agent v1 默认只读；业务写操作回到主 Agent，避免多个执行体竞争修改计划和长期记忆。
- 外部用户安装完成率、连续学习闭环和 7 日留存均为**待真实用户验证**；H0 的合成夹具、自动化测试和 Chrome 冷启动不能替代 H8 的真实样本记录，也不能由 Agent 自行宣布完成。

上述边界不影响受控本机 Demo，但审批恢复、事务一致性、Evidence 正确性、Context 连续性、移动导航和服务器安全边界会阻塞 V2 Alpha、无人值守运行和对陌生用户公开推广。后续执行以 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 为准，不能在界面或文档中把待修能力写成已完成。
