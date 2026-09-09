# 开发路线图

> 2026-09-10当前交付：应用与评测共同完成。48个真实应用场景、24份三档输出各评3次及8份评分操纵对抗已完成，128次评分与逐例AI复核均归档。三档严格排序22/24；应用复核46通过、2未通过。当前入口为[项目与评测报告](第三阶段项目与评测报告.md)、[评测方法](学习决策评测方法.md)、[完整实验](../evaluation/artifacts/decisionbench-study-20260910/README.md)和[提交清单](第三阶段提交材料清单.md)。E7产品比较暂时停用，旧档案保留；两份已提交项目方案保持原文，[落实差异](第三阶段方案落实差异.md)另列。

## 项目管理规则

- `STATUS.md` 是当前进度事实来源。
- 每个里程碑都有可运行验收流程，不以文件数量判断完成。
- 首个开源版本优先保证真实 Hy3 学习闭环。
- 验证数据必须标明真实调用或固定夹具，不能混淆。
- 新 Git 仓库只在清理后的版本完成验证后初始化。

## 第三阶段：关键决策评测（当前主线）

- [x] 确定四条轨道：Planning / Intervention / Assessment / Revision。
- [x] 确定 Decision Episode、七维 Rubric、Rule + Hy3 Judge、48 Primary + 24 Calibration 的总体方案。
- [x] E0：落地版本化协议、离线包骨架、四轨 Mini Episode 与确定性校验。
- [x] E1：完成临时库、冻结时钟、资源快照、真实 Outbox 协议 + Recording Delivery Sink、Evaluation Model Recorder、四轨 Runtime Mini 与最小 runtime export。
- [x] E2：发布 DecisionEpisode v2，完成通用 Snapshot/Normalizer/Exporter、完整 State Delta、规则包、Hard Gate 和完整性检查。
- [x] E3：完成版本化 Rubric/四轨锚点、标签盲化、结构化 Judge 协议、一次修复、Judge/聚合契约、Rule-first 确定性聚合与原子 CLI；fixed-response 结果保持 non-formal。
- [x] E3.1：以 v3 干净切换修复有效性边界；分离结构无效与行为失败，新增 CaseSpec/JudgeReference、可重建层级轨迹、失败制品、Provider 可审计归因，并关闭精确盲化、Assessment ACCEPT、多实体身份、相对路径隔离与隐私误报缺口。
- [x] E3.1.1：以 v4 干净切换闭合 14 类动作、跨轨/组合/失败尝试、并发子 Agent ordinal 与因果 call、Failure-only 分区、formal 单调继承、Case predicate 语义、隔离 Hard Gate 和 Rules/Aggregate 实现源码摘要。
- [x] E3.1.2：发布 Evaluation Protocol Release 1.0；建立空的生产可信 Benchmark 注册表、完整 Suite/终态闭合、formal 三层语义、唯一活动入口、source bundle、独立 Schema Lock 与 `model-action-declaration-v2`。
- [x] E4（内容与工程验收完成）：48 Primary、24 Calibration及8来源已完成摘要绑定的AI内容裁决，候选已重建；历史真实Primary保留45 Episode + 3 Failure。当前验收内容与工程链；原正式冻结/登记承诺移至E5方法稳定后、E6前。进展见 [E4验收记录](E4验收工作记录.md)。
- [x] E5（必需实验与最低方法目标完成）：S06 机制修复；24 例判别力与 16×5 重复实验，88/88 有效；排序 6/8、Good>Severe 8/8、Critical 联合召回 8/8，结论一致 96.25%、平均标准差 2.243425；浏览器体验、必要修复与 AI 审计完成，详见 [验收记录](E5验收工作记录.md)。Assessment 漏检及 Planning 波动保留；独立人工、反事实、对抗及安全扩展未完成。E6 前置已完成新测试输入、协议/Benchmark 冻结与可信登记，见 [E6记录](E6验收工作记录.md)。
- [x] E6 前置：复核方法、48个新测试输入与AI逐例裁决、协议/Benchmark冻结及可信登记；保留E5原版。
- [x] E6 批次执行与报告：48 Episode、46有效Judge/2失败；逐Episode/七维/四轨汇总、85条AI语义裁决及580项制品自审完成。
- [x] E6冻结批次执行与审计归档完成；模型失败作为评测发现保留。工程补修、方法限制与统计完整性分开报告，见[E6最终记录](E6最终修复与验收记录.md)。
- [x] E7（已完成的补充实验）：产品同输入比较、48槽位Judge重复、受控对照及复算归档；保留历史结果，后续不以产品调优为主线，见[E7范围校正](E7工作与验收记录.md#任务书范围校正与产品改动处置2026-09-09)。
- [ ] E8：报告初稿、结果图表、案例、README、两分钟台本及提交清单已备齐；剩余定稿排版、结果成片和最终提交包检查，远端发布/实际提交未执行。

每个 E 里程碑必须同时提交代码、测试、数据说明和状态更新；Mock 只能验证工程链路，不能替代正式 Hy3 结果。首个开发切片和禁止事项见 [`第三阶段开发交接.md`](第三阶段开发交接.md)。

E0 收口边界：三份 v1 Schema、递归校验 CLI、统一 canonical JSON/SHA-256 和 P/I/A/R 四个手工协议夹具已经完成。

E1 收口边界：四个公开合成 Runtime Mini Fixture 已通过独立 Worker、临时 SQLite、冻结时钟、生产 Agent Runtime/工具、Recorder、Guard/通知/Outbox 和 Recording Sink 导出为当时冻结的 v1 最小投影，且同输入逐字节确定。这里的 `fake_outbox` 是真实 Outbox claim/fence/idempotency/Receipt 协议，只替换最后的 SMTP/Web Push Transport；Agent 看不到模拟 Receipt。固定 stub 只验证工程链路，未调用真实 Hy3，也不是正式结果。

E2 收口边界（历史）：工程里程碑、Episode Schema 和 Benchmark 发布名已经分离。`DecisionEpisode v1` 完全冻结；E2 当时的新 Runtime 只写单一权威 `DecisionEpisode v2`。E3.1/E3.1.1 已先后将活动链路切换到 v3/v4，v1/v2/v3 都只保留读取、校验和工程回归，不建设多个可正式运行的活动栈。DecisionBench v1 名称仍不变。E2 的通用 Collector、严格 JSON/UTC、真实 Snapshot/Delta 和 Operation 归因继续由 v4 复用。

E2/E3/E3.1 的旧结果契约现在只作历史工程回归。Evaluation Protocol Release 1.0 的活动链为 `case-spec-v2 → decision-episode-v4/runtime-failure-v2 → rule-result-v3 → judge-result-v3 → aggregate-result-v3`：Validator 只判断证据完整性；14 类动作由 `model-action-declaration-v2` 严格声明，错误、跨轨、组合和缺失声明行为仍进入评分；轨迹保存准确脱敏上下文、并发因果序和 `parent_call_id`；Failure-only 单列。Case predicate 固定为合取命题，`must_not` 对命题取反。Rule Hard Gate、Critical cap 39、Major cap 69、四轨无 overall、路径级盲化、Capture 非事实源、ACCEPT Operation、多实体语义绑定和 Provider 可审计归因继续保持。Runtime/Rules/Judge/Aggregate 使用保守 source bundle 与干净 Git provenance；独立 Schema Lock 冻结历史和活动契约。执行合规、可信完整 Benchmark 运行与能力结论分层；事后过滤、未注册 Release、终态缺失或错误不能建立 formal。E3.1.2 收口时只运行 fixed-response stub。后续审计修复、E4 候选数据和真实运行已经产出，并按用户要求暂停；方案顺序与剩余交付见 [E4 收口复核](E4候选数据收口与方案复核.md)，真实结果以 STATUS 为准。

## M0：工程基线

- [x] 隔离旧 Git 历史
- [x] 可恢复归档旧代码、Windows 依赖和用户数据
- [x] 冻结产品方向和 Harness 架构
- [x] 重建 Linux Python 与 Node 开发环境
- [x] 统一配置、启动命令和 API 前缀
- [x] 建立自动化测试基线

验收：新环境可启动前后端，健康检查和前端构建通过。

## M1：数据与上下文

- [x] 建立 `owner_id` 隔离字段
- [x] 完整 Plan / Stage / Task 与 Quiz/Evidence 模型
- [x] AgentRun / RunEvent / LearningEvent / Operation 模型
- [x] 分层 Memory / MemoryProposal / ContextSnapshot 模型
- [x] ContextAssembler 与 Markdown 快照
- [x] 记忆来源、置信度、确认、纠正替代链和可恢复归档流程
- [x] Session 长对话压缩、相关性分层检索、过期/归档与计划摘要维护
- [x] Session/Plan 可恢复归档、SessionPlanLink 与显式上下文交接
- [x] 全局紧凑计划索引和关联计划优先组装

验收：可以查看全局画像、计划记忆、原始事件和一次可复现的上下文快照。

## M2：Agent Harness

- [x] Agent Run 生命周期和轮次限制
- [x] 独立 System Prompt 行为契约与真实工具 Schema 注入
- [x] 工具注册、参数校验和统一返回协议
- [x] Hy3 tool calling 适配器
- [x] SSE 运行事件流
- [x] 高风险 Run 暂停—批准—恢复状态机
- [x] 错误分类、call-id 幂等、上下文版本检查点和费用预算
- [x] 计划级工具作用域 Guard，不能只依赖 System Prompt
- [x] 计划共创的只读子 Run 分工、独立 `run_id`、父事件投影与 join
- [x] 通用子 Agent spawn/join/cancel、工具白名单、预算和崩溃恢复

验收：用户说“把今天任务缩短到 20 分钟但不改变本周目标”，界面显示读取、决策、工具调用和变更结果，并可撤销。

## M3：主动学习闭环

- [x] Scheduler / heartbeat
- [x] 状态收集与候选事件生成
- [x] Hy3 主动决策策略
- [x] 冷却时间、免打扰和频率 Guard
- [x] 应用内通知收件箱、可恢复归档与批量整理
- [x] 主动提醒投影到连续 Session、收件箱深链回复与历史通知幂等补链
- [x] 页面运行期间的浏览器通知
- [x] SMTP 邮件发送
- [x] IMAP 邮件回复令牌与 `email_reply` Runtime 路由
- [x] 邮件回复回到原 Session、脱敏配置诊断和 SMTP/IMAP 连接测试

验收：没有用户消息时，心跳发现计划风险，Hy3 决定是否提醒并留下完整决策记录。

## M4：计划与考核闭环

- [x] 对话式 PlanningIntake、结构化提问卡和 AI 充分性判断
- [x] 受限规划子 Agent 与 PlanProposal 显式采用；未采用前不创建正式计划
- [x] 完整计划物化、协调和版本管理
- [x] 公开学习资源搜索、正文核验、课程/教程/实验策展与计划资源清单
- [x] 统一学习位置快照与进度感知教学：依据版本、任务、证据、阻塞、逾期、提交、复习和资源选择下一步
- [x] 简答与追问
- [x] 文件或代码提交、工作区读取与有界执行
- [x] 阶段综合考核的数据、Rubric、提交与验收基元
- [x] 证据化评分、薄弱点和间隔复习
- [x] 基于表现的可撤销计划/阶段/任务调整工具

验收：完成任务后 Agent 主动抽查，评分结果更新计划记忆并安排下一次复习。

## M5：Codex 风格工作台

- [x] 对话优先的响应式工作台与消息内可展开 Run 记录
- [x] Session 原始消息恢复、多轮连续画布与当前 Run 内联投影
- [x] 对话/计划归档列表、恢复入口和计划创建承接卡片
- [x] 用户消息复制、不可变 Revision、同 Session 修订重跑与旧下游上下文排除
- [x] 需求提问卡、规划子 Run 轨迹和计划提案采用/调整 UI
- [x] Agent 事件流与工具详情
- [x] 高风险审批卡片、回答/拒绝/批准与原 Run 恢复
- [x] 纵向计划时间线与个人学习日历工具
- [x] 记忆查看器与上下文来源
- [x] 热力图、XP、等级、连续天数和规则成就墙（技能树仍待做）
- [ ] 技能树基础视图（计划进度环已完成；技能树不属于 1.1 发布阻塞项）

验收：桌面和手机均可完成两条 MVP 流程，AI 行动清晰可见。

## M6：开源发布

- [x] 安装与安全说明
- [x] 测试与验证报告
- [x] 清理本地凭据、数据库、日志和个人数据
- [x] 发布完整应用源码
- [x] 建立版本标签与变更日志（v0.5.0 已完成）

## 后续硬化

- 外部日历双向同步
- 容器级不可信代码沙箱
- AI 个性化徽章图像生成
- 移动推送与更多通知渠道
- 技能树视图（计划进度环已完成）

## M7：检索、耐久运行与交付

这是历史 M7 硬化与交付队列；当前执行顺序已经由 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 的 H0–H8 取代。

> 2026-08-18 复审说明：下列勾选表示对应第一版代码和正常路径曾完成，不表示崩溃恢复、事务一致性和长期产品不变量已经通过。复审重新打开的问题统一迁移到 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md)，不得继续用本节的历史勾选作为发布证据。

其中数据迁移与备份恢复已由 H1 重新验收，事务、幂等、outbox、Operation 基础逆向补偿与 planning delegate checkpoint 已由 H2 重新验收；Run 的恢复/finalization 与 child 预算已由 H3 重新验收，配置写入与脱敏已由 H6 重新验收，发布工程已由 H8 重新验收。下列“恢复”“不回传”等文字仅记录当时目标和正常路径候选。

1. [x] **混合语义检索**：建立 Embedding Provider 接口和本地向量表；关键词/BM25、向量相似度、作用域、置信度与时间衰减分别产出分数，再使用可解释的加权融合或 RRF。用固定问题集验证召回率、跨计划隔离和无 Embedding 时的关键词回退。
2. [x] **可恢复 Run 状态机**：为模型轮次、待执行工具、审批请求和上下文版本保存 Checkpoint；实现 `waiting_approval → queued → running`，启动时扫描并恢复未完成 Run。
3. [x] **幂等与预算**：每个工具调用持久化 `idempotency_key`、输入哈希和提交状态；写工具重复调用返回原结果。H2 已用请求/结果摘要 CAS、统一 UoW 和 physical outer transaction 重新验收写入幂等；Run/child 的 Token、工具时间、网络次数、费用预算与 finalization 已由 H3 门禁验收。
4. [x] **浏览器后台通知**：注册 Service Worker、Notification/Push 订阅和离线点击路由；本地部署先支持页面关闭但服务仍运行时的系统通知，不能把电脑关机描述成可提醒。
5. [x] **通用受限子 Agent**：在现有规划专用子 Run 基础上实现 spawn/status/join/cancel、工具白名单和结构化 Artifact；子 Agent 只接收最小上下文，默认只读，写操作回到主 Agent 审批与提交。
6. [x] **邮件/通知设置页**：凭据写入本机 `.env`（0600 权限、原子替换，不宣称加密 Vault），提供免打扰/频率策略、连接测试与删除凭据；设置 API 永不回传秘密。
7. [x] **发布验收**：冻结版本，复核安装/安全说明，并运行后端、前端、移动端、真实 Web 和可选邮箱验收矩阵。

验收门槛：检索结果可解释且计划隔离；中断进程后 Run 能从最后 Checkpoint 恢复且写操作不重复；审批可暂停并继续；子 Agent 不能绕过主 Agent Guard；公开仓库不含任何密钥或个人学习数据。

当前产品主线已完成“澄清 → 规划分工 → 提案 → 正式计划 → 跟踪/教学 → 消息修订”的正常路径。M7 第一版实现已完成，但复审发现其耐久性和安全门槛尚未真正关闭；后续以 H0–H8 的新证据为准。

> M9–M12 同样是历史版本记录。流式/SSE 对账、队列与 steer 由 H3/H7 重新验收；子 Run 由 H3 重新验收；Memory 恢复、压缩、预算、快照有效性和作用域隔离由 H5 重新验收。以下勾选不构成当前保证。

## M9：对话体验与上下文可视化（0.7.0）

- [x] Codex 式可展开问答/工作记录（计划澄清、运行记忆引用、子 Agent 活动）
- [x] 运行中停止、排队等待、打断并发送；代码块与回答复制
- [x] 模型上下文窗口配置与占用显示（`MODEL_CONTEXT_WINDOW`）
- [x] 记忆亮点：对话内引用、首页概览、记忆页搜索与计数
- [x] 后台上下文防陈旧：恢复时重建、计划存在性校验、孤立计划记忆归档

## M10：Codex 1:1 消息队列与渲染（0.8.0）

- [x] 流式输出：Hy3 `stream=True`，SSE `assistant.delta`/`assistant.reasoning`，前端逐 token 渲染、光标、思考指示，运行中发送键变停止。
- [x] 队列 v2：持久化多条排队消息，composer 上方编辑/排序/删除/立即发送，运行结束后自动逐条发送。
- [x] 中途转向：`steer` 注入当前 Run 上下文，不停止 Agent；Enter=默认行为（设置可配）、Tab=排队。
- [x] 子 Agent 状态归入所属 Run：角色标签可展开查看真实子 Run 报告，并保留停止/取消能力。
- [x] 状态感知：运行/等待/空闲、下次主动检查与侧栏会话状态点；运行状态不再重复占据 composer 上方。
- [x] 审批回答：审批卡输入回答并继续，回答回填模型。
- [x] 实时链路稳定化：SQLite WAL、SSE 进程内队列订阅、事件去重与瞬时锁容错。
- [x] 0.8.1 审查硬化：真实协程取消、并发事件 sequence 串行化、SSE 自动重连、转向消息单次渲染、恢复卡片快照、队列焦点校验与多视口可读性复查。

## M11：桌面工作台协议与 Harness 1.0（1.0.0）

- [x] 依据当前 Codex 桌面端实际渲染协议统一 48rem Thread 主轴、24px composer overhang、16px 消息节奏和克制的桌面外壳。
- [x] `Worked for` 式 Run disclosure、低对比 Run rail、逐操作展开、子 Agent 标签、阻塞审批和历史 Run 懒加载。
- [x] Writing Block 式计划/提案 Artifact：工具栏、正文预览、渐隐裁切、圆形展开和明确提交动作。
- [x] 工具事件持久化有界输入/结果；工具契约同时公开输入 Schema、输出 Schema、`idempotent` 与 `blocking`。
- [x] call-id 幂等语义、主 Run 上下文快照版本、通用子 Run 自有检查点/异常终态/重启专用恢复器。
- [x] 真实 Hy3 `plan_list → profile_get` 冒烟、91 项 pytest、前端构建/依赖审计，以及 375/768/1280/1440/2560 浏览器矩阵。
- [x] 1.0.1 子 Agent 稳定化：SQLite 跨 Run 事件串行/锁重试、独立工具上限、强制最终报告、报告持久化和消息内完整工作记录；真实 Hy3 子 Run 与 23 状态浏览器矩阵通过。

## M12：可解释 Context 与 Memory 生命周期（1.1.0）

- [x] 记忆 proposal 去重与强化，避免模型重试或重复陈述生成平行事实。
- [x] 用户纠正建立新旧替代链；归档改为可恢复软归档，到期/陈旧/孤立均保留原因。
- [x] 检索命中记录使用次数与最近时间，Run 快照保存每条记忆的混合分数分解。
- [x] Session 压缩生成不可变摘要版本，保存覆盖消息、来源 ID 与模型/回退方式，原始消息不删除。
- [x] Run 消息内上线上下文检查器，展示实际来源、Token 估算、命中记忆和送入模型的 Markdown。
- [x] 记忆页提供当前/历史、确认、纠正、归档与恢复，并保持计划/Session 作用域隔离。
- [x] 修复设置链路缺少 PUT 客户端方法；增量迁移、101 项 pytest、生产构建/审计和 22 状态多尺寸浏览器回归通过。

## M12.1：v2 前产品状态审计（1.1.1）

> 本节是 v1.1.1 发布时的实现记录。2026-08-18 复审已经发现其中若干长期运行假设不成立；对应项目必须在 H0 中重新建立失败基线，不能只凭历史勾选关闭。

- [x] 历史正常路径：Session、Plan、Run、Proposal 与 Notification 生命周期。H3-RUN-001–007、H5-MAIL-001 与 H7-UI-003 已关闭。
- [x] 历史正常路径：连续 Session 队列与来源元数据。H3-RUN-004/006/007、H5-INT-001 与 H7-UI-004/005 已关闭。
- [x] 历史正常路径：提醒 Session 投影、收件箱聚合和候选冷却。H5 已用 Intervention、InboundMailJob 与 ProactiveDecision 关闭复审缺口。
- [x] 历史正常路径：计划完成、提交/测验终态和撤销。H2-TXN-004 与 H4-EVID-002–004、H4-COMP-006 均已关闭。
- [x] 历史正常路径：Context scope、消息编辑与摘要。H5 已用 typed provenance、generation fence、完整分块与 snapshot blocks 关闭 H5-CTX-001–012。
- [x] 历史正常路径：子 Agent、SQLite、设置与移动导航。H2-TXN-009、H3-RUN-008–011、H6-CONFIG-001/H6-REDACT-001 与 H7-UI-001 已关闭。
- [x] 历史验证记录：旧 126 项 pytest、生产构建和 23 状态浏览器回归仅是历史证据；H4 已用 41 baseline + 41 mutant 关闭 H0-COV-001，H7 又用独立临时库和首次导航前设 viewport 的五宽 Chrome 门禁重新验收前端。

## 第二大版本：Learning Agent 2.0

2.0 不以继续增加 Agent 工具数量为目标，而是把产品从“会执行计划”升级为“根据证据持续维护学习状态并选择下一项最佳学习行动”。主线包括学习证据账本、技能图、可重建学习者状态、复习引擎、自适应学习动作、Context Pack 2.0、耐久主动队列与学习工作台 2.0。

M13/M14 第一版代码已在 `develop` 落地；2026-08-18 全盘审查曾确认其没有通过长期正确性门禁，随后 H0–H8 已完成对应硬化并关闭 87 个缺陷 ID。M15–M20 继续作为 2.0 后续产品路线，不与当前第三阶段评测混合实施。

H0–H8 的历史硬化顺序为：失败基线 → 迁移/时间/备份 → 事务与 Outbox → 耐久 Runtime → Evidence/Competency → Context/Memory/Intervention → 安全边界 → 前端最小闭环 → 首启和发布工程。外部真人采用验证尚未执行，不能由自动化证据替代。

详细领域模型、M13–M20 依赖、接口边界和最终质量门槛仍见 [`V2_ROADMAP.md`](V2_ROADMAP.md)。路线图描述目标能力；在 `STATUS.md` 或对应里程碑明确标记验收前，不得将其写成现有能力。

外部日历、容器沙箱、原生移动端、多用户和无限写权限子 Agent 不进入 2.0 主线，仍按明确需求单独立项。
