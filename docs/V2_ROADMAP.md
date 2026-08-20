# Learning Agent 2.0 路线图

更新时间：2026-08-19（Asia/Shanghai）
目标版本：2.0.0
当前基线：1.1.1

> 2026-08-21 硬化门禁：`develop` 中的 M13/M14 仍是实现候选。H1–H7 已完成，累计关闭 83 个缺陷 ID，矩阵剩余 4 个 open ID，下一门禁是 H8；M13 Evidence 事实层、M14 后端图协议与最小只读前端、M17/M18 所需的 Context/Intervention 地基及应用安全边界已通过对应门禁，learner state/FSRS/自适应动作仍冻结。M15–M20 继续暂停，直到 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 的 H0–H8 全部完成。逐项事实见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)，下方“M13/M14 当前实现状态”只记录已有代码范围，不构成发布完成声明。

H4 场景矩阵为 124 passed，覆盖 41 条 production baseline 与 41 个对应 mutant；H5 又覆盖 10,000 消息、来源图、完整 PromptEnvelope、逻辑 Intervention、IMAP job/ack 与真实进程恢复。完整 pytest、前端结果及历史快照见 [`STATUS.md`](STATUS.md)；这些自动化证据不替代真实 SMTP/IMAP/VAPID、外部安装、连续学习闭环和 7 日留存验证。

## 1. 版本定义

Learning Agent 2.0 的目标不是继续增加零散工具，而是把现有“能执行学习计划的个人 Agent Harness”升级为“能够根据学习证据持续维护学习状态，并选择下一项最佳学习行动的个人学习系统”。

一句话目标：

> Agent 不只知道计划做到哪里，还能说明用户学会了什么、依据是什么、什么可能正在遗忘，以及为什么现在推荐这一步。

2.0 的完整闭环是：

```text
对话 / 阅读 / 练习 / 测验 / 代码 / 文件提交
                    ↓
不可变学习记录与证据
                    ↓
可重建的学习者状态投影
                    ↓
技能图、掌握阶段、错误认识与遗忘风险
                    ↓
有来源、有预算的 Context Pack
                    ↓
候选学习动作 → Agent 选择 → Guard 校验
                    ↓
讲解 / 示例 / 练习 / 考核 / 补救 / 复习 / 等待
                    ↓
新证据进入下一轮状态更新
```

## 2. 已冻结的产品原则

### 2.1 四类状态必须分开

- **计划进度**回答“原定工作完成了多少”。
- **学习者状态**回答“哪些能力有证据支持、证据是否独立且仍然新鲜”。
- **复习状态**回答“某个可提取知识单元现在被遗忘的风险”。
- **用户记忆**保存偏好、约束、经历和经确认的长期事实。

任务打勾不能直接推导为技能掌握；用户记忆也不能被当成掌握度数据库。

### 2.2 事件是事实，状态是投影

- 原始对话、提交、答案、Rubric、工具结果和用户反馈形成不可变事实。
- 掌握阶段、置信度、薄弱点和下一步建议是带算法版本的派生投影，可以从事实重建。
- 大模型可以评估证据并提出结构化观察，但不能直接覆盖掌握状态。
- 修订或撤销通过追加失效/替代记录完成，不改写历史证据。

### 2.3 不展示虚假的精确度

界面默认使用 `未知 → 已接触 → 练习中 → 已证明 → 已保持` 五个阶段，并同时展示证据数量、最近证据、独立程度和置信范围。内部可以保存连续分值用于排序，但不得把一次模型评分渲染成“掌握度 87%”之类的确定事实。

### 2.4 复习模型只处理适合复习的单元

FSRS 一类算法适用于概念、规则、API 用法等可独立提取的 `ReviewItem`；架构设计、调试、游泳动作等复合技能应通过代码任务、项目、口头解释或实际表现重新验证，不能被压缩成闪卡。

### 2.5 Agent 决策必须可解释

每项主动干预和下一步推荐必须保存：

- 目标技能或知识单元；
- 使用的证据、计划状态与限制；
- 候选动作及选择理由；
- 预期耗时和预期产出；
- 要求产生的下一份证据；
- 当时的 Context Pack、策略版本和模型版本。

### 2.6 单用户边界保持不变

2.0 继续面向个人电脑或个人服务器，不建设账号、组织、租户和团队权限系统。所有新增表仍保留 `owner_id`，只是为了数据边界清晰和未来迁移，不把多用户作为本轮工作。单用户不等于无认证：本机模式默认只监听 loopback，个人服务器模式必须具备单用户认证并在缺失时拒绝启动。

## 3. 2.0 非目标

以下内容不进入 2.0 主线，不能挤占学习闭环：

- 云端多租户、团队协作和商业计费；
- 原生 iOS/Android 客户端；
- 无限制写权限的子 Agent 群；
- 为追求动画而重做整套视觉语言；
- 完整 xAPI/CASE 合规或外部 LMS 集成；
- 外部日历双向同步；
- 面向不可信租户的通用代码执行平台；
- 只增加徽章、皮肤或数值的游戏化大版本。

备份恢复与本地部署可靠性属于 2.0；容器沙箱、移动推送、外部日历可在 2.x 单独立项。

## 4. 目标领域模型

### 4.1 技能与知识图

新增：

- `Competency`：稳定技能或概念节点，包含类型、描述、作用域、状态和版本。
- `CompetencyEdge`：`prerequisite`、`part_of`、`related_to`、`equivalent_to` 关系。
- `PlanCompetencyLink`：计划希望达到的技能及目标阶段。
- `TaskCompetencyLink`：任务训练或考核哪些技能，区分 `teaches` 与 `assesses`。
- `ResourceCompetencyLink`：资源覆盖的技能、深度和适用阶段。

约束：

- `prerequisite` 和 `part_of` 必须通过环检测；
- 同一计划内可自动建议节点，计划采用时一并物化；
- 跨计划合并为同一全局技能必须产生候选，不能靠标题相似度静默合并；
- 图的每次结构修改都保存版本和 Operation。

### 4.2 学习证据账本

在现有 `LearningEvent` 之上建立结构化、不可变的 `EvidenceObservation`：

- 来源：quiz、submission、code_run、file、conversation、self_report、manual；
- 来源 ID、Run、Session、Plan、Task 和 Competency；
- Rubric 快照与评价者信息；
- outcome、归一化得分、是否正确；
- assistance_level：独立、轻提示、强提示、直接给答案；
- transfer_level：同题、变式、迁移任务；
- occurred_at、recorded_at、schema_version；
- correlation_id、causation_id、idempotency_key；
- supersedes_id / invalidated_at / invalidation_reason。

自述可以说明信心和阻塞，但默认不能单独把能力推进到“已证明”。任务完成只表示计划动作完成，是否形成掌握证据由任务映射和验收结果共同决定。

### 4.3 学习者状态投影

新增 `LearnerCompetencyState`：

- `stage`：unknown / exposed / practicing / demonstrated / retained；
- `confidence`：投影可信度，不等同掌握概率；
- `independence`、`transfer`、`retention` 三个维度；
- success/failure/evidence 计数；
- last_observed_at、last_success_at、next_review_at；
- evidence_watermark、algorithm_version、state_digest；
- explanation：结构化 reason codes 与关键证据 ID。

状态更新由确定性 reducer 完成。同一事件集合和同一算法版本必须产生相同 digest；更换算法时并行生成新版本，验证后再切换当前投影。

新增 `MisconceptionHypothesis`，生命周期为 proposed / supported / resolved / dismissed。它必须引用证据，模型不能仅凭一次对话把猜测写成长期事实。

### 4.4 复习状态

新增：

- `ReviewItem`：绑定原子知识单元或具体提取提示；
- `ReviewAttempt`：问题、回答、评分、提示程度、耗时与证据；
- `ReviewState`：difficulty、stability、retrievability、due_at、scheduler_version；
- `ReviewBatch`：一次用户可完成的复习会话。

采用 `ReviewScheduler` Provider 接口。首个 Provider 可使用固定版本 FSRS；宽泛技能仍走表现性考核，不进入 FSRS 队列。

### 4.5 学习动作

新增 `LearningAction`：

- 类型：explain / example / practice / quiz / code_exercise / review / remediate / plan_adjust / wait；
- 目标 Competency 与触发证据；
- 预估分钟、难度、前置条件；
- 预期产生的证据；
- rationale、reason_codes、策略版本；
- proposed / accepted / active / completed / cancelled / superseded 生命周期；
- 来源 Run 与执行 Session。

确定性候选生成器负责找出可行集合，主 Agent 负责结合用户意图、时间和上下文排序选择。Guard 负责作用域、自治等级、预算与写入边界。

## 5. Context 与 Memory 2.0

### 5.1 Context Pack 成为规范对象

`ContextSnapshot.markdown` 保留兼容，但事实来源改为结构化 `ContextPack`：

```text
ContextPack
├── identity_and_preferences
├── session_focus_and_handoff
├── plan_state
├── learner_state
├── prerequisite_gaps
├── recent_evidence_and_errors
├── due_reviews
├── candidate_actions
├── relevant_memory
├── pending_approvals
└── policies_and_budget
```

每个区块包含来源、版本、生成时间、有效时间、优先级和估算 Token。Markdown 只是给模型的确定性渲染结果。

### 5.2 分层职责

| 层 | 保存内容 | 生命周期 | 是否直接送入模型 |
|---|---|---|---|
| LearningEvent / EvidenceObservation | 已发生事实 | 不可变，允许追加失效记录 | 只检索相关片段 |
| LearnerCompetencyState | 可重建学习状态 | 随证据更新、带算法版本 | 当前焦点相关状态 |
| Memory | 偏好、约束、经历、长期事实 | 候选、确认、纠正、归档 | 相关性检索后进入 |
| Plan | 目标与预期工作 | 版本化、可撤销修改 | 当前计划或全局摘要 |
| SessionSummary | 连续对话压缩 | 不可变版本 | 当前 Session 相关摘要 |
| ContextPack / Snapshot | 单次 Run 输入 | 不可变审计快照 | 是 |

### 5.3 组装与预算

- 先按 owner、计划和 Session 做硬隔离，再做相关性排序；
- 先保留目标、当前动作、安全策略和最近失败等必需区块，再分配剩余预算；
- 同类证据聚合，关键相反证据不得被摘要掉；
- 保存完整 source manifest、选择/排除 reason code、内容 hash 和 renderer version；
- 相同输入版本允许复用缓存，任一来源版本变化即失效；
- Run 恢复时重新校验焦点和版本，不能盲目重放陈旧的 Context Pack。

### 5.4 重放与比较

提供只读 `shadow replay`：使用历史 Context Pack 和同一工具结果重放模型决策，不提交写操作；可比较原 Run 与重放 Run 的候选动作、工具选择、Token、耗时和最终回答。真实重新调用模型会产生费用，必须由用户显式触发。

## 6. Heartbeat 2.0

### 6.1 两阶段主动系统

```text
轻量扫描器
  → 产生并去重 ProactiveCandidate
  → 优先级、免打扰、冷却与自治级别过滤
  → 建立耐久 AgentJob
  → Hy3 读取计划级 Context Pack
  → wait / remind / assess / reschedule / propose_adjustment
  → 保存 ProactiveDecision 与通知反馈
```

扫描器不直接发送提醒；Agent 也不能绕过通知 Guard。

### 6.2 耐久任务模型

新增 `AgentJob`、`JobAttempt`、`ProactiveCandidate`、`ProactiveDecision`：

- `available_at`、`lease_owner`、`lease_expires_at`；
- attempt、max_attempts、backoff、last_error；
- candidate key、action idempotency key；
- queued / leased / running / succeeded / retry_wait / dead_letter / cancelled；
- context version、policy version、source watermark；
- decision、reason codes、notification/result IDs；
- user response、helpful/dismissed/snoozed 等反馈。

SQLite 使用原子条件更新领取租约。进程在“决定提醒”和“通知提交”之间退出时，恢复后只能得到同一幂等通知，不能重复触达。

### 6.3 自治等级

- `observe`：只记录判断，不触达、不写计划；
- `suggest`：允许站内建议和询问；
- `assist`：允许提醒、抽查、安排复习和可撤销低风险动作；
- `autonomous`：允许更积极的低风险调整，但长期目标、全局记忆和外部敏感动作仍需确认。

默认 `assist`。自治等级不影响审计和撤销能力。

## 7. Harness 2.0 工具边界

新增或升级的核心工具保持原子化：

- `competency_graph_get`
- `competency_get`
- `evidence_list`
- `learner_state_get`（升级 `study_state_get`，保留兼容期）
- `review_queue_get`
- `review_attempt_grade`
- `learning_action_propose`
- `learning_action_commit`
- `misconception_propose`
- `context_explain`

规则：

- 所有工具继续提供正式输入/输出 Schema、idempotent、blocking 和权限元数据；
- Evidence 写入必须引用来源 Artifact 或消息，不能只接受模型自由文本；
- learner state 只由 reducer 更新，不注册“设置掌握度”工具；
- 子 Agent 可调查、映射技能和提出结构化候选，但最终证据判定、状态提交和计划修改仍由主 Agent 完成；
- 每个写工具必须有 idempotency key、Operation 或不可变事件语义，以及明确的重放行为。

### 7.1 面向前端的 REST 接口

计划新增以下领域接口，继续使用 `/api/v1` 前缀和本地 owner 边界：

- `GET /learning/today`：主动作、后续队列、到期复习及推荐依据；
- `GET /competencies`、`GET /competencies/{id}`：技能图索引与节点详情；
- `GET /competencies/{id}/evidence`：证据、状态版本和错误认识时间线；
- `GET /plans/{id}/learner-state`：计划目标技能、证据覆盖和前置缺口；
- `GET /reviews/queue`、`POST /reviews/{id}/attempt`：复习队列和一次结构化作答；
- `GET /proactive/decisions`：候选、保持安静或介入的理由；
- `GET /agent/runs/{id}/context-pack`：结构化 Context Pack 与渲染信息；
- `POST /agent/runs/{id}/replay`：仅允许显式触发的 shadow replay。

投影重建、完整性校验和数据迁移优先提供 CLI，不开放成普通页面按钮，避免误操作和长任务阻塞 HTTP 请求。

### 7.2 配置与兼容开关

新增配置集中在算法和运行边界，不增加新的秘密：

- `LEARNER_MODEL_V2_ENABLED`：alpha 阶段双写/影子投影开关；
- `LEARNER_STATE_ALGORITHM_VERSION`：当前 reducer 版本；
- `REVIEW_SCHEDULER_PROVIDER`：默认 fsrs；
- `REVIEW_DESIRED_RETENTION`：默认保留率，设置页提供安全范围；
- `PROACTIVE_AUTONOMY_LEVEL`：observe / suggest / assist / autonomous；
- `AGENT_JOB_LEASE_SECONDS`、`AGENT_JOB_MAX_ATTEMPTS`：耐久任务边界。

现有 `study_state_get`、ReviewSchedule 和 ContextSnapshot 在至少一个预发布周期内保持兼容读取；新路径稳定后才迁移默认调用，不做一次性替换。

## 8. UI 信息架构

对话仍是主工作区，不引入平行的“AI 页面”。侧栏分层为：

```text
新对话
今天
学习计划
复习
技能
收件箱
学习记忆
——
置顶计划
连续对话
```

### 今天

- 只突出一个“现在最值得做”的主动作；
- 展示推荐原因、预计耗时、目标技能和需要产生的证据；
- 下方是可调整顺序的后续队列、到期复习和阻塞项；
- 点击任何动作进入或复用明确焦点的 Session。

### 计划详情

- 同时显示计划完成度与技能证据覆盖，二者不再共用一个进度环；
- 时间线任务标出 teaches / assesses 的技能；
- 当前动作、证据缺口和 Agent 修改记录保持在同一视觉中轴；
- 不回到密集 Dashboard 或横向 Kanban。

### 技能

- 默认使用稳定的分层图/路径视图，不做无限画布炫技；
- 节点展示阶段而非假精确百分比；
- 点击进入证据时间线、前置关系、相关计划、错误认识和下次复习；
- 支持列表视图，保证手机和无障碍使用。

### 复习

- 队列页只负责说明今天为什么有这些项目；
- 真实问答、代码或讲解仍在连续 Session 中完成；
- 评分后原地显示证据、状态变化和下一次时间。

### Harness 可观察性

- Run disclosure 增加候选动作、学习状态读取和决策依据；
- Context Inspector 展示 Context Pack 区块、来源、排除原因与版本差异；
- 设置页增加自治等级、复习保留率和学习负担上限；
- 增加 Run Replay/Compare 诊断入口，但不把开发调试信息常驻普通学习视图。

## 9. 实施里程碑

依赖关系：

```text
M13 学习账本
  └─ M14 技能图
       └─ M15 学习者状态与复习
            └─ M16 自适应学习动作
                 └─ M17 Context Pack 2.0
                      └─ M18 耐久主动系统
                           └─ M19 学习工作台 2.0
                                └─ M20 发布硬化
```

后续里程碑可以提前制作不落库的原型，但不得绕过前置数据契约进入正式实现。

### M13：学习账本与评测基线

目标：先建立不会随模型输出漂移的事实层和 2.0 质量基线。

- 扩展 LearningEvent 公共信封：schema_version、occurred_at、correlation_id、causation_id、idempotency_key、invalidation。
- 新增 EvidenceObservation、Artifact 引用和 Rubric 快照。
- 将现有 quiz/submission/task 事件双写到 v2 账本；旧数据只做可证明的保守回填，不推测技能。
- 增加投影 rebuild CLI、账本完整性检查和备份前校验。
- 建立至少 40 个固定学习场景：跨计划隔离、冲突证据、提示后答对、代码验收失败、长期未复习、用户自述与实际证据矛盾等。

验收：相同输入幂等；历史修订不改写原证据；v1 数据迁移可回滚；固定场景形成可重复基线报告。

#### M13 实现候选清单（H4 事实层已验收，尚未发布）

`develop` 已完成第一条可运行纵向切片：

- revision 4 将 `EvidenceObservation` 固定为 append-only fact，包含来源、作用域、Rubric/评价者、评分、提示/迁移等级、因果链和幂等键；数据库 trigger 禁止更新/删除原事实。
- submission/quiz/task producer 在同一 UoW 写一份 primary observation 及 Artifact/Competency/Operation 关联；undo/redo/correction 追加 amendment/invalidation/reinstatement，不重复计权。
- `study_state_get`、Context、tool、HTTP API 和 CLI 已接入同一个无截断投影；增量 watermark 与 full oracle、0/1/500/501/10,000、关闭重开和备份恢复 digest 一致。
- Artifact 保存 canonical envelope、耐久 bytes snapshot、size/hash 与 scope；同 key 异请求/结果稳定冲突，篡改或 legacy unavailable 不进入可计权 Evidence。
- `scripts/rebuild-evidence.py` 已提供重建、审计、v1 回填和派生快照命令；H1 已使纯 audit 保持逐字节只读，并要求任何回填/重建写操作先取得协调 lease 和全量验证备份。
- 41 个网络无关场景均执行 production adapter/reducer/audit，并由 41 个对应 mutant 独立触发 failure code。

H4 已关闭 M13 事实层的迁移、完整账本、撤销/重做、重复成功、保守 eligibility、Artifact snapshot/指纹和 41 场景独立断言阻塞项。H5 完成长期 Context/Intervention 地基，H6 完成默认本机与个人服务器的失败关闭边界，H7 完成 M14 最小只读学习依据与真实浏览器投影；M13/M14 仍不单独打正式标签，项目保持 2.0.0-alpha.1 暂停发布，必须继续完成 H8 发布门禁。

### M14：技能图与计划映射

目标：让计划任务、资源、考核和证据拥有共同的能力坐标系。

- 实现 Competency、Edge 和 Plan/Task/Resource Link。
- 计划提案增加技能图草案、任务 teaches/assesses 映射和目标阶段；采用时事务化物化。
- 实现图版本、环检测、跨计划别名候选和可撤销修改。
- 新增 competency_graph_get / competency_get / evidence_list 及正式输出 Schema。
- 先提供后端/API 和最小只读视图，不提前精雕完整技能 UI。

验收：一个计划可解释每项任务训练或证明什么；跨计划相似技能不会静默合并；图修改可审计、可撤销。

#### M14 实现候选清单（H4 后端协议与 H7 最小前端已验收）

- 已新增 `Competency`、`CompetencyEdge`、`PlanCompetencyLink`、`TaskCompetencyLink` 和 `ResourceCompetencyLink` 增量表。
- 已提供 `competency_create`、`competency_link`、`competency_edge`、`competency_graph_get`、`competency_get` 和 `evidence_list` 的第一版工具与顶层输出模型。
- `prerequisite` 与 `part_of` 关系在写入前执行确定性环检测；同 key 不会根据标题相似度静默合并，重复调用按工具幂等键复用。

H4 已关闭后端阻塞项：global/plan key 使用 scope-aware partial unique，edge/link 从数据库解析两端真实 owner/plan，Evidence 在同一 UoW 快照 task `assesses` 多技能关联，SQL 在 limit 前过滤，工具使用严格嵌套 Schema；graph revision/mutation/dependency 与 `RESTRICT` FK 使变更可审计且撤销不会静默级联。H7 已补 task teaches/assesses、技能 Evidence 覆盖、时间线、来源与 eligibility 的最小只读视图。M14 仍是 2.0.0-alpha.1 候选；H0–H8 关闭前不允许技能节点承载 M15 学习状态。

### M15：学习者状态与复习引擎

目标：从证据得到可解释、可重建的学习状态。

- 实现版本化 reducer 和 LearnerCompetencyState。
- 明确不同证据类型、提示程度、迁移程度、冲突和时间衰减的更新规则。
- 实现 MisconceptionHypothesis 生命周期。
- 实现 ReviewItem/Attempt/State 和 ReviewScheduler Provider；首个 FSRS Provider 固定版本和参数。
- 宽泛技能通过表现性考核更新，不进入 FSRS。
- 升级 learner_state_get 和复习队列 API。

验收：删除投影后可从账本得到相同 digest；一次自述或打勾不能得到“已证明”；延迟回忆成功后才可进入“已保持”。

### M16：自适应学习动作

目标：Agent 每轮能够选择一个有依据、适合当前约束的下一步。

- 实现 LearningAction 状态机和候选生成器。
- 候选考虑用户可用时间、目标、前置缺口、最近失败、复习风险、截止时间和资源。
- 主 Agent 在候选内选择 explain/example/practice/quiz/code/review/remediate/wait。
- 每项动作声明预期证据，完成后由实际证据关闭动作。
- 计划调整继续使用既有 Operation、审批和撤销边界。
- 对话实现“教我下一步”完整循环，而不是一次性输出课程。

验收：用户给出 20/40/90 分钟三种约束时得到不同但合理的动作；连续失败会触发补救或前置回退；没有证据时不虚报状态改善。

### M17：Context Pack 2.0 与 Run 重放

目标：让学习决策上下文结构化、可预算、可比较。

- 新增 ContextPack schema、renderer version、source manifest v2 和缓存键。
- 将 learner state、错误认识、候选动作和复习风险加入计划级上下文。
- 实现强制区块、预算分配、相反证据保护和排除 reason codes。
- ContextSnapshot 同时保存结构化 Pack 与渲染 Markdown。
- 实现 context_explain、shadow replay 和 Run diff。
- 增加 Context Pack 固定问题集、token/延迟基准与跨计划泄漏测试。

验收：每个推荐都能追溯到 Pack 来源；同一 Pack 可重放；12k 默认预算内不丢失目标、安全策略和关键相反证据。

### M18：耐久主动系统

目标：把心跳从单进程定时扫描升级为可恢复、可反馈的长期执行队列。

- 实现 Candidate → Job → Attempt → Decision 两阶段模型。
- 增加 SQLite 原子租约、超时回收、指数退避、dead letter 与幂等通知。
- 候选加入掌握下降、复习风险、连续失败、阻塞和用户学习时间窗口。
- 增加自治等级、snooze、dismiss、helpful 和实际回复反馈。
- 收件箱展示“为什么提醒/为什么保持安静”、候选来源、最近任务和下一次检查。
- 可选输出与 OpenTelemetry GenAI 语义接近的 trace 字段；不要求外部观测服务才能运行。

验收：在领取任务、模型调用、工具提交和通知发送四个故障点杀进程，恢复后不丢任务、不重复写操作、不重复提醒。

### M19：学习工作台 2.0

目标：让新状态和新动作在桌面、平板和手机上都容易理解和执行。

- 上线“今天”、技能、复习入口和新的侧栏层级。
- 计划页拆分计划完成度与技能证据覆盖。
- 技能视图提供图/列表双模式和证据详情。
- 复习卡、学习动作、状态变化、错误认识和提醒反馈嵌入消息流。
- Run disclosure、Context Inspector 和 Replay Compare 完成视觉统一。
- 使用现有设计 token；执行 375/768/1280/1440/2560 浏览器矩阵、键盘导航和 reduced-motion 检查。

验收：用户不看文档也能完成“今天开始 → 学习 → 提交证据 → 状态变化 → 安排复习 → 收到主动跟进”全流程；没有横向溢出、裁切、伪按钮或重复 Header。

### M20：迁移、备份、评测与 2.0 发布

目标：证明 2.0 可以在真实个人数据上长期运行，而不只是演示夹具。

- 提供 v1.1 → v2 增量迁移、dry-run、备份、校验和恢复脚本。
- 备份包含数据库、Context/Workspace 文件和版本 manifest，默认排除 `.env` 秘密。
- 增加 JSON/Markdown 数据导出；只声明“xAPI/CASE-inspired”，不宣称未验证的标准合规。
- 运行 deterministic reducer、API、工具契约、Run 恢复、Job 混沌、浏览器和安装测试。
- 真实 Hy3 跑固定场景三轮，报告正确焦点、证据引用、动作适配和失败率。
- 完成至少 7 天本地 dogfooding，记录误提醒、重复提醒、无依据状态更新和恢复失败。
- 更新 README、PRODUCT、ARCHITECTURE、HARNESS、STATUS、CHANGELOG 和安全边界。

验收：公开仓库无秘密或个人数据；从 v1.1 备份升级、回退和重新升级成功；全部硬性质量门槛通过后才合并 main、打 `v2.0.0` 标签和 Release。

## 10. 发布节奏与分支策略

| 节点 | 覆盖范围 | 发布条件 |
|---|---|---|
| `2.0.0-alpha.1` | M13–M14 + H0–H8 | 前置硬化、外部安装验证、账本、迁移、技能图和工具契约全部通过 |
| `2.0.0-alpha.2` | M15–M16 | 状态 reducer、复习和自适应动作闭环通过 |
| `2.0.0-beta.1` | M17–M19 | Context Pack、主动队列和真实 UI 流程通过 |
| `2.0.0-rc.1` | M20 | 数据迁移、混沌恢复、7 天 dogfooding 和文档冻结 |
| `2.0.0` | 全部 | RC 无阻塞缺陷，main 与发布标签指向同一验收提交 |

- `main` 始终保持已发布版本；所有 2.0 工作进入 `develop`。
- 每个 M 里程碑独立提交，schema、后端、前端、测试和文档一起完成。
- 新表和新列走增量迁移，不重写现有 Git 历史，不要求清空个人数据库。
- 在 M16 前使用 feature flag 隔离 v2 投影；达到重建与一致性门槛后再成为默认路径。
- 每个 alpha/beta 都必须能从备份恢复，不能把“预发布”当成允许损坏数据的理由。
- 2026-08-18 起，H0–H8 是恢复 M15 和发布任何 2.0 预发布版本的前置门禁；执行细节以 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 为准。

## 11. 2.0 硬性质量门槛

### 数据正确性

- 投影重建 digest 100% 一致；
- 重复事件、工具重放和 Job 重试不产生重复证据或通知；
- 跨计划私有证据隔离测试 100% 通过；
- 所有状态变化包含算法版本、reason codes 和证据 ID；
- 模型没有直接写入 learner state 的路径。

### Agent 质量

- 固定场景中安全/作用域约束 100% 通过；
- 正确引用关键证据与当前焦点不少于 95%；
- 人工评审认为下一动作适合时间、前置条件和目标的比例不少于 85%；
- 三轮真实模型测试报告方差，不能只挑最好一次；
- 与上一个 RC 相比，核心场景指标不得下降 5 个百分点以上。

### 耐久性

- Run、Job、通知和投影四条链路都有进程中断恢复测试；
- 失败任务可重试、可取消、可进入 dead letter，不能永久停在 running；
- 备份恢复后引用完整性检查通过；
- 7 天 dogfooding 中重复外部触达为 0，无法解释的状态提升为 0。

### UI 与可访问性

- 375、768、1280、1440、2560 真实浏览器回归通过；
- 无横向溢出、内容裁切、重复 Header 和不可点击伪控件；
- 键盘可以完成对话、复习、审批和主要导航；
- reduced-motion 下不依赖动画表达状态；
- 普通页面默认不展示内部 JSON，诊断信息按需展开。

### 发布工程

- pytest、前端生产构建、生产依赖审计和 CI 全绿；
- 安装、升级、回退、备份、恢复各有一次干净环境验证；
- secrets、数据库、日志、Context 快照和个人文件扫描通过；
- Release Notes 明确现有能力和边界，不把计划项写成已实现。

## 12. 关键风险与应对

### 技能图被模型制造得过细或过粗

先限定节点类型、层级深度和每计划节点预算；计划采用时展示摘要；跨计划合并必须确认。用真实计划夹具做图稳定性评测。

### 掌握度成为另一种幻觉

状态只由结构化证据 reducer 生成；展示阶段、置信和证据，不展示伪精确百分比；自述、提示后答对和独立迁移任务使用不同语义。

### FSRS 被错误用于复杂技能

只有 ReviewItem 进入调度器；Competency 本身不直接成为卡片。性能技能要求新的实际证据。

### Context Pack 膨胀

按区块设强制/可选优先级、token 上限和来源数量；保存排除原因；固定大数据夹具持续测量。

### 心跳越来越吵

候选与通知分离；加入冷却、每日上限、自治级别、snooze 和反馈；提醒质量进入发布指标。

### 里程碑过大导致长期不可演示

每个里程碑都必须形成真实可运行切片。alpha.1 先证明“证据 → 技能”，alpha.2 再证明“状态 → 下一动作”，beta 才扩展主动系统和完整 UI。

### 范围膨胀拖延 2.0

若必须裁剪，按以下顺序延后，不能裁掉证据账本、状态重建、Context 来源和主动幂等：外部 OpenTelemetry Exporter、技能图复杂画布（列表保留）、个性化 FSRS 参数训练（固定参数保留）、标准格式导出、个性化徽章图像。

## 13. 设计依据

- 技能图的数据形态参考 1EdTech CASE 1.1 的 competency item、association 与 rubric 对齐思想，但 2.0 不承诺 CASE 服务兼容。
- 学习记录信封参考 xAPI Statement 的 actor / verb / object / result / context 思路，但本地账本保留自己的领域 Schema。
- 状态建模借鉴 Knowledge Tracing 的“知识是潜变量、行为是观察”原则；首版采用可解释 reducer，而不是在单用户少量数据上训练黑盒模型。
- 主动考核坚持 retrieval practice：能回忆、解释和迁移比重复阅读更能形成有效证据。
- ReviewScheduler 参考 FSRS 的 difficulty / stability / retrievability；只有适合提取练习的 ReviewItem 使用该模型。
- Agent 观测字段尽量与 OpenTelemetry GenAI 的 conversation、agent、operation、tool 和 token 语义对齐，同时保持无外部服务也能运行。

参考：

- https://standards.1edtech.org/case/
- https://www.adlnet.gov/assets/uploads/xAPI_v1.0.1-2013-10-01.pdf
- https://www.andrew.cmu.edu/course/85-412/readings/seven-orders.pdf
- https://pubmed.ncbi.nlm.nih.gov/21252317/
- https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm
- https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/
