# Personal Learning Harness

> 状态说明（2026-08-20）：本文描述产品目标，并区分已经验收的 H1–H4 边界与后续正常路径候选。迁移/UTC/备份、工具事务/幂等/outbox、耐久 Runtime/Queue/child 与 Evidence/Competency 事实层已完成，累计关闭 50 个缺陷 ID；矩阵剩余 37 个 open ID，下一门禁为 H5，M15–M20 继续冻结。当前阻塞项见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)，修复门禁见 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md)；H6 完成前仍只建议受控 loopback 使用。

## 产品边界

Learning Agent 的产品目标是个人本地部署或经过认证的个人服务器长期运行，不做账号、组织、租户或云端多用户平台。当前服务器认证尚未实现（H6-AUTH-001），所以现有版本只能绑定 loopback；`owner_id=local` 只是本地数据的稳定命名空间，不是认证机制。

Harness 的目标由四层共同实现：System Prompt 定义工作方式，ContextAssembler 选择证据，Function Calling Schema 声明可执行能力，后端 Guard 强制焦点、路径、时间和通知边界。H2 已验收统一 UoW、请求身份、effect 分类和外部副作用围栏，H4 已验收 Evidence/Competency scope 与 eligibility；H5、H6 的 Context/Intervention 和应用部署权限仍有缺口，不能用 Prompt 或页面行为代替后端约束。

## 当前正常路径演示闭环（非耐久保证）

```text
澄清需求 → 规划子 Agent 调研 → 提案确认 → 搜索/核验资源 → 正式计划
        ↓
执行任务 → 提交文字/文件/代码/链接证据
        ↓
读取文件/运行代码 → Agent 验收 → 更新进度与 XP
        ↓
创建测验/安排复习/写入日历 → 主动候选扫描
        ↓
站内提醒（默认）/邮箱 → 用户回复邮件重新进入同一 Runtime
```

这条链路不是后端写死的工作流。Hy3 在每轮观察工具结果后自主选择下一项原子能力，直到完成、需要确认、失败、取消或达到预算。H3 已验收 Run/Queue 恢复；图中的提醒 target、多渠道 Intervention 与 IMAP ack 仍由 H5-INT/MAIL 阻塞。

## Runtime 契约

- `backend/app/runtime/prompt.py`：身份、循环、计划焦点、工具纪律、证据标准、主动触达和安全边界。
- `backend/app/runtime/agent.py`：多轮 Function Calling、结果回填、耐久 retry、取消、SSE 事件和 Session 压缩。模型/压缩/child cancel 等等待不持有 writer；模型/工具前提交版本化 checkpoint，最终消息/output/终态/Queue successor 原子收口。
- `backend/app/runtime/state.py`：统一主/子 Run 的 claim、lease heartbeat、checkpoint、审批、steer、retry、finalize、cancel 与 restart reconcile；无法证明的旧状态进入 `needs_reconciliation`。
- `backend/app/runtime/tasks.py`：按 `run_id` 跟踪当前进程内的主 Run、心跳和子 Run，使停止操作取消真实协程；stable wake key 避免审批 task 收尾窗口丢唤醒或重复启动。
- `backend/app/tools/registry.py`：向 Hy3 注入工具 Schema 并校验成功结果；48 个工具公开并执行 `effect_kind`，以 stable action key、canonical request digest、CAS claim token/version 和 typed retry/conflict 强制幂等。Evidence/Competency 输入输出使用具名严格嵌套模型，并在每层拒绝额外字段。
- `backend/app/runtime/scheduler.py`：先用确定性规则发现到期复习、24 小时内任务和长期停滞，再为有价值的候选启动 Hy3。

## 分层上下文与记忆

| 层 | 内容 | 生命周期 |
| --- | --- | --- |
| Working | 当前 Run 的目标、工具观察和临时决策 | Run 完成后只保留事件，不提升为事实 |
| Conversation | 全量原始消息、版本化 Session 摘要、最近消息窗口、Session 私有记忆 | 目标：只覆盖摘要器真实读取且成功提交的消息；旧实现会遗漏输入或错误推进 coverage（H5-CTX-002–004） |
| Planning | Intake 已确认事实/问题/充分性、提案与规划子 Run 报告 | 绑定 Session；提案显式采用后才成为正式 Plan |
| Session–Plan relation | 创建、讨论、聚焦关系和跨作用域交接摘要 | 目标：永久保留来源且只在显式转场建立；旧 handoff 可被后续消息改写（H5-CTX-005） |
| Event ledger | 计划、任务、提交、评分、提醒和邮件回复事件 | 不可变运行事实流 |
| Evidence ledger (V2 M13) | append-only `EvidenceObservation` fact、Artifact snapshot、Rubric/评价者、来源、技能关联和因果链 | H4 已验收完整账本、amendment/invalidation/reinstatement、完整性审计和全量/增量 digest |
| Episodic | 某次学习表现、阻塞或干预结果 | 相关性检索；90 天后可归档 |
| Plan semantic | 计划目标、进度、当前任务和阻塞摘要 | 目标是严格按 `plan_id` 隔离；旧 global discussed link 会泄漏私有块（H5-CTX-001） |
| Global semantic | 稳定偏好、长期约束和跨计划画像 | Agent 只可提出候选，用户确认后生效 |

检索、归档、Token 预算和 Run 快照均已有正常路径候选，但不是已关闭的不变量。旧 Memory restore 会清空未来 expiry，检索缺阈值/层级配额，预算未覆盖 system/tool/output reserve，manifest 不能区分 retained/dropped，编辑后的 Summary/Snapshot 也没有可靠 stale 标记（H5-CTX-007–011）。SQLite 与 `data/context/*.md` 的投影关系必须在这些门禁修复后再称为可核对事实。

## 工具边界

- 目标 Guard 要求计划焦点 Run 不能读取或修改其他计划的私有数据；Competency edge/link 与 Evidence association 已由 H4 从数据库解析真实作用域，Context discussed-link 泄漏仍由 H5-CTX-001 阻塞。
- 文件工具只能访问 `data/workspace/`；路径穿越会被拒绝。
- Python/Bash 运行有工作目录、环境、时间和输出限制，但当前是**有界进程，不是安全容器**，只适合个人可信代码。
- Web 请求已有初步 HTTP(S)/地址限制，但 DNS rebinding、实际 peer、响应大小和 Content-Type 边界尚未关闭（H6-WEB-001）。
- Web 搜索通过可替换的 Provider 接口执行；旧实现会在 URL 层重新检查重定向，但没有 pin/复核实际连接 peer，也缺 wire/decompressed size 与精确 Content-Type 边界（H6-WEB-001），不能据此宣称 SSRF 边界完整。
- 站内通知是默认渠道，邮箱与 VAPID Web Push 是可选增强。H2 已让 SMTP/Web Push delivery 先落 outbox，并按 action receipt 聚合状态；提醒回同一 Session、多渠道唯一 Intervention、活动 Run target、IMAP ack 和失败冷却仍由 H5 缺陷阻塞。
- 计划、任务、策展资源、测验、日历和文件写入生成 `Operation` 与逆向 Patch。H2 已使 Operation undo 使用 CAS，workspace undo 通过 durable outbox 与 forward hash 防止覆盖后续用户修改；H4 进一步让 Evidence undo 追加控制事实，并以 graph dependency/revision 和 `RESTRICT` FK 防止静默级联。
- 用户消息编辑会保存 Revision 并排除部分旧下游消息；旧实现没有失效全部派生 Memory/Summary/Snapshot/handoff（H5-CTX-006/007），因此不能保证编辑后的 Context 已完全收敛。

H1 的数据维护边界已经使用受控 lifecycle lease、固定的路径/目录描述符/inode、内容摘要复验和 trusted snapshot 回退。restore 在写入前拒绝把目标数据库或安全备份根目录放在 source backup 本身或其子路径中；source backup 位于安全备份根目录下仍是正常布局。路径规范化消除 `.`/`..` 别名但不跟随 symlink；legacy `_write_probe` 也只接受精确的空单列旧残留。该协议不能约束绕过 Runtime/maintenance 的原始 SQLite writer。

H2 在应用写路径上增加统一 UoW 和 SQLite physical outer transaction，防止嵌套 savepoint release 提前提交。SMTP、Web Push、workspace 文件与子进程采用 durable outbox；外部已接受但 receipt 未提交时进入 `needs_reconciliation`。SMTP/Web Push/subprocess 只能由人工或 provider 对账，只有 workspace 能依据本地 hash 自动恢复。Context Markdown 虽以原子替换发布，但它是可从 SQLite 重建的投影，数据库提交后、文件发布前崩溃尚无跨重启 durable recovery。

H3 在其上增加 revision 3 与统一 Run 状态机：主/子 Run 共享 lease/version fence、checkpoint、审批、retry 和完整预算；Queue/late steer/终态 successor 以短 UoW/CAS 仲裁。真实 SIGKILL、双进程 claim、SQLite busy、100 轮语义恢复和 Hy3 双中断演示已通过。SQLite lease 是共享单库执行 fence，不提供多节点 scheduler 或分布式数据库语义。

H4 增加 revision 4 与 Evidence/Competency 事实协议：Evidence、Artifact 和关联由数据库 append-only/scope/hash 约束保护，undo/redo 追加控制事实；全量与增量投影共享 canonical digest，Competency graph 以 owner revision、mutation ledger、dependency 和 scope Guard 维护。41 条 baseline 与 41 个对应 mutant、10k 账本、备份恢复和迁移 SIGKILL 均通过。

## 完整性结论

当前版本已经形成真实可运行的个人学习 Harness 原型：计划、资源、执行、证据、检查、记忆和主动提醒均有正常路径能力；SMTP/IMAP 代码、连续 Session 路由和诊断接口已经存在，真实供应商收发仍依赖本机邮箱凭据。它不是通用操作系统 Agent，也不宣称拥有容器级代码隔离、任意宿主目录权限或多节点分布式调度能力。

截至 2026-08-20，H1–H4 已完成，累计关闭 50 个 defect ID；H4 的 41 条 baseline/41 个 mutant、10k 投影、备份恢复和 revision 4 故障注入已通过。Context、提醒线程、移动导航和应用部署边界仍有阻塞问题，统一按硬化计划 H5–H8 修复；没有真实 SMTP/VAPID 验证，外部安装、连续使用和 7 日留存也必须由真人记录验收。完整结果以 [`STATUS.md`](STATUS.md) 的唯一记录为准。
