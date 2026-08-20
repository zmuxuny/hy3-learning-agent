# Learning Agent 2.0 前置硬化实施计划

更新时间：2026-08-19（Asia/Shanghai）
适用分支：`develop`  
稳定基线：`v1.1.1` / `fe3db33`  
审查基线：`1c5ff50`  
状态：必须在继续 M15 前完成

## 1. 决策与目标

当前 `develop` 已包含 M13 证据账本和 M14 技能图的第一版实现，但 2026-08-18 全盘审查确认：Run 恢复、SQLite 事务、证据投影、Context/Memory 连续性、提醒线程、移动端导航和服务器安全边界仍存在会破坏用户数据或产品语义的问题。

因此从本文件生效起：

- 暂停 M15–M20 新功能开发；
- M13、M14 重新标记为“实现候选，尚未验收”；
- `2.0.0-alpha.1` 暂不发布；
- 不把现有 126 项测试通过等同于 V2 领域不变量通过；
- 先完成 H0–H8，再决定是否恢复 M15；
- `main` 和 `v1.1.1` 保持稳定，不用清空数据或重写 Git 历史换取通过。

本计划的目标不是继续堆功能，而是把项目修到以下标准：

1. 用户拒绝、撤销、归档和编辑在任何重启点都保持原语义；
2. 证据、技能、记忆和提醒可以从事实重建，不依赖偶然的内存状态；
3. Agent 的写入、外部副作用和恢复行为具备可解释的一致性；
4. 本机默认安全，服务器模式失败关闭；
5. 桌面和手机都能完成同一条学习闭环；
6. 陌生用户可以安装、首次运行、备份、升级和恢复；
7. 文档只陈述真实通过的能力。

## 2. 冻结的产品与工程不变量

### 2.1 Runtime

- 任一非终态 Run 必须处于“可领取、等待用户、等待重试或等待人工协调”之一，不能永久悬停。
- 任意时刻最多一个执行者持有某个 Run 的有效 lease。
- checkpoint 必须说明当前阶段、当前工具、剩余工具、消息状态、Context 版本、预算和下一步。
- 用户拒绝永远不能因为进程重启变成批准。
- 已提交的工具结果、最终消息、通知和外部邮件不得因恢复而重复。
- 进程内 task map、SSE 订阅和 asyncio Task 只能是执行缓存，不能成为耐久事实源。

### 2.2 事务与副作用

- 数据库写事务中不得等待模型、HTTP、SMTP、IMAP、子 Agent 或子进程。
- service 和 tool handler 默认只 `flush`；事务提交由 API/Runtime Unit of Work 统一负责。
- 一个数据库写工具的领域数据、Operation、Evidence、RunEvent 和 ToolInvocation 要么一起提交，要么一起失败。
- 外部写入采用 outbox 和稳定 action key；超时不等于副作用没有发生。
- 不确定的外部结果进入 `needs_reconciliation`，不能盲目重放或伪装为普通失败。

### 2.3 Evidence 与 Competency

- 原始 Evidence 一旦写入不更新、不物理删除；纠正和撤销通过追加 amendment/invalidation 表达。
- 自述、任务打勾、普通对话和模型自由文本不能单独证明掌握。
- 一次真实学习行为只产生一份主观察，不因业务状态同步而重复计权。
- CLI、Context、API 和在线投影对同一账本必须产生相同 digest。
- 计划 A 的私有技能和证据不能被计划 B 的 Run 读取或修改。
- Evidence 必须引用可验证来源；同幂等键、不同内容必须报冲突。

### 2.4 Session、Context 与 Memory

- Session 是连续对话的唯一导航单位；Run、steer、提醒和邮件回复只能追加到所属 Session。
- 全局 Session 只自动装配画像、全局记忆和紧凑计划索引；详细计划状态必须显式读取或进入计划 Session。
- handoff 是创建计划 Session 时冻结的交接事实，不能被源 Session 的后续内容覆盖。
- Session 压缩只能确认实际读取并总结的连续消息区间；失败不能推进覆盖游标。
- 消息修订必须失效受影响的摘要、Context 缓存和来源记忆，但不删除原始审计记录。
- 长期记忆必须遵循 proposed → confirmed → corrected/superseded → archived/expired → restored。

### 2.5 Intervention 与主动性

- 一次逻辑提醒只有一个权威 Intervention 和一条 Agent 消息；in-app、email、push 只是 Delivery。
- 多渠道投递在 Context 中只能计为一次提醒。
- 点击提醒或回复邮件必须回到原 Session、原计划焦点和原 Intervention。
- 冷却依据持久化决策终态，而不是“曾创建过 heartbeat Run”。
- quiet hours、模型失败和 Guard 拒绝不能消耗完整成功冷却。

### 2.6 UI 与安全

- UI 时间顺序必须与事实顺序一致，不能把中途 steer 渲染到最终回答之后。
- 375、768、1280、1440、2560 均能直接导航到 Session、计划、提醒、记忆和设置，而不是先在桌面进入再缩放。
- 默认只监听 `127.0.0.1`；无认证的服务器模式必须拒绝启动。
- `code_execute` 在没有真实隔离时默认关闭或逐次审批，不能把 `prlimit` 称为沙箱。
- 网页、搜索结果、邮件正文和文件内容均视为不可信数据，不能提升工具权限。

## 3. 固定依赖顺序

```text
H0 失败基线与冻结
  → H1 迁移、时间与备份基础
    → H2 事务、幂等与 Outbox
      → H3 耐久 Run / Queue / Sub-Agent
        → H4 Evidence 与 Competency 事实层
          → H5 Context / Memory / Intervention
            → H6 安全运行边界
              → H7 前端状态架构与 V2 最小闭环
                → H8 首启、发布工程与外部用户验收
                  → 重新评审是否进入 M15
```

H4 与 H5 可以在 H3 稳定后分支开发，但必须按顺序集成。H6 的文档和默认关闭策略可以提前，涉及 Runtime Guard 的实现必须基于 H2/H3 的统一状态机。

## 4. H0：失败基线与范围冻结

目标：先证明旧实现如何失败，禁止修复后再补一个无法区分新旧实现的测试。

执行追踪：[`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md) 记录本轮派生的稳定缺陷 ID、失败原因、测试、目标门禁和修复提交。原审查没有保存逐项 P0/P1 编号，矩阵不得伪称恢复了历史编号或分级。

### 实施任务

- 为本次审查的每个 P0/P1 缺陷增加最小回归测试，并确保它在审查基线稳定失败；进入 H0 提交时使用带缺陷编号和原因的 `xfail(strict=True)`，后续修复必须删除对应 xfail，不能让集成分支长期全红。
- 测试必须使用独立临时 SQLite 数据库，不复用用户数据库或进程间固定测试库。
- 保存三类只读夹具：空库、`v1.1.1` 完整业务库、包含边界数据的大型库。
- 将原“41 个固定场景”标记为待重写，建立每个场景的输入、期望状态、失败 reason code 和对应不变量。
- 新增审查矩阵，至少包含：
  - SQLite 往返后的时区和 digest；
  - 500/501/10,000 条 Evidence；
  - submission/quiz/task undo；
  - 审批拒绝后的进程崩溃；
  - 工具执行中的崩溃和二次恢复；
  - queued 无 checkpoint；
  - 主 Run、子 Run、心跳同时竞争 SQLite；
  - 跨计划 competency edge/link；
  - Context 跨计划泄漏、长消息压缩遗漏、handoff 漂移；
  - 活动 Run 中提醒回复；
  - 手机 Session 切换和设置入口。

### 完成门槛

- 每个已知缺陷都有当前实现会失败的自动化测试或浏览器断言；H0 中的已知失败以 strict xfail 明确登记，普通测试仍保持通过。
- 所有失败均有稳定错误原因，不依赖公网和真实模型。
- `STATUS.md` 不再把场景数量、构建通过或截图数量写成领域正确性证明。

### 禁止事项

- 不先修改生产代码再编写失败测试。
- 不使用无条件 skip，不放宽或删除失败断言；只能在修复后删除 xfail 标记。
- 不继续 M15、复杂技能 UI或新主动策略。

### H0 实施记录（2026-08-18）

- 生产代码未改；全局测试数据库已移出仓库 `data/`，所有故障注入使用临时 SQLite 或确定性 SQL 副本。
- 三类只读公开夹具与 hash/count/schema manifest 已建立；原 41 场景在 H0 只登记独立 contract，H4 已把它们逐项改为 production reducer/audit baseline 与 mutant 门禁。
- 定向套件登记 87 个 open defect ID、113 个 strict-xfail 节点和 20 个 passing gate。实现门禁修复每一项时必须删除对应 xfail，并在矩阵写入修复提交。
- 最终全量回归为 146 passed、113 xfailed、0 XPASS、0 failed；Python compileall、pip check、前端生产构建与完整/production-only npm audit 均通过。五尺寸 Chrome 使用独立临时数据库和精确合成 Session 令牌，得到 3 个已登记 H7-UI-001 XFAIL，未产生非预期失败或 API 写请求。
- H0 当时只关闭“失败基线缺失”这一准备工作，不关闭任何生产缺陷；H1–H4 已于后续阶段关闭，当前下一门禁为 H5。

## 5. H1：迁移、时间与备份基础

目标：建立后续状态机和事实账本可以依赖的数据层。

### 实施任务

- 引入显式 schema version/migration history，记录版本、校验和、应用时间和结果。
- 统一 UTC canonicalization：写入前转为 aware UTC，读取历史 naive 时间时按明确策略恢复，digest 使用固定精度字符串。
- 对 SQLite 不能通过 `ALTER TABLE` 正确补齐的外键和约束，使用“新表 → 复制 → 校验 → 原子替换”。
- 生成可比较的规范 schema，验证 fresh install 与 `v1.1.1` upgrade 等价。
- 启动迁移前先对原数据库生成一致性备份；audit/rebuild 不得在备份前修改原库。
- 备份 manifest 记录应用版本、schema 版本、数据库/文件 hash、完整性结果和时间；默认排除 `.env`。
- 提供固定流程：`preflight → copy 上 dry-run → backup → migrate → verify`。
- 提供 restore/verify，而不是假装所有迁移都可逆。

### 测试与故障注入

- 空库、`v1.1.1` 库和部分 V2 schema 各执行两次迁移。
- 在建表、复制、索引、版本登记前后分别杀进程。
- 校验 `integrity_check`、`foreign_key_check`、索引、唯一约束和默认值。
- 执行备份 → 升级 → 回退 → 再升级，比较业务对象、文件 hash 和 Evidence digest。
- 覆盖 UTC、Asia/Shanghai、夏令时、naive 历史数据和微秒精度。

### 完成门槛

- fresh/upgraded schema 差异为零。
- 中断迁移可以安全重试，或停止启动并给出恢复路径。
- 备份发生在任何迁移、建表、回填和 audit 写入之前。
- 时间和 digest 在重启、备份恢复后保持一致。

### H1 实施记录（2026-08-19，已完成）

- 已新增冻结 `MIGRATION_REGISTRY`、`schema_migrations` 历史表、fresh/install 与 `v1.1.1` upgrade 对照校验，以及只接受公开 provenance legacy schema checksum 的分类器。
- 已统一 `UTCDateTime`、`canonical_utc()`、固定精度时间串与历史 naive → UTC 兼容；所有 H1 定向时间契约均通过。
- 迁移协议已经切为 `snapshot → dry-run → verified backup → candidate publish → post-publish verify/rollback`，覆盖 writer gap、WAL/rollback journal、new inode、absent target、killpoint、restore safety backup 与 portable `database_identity`。发布、回滚和恢复在交接边界重新校验固定的路径、目录描述符/inode 与内容摘要，晚到篡改时只从已验证的 trusted snapshot 回退，不返回无效 recovery reference。
- legacy `_write_probe` 只接受“精确名称、精确单列 INTEGER、零行且无 index/trigger/view”的旧启动残留；其他同名或近似 schema 一律以 `unknown_legacy_schema` 失败关闭，规范新库不会保留该表。
- 已提供 `scripts/data-maintenance.py` / `app.db.maintenance` 的安全维护协议，`reset-data.sh`、`seed-fixture.sh`、`demo-data.sh` 与 `rebuild-evidence.py` 已接入该协议。restore 会在加锁和写入前拒绝把目标数据库或安全备份根目录放在 source backup 本身或其子路径中；source backup 位于安全备份根目录下仍是正常布局。共享 lexical normalization 消除 `.`/`..` 别名但不跟随 symlink，SQLite URI 对空格、`%`、`#`、`?` 等路径字符安全。
- H1 收口历史快照为 281 passed：`test_h1_time_contracts.py` 10、`test_h1_migration_protocol.py` 183、`test_h1_maintenance.py` 79、`test_h1_rebuild_coordinator.py` 5、`tests/test_migrations.py` 4；当时 H0 跨阶段为 27 passed / 17 strict xfailed，全仓为 441 passed / 98 strict xfailed，0 unexpected failure、0 XPASS。该组数字只记录 H1 收口时点，不是当前 H2 结果。
- H1 关闭 13 个缺陷 ID、15 个基线节点；其收口时矩阵剩余 74 个 open ID、下一门禁为 H2。H2 的当前状态见下一节。
- H1 收口时前端尚无独立自动化 `test` 脚本；H2 已补 6 个 Node test runner 回归，但 Vitest/Playwright 和 H7/H8 发布门禁仍未完成。legacy naive 时间不会恢复不存在的原始 offset；受控 lease 不能约束绕过 Runtime/maintenance 的外部原始 SQLite writer；外部安装、连续学习闭环和 7 日留存仍待真实用户在 H8 验证。

## 6. H2：事务、幂等与 Outbox（已完成）

目标：消除长写锁和分散提交，为 Runtime 恢复提供原子语义。

### 实施任务

- 定义 Unit of Work，收拢目前散落在 API、service、tool、Runtime 中的 `commit()`。
- 对工具分类：纯读、数据库写、外部读、外部写；每类使用不同执行协议。
- ToolInvocation 使用短事务 CAS claim，立即释放写锁；模型、Web、SMTP、IMAP、代码和子 Agent 均在事务外运行。
- 数据库写工具把领域修改、Operation、Evidence、事件和 invocation result 原子提交。
- 外部写先落 outbox，再由独立投递器执行并回写 receipt。
- ToolInvocation 和 domain idempotency 均保存 request digest；同 key 同内容重放，同 key 不同内容返回 conflict。
- 引入 `needs_reconciliation` 处理“副作用可能已经发生但无回执”的情况。
- 只对 CAS/短事务做有界退避，禁止无差别重跑整个工具 handler。
- 增加架构测试，限制允许直接 `commit()` 的模块和函数。

### 测试与故障注入

- 两个执行者同时 claim 同一调用。
- 在 claim、领域写、commit、outbox 投递、receipt 保存的每个边界杀进程。
- 人工持有 SQLite 写锁，同时运行主 Agent、子 Agent 和心跳。
- 模拟 SMTP 接受邮件后连接中断、HTTP 超时和 DB commit 失败。

### 完成门槛

- 无外部 await 发生在活动写事务内。
- 压力测试无重复 Operation、Evidence、Notification 或外部投递。
- `database is locked` 不再导致 Run 丢失；无法提交时进入可恢复状态。
- service/tool handler 不再自行决定事务终点。

### H2 实施记录（2026-08-19，已完成）

- schema revision 2 为 ToolInvocation 增加 canonical request digest、规范参数、effect kind、claim token/version/expiry，为 Artifact/Evidence 增加 request digest，并新增受约束的 `OutboxAction` / `OutboxReceipt`。H1 遗留 running invocation 与无 receipt 的 email/browser queued row 无法重建原始身份，因此一律迁移为 `needs_reconciliation`，不伪造 digest 或自动重放。
- `app.db.uow` 成为应用事务协调入口；API、Runtime、service 与 tool handler 只 stage/flush，由最外层 coordinator 提交。SQLite 写路径在嵌套 savepoint 前显式建立 physical outer transaction，避免最外层 savepoint release 提前发布半份领域状态。只有 DB-only、可安全重放的 claim/CAS、事件与 receipt 短回调执行有界退避，任意业务 handler 不会因锁竞争被整体重跑。
- 48 个注册工具完成 `pure_read` 17、`database_write` 21、`external_read` 4、`external_write` 6 的 effect 分类并通过设置契约公开。stable action key 与 canonical request digest 分离；同键同内容返回原结果，同键异内容返回 typed `idempotency_conflict`；claim token/version/expiry 阻止旧执行者提交或覆盖被新 worker 领取的调用。
- 数据库写工具在一个 UoW 中提交领域对象、Operation、Evidence、LearningEvent、RunEvent 与 ToolInvocation 终态。模型、HTTP、embedding、SMTP、Web Push、子进程和子 Agent await 均移到活动 writer 之外；锁预算耗尽返回 typed `database_busy` 和稳定 retry 元数据，不丢失为不可解释异常。
- SMTP、Web Push、workspace 文件和子进程采用 durable intent → 独立 dispatcher → receipt 协议。并发 dispatcher 只能有一个 claim；外部已接受而 receipt 未提交时转入 `needs_reconciliation` 并禁止盲重发。workspace effect 额外保存前后 hash，可在 publish 后中断时自动对账；上传、`.env` 与 Context 投影使用原子临时文件、文件/父目录 fsync 和失败清理，其中 `.env` 的 read-modify-write 由跨进程目录锁串行化。
- Operation undo 使用状态 CAS；并发 exact replay 只应用一次 inverse 和一次审计事件。workspace undo 也先持久化 outbox intent，并以 forward digest 防止覆盖用户后续修改。该事务/文件闭环不等同于 H4 的 Evidence amendment/invalidation 或 Competency 依赖撤销。
- H2-TXN-001–009 的 10 个原基线节点均删除 strict xfail；request identity 工作提前关闭 H4-EVID-006 的 2 个节点，planning delegate 在 model wait 前保存确定性 child ID、Context/messages 与 checkpoint，提前关闭 H3-RUN-008。H6-CONFIG-001 只提前关闭原子临时文件的 1 个节点，控制字符和复杂值往返 2 个节点仍 strict xfail，所以该 ID 保持 open。
- H2 收口时矩阵累计关闭 24 个 defect ID、剩余 63 个 open ID。H2 定向为 84 passed，H0 当时为 49 passed / 84 strict xfailed，普通非-hardening 回归为 139 passed；前端 Node 为 6 passed，生产构建通过，完整与 production-only audit 均为 0 vulnerabilities。当前数字见 H3 记录与 [`STATUS.md`](STATUS.md)。
- 已知限制：SMTP、Web Push 与子进程的不确定结果只能等待人工或 provider 对账，仅 workspace 可依据本地 hash 自动恢复；Context Markdown 是可重建派生投影，数据库提交后、投影发布前崩溃尚无跨重启 durable recovery；真实 SMTP/VAPID、外部安装、连续学习闭环与 7 日留存均未验证。
- H2 收口时下一门禁为 H3；该门禁现已完成，当前状态见下一节。H4–H8 全部关闭前继续冻结 M15–M20。

## 7. H3：耐久 Run、Queue 与子 Agent

目标：使每个 Run 在任意持久化边界中断后都能继续、等待或安全协调。

### 实施任务

- 建立集中状态转换服务，覆盖 `queued → leased/running → waiting_approval → running → completed/failed/cancelled/needs_reconciliation`。
- checkpoint 版本化，并保存 phase、step、messages/reference、`current_tool_call`、remaining calls、ToolInvocation、Context、cards、budget 和 state version。
- 当前工具必须在执行前进入 checkpoint，只有工具结果和消息回填完成后才能清除。
- approve/reject/answer/note/decided_at 原子持久化；恢复时不存在默认批准。
- 恢复开始时保留旧 checkpoint，直到新 checkpoint 或终态原子替换。
- queued 且无 checkpoint 代表尚未开始，必须重新领取而不是标记失败。
- Run 使用 lease/version CAS，防止双执行者恢复。
- 最终消息、Run output、终态、budget 和 checkpoint 清理原子提交，并使用稳定 final-message key。
- 同 Session 根 Run、计划 heartbeat 和邮件回复建立明确的并发仲裁；取消后自动推进耐久队列。
- steer 在最终模型输出期间到达时必须被消费或明确转成下一条队列消息，不能静默遗留。
- 主 Run 与子 Run 使用同一状态机、lease、预算和恢复协议；子 Agent 网络错误使用有界、可观察重试。
- 在真正完成数据库 lease 前明确限制单进程/单 worker，不允许部署文档暗示多 worker 安全。

### 测试与故障注入

- 在 Run 创建、模型调用、工具调用、审批、最终消息等每个边界杀进程。
- 同一 Run 连续中断并恢复至少三次。
- 两个进程同时恢复同一 Run。
- 覆盖 queued 无 checkpoint、waiting approval、多工具、预算耗尽、取消和父子联动。
- 随机 failpoint 重启 100 轮，比较无故障运行的语义结果。

### 完成门槛

- 不丢 Run、不默认批准、不重复工具、不重复最终回复。
- 所有非终态 Run 重启后均能被确定分类。
- 状态只能通过统一服务转换，API 和工具不能任意赋值。
- 完成一次真实 Hy3 双中断恢复演示。

### H3 实施记录（2026-08-20，已完成）

- schema revision 3 新增受约束的 Run phase/state version、checkpoint schema version、lease、retry deadline/reason、审批事实与 Queue version/position；fresh install 与 frozen H2 upgrade 的 `sqlite_schema` 完全一致。旧 current-tool、缺失审批身份和不完整 checkpoint 只在可证明时回填，否则 fail closed 到 `needs_reconciliation`。
- `app.runtime.state` 成为主 Run、planning child 与通用 child 的统一状态转换入口。claim/heartbeat 使用 lease token 与 state-version fence；checkpoint 在模型/工具外部等待前提交，terminal transition 清理 lease/checkpoint/approval/retry 字段；API、scheduler、email 与 Queue 使用同一根 Run scope 仲裁。
- approval decision、current/remaining tool、ToolInvocation、late steer、final message/output/event、取消与下一个 Queue successor 均纳入短 UoW。Queue position 由 scoped partial unique index 强制，并以两阶段重排避免 SQLite 即时唯一约束的中间冲突。
- 主/子 Run 共享 model/tool/network/elapsed/token/cost 预算、耐久有界 retry 和只读 child Guard。child finalizing checkpoint 冻结报告，父终态取消覆盖所有非终态 child，child 终态与父 `subagent.completed` 在同一事务投影或可幂等修复。
- 真实进程故障覆盖 claim/checkpoint/finalizing、同一 Run 连续三次中断、双进程 claim、SQLite busy 与 frozen-H2 migration publish；固定种子 100 轮逐轮比较无故障 baseline、恢复运行与手写语义 oracle。另有两工具测试在第二个数据库副作用已提交、Run checkpoint 尚未推进时中断，恢复后只保留两份领域事实与两个 completion。
- H3 定向为 40 passed；前端状态契约为 9 passed，生产构建和 npm audit 通过。真实 Hy3 临时库演示完成两次 SIGKILL、三次 claim，最终只有一条 assistant message 与一个 completed event，正文和凭据未输出。完整结果只在 [`STATUS.md`](STATUS.md) 维护。
- 已知限制：SQLite lease 保证同一共享数据库上的 fenced executor，不提供多节点调度、leader election 或分布式数据库承诺；`needs_reconciliation` 仍要求人工/provider 对账；SSE delta 仍是进程内瞬时投影。H4 已完成，H5–H8、外部安装、连续使用和 7 日留存未完成，M15–M20 继续冻结。下一门禁为 H5。

## 8. H4：Evidence 与 Competency 事实层

目标：在 M15 reducer 之前，使证据和技能映射真正可重建、可撤销、可隔离。

### 实施任务

- 移除规范投影的 500 条截断；增量 reducer 必须持续和全量重建比较 watermark/digest。
- undo/redo/correction 通过追加 amendment/invalidation 记录，不修改历史 Evidence。
- Operation 保存产生的 observation IDs；undo 在同一事务追加对应失效事实。
- 定义类型化 `EvidenceRef` 和 Eligibility Policy，校验 source owner/plan/task/hash、Rubric、evaluator 和 `assesses` 映射。
- self_report、checkbox、submitted、attempted 和普通对话最高只能进入相应的保守阶段。
- 一次 submission/quiz/attempt 只产生一份主观察；任务完成是业务投影，不再作为第二份独立成功证据。
- Artifact 对正文和 metadata 使用规范 envelope 指纹；文件复制到内容寻址的不可变存储或保存可验证快照。
- 检测 supersession 环、跨 owner/plan/task 替代和多分支冲突。
- Evidence 与 Competency 改为能表达一份证据对应多个被考核技能；通过 Task `assesses` 映射自动关联。
- 修复 Competency 唯一性和 Guard：计划级 `(owner, plan, key)`、全局级单独唯一；edge/link 两端均从数据库解析真实作用域。
- 图修改具有 revision/version 和可撤销依赖检查；不能删除节点后级联破坏后续 Operation。
- `evidence_list` 在 SQL 中先按 competency/plan/task 过滤再分页，输出正式嵌套 Schema，默认拒绝额外字段。
- 重写 41 个场景，每个场景拥有独立输入、期望投影、审计结果和失败原因。

### 测试与故障注入

- 0/1/500/501/10,000 条账本全量与增量重建。
- 保存、关闭、重开和备份恢复后的 digest。
- submission、quiz、task 的提交、撤销、重做、再次撤销。
- 同 key 同内容重放与不同内容冲突。
- 自述与测验冲突、提示后答对、独立迁移成功、旧成功后新失败。
- 跨计划读取、edge、link、相同 key 和 merge candidate。

### 完成门槛

- 全量/增量/CLI/Context/API 的 digest 100% 一致。
- 撤销后历史仍可审计，但不再影响当前投影。
- 跨计划泄漏和越权写入为零。
- 每个基线场景都能在破坏对应规则时独立失败。
- M14 最小闭环能回答“这个任务训练什么、证明什么、依据是什么”。

### H4 实施记录（2026-08-20，已完成）

- schema revision 4 `h4_evidence_competency_facts` 冻结 append-only Evidence fact、不可变 Artifact snapshot、规范化 Evidence↔Artifact/Competency/Operation 关联、scope-aware Competency、owner-wide graph revision/mutation/dependency 与计划投影 watermark；canonical checksum 为 `851f34b9c3d455208b73c6815856da70edc57e52d5676f8b6b391e8ddf1b0ace`。
- revision 3 与 partial-M13 来源升级均为 source-aware：只回填可由原库证明的 Artifact、Competency、assesses 和 Operation 关联，不读取 `file://` 源文件，不按标题或 Run 猜测身份；跨 owner/plan/task、supersession 环/分支、图环、损坏 FK 或 hash 一律 fail closed。fresh 与升级后的 `sqlite_schema` 精确等价，H4 semantic backfill/history/publish 的真实 SIGKILL 重试收敛且不重复派生事实。
- Evidence writer 在同一 UoW 持久化 primary observation 与来源/技能/Operation 关联；undo/redo/correction 追加 invalidation/amendment/reinstatement 事实，不更新或删除原观察。Artifact 保存 canonical envelope、bytes snapshot、size 与 hash；eligibility 后端 Guard 校验 owner/plan/task、Rubric、evaluator、source 类型与 `assesses`。
- 全量 reducer 不再截断，增量投影持久化 watermark 并持续和 full oracle 对比；0/1/500/501/10,000 条在全量、增量、CLI、Context、tool 与 HTTP API 上得到同一 digest，关闭重开和 SQLite backup restore 后仍一致。
- Competency key 由数据库 partial unique 区分 global 与 plan scope；edge/link 从数据库解析两端真实 owner/plan，SQL 在 limit 前按 Evidence↔Competency 关联过滤。每次真实图 mutation 原子推进 durable revision；Operation dependency 与 `RESTRICT` FK 防止 undo 静默级联，失败保持原 Operation committed。
- 41 个 `verified` 场景各自运行 production adapter/reducer/audit；41 个唯一 mutant 均先验证 baseline，再由独立 failure code 捕获。场景矩阵为 124 passed，未保留 `pending_rewrite`、synthetic oracle、测试专用规则或 strict xfail。
- H4 新关闭 16 个 defect ID，累计关闭 50 个、剩余 37 个。外部安装、连续使用与 7 日留存仍为待真实用户验证；M14 的复杂前端属于 H7，M15–M20 继续冻结。下一门禁为 H5。

## 9. H5：Context、Memory 与 Intervention

目标：让用户、计划、Session、提醒和邮件在长期使用中保持同一个可解释身份与上下文。

### 实施任务

- 建立 `Message → Summary → Memory → ContextPack/Snapshot` 来源图、版本、覆盖区间和失效规则。
- SessionSummary 使用连续区间压缩；只记录实际读取的消息 ID，失败不得推进 coverage。
- SessionHandoff 改为不可变、版本化事实，以 accepted proposal、PlanningIntake、source Run 和当时摘要为来源。
- 全局 Context 始终只自动装配计划摘要索引；SessionPlanLink 只影响排序和关系标签，不解锁计划细节。
- 消息编辑影响集合包含原 Run 和所有下游 Run；来源记忆进入可审计的待复核/失效状态。
- Context 按块预算而不是字符切头尾；预算包含 System Prompt、工具 Schema、输出预留和工具结果预留。
- source manifest 只列最终保留块，另存 dropped manifest 和 reason code。
- 检索增加最低相关性阈值和层级配额，泛化查询只携带少量核心画像。
- 新增权威 Intervention：source job/run、plan/session、canonical message、reason、state、deliveries、reply 和 outcome。
- 通知发送时立即建立 canonical message 映射；打开收件箱不再按正文或 Run 启发式猜测。
- 活动 Run 中提醒回复保存 `reply_to_intervention_id`；默认进入结构化队列，不作为普通 steer 丢失目标。
- 邮件使用 `BODY.PEEK[]`，只有 durable reply job 成功保存后才标记 Seen。
- 归档计划的历史邮件回复进入只读 continuation，并返回明确答复或失败回执。
- 心跳冷却基于持久化 ProactiveDecision 终态；quiet hours 计算下一次可触达时间，失败使用短退避。
- Scheduler 使用 Evidence `occurred_at` 和有效投影，不使用已失效事件的写入时间冒充学习活动。

### 测试与故障注入

- 10,000 条消息压缩后无遗漏、重复和越界 coverage。
- 一个全局 Session 创建多个计划后仍不注入计划私有事件/测验/提醒。
- handoff digest 不受源 Session 后续对话影响。
- 消息编辑后 Session/Plan/Global 来源记忆均进入正确生命周期。
- 同一提醒经三渠道投递时 Context 计数仍为 1。
- 投递、写消息、回复入队、IMAP ack 各阶段杀进程后均不重复、不丢失。
- active Run 中从收件箱回复后，目标 ID 保留且不会污染下一条普通消息。

### 完成门槛

- 固定 Context 问题集跨计划泄漏为零。
- 每个 Context 块都能解释进入/排除原因和真实 token 预算。
- 任意渠道回复都进入原 Session 和原 Intervention。
- quiet hours、模型失败和 Guard 拒绝不触发成功冷却。

## 10. H6：安全运行边界

目标：让默认本机安装安全，并为个人服务器模式建立失败关闭的边界。

### 实施任务

- 明确 `local` 与 `server` 模式：local 默认 loopback；server 未配置认证时拒绝启动。
- server 模式保护全部 API、设置、通知、文件和 Run；收紧 Host、CORS、Cookie/Token 和 CSRF。
- `code_execute` 默认关闭；可信本地模式逐次审批；server 模式无 sandbox Provider 时硬禁用。
- 如果实现 sandbox Provider，至少隔离宿主文件系统、环境秘密、网络、PID、资源和工作目录；缺失能力时 fail closed。
- 外部网页、搜索、邮件和文件标记 `external_untrusted`；其内容不能直接授权写入、代码执行或外部通知。
- Web fetch 固定已验证连接目标，限制响应字节、解压大小、内容类型、时间和跳转，覆盖 DNS rebinding。
- 子进程使用最小环境白名单，不继承 `.env`、API Key 或邮箱凭据。
- `.env` 写入拒绝换行和控制字符，使用安全转义与原子权限校验。
- 日志、RunEvent、Context、诊断包和错误响应执行统一秘密脱敏。
- 增加 `SECURITY.md` 和真实部署边界，删除“个人服务器可直接公网暴露”的暗示。

### 完成门槛

- 无认证的非 loopback/server 启动失败。
- 路径逃逸、符号链接、秘密读取、网络逃逸和提示注入测试为零通过漏洞。
- SSRF、重定向到私网、大响应和解压炸弹测试通过。
- 安全能力缺失时高风险工具不可调用。

## 11. H7：前端状态架构与 V2 最小闭环

目标：保持现有克制的 Codex 式视觉语言，同时修复导航、事实顺序和 V2 不可见问题。

### 实施任务

- 将大型 workspace Store 拆为 session、run、plan、inbox、memory、settings 状态域；服务端事实和临时 UI 状态分离。
- 引入正式路由和可恢复深链，覆盖 Session、计划详情、提醒消息、设置和归档列表。
- 首屏请求分为核心与可降级数据；设置/邮箱/统计失败不能清空工作台。
- 统一桌面侧栏、平板收缩栏和手机底栏的导航模型，修复 Session 切换和设置入口。
- 修复计划归档后的 stale focus、提醒 reply target、steer 时间顺序和 SSE 断线对账。
- 建立统一 Message/Run/Artifact/Intervention 渲染协议，规范换行、代码块、表格、长链接、嵌套列表和不可信 HTML。
- Run 事件只有可展开项使用 button；补 dialog 语义、焦点锁、Escape、焦点恢复和键盘操作。
- 拆分单体 CSS，统一 token 和断点，删除互相覆盖的媒体查询；不引入平行设计体系。
- 提供 M14 最小只读视图：计划任务的 teaches/assesses、技能证据覆盖、Evidence 时间线和来源解释。
- 为 Store、路由、消息投影和关键组件增加单元/交互测试。
- 浏览器夹具必须从公开、确定性 seed 构建，不能依赖私人备份。

### 真实浏览器矩阵

- 宽度：375、768、1280、1440、2560。
- 状态：空数据、无模型、全局/计划 Session、长消息、工具成功/失败、活动 Run、steer/queue、审批、提醒回复、归档、断网、设置错误。
- 压力：中英文、长 URL、Markdown 表格、代码块、100 条消息、长计划名、多个子 Agent。
- 键盘完成导航、发送、审批、提醒回复和归档；检查 focus、`aria-expanded` 和 reduced-motion。
- 断言无横向滚动、裁切、重叠、重复 Header、伪按钮、错误时间顺序和加载后大幅跳变。
- 每个关键状态保存桌面/手机基准截图。

### 完成门槛

- 从手机冷启动即可切换历史 Session 并进入设置。
- 浏览器矩阵全部通过，且脚本不通过预先桌面导航掩盖手机入口缺失。
- V2 对普通用户可回答“现在学到哪里、证据是什么、下一步为什么”。
- 不新增密集 Dashboard、卡片套卡片或无产品含义的 Codex 文案。

## 12. H8：首启、发布工程与真实用户验收

目标：从“开发者能跑”升级为“陌生用户能安装、信任并持续使用”。

### 实施任务

- 首次打开提供短向导：模型配置/连接测试 → 学习目标 → 第一条 Session；邮箱、Push、代码执行可跳过。
- 模型 Provider 建立适配边界；Hy3 保持一等支持，同时允许明确兼容的 OpenAI-compatible Provider。
- 提供不污染真实数据的 Demo 模式和样例计划。
- 发布包包含已构建前端，普通用户不应为运行 Release 安装 Node。
- 至少提供一种真正持续运行的一键方案，并明确支持平台；若选择 Docker，代码执行必须使用单独、低权限 sandbox，而不是共享主服务秘密。
- 提供 doctor、脱敏诊断、JSON/Markdown 导出、备份和恢复。
- README 明确哪些数据会发送给云模型、如何检查 Context、如何关闭高风险能力。
- CI 加入 lint/typecheck、Python 依赖审计、真实历史库迁移、Evidence audit、Context 隔离、浏览器矩阵、secret scan 和构建。
- 仓库补齐 description、homepage、topics、双语入口、真实截图、90 秒 Demo、CONTRIBUTING、SECURITY、Issue/PR 模板和 Good First Issue。
- Release 提供源码/运行资产、校验值、迁移说明、已知限制和准确的能力矩阵。

### 外部验收

- 至少 10 名外部用户尝试安装；至少 8 人无需维护者远程操作完成首启。
- 至少 5 人完成“创建计划 → 学习 → 提交证据 → 收到跟进”。
- 至少 3 人连续使用 7 天。
- 观察期内数据丢失、重复提醒、跨计划泄漏、默认批准和不可恢复 Run 均为 0。
- 外部用户阶段不能由 Codex 自行标记完成，必须由真实记录确认。

### 发布门槛

- H0–H7 全部完成且无 P0/P1。
- 三类数据库夹具完成升级、回退和再次升级。
- pytest、前端测试/构建、依赖审计、浏览器、安全和秘密扫描全绿。
- 完成 7 天本地 dogfooding；外部验收达到上述最低样本。
- 只有此时才能重新评审 `2.0.0-alpha.1`、恢复 M15，或合并 `main`。

Star 数量不是质量门槛。先用安装完成率、首个学习闭环完成率、7 日持续使用和有效 Issue 证明真实采用，再安排公开传播。

## 13. 明确推迟

在 H0–H8 完成前不实现：

- M15 learner-state、FSRS 个性化和 M16 自适应动作；
- 无限画布技能图、复杂图动画和视觉重做；
- 原生移动 App、更多通知渠道和外部日历；
- 多用户、团队协作、云端托管和商业计费；
- 子 Agent 任意写权限；
- 游戏化扩展、徽章生图和新搜索提供商；
- CASE/xAPI 完整兼容和外部 OpenTelemetry 服务。

容器级不可信代码隔离如果本轮不实现，`code_execute` 必须保持默认关闭/逐次审批，且不能宣传为安全执行互联网代码。

## 14. 实施与提交纪律

- 开始前完整阅读根目录 `AGENTS.md`、本文件、`STATUS.md`、`V2_ROADMAP.md`、`ARCHITECTURE.md` 和 `HARNESS.md`。
- 每个 H 里程碑建立独立执行计划；完成一个、验证一个、更新文档、单独提交一个。
- 不重写 Git 历史，不清空用户数据，不修改 `.env`，不输出任何秘密。
- 所有数据库和浏览器测试使用临时副本；真实数据只做只读核验。
- 每个修复必须包含“旧实现失败、新实现通过”的回归测试。
- 修改后检查所有调用方、Schema、迁移、Context、UI、测试和文档，不留下兼容残骸或重复实现。
- 遇到架构冲突先记录 ADR 或在计划中说明，不用临时 if/try/except 掩盖。
- 使用有界 Git 检查，例如 `timeout 5s git status --short --untracked-files=no`；不运行可能长期扫描大目录的无界命令。
- 不自行 push、合并 `main`、打 tag 或创建 Release，除非用户另行明确授权。
- 每完成一个里程碑，在 `STATUS.md` 记录真实命令、结果、未解决问题和下一门禁。

## 15. 每个里程碑的统一验收模板

```text
范围：本里程碑解决的明确不变量
失败基线：修复前稳定失败的测试/复现
实现：Schema、服务、Runtime、前端和文档变更
数据安全：迁移、备份、回滚与兼容性
自动化：pytest / frontend tests / build / audit
故障注入：本阶段要求的 kill-point / 并发 / 恢复测试
浏览器：需要检查的宽度、状态和截图
真实链路：如需 Hy3/Web/Email，说明是否真实调用及临时数据边界
残留风险：未解决项和为什么不在本阶段处理
Git：单独提交，工作区干净；未经授权不 push
```

## 16. 交给执行 Codex 的完成定义

执行 Codex 不能因为 token、时间或工作量自行缩小范围，也不能一次声称完成 H0–H8。它应从 H0 开始，按门禁推进；如果一次会话无法完成全部工作，应留下真实、可继续的 `STATUS.md` 和计划状态，而不是伪造完成。

最终完成必须同时满足：

- 代码实现、迁移、测试、浏览器和文档一致；
- 当前所有 P0/P1 均有回归测试并关闭；
- M13/M14 重新通过领域验收；
- 安全边界和部署声明一致；
- 真实用户才能完成的门槛保持“待外部验证”，不由 Agent 代填。
