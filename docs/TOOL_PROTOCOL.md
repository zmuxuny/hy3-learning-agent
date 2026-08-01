# Agent 工具与运行协议

## 设计原则

工具是 Agent 的基础系统调用：输入输出类型明确、能力正交、结果可观察。每个工具同时注册 Pydantic 输入模型和输出模型；输入用于 Function Calling，成功输出在回填模型前再次校验。完整契约通过 `GET /api/v1/settings/tools` 暴露。高层流程由 Hy3 规划；用户消息、后台候选、复习到期和邮件回复共享同一 `AgentRuntime`。

## 32 个已注册工具

### 状态与计划

| 工具 | 作用 |
| --- | --- |
| `profile_get` | 读取个人画像、免打扰和游戏化状态 |
| `plan_list` / `plan_get` | 读取全部计划或焦点计划完整结构 |
| `plan_create` / `plan_patch` | 创建计划；可撤销地修改目标、期限、投入和资源 |
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
| `memory_search` | 按作用域、层、相关性、置信度和时间检索确认记忆 |
| `memory_propose` | 创建等待用户确认的长期记忆候选 |
| `memory_maintain` | 过期短期记忆、归档旧情节并刷新计划摘要 |

### 网页、文件、代码和日历

| 工具 | 作用 |
| --- | --- |
| `web_search` / `web_open` | 通过可替换 Provider 搜索公开资料；逐跳校验重定向并核验正文 |
| `resource_save` | 把核验过的具体课程、教程、实验或参考资料保存到计划；记录来源、难度、语言、摘要和适配理由，并支持撤销 |
| `file_list` / `file_read` / `file_write` | 操作个人 Agent 工作区内的学习文件 |
| `code_execute` | 有超时和输出上限地运行 Python/Bash；不是安全容器 |
| `calendar_list` / `calendar_create` / `calendar_patch` | 读取、创建和调整个人学习日历 |

### 通信

| 工具 | 作用 |
| --- | --- |
| `notification_send` | 默认写站内收件箱，可选浏览器或 SMTP 邮件 |

SMTP 邮件主题携带回复令牌。启用 IMAP 后，未读回复会被路由为 `email_reply` Run，再由同一个 Agent 观察和处理。

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
→ approval.required / operation.committed / notification.sent
→ assistant.message → run.completed
```

私有思维链不写入事件；TokenHub 要求的 `reasoning_content` 只在同一 Run 的模型轮次间回填。

## 权限与撤销

- `plan_id` 是后端作用域，不依赖 Prompt 自觉。
- 核心任务只有在 `submission_check` 通过或提供有效证据后才能完成。
- 删除、全局长期记忆和后台改变最终目标需要用户确认；没有恢复型审批能力时只生成候选并停止。
- `Operation` 保存正向和逆向 Patch。计划、任务、策展资源、测验、日历、提交验收和文件写入可从运行抽屉撤销。
