# Personal Learning Harness

## 产品边界

Learning Agent 是个人本地部署或个人服务器部署的长期运行 Harness，不做账号、组织、租户或云端多用户平台。`owner_id=local` 只是本地数据的稳定命名空间。

Harness 由四层共同组成：System Prompt 定义工作方式，ContextAssembler 选择证据，Function Calling Schema 声明可执行能力，后端 Guard 约束焦点、路径、时间和通知。页面按钮只提交目标，不能绕过 Runtime 直接拼一条固定业务流程。

## 当前完整学习闭环

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

这条链路不是后端写死的工作流。Hy3 在每轮观察工具结果后自主选择下一项原子能力，直到完成、需要确认、失败、取消或达到预算。

## Runtime 契约

- `backend/app/runtime/prompt.py`：身份、循环、计划焦点、工具纪律、证据标准、主动触达和安全边界。
- `backend/app/runtime/agent.py`：多轮 Function Calling、独立工具事务、结果回填、模型超时重试、取消、SSE 事件和 Session 压缩。
- `backend/app/tools/registry.py`：向 Hy3 注入 41 个真实工具输入 Schema，并用 41 个 Pydantic 输出 Schema 校验成功结果；完整双向契约可由 `/api/v1/settings/tools` 检查。
- `backend/app/runtime/scheduler.py`：先用确定性规则发现到期复习、24 小时内任务和长期停滞，再为有价值的候选启动 Hy3。

## 分层上下文与记忆

| 层 | 内容 | 生命周期 |
| --- | --- | --- |
| Working | 当前 Run 的目标、工具观察和临时决策 | Run 完成后只保留事件，不提升为事实 |
| Conversation | 全量原始消息、Session 摘要、最近消息窗口、Session 私有记忆 | 超过阈值后压缩旧消息；原文不删除；切换 Session 后不再检索 |
| Planning | Intake 已确认事实/问题/充分性、提案与规划子 Run 报告 | 绑定 Session；提案显式采用后才成为正式 Plan |
| Session–Plan relation | 创建、讨论、聚焦关系和跨作用域交接摘要 | 永久保留来源；归档不删除；只在显式转场时建立计划 Session |
| Event ledger | 计划、任务、提交、评分、提醒和邮件回复事件 | 不可变事实流 |
| Episodic | 某次学习表现、阻塞或干预结果 | 相关性检索；90 天后可归档 |
| Plan semantic | 计划目标、进度、当前任务和阻塞摘要 | 每次维护刷新；严格按 `plan_id` 隔离 |
| Global semantic | 稳定偏好、长期约束和跨计划画像 | Agent 只可提出候选，用户确认后生效 |

检索综合 BM25 关键词、本地 SimHash 向量（中文双字 + 英文词）、作用域、记忆层、置信度和更新时间，用 RRF 融合并返回可解释分数分解；无向量或无检索词时回退关键词权重排序。上下文有明确 Token 预算，超预算时保留高优先级头部与近期对话尾部。SQLite 保存结构化事实，`data/context/*.md` 保存可读快照。

## 工具边界

- 计划焦点 Run 不能读取或修改其他计划的私有数据。
- 文件工具只能访问 `data/workspace/`；路径穿越会被拒绝。
- Python/Bash 运行有工作目录、环境、时间和输出限制，但当前是**有界进程，不是安全容器**，只适合个人可信代码。
- 所有公开网页请求限制为 HTTP(S)，拒绝 localhost 和 `.local` 地址。
- Web 搜索通过可替换的 Provider 接口执行；页面打开关闭自动重定向并逐跳重新校验目标。代理/TUN 的 `198.18/15` 与 `2001::/32` Fake-IP 只对域名解析兼容，直接 IP 请求仍被拒绝。
- 站内通知始终是默认渠道；邮箱与 VAPID Web Push 是可选增强。自动触达受免打扰、每日上限和冷却时间约束。
- 计划、任务、策展资源、测验、日历和文件写入尽可能生成 `Operation` 与逆向 Patch。
- 用户消息编辑先保存不可变 Revision；旧下游消息退出当前上下文，但旧 Run、快照和 Operation 均保留。

## 完整性结论

当前版本已经形成完整的个人学习 Harness：计划、资源、执行、证据、检查、记忆和主动提醒均有真实执行能力；SMTP/IMAP 代码、连续 Session 路由和诊断接口已完成，真实供应商收发仍依赖本机邮箱凭据。它不是通用操作系统 Agent，也不宣称拥有容器级代码隔离、任意宿主目录权限或生产级崩溃检查点。

后续硬化项不阻塞当前学习闭环：阻塞型 Run 审批的暂停—批准—恢复检查点、进程崩溃续跑、写工具幂等键和费用预算均已实现；从当前只读规划委员会扩展到通用 spawn/join/cancel 子 Agent 仍在推进。
