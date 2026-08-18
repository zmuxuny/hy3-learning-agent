# V2 H0 缺陷—测试—门禁矩阵

更新时间：2026-08-19（Asia/Shanghai）

审查基线：`baf256430d7aff43708188955c8049137512b601`

## 口径

本文件是 H0 新建立的稳定追踪表，不是对一份历史缺陷清单的转录。Git 历史中没有保存逐项 P0/P1 编号或优先级；审查文档只把问题集合描述为 P0/P1。因此下表的 ID 是本轮依据修复门禁派生的稳定 ID，`优先级来源=U` 表示“逐项优先级未留档”，不能引用为原审查编号或原始 P0/P1 分级。

所有 `open · strict xfail` 都表示旧实现已被自动化测试稳定复现，**不表示缺陷已修复**。对应门禁修复后必须删除该测试的 xfail 标记；全局 `xfail_strict=true` 使未登记的提前通过成为 CI 失败。测试只使用进程级或用例级临时 SQLite、合成数据和离线 fake；不会以 SQLite 打开、查询、迁移、复制或修改正式数据库，也不调用真实邮箱或公网。全局保护夹具只在本机读取受保护路径字节并计算不输出的单向完整性指纹。

来源缩写：

- `HP4`–`HP12`：[`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 的 H0–H8 章节。
- `VR13` / `VR14`：[`V2_ROADMAP.md`](V2_ROADMAP.md) 的 M13 Evidence / M14 Competency 审查项。
- `STATUS`：[`STATUS.md`](STATUS.md) 的 2026-08-18 全盘审查结论。
- `U`：原审查没有保存该项究竟属于 P0 还是 P1。

测试缩写：

- `MIG`：[`test_h0_migration_regressions.py`](../tests/hardening/test_h0_migration_regressions.py)
- `TXN`：[`test_h0_transaction_regressions.py`](../tests/hardening/test_h0_transaction_regressions.py)
- `RUN`：[`test_h0_runtime_regressions.py`](../tests/hardening/test_h0_runtime_regressions.py)
- `EVID`：[`test_h0_evidence_regressions.py`](../tests/hardening/test_h0_evidence_regressions.py)
- `COMP`：[`test_h0_competency_regressions.py`](../tests/hardening/test_h0_competency_regressions.py)
- `CTX`：[`test_h0_context_memory_regressions.py`](../tests/hardening/test_h0_context_memory_regressions.py)
- `INT`：[`test_h0_intervention_regressions.py`](../tests/hardening/test_h0_intervention_regressions.py)
- `SEC`：[`test_h0_security_regressions.py`](../tests/hardening/test_h0_security_regressions.py)
- `UI`：[`test_h0_frontend_contracts.py`](../tests/hardening/test_h0_frontend_contracts.py)
- `BROWSER`：[`h0_browser_check.mjs`](../scripts/h0_browser_check.mjs)
- `COV`：[`test_h0_scenario_contracts.py`](../tests/hardening/test_h0_scenario_contracts.py)
- `REL`：[`test_h0_release_regressions.py`](../tests/hardening/test_h0_release_regressions.py)

## H0：覆盖真实性

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H0-COV-001 | 41 个领域场景必须各有独立输入、字面期望和特定回归断言；旧套件只有 27 个输入指纹、3 个场景专属 oracle。 | HP4, VR13 | COV（1 节点） | U | H4 | open · strict xfail / — |

## H1：迁移、时间与备份

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H1-MIG-001 | fresh 初始化必须显式注册全部模型；旧 `create_schema()` 依赖调用方导入副作用。 | HP5 | MIG | U | H1 | open · strict xfail / — |
| H1-MIG-002 | `v1.1.1` 升级后规范 schema 必须与 fresh schema 相同；旧 additive migration 留下 nullability、default、index 和列序差异。 | HP5 | MIG | U | H1 | open · strict xfail / — |
| H1-MIG-003 | 部分 M13 schema 升级也必须补齐 `competency_id` 外键；旧 `ALTER` 只加列。 | HP5, VR14 | MIG | U | H1 | open · strict xfail / — |
| H1-TIME-001 | 时间写入 SQLite、关闭、重开后必须保持同一 UTC instant 和 digest，且 naive/aware 历史可比较；旧实现丢 offset。 | HP4, HP5, VR13 | EVID（2 节点） | U | H1 | open · strict xfail / — |
| H1-TIME-002 | 相同 instant 的 UTC/Asia-Shanghai 表示必须规范为同一 aware UTC 值与 digest；旧实现保存 wall time。 | HP5 | MIG | U | H1 | open · strict xfail / — |
| H1-AUDIT-001 | audit 在备份前必须逐字节只读；旧 rebuild audit 会先建/迁移 schema。 | HP5, STATUS | MIG | U | H1 | open · strict xfail / — |
| H1-SCHEMA-001 | 启动可写性探针不得留下 schema 对象；旧实现永久留下 `_write_probe`。 | HP5 | MIG | U | H1 | open · strict xfail / — |
| H1-BACKUP-001 | 活动 SQLite writer 存在时 reset 必须 fail closed 且源 DB/WAL/SHM 与备份目录均不变；旧脚本只探测 8000 端口后直接移动。 | HP5, HP12 | REL | U | H1 | open · strict xfail / — |
| H1-BACKUP-002 | 不同历史路径的同名数据库必须按 manifest 保留各自身份；旧 reset 把 basename 压平并静默覆盖。 | HP5, HP12 | REL | U | H1 | open · strict xfail / — |
| H1-RESTORE-001 | restore 必须先验证 integrity/schema/manifest，验证失败时保留 live 状态；旧 seed 会用损坏文件替换有效库并返回成功。 | HP5, HP12 | REL | U | H1 | open · strict xfail / — |
| H1-RESTORE-002 | 文档化 reset 产物必须可由 restore 完整往返；旧 reset 只生成 `pre-clean-*`，seed 只接受 `pre-demo-*`。 | HP5, HP12 | REL | U | H1 | open · strict xfail / — |
| H1-DEMO-001 | Demo reset 也必须拒绝活动 writer 并按原路径保存同名库；旧 `demo-data.sh` 复制了端口探测与 basename 压平缺陷。 | HP5, HP12 | REL（2 节点） | U | H1 | open · strict xfail / — |
| H1-DEMO-002 | Demo restore 必须验证来源并在失败时保持 live 状态；旧脚本先移动 live，再接受损坏 source。 | HP5, HP12 | REL | U | H1 | open · strict xfail / — |

## H2：事务、幂等与外部副作用

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H2-TXN-001 | 工具必须声明副作用类型，写事务由单一 UoW 持有；旧 handler/service 各自 `commit()`。 | HP6, STATUS | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-002 | 同一 ToolInvocation 并发 claim 只能有一个执行者，输家得到 typed replay/claim 结果；旧 SELECT→INSERT 在错误边界外冲突。 | HP6 | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-003 | stable action key 与 canonical request digest 必须分离；同 key 异内容返回 `idempotency_conflict`，旧实现执行第二次。 | HP6 | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-004 | 领域对象、Operation、Evidence、LearningEvent、Invocation 结果必须全有或全无；旧实现分次提交。 | HP6, STATUS | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-005 | 等待 HTTP/搜索时不得持有 SQLite writer；旧未提交 Invocation claim 跨外部 await。 | HP6 | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-006 | 等待标题/压缩模型和取消 child 时不得持有父写事务或形成自锁；旧 finalization 先 flush 后 await。 | HP6 | TXN（2 节点） | U | H2 | open · strict xfail / — |
| H2-TXN-007 | SMTP 接受而 receipt commit 失败必须进入 outbox reconciliation，不能盲重发；旧重试会重复发送。 | HP6 | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-008 | 文件写入与数据库故障必须可回滚或留下耐久 reconciliation；旧实现可留下孤儿文件。 | HP6 | TXN | U | H2 | open · strict xfail / — |
| H2-TXN-009 | 主 Run、子 Run、心跳并发遇锁必须形成可重试耐久工作；旧锁异常直接逃逸或丢 Run。 | HP4, HP6 | TXN | U | H2 | open · strict xfail / — |

## H3：耐久 Runtime、Queue 与子 Agent

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H3-RUN-001 | 审批拒绝必须是耐久事实；旧重启恢复在 decision 缺失时默认批准并执行工具。 | HP4, HP7, STATUS | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-002 | 恢复期间再次崩溃仍须保留上一 checkpoint；旧 resume 一开始就清空。 | HP4, HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-003 | checkpoint 必须区分 current tool 与 remaining calls；旧实现先 pop 当前调用再保存，恢复会跳过。 | HP4, HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-004 | 已提交 queued/no-checkpoint Run 必须可 claim；旧启动 reconcile 将其标为 `process_interrupted` failed。 | HP4, HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-005 | final reply、ChatMessage 与终态必须有稳定 finalization phase/key；旧 kill-point 会丢回复或重放 final。 | HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-006 | final stream 期间到达的 steer 必须被消费或原子转队列；旧终态留下 `applied_at=NULL`。 | HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-007 | reconcile 必须使用 lease/version/CAS，单 Run 只能被一个 worker 领取；旧实现可双恢复。 | HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-008 | planning delegate 必须复用统一 durable child 状态机并在 model wait 前 checkpoint；旧实现是无 checkpoint 的第二套 runtime。 | HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-009 | child 终态与父 `subagent.completed` 投影必须可原子提交/修复；旧分次提交会永久缺父事件。 | HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-010 | 可重试模型错误必须有有界、可观察、耐久重试；旧 child 第一次超时即失败并清 checkpoint。 | HP7 | RUN | U | H3 | open · strict xfail / — |
| H3-RUN-011 | child 与主 Run 必须共享并持久化 model/tool/time/network/cost 预算；旧 child 只存局部工具计数。 | HP7 | RUN | U | H3 | open · strict xfail / — |

## H4：Evidence 与 Competency 事实层

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H4-EVID-001 | 在线投影、Context、工具和 CLI 必须读取 0/1/500/501/10k 完整账本并得到同一 digest；旧在线路径截断 500。 | HP4, HP8, VR13 | EVID（501/10k；500 为 passing 边界） | U | H4 | open · strict xfail / — |
| H4-EVID-002 | submission/quiz/task undo 必须追加 amendment/invalidation，并让旧成功退出 active projection；旧实现只回滚业务表。 | HP4, HP8, VR13 | EVID（3 节点） | U | H4 | open · strict xfail / — |
| H4-EVID-003 | 一次 accepted submission 只能贡献一次主成功；旧 task completion 与 submission 双重计权。 | HP8, VR13 | EVID | U | H4 | open · strict xfail / — |
| H4-EVID-004 | self-report、checkbox、自由文本不能仅凭 `verified/passed` 达到 demonstrated；旧 reducer 只信 outcome。 | HP8, VR13 | EVID（3 节点） | U | H4 | open · strict xfail / — |
| H4-EVID-005 | Artifact 必须使用规范 envelope 指纹并耐久保存文件 bytes；旧实现 body/metadata 二选一且只记路径。 | HP8, VR13 | EVID（2 节点） | U | H4 | open · strict xfail / — |
| H4-EVID-006 | Observation/Artifact 同幂等键异内容必须稳定冲突且原行不变；旧实现静默复用。 | HP8, VR13 | EVID（2 节点） | U | H4 | open · strict xfail / — |
| H4-EVID-007 | `assesses` 必须自动关联 Evidence，且一条行为可映射多个技能；旧模型只有单 competency。 | HP8, VR14 | COMP（2 节点） | U | H4 | open · strict xfail / — |
| H4-EVID-008 | competency 过滤必须在 limit/pagination 前完成；旧实现先取最新 N 条再在 Python 过滤。 | HP8, VR14 | COMP | U | H4 | open · strict xfail / — |
| H4-COMP-001 | 计划私有 competency key 的唯一性必须包含 plan；旧 owner-wide unique 阻止不同计划复用同 key。 | HP8, VR14 | COMP | U | H4 | open · strict xfail / — |
| H4-COMP-002 | edge source/target 两端都必须受 owner/plan Guard；旧计划 A 可连接计划 B 私有节点。 | HP4, HP8, VR14 | COMP（2 节点） | U | H4 | open · strict xfail / — |
| H4-COMP-003 | task link 必须以目标 task 的 plan 为事实来源；旧实现错误信任 Run plan。 | HP4, HP8, VR14 | COMP | U | H4 | open · strict xfail / — |
| H4-COMP-004 | resource link 也必须校验 competency plan；旧实现允许跨计划连接。 | HP8, VR14 | COMP | U | H4 | open · strict xfail / — |
| H4-COMP-005 | 图的每次 mutation 必须推进 durable revision；旧图无版本。 | HP8, VR14 | COMP | U | H4 | open · strict xfail / — |
| H4-COMP-006 | undo 节点不得静默级联删除后来 edge 并留下 committed Operation；旧 FK cascade 破坏依赖审计。 | HP8, VR14 | COMP | U | H4 | open · strict xfail / — |
| H4-SCHEMA-001 | Evidence 工具嵌套输入/输出必须用具名 typed model；旧输出暴露裸 `list/dict`。 | HP8, VR13 | COMP | U | H4 | open · strict xfail / — |
| H4-SCHEMA-002 | Evidence 契约必须 forbid extra fields；旧 schema 静默接受未声明字段。 | HP8, VR13 | COMP | U | H4 | open · strict xfail / — |

## H5：Context、Memory 与 Intervention

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H5-CTX-001 | 全局 Session 的 discussed link 不是读取计划私有状态的权限；旧 assembler 注入 event/quiz/reminder/review/calendar。 | HP4, HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-002 | summary coverage 只能包含模型真实看到的消息；旧 10k 压缩把未进入 30k 字符窗口的消息也标 covered。 | HP4, HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-003 | 长消息必须先完整分块读取再推进 coverage；旧实现截掉前缀仍覆盖整条消息。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-004 | 模型/压缩失败不得推进 coverage；旧 fallback partial summary 被当成功。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-005 | handoff 在 child Session 创建时冻结；旧重复 handoff 用源 Session 后续消息重写。 | HP4, HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-006 | 消息编辑须失效所有派生 Memory scope/source；旧实现漏 original Run、Plan/Global、Message source。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-007 | 编辑保留审计但必须标记 Summary/Snapshot 失效；旧快照无法区分 stale/valid。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-008 | Context manifest 必须区分 retained/dropped source 并记录 reason；旧截断 Markdown 后仍把 dropped 列为 active。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-009 | 预算必须覆盖 system、tool schema、Context、output/tool-result reserve；旧实现只限制 snapshot Markdown。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-010 | Memory 检索必须有 relevance threshold 与 layer quota；旧无关 Session 噪声可挤掉 Global 核心记忆。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-011 | 手工归档再恢复必须保留未来 `expires_at`；旧 restore 无条件清空。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-CTX-012 | manifest 必须追踪 prompt 中 active summary 与 frozen handoff；旧只列 recent messages。 | HP9 | CTX | U | H5 | open · strict xfail / — |
| H5-INT-001 | 活动 Run 中提醒回复必须带唯一耐久 target，不能退化成普通 steer/queue；旧请求丢 target。 | HP4, HP9, STATUS | INT, UI | U | H5 | open · strict xfail / — |
| H5-INT-002 | 一次逻辑 Intervention 的站内/邮件/浏览器 delivery 只能投影一次 Context；旧按 delivery 重复注入。 | HP9 | INT | U | H5 | open · strict xfail / — |
| H5-INT-003 | canonical message/Intervention 映射必须用稳定 ID；旧用 run/title/body/thread 启发式错误合并。 | HP9 | INT | U | H5 | open · strict xfail / — |
| H5-MAIL-001 | 归档计划的 email reply 必须生成只读答复或明确失败回执；旧一端启动 Run、另一端静默阻断。 | HP9 | INT | U | H5 | open · strict xfail / — |
| H5-MAIL-002 | IMAP fetch 必须 `BODY.PEEK[]`，Seen 只能在耐久 reply job 后更新；旧 `(RFC822)` 可提前置 Seen。 | HP9 | INT（另有 commit-before-Seen passing gate） | U | H5 | open · strict xfail / — |
| H5-PRO-001 | failed heartbeat 不得消耗成功冷却；旧只看 Run 创建时间。 | HP9 | INT | U | H5 | open · strict xfail / — |
| H5-PRO-002 | quiet-hours 拒绝必须记录 `next_eligible_at` 且不消耗成功冷却；旧只留下字符串原因。 | HP9 | INT | U | H5 | open · strict xfail / — |
| H5-PRO-003 | Guard 拒绝不得因存在 heartbeat Run 就消耗成功冷却；旧缺 ProactiveDecision 事实。 | HP9 | INT | U | H5 | open · strict xfail / — |

## H6：安全运行边界

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H6-AUTH-001 | 默认只允许 loopback；未认证 non-loopback/server 必须 fail closed，并校验 Host；旧实现可直接启动/路由。 | HP10, STATUS | SEC（2 节点） | U | H6 | open · strict xfail / — |
| H6-CODE-001 | 无真实 sandbox 时 `code_execute` 默认关闭且逐次审批，不能读取宿主或访问网络；旧 `prlimit` 仍拥有宿主权限。 | HP10, STATUS | SEC（4 节点） | U | H6 | open · strict xfail / — |
| H6-TRUST-001 | Web/File/Email 内容必须标成 untrusted，且不能成为隐式写授权；旧外部内容进入模型后可触发持久写。 | HP10 | SEC（3 节点） | U | H6 | open · strict xfail / — |
| H6-WEB-001 | fetch 必须拒绝所有 non-global 地址、防 DNS rebinding、限制 wire/decompressed bytes、精确校验 Content-Type；旧边界均可绕过。 | HP10 | SEC（5 节点） | U | H6 | open · strict xfail / — |
| H6-ENV-001 | 子进程 allowlist 中的 `PATH` 必须是固定可信值，解释器不得按父 PATH 解析；旧实现把父 PATH 原样带入并可被劫持。 | HP10 | SEC | U | H6 | open · strict xfail / — |
| H6-CONFIG-001 | `.env` 更新必须拒绝控制字符、使用安全原子临时文件并可无损往返复杂值；旧实现可注入/跟随 symlink/损坏值。 | HP10 | SEC（3 节点） | U | H6 | open · strict xfail / — |
| H6-REDACT-001 | tool trace、模型观察、RunEvent、Context、诊断错误必须统一脱敏；旧多条路径原样持久化合成 secret。 | HP10 | SEC（4 节点） | U | H6 | open · strict xfail / — |

## H7：前端状态与浏览器契约

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H7-UI-001 | 375/768 冷启动必须可见且可操作地切换 Session，375 必须有独立设置入口；旧 CSS 隐藏控件且底栏无设置。 | HP4, HP11, STATUS | BROWSER（3 断言） | U | H7 | open · strict expected failure / — |
| H7-UI-002 | 实时 steer 的事实顺序必须是原问题→steer→答复；旧 live Run 固定锚在首条 user 后。 | HP11 | UI | U | H7 | open · strict xfail / — |
| H7-UI-003 | 归档计划必须同时清除 current/focus scope；旧 store 可留下 archived stale focus。 | HP11 | UI | U | H7 | open · strict xfail / — |
| H7-UI-004 | 提醒 reply target 必须在 active Run queue 中耐久保存且成功后消费；旧前端/API schema 丢失 target。 | HP9, HP11 | UI, INT | U | H5→H7 | open · strict xfail / — |
| H7-UI-005 | SSE 丢终态/断线后必须有界重连并以 durable REST 对账；旧 `onerror` 空操作使 UI 永久 running。 | HP11 | UI | U | H7 | open · strict xfail / — |
| H7-UI-006 | 可降级设置/统计请求失败不能清空 core workspace；旧 16 路 `Promise.all` 全有或全无。 | HP11 | UI | U | H7 | open · strict xfail / — |

## H8：首启与发布工程

| ID | 不变量与旧实现失败原因 | 来源 | 基线测试 | 优先级来源 | 修复门禁 | 状态 / 修复提交 |
| --- | --- | --- | --- | --- | --- | --- |
| H8-BOOT-001 | fresh/no-key/zero-Session 首启必须先进入模型连接向导，不能持久化一个注定失败的 Run；旧 store 仍打开普通 Home。 | HP12 | REL | U | H8 | open · strict xfail / — |
| H8-REL-001 | 必须有 worktree-aware release builder/runtime-asset packaging 入口；旧仓库没有可调用 builder。其后续验收同时要求产物含已构建前端并可在无 Node 主机启动。 | HP12 | REL | U | H8 | open · strict xfail / — |
| H8-REL-002 | Release 启动入口不得在目标机动态调用 npm；缺 runtime asset 时应返回稳定 `missing_release_asset`。旧 `start.sh` 在 `dist` 缺失时直接运行 npm。 | HP12 | REL | U | H8 | open · strict xfail / — |
| H8-CI-001 | 必须有可执行 release gate：完整候选通过，逐项缺 lint/typecheck、Python audit、历史迁移、Evidence audit、Context 隔离、浏览器、secret scan 或前端构建时精确拒绝；旧仓库无入口。 | HP12 | REL | U | H8 | open · strict xfail / — |

## 41 场景重写登记

[`evidence_scenarios.json`](../tests/fixtures/evidence_scenarios.json) 为原 41 个名称逐项登记了独立的 `setup + action`、字面 `expected_projection`、`expected_audit_ok`、唯一 failure reason code、不变量 ID 和可杀死的 mutant。当前每项状态均为 `pending_rewrite`：它们是 H4 可执行测试的规格输入，不是已经通过的 41 项领域测试。

H0 的 passing contract tests 只证明清单完整、输入/oracle/mutant 互不重复；`H0-COV-001` strict xfail 则持续证明旧 `evaluate_baseline()` 仍只有 27 个输入指纹和 3 个场景专属 oracle。H4 必须把 41 项逐条接入真实 reducer/audit，确保对应 mutant 会触发指定 reason code，才能删除 `H0-COV-001` 并宣称覆盖闭环。

## H0 基线验收记录

- `pytest -q tests/hardening -rxX`：20 passed，113 xfailed，0 XPASS，0 failed。
- `pytest -q tests/hardening --runxfail --tb=no`：20 passed，113 failed；解除登记后 113 个旧实现缺陷节点全部失败。
- `pytest -q -rxX`：146 passed，113 xfailed，0 XPASS，0 failed；Python compileall、pip check、前端生产构建、完整 npm audit 与 production-only npm audit 均通过。
- 真实 Chrome：375/768/1280/1440/2560 各自冷启动；375/768 Session 与 375 设置共 3 项 strict expected failure，五尺寸 shell/overflow/error/read-only gate 均通过。验收脚本必须显式传入 loopback URL 与随机 `H0_BROWSER_FIXTURE_TOKEN`，目标 API 必须且只能含标题为 `H0 synthetic alpha/beta <token>` 的两条合成 Session；报告不保存 Session ID 或标题。
- 三类确定性数据库源位于 [`tests/fixtures/databases/`](../tests/fixtures/databases/README.md)：空库、v1.1.1 全业务库、0/1/500/501/10,000 Evidence 与 10,000 messages 边界库；manifest 固定 hash、schema digest 和行数。
- H0 只建立失败基线和追踪关系。表中所有生产缺陷仍为 open；下一门禁是 H1，M15–M20 继续冻结。
