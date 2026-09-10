# 工具与权限协议

Hy3通过登记的工具读取材料、形成提案或执行操作。工具调用包含结构化参数；返回结果说明实际成功、失败、待批准或被规则拦截的情况。模型声明与实际效果分别记录。

## 工具分工

| 类型 | 典型用途 | 行为要求与实现位置 |
| --- | --- | --- |
| 状态读取 | 查看计划、任务、记忆和学习证据 | 返回当前数据及来源；[tools](../backend/app/tools/) |
| 规划与提案 | 创建可审阅的学习路径 | 新提案等待用户决定；[planning.py](../backend/app/tools/planning.py) |
| 学习与验收 | 处理任务、提交材料及反馈 | 依据任务要求和可见证据记录结论；[learning.py](../backend/app/tools/learning.py) |
| 资源访问 | 搜索、读取网页与工作区材料 | 区分外部材料和可信用户指令；[web.py](../backend/app/tools/web.py)、[workspace.py](../backend/app/tools/workspace.py) |
| 记忆操作 | 保存与更新学习事实 | 保留来源和确认关系；[memory.py](../backend/app/tools/memory.py) |
| 外部投递 | 发送站内或已配置渠道的消息 | 检查授权、频率、免打扰并记录实际投递；[notifications](../backend/app/notifications/)、[outbox.py](../backend/app/outbox.py) |

## 执行与记录

工具注册表与参数契约分别位于[registry.py](../backend/app/tools/registry.py)和[contracts.py](../backend/app/tools/contracts.py)。运行器读取工具返回，继续推理或形成最终反馈。需要批准时保存待处理状态，不能把计划中的动作记成已经执行。

数据库修改记录具体操作与版本；撤销恢复旧业务值，但版本计数继续增加。外部发送记录意图与实际结果，用于处理重复触发或中断后的状态核对。失败和被拦截的尝试同样是可观察记录。

## 评测中的使用

评测同时检查助手尝试了什么以及实际发生了什么。例如，重复通知被拦截表示没有产生第二次投递，但仍可在行动适度上记录多余尝试。工具成功也不能替代内容正确性检查：一次验收写入成功，仍可能接受了实际失败的作品。

七维判据及相关例子见[学习决策评测方法](../学习决策评测方法.md)，运行与数据流见[系统架构](ARCHITECTURE.md)。
