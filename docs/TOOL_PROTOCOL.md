# Agent 工具与运行协议

## 设计原则

工具应像代码 Agent 的文件和终端工具一样：数量有限、能力正交、输入输出明确，可以被 Agent 自由组合。高层工作流由 Agent 规划，不为每个页面动作编写专用 Prompt。

## 只读工具

| 工具 | 作用 |
| --- | --- |
| `profile.get` | 获取全局学习画像和通知偏好 |
| `memory.search` | 按作用域、时间和关键词检索记忆 |
| `plan.list` | 列出计划及摘要 |
| `plan.get` | 获取计划、阶段、任务和版本 |
| `event.search` | 查询学习事件、提醒反应和考核历史 |
| `file.read` | 读取用户明确提供的学习文件 |
| `web.search` | 搜索公开课程和技术资料 |
| `calendar.list` | 读取学习时间窗口和日程冲突 |

## 写工具

| 工具 | 作用 | 默认权限 |
| --- | --- | --- |
| `plan.create` | 创建完整计划 | 确认 |
| `plan.patch` | 基于版本执行原子计划修改 | 低风险自动，高风险确认 |
| `task.create` | 创建任务或补救任务 | 可撤销自动执行 |
| `task.patch` | 更新状态、时间、顺序和证据要求 | 可撤销自动执行 |
| `review.schedule` | 安排间隔复习 | 自动 |
| `quiz.create` | 创建主动考核 | 自动 |
| `quiz.grade` | 依据 Rubric 和证据评分 | 自动 |
| `memory.propose` | 创建长期记忆候选 | 自动创建候选 |
| `memory.commit` | 接受候选并写入长期记忆 | 确认 |
| `notification.send` | 写收件箱并发送允许的通知 | 自动，受 Guard 限制 |
| `calendar.upsert` | 创建或调整学习日程 | 确认 |

## 执行工具

| 工具 | 作用 |
| --- | --- |
| `code.run` | 在受限临时目录中运行代码或测试 |
| `submission.inspect` | 安全读取提交文件和元数据 |
| `subagent.spawn` | 启动受限的调研或评测子 Agent |

`code.run` 不允许访问 API Key、应用数据库和宿主机任意路径。文件工具只允许访问上传区或显式授权目录。

## 统一工具结果

```json
{
  "ok": true,
  "tool_call_id": "call_123",
  "summary": "Updated task 8 deadline",
  "data": {},
  "evidence": [],
  "operation_id": "op_456",
  "undo_available": true
}
```

错误同样是模型可观察的结果。Agent 可以修正参数后重试，但运行时限制总轮次、单工具重试次数和 Token 预算。

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
