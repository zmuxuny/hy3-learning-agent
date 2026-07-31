# Agent 工具与运行协议

## 设计原则

工具应像代码 Agent 的文件和终端工具一样：数量有限、能力正交、输入输出明确，可以被 Agent 自由组合。高层工作流由 Agent 规划，不为每个页面动作编写专用 Prompt。

工具层只是 Harness 的执行基元，不等于 Harness 本身。`AgentRuntime` 负责目标继承、上下文组装、多轮规划、错误观察、停止条件和事件记录；Scheduler、用户消息与任务事件都复用该 Runtime。页面按钮不得绕过 Runtime 拼装一条固定工具链。

## 当前已注册工具

名称与实际传给 Hy3 的 Function Calling 名称一致。

### 只读

| 工具 | 作用 |
| --- | --- |
| `profile_get` | 获取全局学习画像和通知偏好 |
| `plan_list` | 列出计划及摘要 |
| `plan_get` | 获取计划、阶段、任务和版本 |
| `quiz_get` | 获取测验题目、Rubric 和待评分状态 |

### 写入与触达

| 工具 | 作用 | 默认权限 |
| --- | --- | --- |
| `plan_create` | 创建包含阶段和任务的完整计划 | 显式用户请求；记录可撤销操作 |
| `task_patch` | 更新任务状态、截止时间、时长、证据和复习时间 | 低风险可撤销；核心任务受证据门槛约束 |
| `review_schedule` | 安排间隔复习或主动抽查 | 自动 |
| `quiz_create` | 创建基于证据的测验 | 自动 |
| `quiz_grade` | 依据 Rubric 和证据评分并安排复习 | 自动；记录可撤销操作 |
| `memory_propose` | 创建等待用户确认的长期记忆候选 | 自动创建候选，不直接提升为长期事实 |
| `notification_send` | 写收件箱并按配置投递邮件/浏览器通知 | 自动，受 Guard 限制 |

## 规划中但尚未注册

| 工具 | 作用 |
| --- | --- |
| `memory_search` / `event_search` | 按作用域、时间和关键词检索记忆与学习事件 |
| `file_read` / `submission_inspect` | 读取用户明确授权的学习文件和提交证据 |
| `web_search` | 搜索公开课程和技术资料 |
| `calendar_list` / `calendar_upsert` | 读取学习时间窗口并在确认后调整日程 |
| `memory_commit` | 用户确认后提升长期记忆候选 |
| `plan_patch` / `task_create` | 原子修改计划或创建补救任务 |
| `code_run` | 在受限临时目录中运行代码或测试 |
| `subagent_spawn` | 启动受限的调研或评测子 Agent |

`code.run` 不允许访问 API Key、应用数据库和宿主机任意路径。文件工具只允许访问上传区或显式授权目录。

## 统一工具结果

当前 Runtime 的实际结果信封是：

```json
{
  "ok": true,
  "data": {
    "operation_id": "op_456",
    "undo_available": true
  }
}
```

`tool_call_id`、工具名和完整结果记录在 `tool.completed` RunEvent 外层。错误返回 `{"ok": false, "error": "..."}`，同样会回填给模型成为可观察结果。Agent 当前可以在 Run 最大轮次内修正参数后重试；单工具重试上限、Token/费用预算和崩溃检查点仍属于完整 Harness 的待实现能力。

## 可撤销操作

每个写工具在事务中记录：操作者、触发源、修改前后版本、正向 Patch、逆向 Patch、关联 `run_id` 和撤销状态。撤销本身也是新操作，不能删除原审计记录。

## 计划 Patch 示例

```json
{
  "plan_id": 3,
  "expected_version": 12,
  "operations": [
    {
      "op": "replace",
      "path": "/tasks/8/due_at",
      "value": "2026-08-02T20:00:00+08:00"
    },
    {
      "op": "add",
      "path": "/stages/2/tasks/-",
      "value": {
        "title": "异步上下文管理补救练习",
        "kind": "exercise"
      }
    }
  ],
  "reason": "用户连续两次未通过异步编程抽查"
}
```

版本不匹配时工具拒绝写入，Agent 必须重新读取计划后再决策。
