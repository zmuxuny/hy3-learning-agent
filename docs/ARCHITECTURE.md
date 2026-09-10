# 系统架构与数据流

Learning Agent由Vue工作台、FastAPI服务、Hy3决策运行器和SQLite学习状态组成。对话请求与后台学习事件进入同一运行流程；评测在独立环境中记录和分析这些决策。

![整体架构](../assets/proposal/architecture/learning-agent-system-architecture.svg)

## 一次请求的数据流

1. 用户消息或学习事件触发运行，系统分配运行标识并读取相关计划。
2. 上下文组装器收集画像、当前任务、近期事件、提交证据和相关记忆。
3. Hy3根据当前材料生成解释、澄清问题或工具调用。
4. 工具检查参数及操作权限，执行后返回实际结果；运行器将结果继续交给模型。
5. 对话、工具结果、审批事实与状态变化写入持久化记录，前端通过事件流更新展示。

| 组件 | 负责的内容 | 实现位置 |
| --- | --- | --- |
| 前端工作台 | 对话、计划、收件箱、学习记忆、设置与运行状态 | [frontend/src/views](../frontend/src/views/)、[stores](../frontend/src/stores/) |
| 决策运行器 | 模型调用、工具循环、等待批准、停止与恢复 | [runtime/agent.py](../backend/app/runtime/agent.py)、[checkpoints.py](../backend/app/runtime/checkpoints.py) |
| 上下文与来源 | 按当前任务组织事实、材料与来源 | [context/assembler.py](../backend/app/context/assembler.py)、[provenance.py](../backend/app/context/provenance.py) |
| 学习工具 | 提案、任务、提交证据、提醒与修改 | [tools](../backend/app/tools/) |
| 主动检查 | 根据学习状态及事件判断支持时机 | [runtime/proactive.py](../backend/app/runtime/proactive.py)、[scheduler.py](../backend/app/runtime/scheduler.py) |
| 数据层 | 计划、任务、记忆、证据、运行及操作记录 | [models](../backend/app/models/)、[db](../backend/app/db/) |
| 评测层 | 隔离运行、证据导出、质量评分与实验复算 | [evaluation](../evaluation/README.md) |

## 持续状态与操作恢复

计划、任务与学习证据跨对话保存。运行记录保留状态、执行位置与工具结果，用于在等待批准、接口故障或进程重启后识别已经完成的步骤。修改操作保存可核对的信息；撤销恢复业务值，同时继续更新版本计数。

外部消息通过待发送记录和投递结果追踪。网络等待与数据库写入分开处理，重复触发需检查已经存在的操作和效果。具体工具行为见[工具协议](TOOL_PROTOCOL.md)，服务访问与外部内容处理见[安全说明](../SECURITY.md)。

## 评测与应用的连接

评测用例提供合成用户条件、固定时间和资源，在仓外副本与临时数据库运行同一产品逻辑。导出的证据包含用户要求、助手内容、工具尝试、拦截结果和状态差异。评测器以此判断决策质量；它不在学习者使用过程中自动改写产品行为。

完整方法和实验见根目录[项目与评测报告](../第三阶段项目与评测报告.md)与[评估方法说明](../学习决策评测方法.md)。
