# Agent 工具与运行协议

## 设计原则

工具是 Agent 的基础系统调用：输入输出类型明确、能力正交、结果可观察。每个工具同时注册 Pydantic 输入模型和输出模型；输入用于 Function Calling，成功输出在回填模型前再次校验。完整契约通过 `GET /api/v1/settings/tools` 暴露。高层流程由 Hy3 规划；用户消息、后台候选、复习到期和邮件回复共享同一 `AgentRuntime`。

## 41 个已注册工具

`GET /api/v1/settings/tools` 对每个工具返回正式 `input_schema`、`output_schema`、`idempotent` 与 `blocking`。`blocking=true` 表示该工具在特定 Guard 条件下可能暂停 Run；是否阻塞仍由本次触发来源、操作字段和审批状态决定。

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

子 Agent 的模型轮次与工具调用分别有界；工具结果进入模型上下文前压缩，RunEvent 保留可展开的结构化预览。若所有调查轮次都用于调用工具，Runtime 会追加一次不暴露工具的总结轮，保证正常完成的子 Run 形成报告。SQLite 部署把跨 Run 事件写入串行化并对短暂锁冲突重试；成功报告或失败说明持久化在子 `AgentRun.output`，同时投影回父 Run。

### 状态与计划

| 工具 | 作用 |
| --- | --- |
| `profile_get` | 读取个人画像、免打扰和游戏化状态 |
| `plan_list` / `plan_get` | 读取全部计划或焦点计划完整结构 |
| `plan_create` / `plan_patch` | `plan_create` 仅保留为无 Session 的底层兼容能力；对话必须走 Intake → Proposal → 用户采用；`plan_patch` 可撤销地修改正式计划 |
| `stage_create` / `task_create` / `task_patch` | 增加阶段/任务，更新任务状态、证据、时间和复习 |
| `learning_event_list` | 检索不可变学习事件 |
| `resource_list` | 按课程、教程、实验、难度和推荐理由读取计划资源 |

### 提交、考核与复习

| 工具 | 作用 |
| --- | --- |
| `submission_create` / `submission_get` / `submission_list` | 保存并读取文字、文件、代码或链接证据 |
| `submission_check` | 保存检查项、分数和反馈；通过后完成任务并更新 XP |
| `quiz_create` / `quiz_get` / `quiz_grade` | 创建测验、读取 Rubric、证据化评分 |
| `review_schedule` | 安排下一次复习或主动抽查 |

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
| `file_list` / `file_read` / `file_write` | 操作个人 Agent 工作区内的学习文件 |
| `code_execute` | 有超时和输出上限地运行 Python/Bash；不是安全容器 |
| `calendar_list` / `calendar_create` / `calendar_patch` | 读取、创建和调整个人学习日历 |

### 通信

| 工具 | 作用 |
| --- | --- |
| `notification_send` | 先把主动提醒写入计划对应的连续 Session，再投影到站内收件箱；可选浏览器或 SMTP 邮件，返回 `session_id` 供追溯 |

SMTP 邮件主题携带回复令牌。启用 IMAP 后，未读回复会被路由为 `email_reply` Run，并作为用户消息接在原提醒后由同一个 Agent 观察和处理。站内收件箱也通过同一 Session 深链回复，不建立第二套通知上下文。

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

成功数据必须通过对应 Output Schema；错误统一为 `{"ok": false, "error": "...", "retryable": false}`，并作为 tool message 回填给模型。每次工具在独立数据库 Session 中执行，回滚不会污染主 Runtime。对用户可见的轨迹包括：

```text
run.started → context.built → assistant.status
→ tool.started → tool.completed
→ subagent.started / subagent.completed
→ approval.required / operation.committed / notification.sent
→ assistant.message → run.completed
```

带 `blocking: true` 的审批会在 `approval.required` 事件后把 Run 停在 `waiting_approval` 并持久化待批工具与参数；`POST /agent/runs/{id}/approval` 批准后从检查点恢复并执行原工具，拒绝后把拒绝结果作为 tool message 回填给模型继续调整。候选式确认（如 `memory_propose`）不阻塞 Run。

写工具带有 `idempotent` 契约标记：`run_id + 工具名 + provider call_id + 参数哈希` 生成 `idempotency_key`；同一个 provider 调用重放会返回首次提交结果并带 `replayed: true`，但不会错误吞掉模型后续有意发起的同参数新调用。审批中的调用记录为 `pending_approval`，只有批准执行后才转为 `committed`。每次模型调用和工具调用都计入 `budget_usage`，超限时产生 `run.budget_exceeded` 可观察事件后安全停止。

私有思维链不写入事件；TokenHub 要求的 `reasoning_content` 只在同一 Run 的模型轮次间回填。

## 权限与撤销

- `plan_id` 是后端作用域，不依赖 Prompt 自觉。
- Session 内的计划创建只能写提案；提案采用 API 幂等地物化正式计划，未采用时数据库中不存在对应 Plan。
- 规划子 Run 与通用子 Agent 共用受限执行器；`spawn/status/join/cancel` 已开放，但只能看到显式只读白名单和最小上下文，不写主 Session 消息或业务状态。
- 核心任务只有在 `submission_check` 通过或提供有效证据后才能完成。
- 删除、全局长期记忆和后台改变最终目标需要用户确认；阻塞型审批会暂停 Run 等待批准/拒绝，候选式确认只生成候选不中断运行。
- `Operation` 保存正向和逆向 Patch。计划、任务、策展资源、测验、日历、提交验收和文件写入可从操作记录撤销。
