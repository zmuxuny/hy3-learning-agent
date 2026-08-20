# Agent 工具与运行协议

> 状态说明（2026-08-20）：H2 已验收工具 effect 分类、统一 UoW、请求身份、CAS claim 与外部写 outbox；H3 已验收审批、Run/Queue/child 恢复和完整运行预算；H4 已验收 Evidence/Competency 领域作用域、事实语义与严格嵌套 Schema。Context/提醒线程和应用部署边界仍有阻塞缺陷，逐项以 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md) 为准。

## 设计原则

工具是 Agent 的基础系统调用：输入输出类型明确、能力正交、结果可观察。当前实现为工具注册输入/输出 Schema 与 H2 effect kind，并通过 `GET /api/v1/settings/tools` 暴露；Evidence/Competency 已使用具名嵌套模型并在每层拒绝 extra fields。高层流程由 Hy3 规划；H2/H3 已统一工具事务与 Run 状态机，各触发源仍须完成 H5 的 Intervention 语义，才能称为共享同一长期上下文。

## 48 个已注册工具

`GET /api/v1/settings/tools` 对每个工具返回正式 `input_schema`、`output_schema`、`effect_kind`、`idempotent` 与 `blocking`。`blocking=true` 表示该工具在特定 Guard 条件下可能暂停 Run；是否阻塞仍由本次触发来源、操作字段和审批状态决定。

H2 固定四类执行协议：

| `effect_kind` | 当前数量 | 执行协议 |
| --- | ---: | --- |
| `pure_read` | 17 | 只读取调用者快照；不创建含糊写意图 |
| `database_write` | 21 | 短 CAS claim 后，在一个 UoW 原子提交领域状态、Operation/Evidence/Event 与 invocation 结果 |
| `external_read` | 4 | provider/HTTP/embedding 等等待不持有 SQLite writer；结果再以短事务归档 |
| `external_write` | 6 | 先提交 durable outbox intent，再由独立 dispatcher 执行 SMTP/Web Push/workspace/subprocess 并写 receipt |

SQLite 写协调器会在嵌套 savepoint 前显式建立 physical outer transaction，防止 release 最外层 savepoint 时提前提交。仅 claim/CAS、事件和 receipt 等 DB-only 可重放短回调使用有界退避；整个工具 handler 不会因 `database is locked` 被盲目重跑。

### 计划共创与学习位置

| 工具 | 作用 |
| --- | --- |
| `planning_intake_get` / `planning_intake_update` | 读取或保存已确认事实、1–3 个结构化问题和 AI 的充分性判断 |
| `planning_delegate` | 把最多三个只读规划调查分给独立子 Run，并 join 结论 |
| `plan_proposal_create` | 保存等待用户采用的完整计划提案，不直接创建正式 Plan |

### 通用受限子 Agent

| 工具 | 作用 |
| --- | --- |
| `subagent_spawn` | 启动一个只读受限子 Run；可指定工具白名单，但 v1 只会授予只读能力 |
| `subagent_status` / `subagent_join` | 查询子 Run 状态/输出；join 等待子 Run 结束并返回结构化报告 |
| `subagent_cancel` | 取消由当前 Run 发起的子 Run |
| `study_state_get` | 读取带计划版本的当前阶段/任务、下一步、证据、阻塞、逾期、复习和近期提交快照 |

子 Agent 使用 stable action key/index 派生 child ID，并在 model wait 前保存 Context、messages 与 checkpoint。H3 已让 planning 与通用 child 复用统一 lease/checkpoint/retry/finalize 状态机；终态与父 completion 同事务投影或幂等修复，model/tool/network/elapsed/token/cost 预算与主 Run 共享语义。

### 状态与计划

| 工具 | 作用 |
| --- | --- |
| `profile_get` | 读取个人画像、免打扰和游戏化状态 |
| `plan_list` / `plan_get` | 读取全部计划或焦点计划完整结构 |
| `plan_create` / `plan_patch` | `plan_create` 仅保留为无 Session 的底层能力且同样执行正式计划完整性校验；对话必须走 Intake → Proposal → 用户采用；`plan_patch` 可撤销地修改正式计划 |
| `stage_create` / `task_create` / `task_patch` | 增加阶段/任务，更新任务状态、证据、时间和复习 |
| `learning_event_list` | 检索不可变学习事件 |
| `resource_list` | 按课程、教程、实验、难度和推荐理由读取计划资源 |

### 提交、考核与复习

| 工具 | 作用 |
| --- | --- |
| `submission_create` / `submission_get` / `submission_list` | 保存并读取文字、文件、代码或链接证据 |
| `submission_check` | 保存检查项、分数和反馈；通过后完成任务并更新 XP |
| `quiz_create` / `quiz_get` / `quiz_grade` | 创建测验、读取 Rubric、证据化评分 |
| `review_schedule` / `review_resolve` | 安排下一次复习或主动抽查；完成、延后或取消既有复习，并保留可撤销操作记录 |

### 上下文与记忆

| 工具 | 作用 |
| --- | --- |
| `memory_search` | 按 BM25 + 本地 SimHash 混合相关性、作用域、层、置信度和时间检索确认记忆，并返回 `score_breakdown` 分数分解 |
| `memory_propose` | 创建等待用户确认的长期记忆候选；相同内容强化原记录，`supersedes_id` 用于提出可追溯纠正 |
| `memory_maintain` | 过期短期记忆、带原因归档旧情节、持久化本地向量并刷新计划摘要 |

### 网页、文件、代码和日历

| 工具 | 作用 |
| --- | --- |
| `web_search` / `web_open` | 通过可替换 Provider（DuckDuckGo 主源 + Bing 备选源）搜索公开资料；主源失败/超时/空结果时自动降级并带 `fallback_used` 标记；逐跳校验重定向并核验正文 |
| `resource_save` | 把核验过的具体课程、教程、实验或参考资料保存到计划；记录来源、难度、语言、摘要和适配理由，并支持撤销 |
| `file_list` / `file_read` / `file_write` | 读取工作区；写入先持久化 outbox intent，再以原子替换、fsync、hash receipt 发布 |
| `code_execute` | 通过 durable subprocess intent 运行有超时和输出上限的 Python/Bash；不是安全容器 |
| `calendar_list` / `calendar_create` / `calendar_patch` | 读取、创建和调整个人学习日历 |

### V2 技能图与证据

| 工具 | 作用 |
| --- | --- |
| `competency_create` | 创建明确命名的技能/概念节点；不会根据标题相似度自动合并 |
| `competency_link` | 将技能映射到计划、任务或已策展资源，区分 targets / teaches / assesses / covers |
| `competency_edge` | 建立技能关系；`prerequisite` 与 `part_of` 会做环检测 |
| `competency_graph_get` / `competency_get` | 读取计划范围或单个技能图节点与映射 |
| `evidence_list` | 按计划、任务或技能读取不可变证据观察，并保留 Artifact 来源引用 |

### 通信

| 工具 | 作用 |
| --- | --- |
| `notification_send` | 原子写入连续 Session/站内收件箱，并为可选浏览器或 SMTP 渠道分别建立 outbox action；返回 `session_id` 供追溯 |

SMTP/IMAP 回复令牌和站内深链已有正常路径候选。活动 Run target、多渠道唯一 Intervention、归档计划回执和 IMAP durable ack 仍由 H5-INT-001–003、H5-MAIL-001/002 阻塞，不能保证中断/并发时不会形成错线程或丢回复。

## 统一结果与运行事件

```json
{
  "ok": true,
  "data": {
    "operation_id": "uuid",
    "undo_available": true
  }
}
```

成功数据通过具名 Output Schema，错误使用稳定 typed envelope。H2 执行协调器先从已校验参数生成 canonical request digest，并把它与 stable action key 分开：同键同内容精确重放，同键异内容返回 `idempotency_conflict`。ToolInvocation 用短事务 CAS claim、claim token/version/expiry 围栏旧执行者；数据库写在一个 UoW 中提交领域对象、Operation、Evidence、LearningEvent、RunEvent 与 invocation 结果，外部读/写等待不持有 writer。

外部写在领域 UoW 中只建立 outbox intent。dispatcher 独立 claim action、调用 transport、再写唯一 receipt；外部可能已经接受但 receipt 未提交时，action/invocation/notification 进入 `needs_reconciliation` 并禁止盲重放。workspace action 可以比较目标 hash 自动恢复；SMTP、Web Push 与 subprocess 只能等待人工或 provider 对账。稳定错误还包括可重试的 `database_busy`、`invocation_claim_lost` 与不可自动重放的 `needs_reconciliation`。

对用户可见的目标轨迹包括：

```text
run.started → context.built → assistant.status
→ tool.started → tool.completed
→ subagent.started / subagent.completed
→ approval.required / operation.committed / notification.sent
→ assistant.message → run.completed
```

审批暂停/恢复现在把 decision、answer、note、decided_at、ToolInvocation identity 与 checkpoint 原子持久化；拒绝不会在重启后默认批准，二次中断与 current-tool 边界保留上一份可信 checkpoint。缺失或无法证明的 legacy 审批进入 `needs_reconciliation`。

`idempotent` 由数据库约束、canonical digest、CAS claim 与 fenced finalization 强制；H2-TXN-002/003 和 H4-EVID-006 已关闭。H3 进一步让 child 与主 Run 共享并持久化 model/tool/time/network/token/cost 预算及耗尽 reason。

私有思维链不写入事件；TokenHub 要求的 `reasoning_content` 只在同一 Run 的模型轮次间回填。

## 权限与撤销

- 目标上 `plan_id` 必须由后端强制且不依赖 Prompt；Competency edge/link 已由 H4 从数据库解析真实 scope，Context discussed-link 泄漏仍由 H5-CTX-001 阻塞。
- Session 内的计划创建只能写提案；提案采用 API 幂等地物化正式计划，未采用时数据库中不存在对应 Plan。
- `spawn/status/join/cancel` 与执行层只读白名单已接入统一 durable child 状态机；终态父事件、重试和预算对应的 H3-RUN-008–011 均已关闭。
- 核心任务的目标门槛是可验证 Evidence；H4 eligibility Guard 已限制自由文本/self-report/checkbox 的阶段上限，并保证一次 submission/quiz/task 行为只有一份 primary observation。
- 删除、全局长期记忆和后台改变最终目标需要用户确认；阻塞型审批会暂停 Run 等待批准/拒绝，候选式确认只生成候选不中断运行。
- `Operation` 的数据库 undo 使用 CAS，workspace undo 先提交 outbox intent 并以 forward hash 拒绝覆盖后续用户修改；H2-TXN-008 已关闭。H4 进一步让 Evidence undo 追加 amendment/invalidation 控制事实，并在图节点 undo 前执行 dependency preflight、revision mutation 与 `RESTRICT` FK backstop。
