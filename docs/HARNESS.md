# Personal Learning Harness

> 状态说明（2026-08-18）：本文描述产品目标和正常路径候选，不代表崩溃恢复、事务一致性、长期 Context 或服务器安全已经验收。当前阻塞项见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)，修复门禁见 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md)；完成前仅建议受控 loopback 使用。

## 产品边界

Learning Agent 的产品目标是个人本地部署或经过认证的个人服务器长期运行，不做账号、组织、租户或云端多用户平台。当前服务器认证尚未实现（H6-AUTH-001），所以现有版本只能绑定 loopback；`owner_id=local` 只是本地数据的稳定命名空间，不是认证机制。

Harness 的目标由四层共同实现：System Prompt 定义工作方式，ContextAssembler 选择证据，Function Calling Schema 声明可执行能力，后端 Guard 强制焦点、路径、时间和通知边界。旧实现只有正常路径候选；H2、H4、H5、H6 已证明 Guard、事务和权限仍有缺口，不能用 Prompt 或页面行为代替后端约束。

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

这条链路不是后端写死的工作流。Hy3 在每轮观察工具结果后自主选择下一项原子能力，直到完成、需要确认、失败、取消或达到预算。图中的邮件回复、提醒 target、队列恢复和多渠道线程只是正常路径候选；H3-RUN-004/006/007 与 H5-INT/MAIL 已证明它们尚不耐久。

## Runtime 契约

- `backend/app/runtime/prompt.py`：身份、循环、计划焦点、工具纪律、证据标准、主动触达和安全边界。
- `backend/app/runtime/agent.py`：多轮 Function Calling、结果回填、模型超时重试、取消、SSE 事件和 Session 压缩的旧实现候选；当前工具事务、checkpoint 和 finalization 不满足 H2/H3。
- `backend/app/runtime/tasks.py`：按 `run_id` 跟踪当前进程内的主 Run、心跳和子 Run，使停止操作取消真实协程而不只写数据库标记。
- `backend/app/tools/registry.py`：向 Hy3 注入工具 Schema 并校验成功结果的旧实现候选；H4-SCHEMA-001/002 已证明 Evidence 嵌套项仍有裸 `list/dict` 与 extra-field 缺口，`idempotent` / `blocking` 声明也不能替代 H2/H3 的后端状态机。
- `backend/app/runtime/scheduler.py`：先用确定性规则发现到期复习、24 小时内任务和长期停滞，再为有价值的候选启动 Hy3。

## 分层上下文与记忆

| 层 | 内容 | 生命周期 |
| --- | --- | --- |
| Working | 当前 Run 的目标、工具观察和临时决策 | Run 完成后只保留事件，不提升为事实 |
| Conversation | 全量原始消息、版本化 Session 摘要、最近消息窗口、Session 私有记忆 | 目标：只覆盖摘要器真实读取且成功提交的消息；旧实现会遗漏输入或错误推进 coverage（H5-CTX-002–004） |
| Planning | Intake 已确认事实/问题/充分性、提案与规划子 Run 报告 | 绑定 Session；提案显式采用后才成为正式 Plan |
| Session–Plan relation | 创建、讨论、聚焦关系和跨作用域交接摘要 | 目标：永久保留来源且只在显式转场建立；旧 handoff 可被后续消息改写（H5-CTX-005） |
| Event ledger | 计划、任务、提交、评分、提醒和邮件回复事件 | 不可变运行事实流 |
| Evidence ledger (V2 M13) | `EvidenceObservation`：提交/验收/测验/带证据完成的结构化观察、Rubric、来源和因果链 | 目标为追加式和 digest 稳定；旧 undo、时间往返、截断和幂等冲突仍未修（H1/H4） |
| Episodic | 某次学习表现、阻塞或干预结果 | 相关性检索；90 天后可归档 |
| Plan semantic | 计划目标、进度、当前任务和阻塞摘要 | 目标是严格按 `plan_id` 隔离；旧 global discussed link 会泄漏私有块（H5-CTX-001） |
| Global semantic | 稳定偏好、长期约束和跨计划画像 | Agent 只可提出候选，用户确认后生效 |

检索、归档、Token 预算和 Run 快照均已有正常路径候选，但不是已关闭的不变量。旧 Memory restore 会清空未来 expiry，检索缺阈值/层级配额，预算未覆盖 system/tool/output reserve，manifest 不能区分 retained/dropped，编辑后的 Summary/Snapshot 也没有可靠 stale 标记（H5-CTX-007–011）。SQLite 与 `data/context/*.md` 的投影关系必须在这些门禁修复后再称为可核对事实。

## 工具边界

- 目标 Guard 要求计划焦点 Run 不能读取或修改其他计划的私有数据；旧 Context 与 Competency 路径已有跨计划失败基线（H4-COMP-002–004、H5-CTX-001）。
- 文件工具只能访问 `data/workspace/`；路径穿越会被拒绝。
- Python/Bash 运行有工作目录、环境、时间和输出限制，但当前是**有界进程，不是安全容器**，只适合个人可信代码。
- Web 请求已有初步 HTTP(S)/地址限制，但 DNS rebinding、实际 peer、响应大小和 Content-Type 边界尚未关闭（H6-WEB-001）。
- Web 搜索通过可替换的 Provider 接口执行；旧实现会在 URL 层重新检查重定向，但没有 pin/复核实际连接 peer，也缺 wire/decompressed size 与精确 Content-Type 边界（H6-WEB-001），不能据此宣称 SSRF 边界完整。
- 站内通知是默认渠道，邮箱与 VAPID Web Push 是可选增强。提醒回同一 Session、多渠道唯一事实和冷却是目标契约；活动 Run target、delivery 去重、IMAP ack 和失败冷却仍由 H5 缺陷阻塞。
- 计划、任务、策展资源、测验、日历和文件写入尽可能生成 `Operation` 与逆向 Patch。
- 用户消息编辑会保存 Revision 并排除部分旧下游消息；旧实现没有失效全部派生 Memory/Summary/Snapshot/handoff（H5-CTX-006/007），因此不能保证编辑后的 Context 已完全收敛。

## 完整性结论

当前版本已经形成真实可运行的个人学习 Harness 原型：计划、资源、执行、证据、检查、记忆和主动提醒均有正常路径能力；SMTP/IMAP 代码、连续 Session 路由和诊断接口已经存在，真实供应商收发仍依赖本机邮箱凭据。它不是通用操作系统 Agent，也不宣称拥有容器级代码隔离、任意宿主目录权限或多节点分布式调度能力。

2026-08-18 审查确认：阻塞审批、进程恢复、call-id 幂等和子 Agent 检查点虽然已有第一版实现，但尚未满足故障注入和长期运行门槛；Evidence、Context、提醒线程、移动导航和服务器安全也存在阻塞问题。这些项目会阻塞 V2 Alpha 和无人值守使用，统一按硬化计划 H0–H8 修复。
