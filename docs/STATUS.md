# 项目状态

更新时间：2026-09-06（Asia/Shanghai）
当前版本：1.1.2

H1–H8 工程门禁已经完成，产品底座可运行、可恢复、可发布。第三阶段“关键决策评测”的 E0–E3 工程骨架及 E3.1/E3.1.1/E3.1.2 正式评测前置修复已经实现；活动链路收敛为 Evaluation Protocol Release 1.0，DecisionEpisode v1/v2/v3 永久保留为 engineering-only 历史回归。生产可信 Benchmark 注册表仍为空。E4 的48 Primary、24 Calibration及8来源已完成AI逐例内容裁决，候选绑定已重建；最终回归进行中，未进入E5。原DoD中的正式冻结与登记在E5方法稳定后、E6前完成。有效性实验、正式评测、版本对比和最终报告仍未完成。M15–M20 属于另一条 2.0 产品能力路线，不在本阶段范围，也不因评测开发自动恢复。外部真人采用验证尚未执行，当前仍不作 2.0 Alpha 发布声明。

## 2026-09-06 E4 内容验收与最终回归

80条AI内容记录和8组三档裁决已绑定最终Case/资源摘要，身份为`delegated_ai_reviewer`。修订了条件行动包络、静默/日限额时间初态、受保护实验与Mild缺陷；外部副本定向回归18 passed。全部evaluation、全仓pytest和最终全链尚在验收计划中。当前Primary家族明确为探索输入，旧45 Episode + 3 Failure不升级为修订输入的结果。详细进展见 [E4验收工作记录](E4验收工作记录.md)。

## 2026-09-06 E4 接续交接（上一检查点）

已用独立产品反例修复上下文超限误记 `internal_error` 的诊断问题，Worker 仅导出白名单终态原因；定向回归 57 passed。该反例不能证明旧 `primary-s06-p` 的唯一根因。已有 72 个候选仍待逐例裁决，E4 候选 Manifest 与本次新协议摘要尚未重新绑定。用户要求交给下一位继续，详见 [E4 接续验收与交接记录](E4验收工作记录.md)。本次没有新增真实请求，原账本仍占用 5.397645 元；E4 未完成，没有正式能力结果。

## 2026-09-05 审计修复、E4 候选收口与暂停（上一轮记录）

用户已授权修复和本轮 14 元共享预算，要求到 E4 后暂停并复核两份方案。本轮已停止真实调用，没有进入 E5。

- [x] 修复行动适用性、盲化、失败记录、读观察、审批队列、Operation/Delta、实际 Outbox 投递计数与 Git 来源校验。
- [x] 补齐严格可执行 seed、公开资源目录、独立 protocol_pilot 角色及持久费用限额；23 个历史 Schema 原始字节不变。
- [x] 构建 48 Primary 和 24 Calibration 候选输入、8 个变异源、资源摘要、Split/Mutation 清单与复核工作单。
- [x] 在干净 `63be8da` 完整执行 48 个真实 Primary：45 Episode + 3 RuntimeFailure；24 个受控 Calibration：24 Episode + 0 Failure。
- [x] Calibration 的 8 个 Severe 全部命中 Rule Gate，Good/Mild 均未命中；这不是 Judge 判别力/一致性结论。扩展扫描发现旧批次画像中的场景族 ID 泄漏，已于 `df8e8e7` 修复输入投影并离线重建 Calibration。
- [x] 将本批暴露的 profile 逻辑引用及缺失 intake 空引用问题独立复现并修复于 `bd89530`，保留原批次全部失败，未用成功重跑替换。
- [x] 保存输入、公开原始终态、试跑历史、费用及验证记录；完成两份方案的依赖、范围与交付复核。
- [ ] 独立人工内容复核、剩余 `primary-s06-p` Runtime 终止归因、正式协议冻结与 production registry 登记。

本轮共 190 次真实请求，公开 usage 估算 4.355213 元；4 次未知费用请求继续预留 1.042432 元，账本总占用 5.397645 元，未超过 14 元。未核对账户实际账单。

精确修复、分提交测试、仍未完成的原定 E4 DoD、下一切片与暂停边界见 [E4 候选数据收口与方案复核](E4候选数据收口与方案复核.md)；可复查制品见 [运行记录](../evaluation/artifacts/e4-candidate-20260905/README.md)。以下均为修复前各阶段的历史验收，不能替代本轮结果。

## 2026-09-05 第三阶段 E3.1.2 收口（审计修复前的历史记录）

E3.1.2 不新增 Episode 版本，而是在未产生正式数据的活动契约上完成信任分层与发布治理。
对外唯一协议名为 **Evaluation Protocol Release 1.0**，内部活动链保持：

`case-spec-v2 → decision-episode-v4/runtime-failure-v2 → rule-result-v3 →
judge-result-v3 → aggregate-result-v3`。

| 准入问题 | 当前结论 | 闭合证据 |
| --- | --- | --- |
| Benchmark 信任根 | 已闭合 | `benchmark-release-manifest-v1` 固定 Case/digest/order/split/track/resource/lineage/Protocol；代码固定的 production registry 当前为空，调用者不能用路径、环境变量或自带 Release 建立信任 |
| 完整 Suite | 已闭合 | `full_suite` 不再等于“无 CLI 过滤”；注册 Release 与 Runtime 的每个 `case_id/case_spec_sha256/track`、总数和四轨数必须完全一致，每 Case 恰有一个 Episode/Failure 终态 |
| formal 三层语义 | 已闭合 | `protocol_eligible`/`provider_eligible` 表示执行合规，`trusted_benchmark_run` 表示可信完整运行，`formal_capability_result` 只存在于最终闭合 Aggregate Manifest；单制品和中间阶段固定 false |
| 失败披露 | 已闭合 | RuntimeFailure、invalid_input、judge_error 保留并排除均值，同时阻止能力结论；删除 Failure、替换 Case 或事后 Episode/track 过滤均不能恢复 formal |
| 活动入口 | 已收敛 | 包级 API 与 CLI 只指向 `active_{runtime,rules,judge,aggregate}`；旧公共模块是 fail-before-side-effect 薄层，完整旧实现只在私有 `_historical_*` 回归 seam 中保留 |
| 来源归因 | 已闭合 | Runtime/Rules/Judge/Aggregate 记录含仓库相对路径和逐文件摘要的保守 source bundle；Git commit、干净树和 bundle 同时参与 provenance |
| Schema 冻结 | 已闭合 | 独立 `schema-lock-v1` 覆盖全部 44 个 Schema，历史与活动锁分开；模型和生成文件同时漂移仍失败；未注册 engineering candidate 可显式刷新来源绑定，可信注册后失败关闭 |
| 行动协议 | 已闭合 | `model-action-declaration-v2` 为 14 类行动提供 track、语义、字段、边界、正反例和组合政策；JSON 空白/字段顺序无关，未知/重复/歧义严格拒绝，缺失声明仍可评分 |
| 独立 Aggregate 信任 | 已闭合 | 最终 Manifest 绑定 Runtime Manifest digest 与终态记录；Validator 直接查询 production registry 并逐 Case 对照，局部篡改数量/布尔位不能自我提升 |

当前 [`decisionbench-v4-engineering`](../evaluation/datasets/decisionbench-v4-engineering/dataset-card.md)
拥有完整但未注册的 `engineering` Benchmark Release。固定 stub 全链仍得到 11 Episode +
1 RuntimeFailure；即使工作树干净、制品有效，production registry 为空使
`trusted_benchmark_run=false` 和 `formal_capability_result=false`。

E3.1.2 原始收口时未调用真实 Hy3、未访问公网或生产数据库、未创建 Primary/Calibration、未产生正式
能力结论。详细设计、版本资产表、精确技术债和未来小规模真实协议试跑门槛见
[`E3.1.2正式评测准入与版本治理.md`](E3.1.2正式评测准入与版本治理.md)。当时的 E4 授权前置条件已被上文用户授权取代。

E3.1.2 最终修复后实际验收：

- E3.1.2/E3.1.1/Schema 定向：`60 passed in 250.72s (0:04:10)`；
- `.venv/bin/pytest -q evaluation/tests`：`216 passed in 507.99s (0:08:27)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 3.01s`；
- `.venv/bin/pytest -q`：`1291 passed, 2 warnings in 2530.40s (0:42:10)`，0 failed；warning
  仍为既有 Starlette/httpx 弃用提示和 Python 3.14 tar 提取行为预告；
- 文档相对链接、仓库自定义 `lint_typecheck`（compileall、致命 Ruff 规则和 JS
  语法，不声称 mypy/pyright）、Secret Scan、`pip check` 与 `git diff --check`
  通过；
- 仓库级 `ruff check .` 的历史基线是 427 条，当前为 425 条；E3.1.2
  新增/修改 Python 文件为 0 条。本切片不用大范围无关格式化改写历史业务代码。

## 2026-09-04 第三阶段 E3.1.1 有效性门禁闭合

E3.1.1 没有原地改变 v3，而是执行新的版本化干净切换。唯一活动链为
`case-spec-v2 → decision-episode-v4/runtime-failure-v2 → rule-result-v3 →
judge-result-v3 → aggregate-result-v3`；v1/v2/v3 只作历史工程回归，不能获得 formal
资格。没有建设两个可正式运行的活动栈。

| 审计问题 | 当前结论 | 闭合证据 |
| --- | --- | --- |
| 14 类行为 | 已闭合 | `model-action-declaration-v1` 严格记录 14 类模型声明；跨轨、组合、缺失声明、Quiz/Review 写入和未知状态变化都保留为可评分行为，不以关键词猜测 |
| Validator 边界 | 已闭合 | v4 只拒绝损坏或不可重建证据；动作不在当前轨/Case envelope、模型拒绝或行为组合由 Rules 产生 Fail |
| 并发轨迹 | 已闭合 | Recorder 在调用开始时原子保留 ordinal，按开始顺序发布；双 Planning 子 Agent 取得唯一连续 call ID，并带同一因果 `parent_call_id` |
| Failure-only | 已闭合 | 0 Episode + 1 Runtime Failure 可贯穿 Rules/Judge/Aggregate；Judge Provider 调用为 0，轨道均值为 null，不伪装 0 分 |
| formal 单调性 | 已闭合 | Runtime Failure 使上游 non-formal；Rules/Judge/Aggregate 单调继承。任何事后 `--episode-id/--track` 都标为 ad-hoc，不能恢复 formal |
| Case predicate | 已闭合 | `constraint-proposition-v1` 固定 predicates 合取；`must_satisfy` 要求命题为真，`must_not` 要求命题为假；CaseSpec v2 在 Schema 层要求两类约束都存在 |
| 父子调用 | 已闭合 | 主 Agent 将 Recorder call ID 绑定到 ToolContext，生产 planning/generic 子 Agent 的 checkpoint、恢复与每次模型调用完整传播 `parent_call_id` |
| 隔离门禁 | 已闭合 | `prohibited_file_access` 非零成为 Critical Hard Gate；其余数据库、网络、SMTP/Web Push/IMAP、subprocess 和发布 SQLite 计数继续失败关闭 |
| 实现归因 | 已闭合 | Rule/Aggregate Manifest 记录实际执行源码 bundle SHA-256、工作树状态和 Git commit；当前源码摘要由 Validator 重算 |
| 业务盲化 | 已闭合 | `baseline/candidate/Good` 普通文本保留，Calibration/变异/作者/生成者元数据不可见；64 位摘要不再被身份号码正则误报 |

活动 [`decisionbench-v4-engineering`](../evaluation/datasets/decisionbench-v4-engineering/dataset-card.md)
包含 12 个公开合成 Case，完整链得到 11 个有效 Episode 与 1 个故意 Provider Failure。
错误 Assessment、跨轨 plan patch、组合动作和缺失声明分别作为有效行为触发 Rule Gate/cap；
Guard 阻断同时表达危险尝试与零副作用；生产 `quiz_create/review_schedule` 和双子 Agent 并发
轨迹均能完整导出。fixed-response Judge 只验证协议，所有结果 non-formal。

本次没有调用真实 Hy3、公网、真实数据库、`.env`、邮箱或 Push endpoint；没有创建 48 个
Primary、24 个 Calibration，也没有执行 E5–E8。Provider Attestation 仍只表示可审计归因，
不声称密码学证明。

E3.1.1 提交前实际验收：

- 活动负向矩阵 + Schema + Recorder/子 Agent 定向：`47 passed in 119.82s (0:01:59)`；
- `.venv/bin/pytest -q evaluation/tests`：`186 passed in 509.36s (0:08:29)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 1.61s`；
- `.venv/bin/pytest -q`：`1261 passed, 2 warnings in 2842.95s (0:47:22)`，0 failed；warning 仍为既有 Starlette/httpx 弃用提示和 Python 3.14 tar 提取行为预告；
- 仓库自定义 `lint_typecheck`（compileall、致命 Ruff 规则和 JS 语法，不声称为 mypy/pyright）、Secret Scan、`pip check`、文档链接与 `git diff --check` 全部通过。

干净 HEAD 上的双轮完整链、过滤、Manifest HEAD/实现摘要、隐私/标签/凭据/路由/数据库扫描和零外部 Provider 复验由本次完成汇报记录；Git 提交无法在自身文档中保存自引用 HEAD。

## 2026-09-04 第三阶段 E3.1 评测有效性修复（已被 E3.1.1 活动链取代）

E3.1 采用“版本化、一次干净切换”，没有原地改变冻结 v1/v2 Schema，也没有建设长期正式双栈。`case-spec-v1 → decision-episode-v3/runtime-failure-v1 → rule-result-v2 → judge-result-v2 → aggregate-result-v2` 是唯一活动评测链；旧 v1/v2 仍可读、可验证、可回归，但永远不能获得 formal 资格。

| 检查项 | 当前结论 | 证据或入口 |
| --- | --- | --- |
| 有效性语义 | 已修复 | v3 Validator 只判断证据完整性；错误动作保留为有效 Episode，由 Rules 产生 Critical Fail，不再被结构校验提前丢弃 |
| CaseSpec/JudgeReference | 已完成 | CaseSpec 是唯一案例控制面；JudgeReference 只能确定性派生，公开约束文本与结构化谓词可见，质量标签/变异源/作者裁决不可见 |
| 可重建轨迹 | 已完成 | 保存每次调用实际收到的脱敏消息和工具 Schema，以及 run/parent/depth/purpose；Judge 不获得额外 `state_before` 全知视图 |
| 全模型调用 | 已完成 | Worker 级 Model Client Factory 覆盖主 Agent、生产 planning 子 Agent、子 Agent 汇总、标题与记忆压缩；辅助调用标记 non-decision |
| 失败终态 | 已完成 | 每 Case 独立执行并发布 Episode 或 RuntimeFailure；Provider/框架失败不评分且不撤销同批成功制品，安全调用尝试元数据可保留 |
| 精确盲化 | 已完成 | 路径级删除/opaque 化取代词面替换；业务 `baseline/candidate/Good/candidate_key` 保留，Calibration 标签和生成者/路由信息移除 |
| Stable ID | 已完成 | CaseSpec 以公开语义字段绑定数据库实体；多 Stage/Task 与父子 Run/RunEvent 不依赖排序位置，不匹配或歧义时失败关闭 |
| Assessment ACCEPT | 已完成 | 生产 `submission.check` Operation 完整枚举 Submission/Task/Stage/Plan 正反 patch，ACCEPT 与 REVISION_REQUIRED 均可完整导出 |
| Provider 归因 | 已完成 | 固定 TokenHub/Hy3 allowlist，校验请求/响应模型、request ID、时间、配置摘要、Git/洁净树和精确依赖锁；表述为可审计归因而非密码学证明 |
| 隔离/隐私 | 已完成 | 相对 `.env`/`data/...` 同样被审计阻断；普通 reasoning/推理过程业务文本不误报，真正私有推理字段继续失败关闭 |
| Rules/Judge/Aggregate | 已完成 | v2 结果契约保持 D1–D7、一次修复、Evidence Path、Rule-first Hard Gate、Critical 39/Major 69 cap、invalid/error/failure 单列和四轨无 overall |
| 正式 Hy3 | 未调用 | 仅固定 stub 工程响应；全部 engineering Mini、旧 v1/v2 与本次聚合均 non-formal，没有 Hy3 能力结论 |
| E4–E8 | 未开始 | 未创建 48 Primary/24 Calibration，未做有效性实验、人工盲标、正式评测、版本回归、Case、最终报告、Demo 或 Release |

活动 engineering suite 现有 8 个 Case：四轨正例、一个错误 Assessment 动作、一个 Guard 阻断、一个真实生产 `planning_delegate` 父子轨迹和一个故意的 Provider Failure。成功/错误/安全/基础设施失败均使用公开合成数据；错误动作的 v3 Episode 通过结构校验后产生 Critical Rule Fail 和 cap 39，基础设施 Failure 则贯穿 Rules/Judge/Aggregate Manifest 但不进入分母。

Provider Attestation 只能证明产物中的配置、响应归因字段、提交和依赖环境满足固定政策，不能密码学证明远端服务身份。正式运行还必须另行授权、使用组织侧凭据和干净提交；本次没有尝试 real 模式或公网。

E3.1 分片提交为：`2af42e3`（契约）、`9685eba`（轨迹/失败）、`16ad39b`（v3 Runtime）、`874f95a`（评分链），最终正式性/隔离/ACCEPT/多实体与文档收口由 E3.1d 提交承载。Git 提交不能在自身文档中保存自引用哈希，最终 HEAD 由完成汇报记录。

E3.1d 提交前实际验收：

- `.venv/bin/pytest -q evaluation/tests/test_e31_contracts.py evaluation/tests/test_e31_runtime_artifacts.py evaluation/tests/test_e31_scoring_chain.py evaluation/tests/test_protocol_schemas.py evaluation/tests/test_e2_exporter_rules.py`：`79 passed in 156.93s (0:02:36)`；
- `.venv/bin/pytest -q evaluation/tests`：`170 passed in 307.01s (0:05:07)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 1.59s`；
- `.venv/bin/pytest -q`：`1245 passed, 2 warnings in 2571.87s (0:42:51)`，0 failed；warning 仍是既有 Starlette/httpx 弃用提示和 Python 3.14 tar 提取行为预告；
- `.venv/bin/python scripts/check_doc_links.py`、`.venv/bin/python scripts/release-check.py lint_typecheck`、`.venv/bin/python scripts/release-check.py secret_scan`、`.venv/bin/pip check` 与 `git diff --check` 均通过；
- 系统临时目录中的 8 Case 两轮完整链均得到 `7 Episode + 1 RuntimeFailure`、1 个错误动作 Critical Hard Gate 和 1 个聚合 Fail；全部 Runtime/Rules/Judge/Aggregate 通过 validator，两轮逐文件一致；Episode/track 过滤、Manifest Git commit、zero-provider/isolation 与无 SQLite/WAL/SHM 扫描通过，临时目录已删除。

干净 HEAD post-commit smoke 的提交对齐、双轮比较、盲化/凭据/路由扫描和最终工作树状态由本次完成汇报记录。

## 2026-09-02 第三阶段 E3 收口（已被 E3.1 活动链取代）

E3 在不修改 `DecisionEpisode v1/v2`、`rule-result-v1`、Action Envelope v1 或 Environment Manifest v1 的前提下，完成 `完整 v2 + digest 匹配 Rules → blind Judge input → judge-result-v1 → Rule-first aggregate` 的工程闭环。字段能力审计确认 v2 已一等表达 D1–D7 所需公开事实、模型尝试/最终效果、Guard blocked/deferred、WAIT/NO_OP、durable 状态、formal eligibility 与 Evidence Path；Rule Result 已表达严重级别、实际 Hard Gate 和 Episode 关联，因而无需从 Capture/Oracle 自由说明回填，也无需升级冻结契约。

| 检查项 | 当前结论 | 证据或入口 |
| --- | --- | --- |
| Judge/聚合契约 | 已完成 | strict Pydantic + 提交版 `judge-result-v1`、`judge-run-manifest-v1`、`aggregate-result-v1`、`aggregate-track-result-v1`、`aggregate-run-manifest-v1` Schema 与漂移检查 |
| Rubric/锚点 | 已完成 | `decision-rubric-v1` 固定 D1–D7 和 `15/15/20/20/15/5/10`；`decision-track-anchors-v1` 为 P/I/A/R 分别固定 0/1/2 锚点与摘要 |
| 标签盲化 | 已完成 | 稳定纯字母 opaque ID；删除 Good/Mild/Severe、Baseline/Candidate、生成者/Prompt/Invocation 身份、作者说明、ID/tag/source ref、Capture、凭据和路由材料；原 Episode 不改写 |
| Judge 协议 | 已完成 | 独立 OpenAI-compatible seam、`--judge-mode real --allow-real-judge` 双重 opt-in、严格输出 Schema、最多一次修复、稳定 `judge_error`；不导入生产 `app.*`/Settings |
| Evidence/隐私/formal | 已完成 | D1–D7 固定顺序、0/1/2、每维原始 v2 路径、非满分具体问题、blind-visible + original-path 双解析、digest/privacy/mode/status/self-digest 校验 |
| 确定性聚合 | 已完成 | `sum(weight × level / 2)`；Critical Hard Gate 直接 Fail/cap 39，Major cap 69，多个 cap 取最严，Minor 无额外 cap，suggested gate 不升级 |
| invalid 分类 | 已完成 | Rule/Judge invalid 与 `judge_error` 无维度/无分数、单列且不按 0 混入轨道均值；四轨分别发布且无 overall |
| CLI/发布 | 已完成 | `evaluate-judge`、`aggregate-results` 支持 Episode/track 过滤、稳定摘要/错误/退出码、不覆盖、闭合 Manifest、同级 staging + 原子 rename |
| 正式 Hy3 | 未调用 | 仅重放 `e3-fixed-judge-responses-v1.json` 的固定结构化响应；所有 E1 Mini Judge/聚合均 `formal_evaluation_result=false`，没有能力结论 |
| E4–E8 | 未开始 | 未创建 Primary/Calibration，未做有效性实验、人工盲标、正式评测、版本回归、Case、最终报告、Demo 或 Release |

E3 Judge 默认不发布盲化投影或展开 Prompt。`judge-result-v1` 只保存版本/摘要、Episode/Rule/blind input 关联、公共维度/问题/建议 Gate、mode/formal/status 与自摘要；不保存 Provider 原始响应/异常、Key、endpoint 或私有推理。Rule 已确认的时间、阈值、存在性与 Hard Gate 作为权威事实，Judge 不重新裁决。

聚合是纯 evaluation 逻辑，不调用模型、网络、数据库、`.env` 或 subprocess。Judge 高分不能抵消实际 Rule Gate；Judge 建议 Gate 只进入建议字段。机器可读输出仅包含逐 Episode Judge、逐 Episode 聚合、逐轨汇总与 Run Manifest，不实现最终 HTML/CSV/Markdown 报告。

E3 pre-commit 验收实际结果：

- `.venv/bin/pytest -q evaluation/tests/test_e3_judge_aggregate.py`：`25 passed in 29.53s`；
- `.venv/bin/pytest -q evaluation/tests/test_protocol_schemas.py`：`5 passed in 0.35s`；
- `.venv/bin/pytest -q evaluation/tests`：`130 passed in 169.79s (0:02:49)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 1.32s`；
- `.venv/bin/pytest -q`：`1205 passed, 2 warnings in 2119.04s (0:35:19)`，0 failed；两条 warning 仍是既有 Starlette/httpx 弃用提示与 Python 3.14 tar 提取行为预告；
- `.venv/bin/python scripts/check_doc_links.py`、`.venv/bin/python scripts/release-check.py lint_typecheck`、`.venv/bin/python scripts/release-check.py secret_scan`、`.venv/bin/pip check` 与 `git diff --check` 均通过；
- 四轨系统临时目录已完成 v2 → Rules → fixed-response stub Judge → 非正式聚合，Runtime/Rules/Judge/聚合全部通过 `validate-dataset`；两次 Judge/聚合逐文件一致，Episode/track 过滤、Manifest Git commit、隐私/质量标签/凭据/路由、零外部调用和无 SQLite/WAL/SHM 扫描均通过，临时目录已删除。

E3 开发与验收没有调用真实 Hy3 或公网。提交哈希与干净 HEAD post-commit smoke 作为提交后证据记录在本次完成汇报中；Git 提交不能在自身文档内记录自引用哈希。

## 2026-09-01 第三阶段 E2 收口

E2 发布 `DecisionEpisode v2` 作为所有新 Runtime Export、Rules 和后续 E3 Judge 的唯一权威 Episode 格式。三个版本维度必须分开：E0/E1/E2 是工程里程碑，DecisionEpisode v1/v2 是单 Episode 契约，DecisionBench v1 是尚未正式完成的 Benchmark 发布名。

| 检查项 | 当前结论 | 证据或入口 |
| --- | --- | --- |
| v1 历史兼容 | 已冻结 | 三份 E0 v1 Schema 字节摘要锁定，四个手工 Episode 继续只读校验；不再生成新的 v1 Runtime Episode |
| v2 权威契约 | 已完成 | `decision-episode-v2.schema.json` 一等表达前后 Snapshot、完整 Delta、决策分层、耐久状态、隔离与完整性 |
| 通用 Runtime Export | 已完成 | E1 四轨专用 `runtime_export.py` 已退休；新 Exporter 不按 Episode ID、track、`seed_kind`、Oracle 或 scripted 文本分支 |
| Rules 与 Hard Gate | 已完成 | `rule-result-v1`、`e2-rule-pack-v1`、七个规则包、完整性结果和不可抵消的 Critical Hard Gate |
| CLI 与发布 | 已完成 | `run-agent` 只产 v2；`evaluate-rules` 支持 Episode/track 过滤、结构化失败、非零门禁退出码、原子发布和不覆盖 |
| 正式评测 | 尚未执行 | stub Rules 不是 Hy3 能力结果；Judge、标签盲化、聚合、报告、Primary/Calibration 和正式结果仍未实现 |

E2 设计与语义：

- `DecisionEpisode v1`、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` 完全冻结；Schema 注册表和 `validate-dataset` 同时支持历史 v1 与新 v2，但每次新 Runtime 只写 v2，不存在常规 v1/v2 双输出或长期有损 v1 投影。
- `e2-run-output-manifest-v1` 固定声明 `episode_schema_version=decision-episode-v2` 并拒绝混合。E1 Capture 仍可作为脱敏工程审计附件，但不进入 Rules、完整性事实判断或后续 Judge Evidence；正式 Evidence 根只在 v2 Episode 内解析。
- Worker 在 Fixture seed 后、Runtime 前采集前态，在 Runtime 和 Outbox drain 后采集后态；Oracle 直到上述执行和真实 Delta 完成后才读取。Collector 使用闭合实体集合与逐字段 allowlist，覆盖四轨所需的 Runtime trace、RunApproval、计划/任务/提案/提交/验收、Intervention/Decision、Notification 和 Outbox 实体；未登记列不读取，已登记事实出现不支持的形状、引用或状态时失败关闭。
- Normalizer 统一确定性 UTC、递归 canonical JSON、语义数组顺序和集合排序；拒绝非有限数值、未知非 JSON 类型与 `str(unknown)`。跨前后态身份优先使用声明逻辑 ID，否则使用实体类型、作用域和规范化语义 ordinal；数据库自增 ID、随机 UUID、claim/reply token、endpoint、Push key、认证与地址材料不进入产物。
- State Delta 由前后 Snapshot 实际递归比较产生，一等表达 `added/removed/changed`、`missing/present`、before/after、可解析路径、typed source refs、零到多个有序 Operation refs，并以 `compared_entity_refs/unchanged_entity_refs` 证明确认无变化。新实体只在后态，删除实体只在前态，`null` 不等于缺失，确认无变化不等于未采集。
- Operation forward/inverse patch 是归因证据而非后态替代；字段更新逐值对齐，实体新增/删除要求配对的显式创建/删除标记（顶层 `created_ref/delete_ref` 或登记的 typed ref）。支持一个 Operation 影响多个实体/字段和多个 Operation 依次影响一路径。Notification/Outbox/Receipt、pending approval、Guard 和运行终态等无 Operation 事实不绑定伪 Operation；持久化变化无法证明对齐时完整性失败。
- v2 分开表达 `model_attempt → tool_execution → guard_decision → final_effect → durable_status → formal_evaluation_eligibility`。Guard blocked/deferred 保留模型尝试和 ToolInvocation，同时验证禁止副作用为零；Planning 待采纳引用 pending PlanProposal，高风险工具暂停引用 RunApproval。WAIT/NO_OP 可以没有工具和状态变化，但必须有已确认完整的空 Delta、Recorder、终态与完整性证据。
- `rule-result-v1` 使用 `deterministic-rule-evaluator-v1` 和 `e2-rule-pack-v1`。52 个稳定 check 分布于 `common/planning/intervention/assessment/revision/trace/isolation`，输出 `pass/fail/not_applicable/invalid_input`、`minor/major/critical`、observed/expected、公共 reason code 和可解析 v2 Evidence Path。缺字段是 `invalid_input`，不是业务 Fail；`hard_gates` 只引用 failed Critical checks，其他 Pass 不可抵消。
- `dimension_signals` 只保存确定性结构化信号；E2 没有实现 0/1/2 Judge 档位、总分、模型语义评价、聚合或报告。
- E1 的独立 Worker、临时 SQLite、冻结时钟、Recorder、资源 Snapshot、生产 Guard/Notification/Outbox claim-fence-idempotency/Receipt 和 Recording Sink 全部保留。SMTP、SMTP_SSL、Web Push、IMAP、IMAP_SSL、实时网络和真实通知 Provider 调用仍为 0；Agent 看不到模拟 Receipt。

E2 当前已执行的定向验收：

- `.venv/bin/pytest -q evaluation/tests/test_e2_exporter_rules.py`：`38 passed in 55.34s`；
- `.venv/bin/pytest -q evaluation/tests/test_protocol_schemas.py`：`4 passed in 0.33s`；
- `.venv/bin/pytest -q evaluation/tests`：`104 passed in 118.16s (0:01:58)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 0.89s`；
- `.venv/bin/pytest -q`：`1179 passed, 2 warnings in 2115.27s (0:35:15)`，0 failed、0 xfailed；两条 warning 仍是既有的 Starlette/httpx 弃用提示和 Python 3.14 tar 提取行为预告；
- `.venv/bin/python scripts/check_doc_links.py`、`.venv/bin/python scripts/release-check.py lint_typecheck`、`.venv/bin/python scripts/release-check.py secret_scan`、`.venv/bin/pip check` 与 `git diff --check` 均通过；
- 四轨 stub 已生成且只生成 DecisionEpisode v2；v2 validator 和 `evaluate-rules` 均通过，每例 52 个 checks、0 个 Hard Gate、`formal_evaluation_result=false`；
- Guard blocked 与 quiet-hours deferred 的生产 Runtime 边界均通过：ToolInvocation 保留，Notification/Outbox/Receipt/Sink 最终副作用为零；
- 同一输入的 Runtime/Rules 产物逐文件一致；Episode ID 和 track 两类过滤均由自动化测试覆盖。

E2 收口时的已知限制是 Assessment accepted 分支缺少 Plan/Stage Operation 归因。E3.1 已在生产 `submission.check` 契约中补齐真实正反 patch，并用 v3 ACCEPT、多实体干扰项和旧 v2 回归验证关闭该限制；没有用 Oracle 或工具成功状态回填 Delta。本节其余内容仍是 E2 当时的历史证据。

最终提交哈希和干净 HEAD 上的 post-commit smoke 结果由本次开发交接与完成汇报记录；测试产物只写入系统临时目录，不进入仓库。

## 下一阶段 E4–E8 边界

用户已授权修复与 E4 离线准备，当前开始输入边界工作单和四轨协议试跑准备。后续 E4 仍需完成：创建并复核 48 个 Primary Episodes、24 个 Calibration Outputs、正式 Benchmark Release、Split/Mutation Manifest 与数据说明。生产可信注册表当前为空，任何现有 Suite 都不能取得 formal 资格。E5–E8 才能执行有效性实验、人工盲标、真实 Hy3 正式评测、Baseline/Candidate 版本回归、Case/最终报告、Demo 与 Release。

当前 fixed-response Judge 与聚合只证明工程协议，不能复用为 Calibration 标签或 Hy3 能力结果。后续阶段仍须显式授权真实 Hy3、保存正式 Run Manifest，并保持 Judge 不进入产品用户流程、不按评分自动修改 Prompt、计划或用户状态。本轮已授权以 API 余额 14 元为总上限，先开展四轨协议试跑，再推进 E4 数据生产；E4 后暂停开发，审视评测实施方案与项目方案，不进入 E5 或正式评测。完整 E3.1.2 实现与下一阶段接手边界见 [`第三阶段开发交接.md`](第三阶段开发交接.md)。

## 2026-09-01 第三阶段 E1 收口

当前仓库已在冻结的 E0 v1 契约上完成四轨 Runtime Mini 隔离链路。必须区分“固定 stub 响应经过真实生产 Runtime/工具协议”和“真实 Hy3 正式评测”：前者已经完成，后者没有执行，也不能从 E1 结果推导模型能力。

| 检查项 | 当前结论 | 证据或入口 |
| --- | --- | --- |
| 产品底座 | 已就绪 | H1–H8 共 87 个缺陷 ID 已关闭；规划、成果验收、主动介入、计划调整均有可观察事实 |
| Git 基线 | 已就绪 | `main` 的产品基线为 `036e2b7`；`develop` 已快进包含该基线，后续第三阶段工作进入 `develop` |
| CI 基线 | 已就绪 | 产品基线的八项远端 CI 门禁全部通过；本节不把未来评测代码视为已通过 |
| 对外方案 | 已冻结 | [`腾讯犀牛鸟开源实习第三阶段项目方案.md`](腾讯犀牛鸟开源实习第三阶段项目方案.md) |
| 评测设计 | 已形成可执行版本 | [`腾讯犀牛鸟开源实习第三阶段评测实施方案.md`](腾讯犀牛鸟开源实习第三阶段评测实施方案.md) |
| E0 评测代码与数据 | 已完成 | [`../evaluation/README.md`](../evaluation/README.md)：三份 v1 Schema、递归校验 CLI、确定性摘要和四轨手工协议 Episode |
| E1 隔离执行 | 已完成 | 四轨 Runtime Mini Fixture、独立 Worker/临时库、冻结时钟、资源快照、Recorder、真实 Outbox + Recording Sink、最小 runtime export |
| 正式评测系统与数据 | 部分完成 | 本表是 E1 收口时的历史快照；E2 后 Exporter/Normalizer/Delta/Rules 已完成，Judge、Primary/Calibration、聚合、实验与正式结果仍未实现 |

当前执行边界：

- 第三阶段评价 Hy3 的 `Planning / Intervention / Assessment / Revision` 四类关键决策；产品内的用户学习成果验收仍是另一项职责。
- 评测采用隔离的离线/回放平面，不把 Judge 放入用户主流程，不自动按分数修改 Prompt、计划或用户状态。
- 评测只保存模型可见输入、公开输出、工具调用与状态差异，不保存或评价 `reasoning_content`。
- 正式样本只使用合成 Fixture 与公开资源快照；真实用户库、`.env`、通知地址和外部渠道必须保持零访问/零变化。

E0 契约保持不变：

- `DecisionEpisode v1`、`Acceptable Action Envelope v1`、`Environment Manifest v1` 三份 Schema 未被无版本改写；外部 Schema 注册表仍只有这三份契约。
- canonical JSON / SHA-256、递归校验、结构化错误、引用/Evidence Path/Split/摘要和隐私失败关闭仍是 E1 发布前的最终门禁。
- 四个 `manual_protocol_fixture` 继续只证明协议；它们与新的 Runtime Fixture 分目录保存。

E1 已完成：

- `run-agent` 父进程属于纯 evaluation 控制平面；每个 Episode 生成独立 Worker 和系统临时目录。Worker 在导入 `app.*` 前使用最小环境白名单、禁用 `env_file`、绑定目录内绝对 SQLite、关闭 Scheduler/Email Reply Polling，不启动完整 FastAPI lifespan。
- P/I/A/R 各增加一个公开合成、版本化 Runtime Mini Fixture，固定时间、时区、逻辑 ID 与 scripted model/tool-call ID；Oracle 独立存放且不进入模型可见 Context。
- scoped Clock 默认保持真实 UTC，评测作用域内统一冻结 Runtime、Guard、通知冷却/安静时间、RunEvent、Notification、OutboxReceipt 和相关工具时间；确定性身份只在 Fixture 作用域启用。
- Snapshot Provider 只命中版本化 query/URL，未知搜索/Open/`resource_save` 失败关闭且不回退 HTTP/DNS；stub Worker 阻断全部网络。
- Evaluation Model Recorder 通过已有 `AgentRuntime.client` 注入，原样转发 stream/non-stream 对象，只记录公开 messages/output、system/tool digest 与 Function Call；system message 正文只形成 version/digest，产物使用稳定占位。私有推理字段在投影前递归删除，不复制、缓存、散列、计数、导出或评价，字段名和文本值再经 `privacy_issues` 失败关闭。
- “假 Outbox”已按真实语义实现为生产 `notification_send → Guard → Intervention/Notification → OutboxAction claim/fence/idempotency → Recording Delivery Sink → OutboxReceipt`。带类型 Adapter 只注入 SMTP/Web Push Provider 最后边界，生产默认行为不变；SMTP Receipt 为 `accepted`，Web Push 保持 `delivered`。Agent 只观察 `pending_delivery`，看不到模拟 Receipt。
- Recording Sink 只接受 SMTP/Web Push，拒绝 workspace file、subprocess 和未知 destination；只保存稳定 action identity、安全摘要与字段元数据，不保存地址、endpoint、Push keys、认证信息或完整载荷。SMTP、SMTP_SSL、Web Push、IMAP、IMAP_SSL 调用均由陷阱证明为 0。
- E1 最小投影从 Recorder 和临时生产数据库读取模型回合、ToolInvocation、RunEvent、Operation、Guard、通知和 Outbox 事实，把随机数据库身份映射为 Fixture 逻辑 ID/ordinal；结果不从 Oracle 或 scripted expected answer 回填。四个 `runtime_export` 均通过 E0 validator，同输入重复运行 canonical bytes 一致。
- 输出先在 staging 完整执行隐私/摘要/Schema 校验后原子发布，已有目录不覆盖，任何失败不留半成品。real 模式需要 CLI 双重 opt-in 和调用者环境 Key；本次没有执行真实 Hy3。

E1 定向验收（本次实际执行）：

- `.venv/bin/pytest -q evaluation/tests`：`65 passed in 76.11s (0:01:16)`；
- `.venv/bin/pytest -q tests/test_e1_evaluation_seams.py`：`13 passed in 0.82s`；
- `.venv/bin/pytest -q`：`1140 passed, 2 warnings in 2126.35s (0:35:26)`，0 failed、0 xfailed；两条 warning 分别为既有的 Starlette/httpx 弃用提示和 Python 3.14 tar 提取行为预告；
- 四轨 stub `run-agent`：`run_agent_ok episodes=4 tracks=assessment:1,intervention:1,planning:1,revision:1 invocation_mode=stub formal_evaluation_result=false evaluation_status=not_a_formal_model_evaluation`；
- E0 CLI 校验四轨运行产物：`dataset_valid episodes=4 tracks=planning:1,intervention:1,assessment:1,revision:1`；
- 同一四轨 Manifest 两次运行目录逐字节一致；四个 Worker root 和 SQLite 均不同，销毁后不存在，仓库未发布 SQLite/WAL/SHM。

本节是 E1 收口时的历史事实。此后 E2 已用通用 v2 Exporter 替换该最小投影且未推倒 E1 Runtime Harness，E3 也已完成 Judge/盲化/聚合工程闭环；Primary/Calibration、实验、正式评测、版本对比、报告和 DecisionBench v1 发布仍属于 E4–E8。

另一个开发进程应先阅读 [`第三阶段开发交接.md`](第三阶段开发交接.md)，一次只推进一个可验收切片；完成后同步本文件、相关实施方案与测试证据。

## 2026-08-18 全盘审查结论（历史）

本节及后续 H0–H8 小节保留各阶段收口时的事实快照；其中“冻结、等待第三阶段计划”是当时的范围决定，不覆盖本文顶部 2026-09-01 的当前开发状态。

- `develop` 暂不具备 V2 Alpha 发布条件，也不应继续在当前 Evidence 投影上实现 M15 learner state。
- 审查确认了审批拒绝恢复、当前工具 checkpoint、queued Run 恢复、SQLite 长事务、Evidence undo/digest/完整投影、跨计划技能 Guard、Session 压缩、handoff、提醒回复和移动导航等 P0/P1 问题。
- 当前代码适合单进程、本机、有人观察的 Demo；不适合无人值守长期运行、无认证个人服务器或不可信代码执行。
- `main` / `v1.1.1` 仍是稳定发布基线；本轮审查没有合并、发布或改写 Git 历史。
- 修复依赖顺序、失败基线、故障注入矩阵、提交纪律和发布条件以 [`V2_HARDENING_PLAN.md`](V2_HARDENING_PLAN.md) 为执行事实来源；逐项状态见 [`V2_H0_DEFECT_MATRIX.md`](V2_H0_DEFECT_MATRIX.md)。

## H0：失败基线与范围冻结

- 已在基线 `baf2564` 上建立 87 个稳定缺陷 ID；原审查没有保存逐项 P0/P1 编号，矩阵明确标记 `priority_source=unrecorded`，不伪造历史分级。
- 已新增 113 个 strict-xfail 测试节点以及迁移、夹具安全、0/1/500 条 Evidence、IMAP commit-before-Seen 等 20 个 passing gate；`xfail_strict=true`，实现修复后必须删除对应 xfail。
- 全局 pytest 数据库已从仓库内固定文件迁到进程独占的系统临时目录；大型迁移、Evidence、并发和故障注入用例进一步使用每用例 `tmp_path`。三类 SQL 夹具均为公开合成数据并由 manifest 固定 hash、schema digest 和行数。
- 测试现会把 Context、workspace、upload 与配置写入重定向到每用例临时目录，并在 session 前后用不输出内容的单向指纹守卫正式 `.env`、SQLite 与运行目录。保护夹具落地前的基线试跑曾触发旧测试对 Git 忽略的 `data/context` / `data/workspace` 写路径；为避免误删预存数据，H0 没有自动清理或恢复这些歧义文件，后续测试已被禁止再次写入。
- 原 41 个场景在 H0 已逐项登记独立输入、字面期望、不变量和 mutant；H4 已将其全部标为 `verified`，逐项执行 production reducer/audit，并由 41 个对应 mutant 独立证明回归灵敏度。
- H0 阶段没有修改生产代码，当时 87 项缺陷全部 open。H1–H8 工程阶段现已关闭全部 87 个 ID、剩余 0 个 open ID；M15–M20 继续冻结。

## H1：迁移、时间与备份基础（已完成）

- 已实现：
  - `schema_migrations` 冻结历史表、显式 schema version、不可变 revision checksum 与 fresh/install upgrade 等价校验。
  - 全局 UTC canonicalization：新增 `UTCDateTime`、`core/time.py`、固定精度 digest 时间串，以及历史 naive 时间按 UTC 解释的兼容策略。
  - crash-safe SQLite 迁移协议：`dry-run → verified backup → candidate publish → post-publish verify/rollback`，覆盖 writer gap、new inode、absent target creator、WAL/rollback journal、killpoint 和安全恢复。
  - 备份发布、迁移交接与回滚会固定并复核路径、目录描述符/inode 和内容摘要；候选库、备份 payload 与工作快照只有通过边界复验才能作为 trusted snapshot，晚到篡改会回滚且不会暴露无效恢复引用。
  - legacy `_write_probe` 仅接受精确的空单列旧残留；任何额外列、约束、行、index、trigger 或 view 均按未知 schema 失败关闭，规范 schema 不保留探针表。
  - 新的维护入口 `scripts/data-maintenance.py` 与 `app.db.maintenance`：`preflight / backup / verify / restore / recover / migration-backup-verify / migration-backup-restore`。
  - `scripts/rebuild-evidence.py`、`reset-data.sh`、`seed-fixture.sh`、`demo-data.sh` 均切到 H1 维护协议，避免在备份前修改原库。restore 在任何锁或写入前拒绝把目标数据库或安全备份根目录放在 source backup 本身或其子路径中；source backup 位于安全备份根目录下仍是正常布局。共享 lexical path normalization 消除 `.`/`..` 别名但不跟随 symlink，SQLite URI 对空格、`%`、`#`、`?` 安全。

- 已验证（H1 收口历史快照）：
  - H1 定向套件共 `281 passed`：`tests/hardening/test_h1_time_contracts.py` 10、`test_h1_migration_protocol.py` 183、`test_h1_maintenance.py` 79、`test_h1_rebuild_coordinator.py` 5、`tests/test_migrations.py` 4。
  - H0 跨阶段回归为 `27 passed, 17 strict xfailed`；剩余均属于后续门禁的已登记预期失败。
  - 全量回归为 `441 passed, 98 strict xfailed`，0 unexpected failure、0 XPASS；`python -m compileall backend tests scripts`、`pip check`、前端生产构建、完整与 production-only `npm audit` 以及 `git diff --check` 均通过，npm audit 为 0 vulnerabilities。

- H1 收口时尚未实现：
  - 当时的 H2–H8 均未完成；H2–H8 工程门禁现已完成，外部真人采用验证被冻结。

- 已知限制：
  - 历史 naive 时间会按 UTC 解释；旧数据若原本丢失本地偏移，H1 不会伪造恢复不存在的时区信息。
  - 迁移与维护协议只支持受控 lifecycle lease；绕过 Runtime/maintenance 直接写 SQLite 的外部进程不在兼容承诺内。
  - H1 收口时 `frontend` 仅有基于 Node test runner 的 6 个 H2 请求语义回归；H7 后续补齐 Vitest/Playwright 与五尺寸真实浏览器，H8 又完成完整发布门禁。
  - 外部用户安装完成率、连续学习闭环和 7 日留存仍是**待真实用户验证**，不能由自动化测试替代。

- H1 收口时的下一门禁为 H2；该门禁现已完成，当前状态以下一节为准。

## H2：事务、幂等与 Outbox（已完成）

- 已实现：
  - schema revision 2 新增 ToolInvocation request digest、规范参数、effect kind、claim token/version/expiry，以及 `OutboxAction` / `OutboxReceipt` 的约束、关联和状态机。H1 遗留 running invocation 与无 receipt 的外部通知不会伪造 request identity，而是迁移为 `needs_reconciliation`。
  - 统一 Unit of Work 收拢 API、Runtime、service 和 tool 的提交边界；SQLite 写路径在进入嵌套 savepoint 前显式建立 physical outer transaction，防止最外层 savepoint 释放时提前提交。只有可安全重放的短 CAS/事件/receipt 回调执行有界 writer backoff，普通业务事务不会被整体重跑。
  - H2 收口时的 48 个工具按当时注册表分为 `pure_read` 17、`database_write` 21、`external_read` 4、`external_write` 6；H6 将文件读取和 child status/report 纳入可耐久传播信任的 external read 后，当前分类为 14/21/7/6。所有工具公开 `effect_kind`，stable action key 与 canonical request digest 分离；同键同内容精确重放，同键异内容返回 typed conflict；claim token/version 防止过期执行者覆盖新持有者。
  - 数据库写工具把领域对象、Operation、Evidence、LearningEvent、RunEvent 与 ToolInvocation 结果纳入同一提交边界；HTTP、模型、embedding、子 Agent、子进程、SMTP 和 Web Push 等等待不持有 SQLite writer。
  - SMTP、Web Push、workspace 文件和子进程改为 durable outbox intent、独立 dispatcher、receipt 与 `needs_reconciliation`；workspace 写入/删除和 undo 以内容 hash 自动对账，上传与 `.env` 使用跨进程锁、原子替换、文件/父目录 fsync 和失败清理。
  - H2-TXN-001–009 均删除原 strict xfail；同一 request digest 工作提前关闭 H4-EVID-006 的两个节点，planning delegate 在 model wait 前保存确定性 child ID、Context 与 checkpoint，提前关闭 H3-RUN-008。H2 当时只让 H6-CONFIG-001 的原子临时文件节点通过；其余 2 个节点现已由 H6 关闭。

- 已验证：
  - H2 定向为 `84 passed in 160.57s`，其中包含新增的 4 个 physical outer transaction/savepoint 与 nested transaction guard 节点；覆盖原 H0 事务基线、[事务协议](../tests/hardening/test_h2_transaction_protocol.py)、[schema/短事务](../tests/hardening/test_h2_migration_contract.py)、[outbox](../tests/hardening/test_h2_outbox_protocol.py)、[Operation undo](../tests/hardening/test_h2_operation_undo.py)、[真实进程 SIGKILL](../tests/hardening/test_h2_process_faults.py)、[本地文件协议](../tests/hardening/test_h2_local_file_protocol.py) 与 [Memory 事务边界](../tests/hardening/test_h2_memory_transaction_boundary.py)。
  - H2 收口时 H0 为 `49 passed, 84 strict xfailed`，0 XPASS、0 unexpected failure；普通非-hardening 回归为 `139 passed`。
  - 前端 Node 测试为 `6 passed`，生产构建通过，完整与 production-only `npm audit` 均为 0 vulnerabilities；[文档链接测试](../tests/test_doc_links.py) 与本地相对链接检查通过。
  - 全量 pytest：`551 passed, 84 xfailed, 0 failed, 0 XPASS`，耗时 `953.38s (0:15:53)`；唯一 warning 为上游 Starlette `TestClient` 的 `httpx` 弃用提示。

- 尚未实现与已知限制：
  - H3–H8 工程门禁已完成；本节收口时登记的后续缺口不再是当前阻塞项。
  - SMTP、Web Push 与子进程在“外部已接受、receipt 未提交”时只能停止重放并等待人工或 provider 对账；只有 workspace effect 能依据本地 hash 自动恢复，不能把 `needs_reconciliation` 写成 exactly-once 成功。
  - SQLite 是 Context 的事实来源；Markdown 投影使用原子替换，但数据库提交后、投影发布前崩溃尚无跨重启 durable outbox，只能从数据库重建。
  - Evidence amendment/invalidation、完整账本 reducer、Competency scope/revision/undo 已由 H4 验收；`.env` 控制字符与复杂值往返现已由 H6 验收。没有调用真实 SMTP/VAPID，也没有完成外部安装、连续学习闭环或 7 日真人验证。

- 下一门禁：
  - H2 收口时按固定顺序进入 H3；H3–H8 工程门禁现已完成。M15–M20 按当前项目决定继续冻结。

## H3：耐久 Run、Queue 与子 Agent（已完成）

- 已实现：
  - schema revision 3 冻结 Run phase/state version、版本化 checkpoint、lease/retry、审批事实和 Queue CAS/位置约束；fresh 与 frozen H2 upgrade schema 完全等价，无法证明身份或进度的 legacy 状态 fail closed 到 `needs_reconciliation`。
  - `app.runtime.state` 统一主 Run、planning child 与通用 child 的 claim、checkpoint、审批、retry、finalize、cancel 和 restart reconcile。lease heartbeat 跨模型/工具等待续期，token/version fence 阻止旧执行者覆盖新 owner。
  - current/remaining tool、ToolInvocation、Context/cards/budget 进入 checkpoint；第二次中断保留上一检查点。审批 approve/reject/answer/note/decided_at 耐久且幂等，缺少可信审批身份不会默认批准。
  - final message、Run output/budget/终态、checkpoint/lease 清理与 Queue successor 在短事务中全有或全无；stable message/event key 防止重复回复。late steer 必须消费或转换为下一条耐久 Queue 消息。
  - 同 Session、计划 heartbeat、邮件回复和 `needs_reconciliation` 根 Run 使用统一 scope 仲裁。Queue position 由 session/stateless partial unique index 强制，编辑、删除、发送和自动推进采用 version CAS 与无冲突两阶段重排。
  - 主/子 Run 共享 model/tool/network/elapsed/token/cost 预算、有界可观察 retry、lease 与恢复协议；child finalizing 冻结报告，终态与父 `subagent.completed` 同事务投影或幂等修复，父取消会等待 child task 收口。
  - 前端集中定义 blocking/streamable/steerable Run 状态，`retry_wait` 与 `needs_reconciliation` 不再被误判为空闲；审批 wake 使用稳定 key，在旧暂停 task 收尾窗口只交接一个 successor。

- 已验证：
  - H3 定向 `40 passed`；revision 3 migration 专项 7 passed，迁移/H2/H3 合集 25 passed。覆盖真实 SIGKILL、双进程 claim、SQLite busy、审批、原子 finalization/Queue、late steer、父子投影、4 类 child 预算、tool/model wait 分类、finalizing 无二次模型调用和固定种子 100 轮语义 oracle。
  - 多工具恢复在第二个数据库副作用已提交、Run checkpoint 尚未推进时中断；恢复虽重放同一 invocation，最终仍只有 2 条领域事实、2 个 committed invocation 和 2 个完成事件，顺序一致。
  - 真实 Hy3 使用临时数据库完成双中断：同一 Run 两次 SIGKILL、三次 claim，最终 `completed`、checkpoint 清空、1 条 assistant message、1 个 completed event；未输出正文、API key 或用户数据。
  - 前端 Node `9 passed`、`npm run build` 通过、`npm audit` 为 0 vulnerabilities。最终全量 pytest 为 `605 passed, 74 xfailed, 0 failed, 0 XPASS`，耗时 `1104.08s (0:18:24)`；唯一 warning 为上游 Starlette `TestClient` 的 `httpx` 弃用提示。

- H3 收口时尚未实现：
  - 当时的 H4–H8 均未完成；H4–H8 工程门禁现已完成，真人采用观察被冻结。

- 已知限制：
  - SQLite lease/CAS 是共享单库的执行 fence，不是多节点 scheduler、leader election 或分布式数据库协议；部署仍以单机进程生命周期为边界。
  - `needs_reconciliation` 明确停止自动重放，SMTP/Web Push/subprocess 等不确定外部结果仍需人工或 provider 对账。
  - `assistant.delta` / reasoning delta 仍是进程内瞬时流；断线以耐久 checkpoint、事件和 final message 恢复，不承诺逐 token 回放。
  - 外部用户安装完成率、连续学习闭环和 7 日留存仍为**待真实用户验证**，不能由本阶段自动化或 Hy3 演示替代。

- 下一门禁：
  - H3 收口时下一门禁为 H4；H4–H8 工程门禁现已完成。M15 learner state、FSRS 与自适应动作仍按当前项目决定冻结。

## H4：Evidence 与 Competency 事实层（已完成）

- 已实现：
  - schema revision 4 建立不可更新/删除的 Evidence fact ledger、不可变 Artifact snapshot、Evidence↔Artifact/Competency/Operation 关联、Competency graph revision/mutation/dependency 与增量 projection watermark；canonical checksum 为 `851f34b9c3d455208b73c6815856da70edc57e52d5676f8b6b391e8ddf1b0ace`。
  - migration 仅回填源库能够证明的旧 Evidence、Artifact、assesses 与 Operation 关联；不读取可变文件 URI，不按标题/Run 猜测身份。跨 scope、环、多分支、悬空 FK 与 hash 损坏 fail closed；fresh、frozen revision 3 和 partial-M13 升级收敛到同一 schema。
  - submission/quiz/task 每次行为只产生一份 primary observation；undo/redo/correction 以 amendment/invalidation/reinstatement 追加事实表达，旧事实保留审计且退出 active projection。Artifact 保存 canonical envelope、bytes、size、hash 和来源 scope。
  - 全量与增量 reducer 读取完整账本，持久化 watermark 并用 full oracle 自检；CLI、Context、tool 和 HTTP API 复用相同 projection/digest。0/1/500/501/10,000、关闭重开与 SQLite backup restore 均覆盖。
  - Evidence 根据 task 的 `assesses` 在同一 UoW 快照多个 Competency；SQL 在 pagination/limit 前过滤。Competency global/plan key、edge/link scope、图 revision 与依赖撤销均由数据库约束和后端 Guard 强制。
  - Evidence/Competency 工具使用具名严格嵌套 Schema 并拒绝额外字段；只读 merge candidate 不静默合并或写图。
  - 41 个场景均为 `verified`，逐项运行 production adapter/reducer/audit；41 个 mutant 各自改变真实输入或共享生产 seam，并由独立 failure code 捕获。

- 已验证：
  - H4 场景矩阵 `124 passed`；H0 contract、V2 baseline 与场景矩阵合集 `131 passed`。
  - revision 4 migration contract `11 passed`，覆盖 H4 semantic backfill、history、main replace 与 publish verify 的真实 SIGKILL；11 个 migration candidate kill-point 也已通过。
  - Evidence projection 边界 `6 passed`，覆盖 0/1/500/501/10,000、全量/增量/CLI/Context/tool/HTTP、重开与备份恢复。
  - H4 最终非迁移定向集合 `230 passed in 205.63s`；文档与相对链接 `3 passed`。Python compileall 与 `pip check` 通过；前端 Node `9 passed`、生产构建成功，完整与 production-only `npm audit` 均为 0 vulnerabilities。
  - H0/H1/revision 1–4 迁移全链 `237 passed in 1026.53s`，包含 11 个 candidate kill-point 与 H4 semantic/history/publish SIGKILL。临时 fresh revision-4 数据库为 `user_version=4`、4 条不可变 migration history、`integrity_check=ok`、0 FK violation，schema checksum 精确为 `851f34b9c3d455208b73c6815856da70edc57e52d5676f8b6b391e8ddf1b0ace`。
  - H4 收口时最终完整 pytest 为 `822 passed, 50 xfailed, 0 failed, 0 XPASS`，耗时 `1470.71s (0:24:30)`；当时 50 个 strict xfail 全部属于 H5–H8 的已登记门禁。唯一 warning 为上游 Starlette `TestClient` 的 `httpx` 弃用提示。

- 尚未实现：
  - H4 收口时尚缺 H7 复杂技能只读视图/真实 Chrome 与 H8 首启/Release/真人验收。H5–H8 工程门禁现均已完成；真人采用验证与 M15–M20 冻结。

- 已知限制：
  - migration 对不可证明的旧关联选择 fail closed 或保留未关联事实，不伪造历史；`legacy_unavailable` Artifact 不会被当作 durable evidence。
  - graph revision 为单 owner 的可靠递增失效标记，不是跨节点共识协议；SQLite 仍是单机共享数据库边界。
  - 外部安装、连续学习闭环与 7 日留存仍为**待真实用户验证**。

- 下一门禁：
  - H6–H8 工程门禁已完成；真实用户验收被冻结且未执行。

## H5：Context、Memory 与 Intervention（已完成）

- 已实现：
  - schema revision 5 `h5_context_intervention_facts` 新增 Context generation、typed provenance node/edge、SessionCompressionState、不可变 SessionHandoff、MemoryLifecycleEvent、ContextSnapshotBlock、ProactiveDecision、Intervention 与 InboundMailJob；canonical checksum 为 `878d69dc13324434678716be6c4d05bfa77ff80c25e16d157576ec6a5458ec45`。
  - Session 压缩使用短事务 claim、完整 UTF-8 分块、连续 `(created_at,id)` coverage 与 source version/generation CAS；失败和并发竞争不推进 cursor。handoff 以创建时内容、来源与 Context generation 冻结，重复请求精确复用。
  - ContextAssembler 按 typed whole block 选择候选，分别持久化 retained/dropped manifest、reason、source version/digest 与 budget breakdown；全局 Session 不因 discussed/created/focused relation 获得计划私有读取权限。
  - root、child、planning、title 和 compression 每次模型调用前都使用同一 PromptEnvelope，预算覆盖 system、完整 messages、tool schema、output 与 tool-result reserve；超窗时 provider 调用数保持 0。
  - Memory 来源和生命周期版本化；状态、pointer、digest、source edge 与事件通过同一 CAS 收口。检索先做绝对相关性门槛，再做 RRF 与 scope/layer 配额；provider 等待后重验版本、有效期与 provenance。`legacy_unverified` 和 pointerless `valid` 行不会进入检索、强化或维护。
  - 消息编辑保留 Revision，并沿 verified provenance 递归失效 Summary、Memory、Snapshot 与 handoff；legacy 不完整来源只保守失效、不猜测图。编辑提交与唯一 rerun 在进程中断后可恢复。
  - 一次逻辑 Intervention 只有一条 canonical assistant message 和稳定 reply token，多渠道 Notification 只是 delivery。reply target 与 read-only execution mode 耐久贯穿 Queue、Run、Message 与 child，Run 终态和 Intervention outcome 同一 UoW 收口。
  - IMAP 使用 `BODY.PEEK[]`；唯一 UID/UIDVALIDITY inbound job 先提交，再通过 ACK lease/CAS 标 Seen。归档计划回复生成可见只读答复或明确失败回执。ProactiveDecision 区分 success、quiet hours、Guard/model/runtime failure，只让成功消耗长期冷却。

- 已验证：
  - H5 全套与 H0 Context/Intervention 联合为 `120 passed in 237.02s`；涵盖 10,000 消息、跨计划隔离、完整分块/失败/并发压缩、handoff、消息编辑、Memory 检索/恢复、Intervention/邮件/主动决策及真实进程恢复。
  - 旧兼容、产品不变量、事务边界与 child 预算调用面为 `109 passed in 158.93s`；诊断全量发现的 12 个旧契约/夹具节点复跑为 `12 passed in 14.49s`。
  - revision 1–5 migration bridge 为 `48 passed`：H2/H3 21、`tests/test_migrations.py` + H4 15、H5 12；H5 包含 7 个真实 SIGKILL 点，fresh 与 frozen revision 4 升级结构相同，`integrity_check=ok`、0 FK violation。
  - H5 另覆盖 notification、Queue、mail ACK 与消息编辑的 9 个真实进程 SIGKILL 节点；before-commit 全回滚，commit 后重启按稳定身份收敛，不重复 Run、消息、delivery、job 或 ACK。
  - 最终完整 pytest 为 `941 passed, 30 xfailed, 0 failed, 0 XPASS`，耗时 `1999.56s (0:33:19)`；30 个 strict xfail 全部属于 H6–H8。唯一 warning 为上游 Starlette `TestClient` 的 `httpx` 弃用提示。
  - Python compileall、`pip check` 与 `git diff --check` 通过；前端 Node `9 passed`、生产构建通过、`npm audit --audit-level=low` 为 0 vulnerabilities。文档链接测试 `3 passed`，本地相对链接无缺失。

- 尚未实现（H5 收口历史记录，已按当前状态更新）：
  - H6–H8 工程门禁现已完成；真实用户验收按项目决定冻结且未执行。
  - M17 的完整产品化 ContextPack、M18 学习者状态及 M15–M20 仍冻结；H5 只完成可信来源、预算、连续性与 Intervention 地基。

- 已知限制：
  - SQLite 仍是单机事实边界；Context Markdown 是可重建投影，数据库提交后、Markdown 发布前崩溃尚无跨重启 outbox，但不会丢失权威结构化事实。
  - UTF-8 byte 估算是保守上界，不等同供应商计费 tokenizer；它保证不低估公开请求负载，但可能提前拒绝本可容纳的请求。
  - 无法证明来源的 legacy Summary/Snapshot/Memory/handoff 保持 `legacy_unverified`，不会被迁移猜测为有效图。
  - 没有调用真实 SMTP、IMAP 或 VAPID；外部安装、连续使用和 7 日留存仍为**待真实用户验证**，不能由自动化测试替代。

- 下一门禁：
  - H5 收口时的后续 H6–H8 工程门禁已完成；M15 learner state、FSRS 与自适应动作继续冻结。

## H6：安全运行边界（已完成）

- 已实现：
  - `DeploymentPolicy + DeploymentBoundaryMiddleware` 区分 `local/server`。local 的启动参数与直接 ASGI server scope 都必须是 loopback；server 缺少至少 32 字节 bearer token、精确 HTTPS public origin 或相同 CORS 时配置加载失败，并在请求层拒绝非 HTTPS scope。全部 `/api/v1` 需要 bearer 或签名短会话，Cookie 写请求还需精确 Origin 与 CSRF cookie/header。
  - 当前构建没有 capability-attested sandbox Provider。`code_execute` 不进入模型 surface，registry 直接调用与历史 subprocess outbox 都再次检查并失败关闭；内部 runner 仅保留绝对解释器、固定 `/usr/bin:/bin` 和最小环境的兼容原语，不作为支持的 sandbox 能力。
  - Web/File/Email 输入形成耐久 `external_untrusted` authority。外部读取后的数据库写或外部写必须使用同 Run、invocation、tool call、request digest 与 live claim 的已消费 `RunApproval`；parent/child 继承 taint，结构损坏永久拒绝。H5 的只读邮件回复只保留数据库验证过的原 Intervention/原渠道能力。
  - Web fetch 在发送前解析全部地址、拒绝任一 non-global 结果，以数值 IP 发包并保留逻辑 Host/TLS SNI，响应后精确复核 peer；每次 redirect 重做。raw wire 与 identity/gzip decoded bytes、Content-Length、encoding、总 deadline 与 exact MIME 分别受限，所有关闭路径有回归。
  - `.env` 更新在接触目标前验证全部 key/value，拒绝 C0/C1/DEL/U+2028/U+2029、symlink/directory/FIFO；目录锁内写随机 0600 临时文件并 file/dir fsync 后原子替换。空值、引号、反斜杠、Unicode、`#`、`=` 与字面 `${...}` 均精确 round-trip。
  - 统一 redaction 动态读取配置凭据，在 tool trace/model observation、RunEvent/SSE、checkpoint/审批投影、ToolInvocation 输出、Context DB/Markdown、诊断错误与 Python logging 边界执行。含配置凭据或 `[REDACTED]` 的工具参数在 durable claim 前拒绝，不创建 Invocation/Approval。
  - 新增根目录 [`SECURITY.md`](../SECURITY.md) 与 `.env.example` 的真实边界；静态应用 shell 不含用户事实，server 模式全部数据 API 均受保护。

- 已验证：
  - H6 五份协议、脱敏边界与 H0 安全基线联合为 `100 passed in 8.11s`；7 个 H6 defect ID 的 strict xfail 已全部删除。H6 收口当时全仓只保留 H7/H8 的预期失败；H7/H8 现均已关闭。
  - H2/H3/H5 真实进程恢复为 `19 passed in 123.82s`；覆盖 subprocess、Run/Queue、Intervention、mail ACK 与消息编辑的 commit 前后边界，重启按稳定身份收敛。
  - revision 1–5 migration bridge 为 `48 passed in 399.85s`；包含 H5 的 7 个真实 SIGKILL 点，fresh 与 frozen upgrade 结构一致，迁移合同验证 `integrity_check=ok` 与 0 FK violation。
  - Web/部署/信任/代码相关旧调用面与 Runtime、H2 outbox、H3 state/child、H5 Context/Intervention 的扩大回归分别为 `111 passed in 40.57s` 与 `97 passed in 167.68s`。
  - 最终单次完整 pytest 为 `1040 passed, 9 xfailed, 0 failed, 0 XPASS`，耗时 `2114.63s (0:35:14)`；9 个 strict xfail 精确属于 H7 五项与 H8 四项。唯一 warning 为上游 Starlette `TestClient` 的 `httpx` 弃用提示。
  - Python compileall、`pip check` 与 `git diff --check` 通过；前端 Node `9 passed`、生产构建通过，完整与 production-only `npm audit` 均为 0 vulnerabilities。
  - 文档链接测试 `3 passed` 且全仓 Markdown 相对链接通过；暂存差异未发现高置信私钥/云 token 模式，正式 `.env` 不在 Git 索引中。H8 后续已完成发布候选级 secret scan 和制品检查。
  - 所有 Web/DNS、Provider、凭据和邮箱测试使用离线 transport、合成值与临时数据库；没有请求公网、真实模型/SMTP/IMAP，也没有读取、修改或输出正式 `.env`、用户数据库与个人内容。

- 尚未实现：
  - 没有真实代码 sandbox Provider，因此代码执行能力保持不可用，而不是降级为宿主 `prlimit`。
  - H6 收口时 server 浏览器登录体验、完整前端事实对账和五尺寸真实 Chrome 仍属 H7；H7 后续关闭前端事实对账/Chrome，H8 完成 local 首次设置、release secret scan 与安装包。普通用户 server 登录页和外部部署采用验证仍未实现。
  - 没有真实 SMTP/IMAP/VAPID 或公网账户验收；外部安装、连续使用和 7 日留存仍为**待真实用户验证**。

- 已知限制：
  - server 是单 owner 边界，不是账号系统或多租户授权模型；静态 shell 可公开加载，但所有用户事实只通过受认证 API 返回。
  - bearer session 是进程间无状态签名；token 轮换会立即使旧 session 失效。反向代理必须终止可信 HTTPS 并保留精确 public Host，不能把 local 模式端口转发到公网。
  - redaction 是纵深防御，不会把既有历史数据库自动改写；若凭据在 H6 前可能进入过对话或持久化事实，应由用户轮换凭据并按备份协议处理，而不是让迁移猜测或静默删除历史。

- 下一门禁：
  - H6 收口时的下一门禁为 H7；该门禁现已完成，当前状态见下一节。M15–M20 继续冻结。

## H7：前端状态架构与 V2 最小闭环（已完成）

- 已实现：
  - 删除 1208 行单体 workspace Store，不保留兼容 facade；新增 session、run、plan、inbox、memory、settings、shell 七个 Pinia 状态域和 generation fence/LKG 公共辅助。所有组件只依赖所属事实域。
  - 新增 Vue Router 与 9 条可恢复路径，覆盖首页、Session、计划列表/详情、Intervention、Memory、Settings 与 Archive；未知或已失效的深链安全回退，不把 route、focus 与已归档计划混为同一事实。
  - 首屏核心请求和可降级域独立提交；optional reject/hang、迟到旧 generation 和局部 malformed response 不再清空 profile/plan/session 等 last-known-good core state。
  - 实时 steer 按 durable `steer_id/sequence` 放在最近一条 user 事实之后；SSE 断线会合并 burst、单飞读取 durable Run/events/messages，按 generation/sequence 排序去重，终态停止且旧 Session 响应不能污染新视图。
  - Intervention ID 在 Notification list/open、深链、composer、Queue reload/edit/reorder/dispatch 中使用顶层 typed 字段；失败保留 target，成功消费后下一条普通消息不会继承。
  - CSS 拆为 token/base/layout/components/responsive 五层，统一桌面侧栏、平板收缩栏和 375px 五项底栏；补 focus-visible、44px 目标、reduced-motion、长内容边界、dialog/drawer 和计划 composer 滚动余量，没有引入平行设计体系。
  - Message/Run/Artifact 统一渲染不可信 Markdown、长 URL、表格、代码和嵌套列表；只有可展开 Run event 使用 button。Trace dialog 支持语义化标题、焦点锁、Escape 和焦点恢复。
  - 计划详情新增 M14 最小只读学习依据：task teaches/assesses、Competency Evidence 覆盖、时间线、来源与 eligibility；明确不把完成进度推断为掌握度。

- 已验证：
  - 前端 Node 契约 `9 passed`，Vitest 为 `5 files / 32 passed`，0 unhandled error；生产构建成功，完整与 production-only `npm audit` 均为 0 vulnerabilities。
  - H0 前端五个历史失败节点已改为调用真实 Vue/Pinia/DOM 测试并全部通过；H7 后端/UI 与 H5 Intervention 定向为 `16 passed`。
  - 真实 Chrome 使用公开确定性 seed、独立临时 SQLite 与临时代码副本；375/768/1280/1440/2560 每次都先创建新的 BrowserContext、在首次 app 导航前设置 viewport。最终 `5 viewports / 43 route checks` 全部通过。
  - Chrome 门禁覆盖 8 条冷深链、100 消息与中英文/长 URL/表格/代码、工具成功/预期失败/子 Agent 事件、M14、Intervention target、移动 Session/Settings、optional 503、reduced-motion、键盘 archive/restore；全部页面无根级横溢、重复 shell/header、加载残留、console/page/request failure。375px 每页额外断言 5 个底栏目标完整绘制，计划最后任务可滚到 sticky composer 上方。
  - 浏览器报告只保存 viewport、route label 和通过状态，不保存数据库 ID、标题或正文；没有读取正式 `.env`、用户数据库、私人备份，也没有调用公网、真实模型或外部邮箱。
  - 最终单次完整 pytest 为 `1048 passed, 4 xfailed, 0 failed, 0 XPASS`，耗时 `1999.71s (0:33:19)`；4 个 strict xfail 精确属于 H8 首启/发布工程。唯一 warning 为上游 Starlette `TestClient` 的 `httpx` 弃用提示。

- 已知限制：
  - jsdom/FakeEventSource 单元层和本机 system Chromium 门禁不等于所有浏览器、字体、GPU 或真实弱网组合；H8 已在 release gate 复用同一真实浏览器矩阵。
  - server 浏览器登录页仍是高级部署限制。首次配置、无 Node 发布资产与 release gate 已由 H8 完成；外部安装、连续使用和 7 日留存明确冻结且没有自行宣布通过。

- 下一门禁：
  - H7 收口时下一门禁为 H8；H8 工程阶段现已完成。当前不启动下一功能门禁，M15–M20 保持冻结。

## H8：首次设置与发布工程（工程已完成）

- 已实现：
  - fresh/no-key/zero-Session 安装进入三步设置：TokenHub/Hy3 最小连接验证、学习目标、第一条 Session。失败不持久化模型回复、不回显密钥、不创建 Run；成功配置通过既有 0600 `.env` 原子协议持久化并立即作用于当前进程。
  - `scripts/build-release.py` 从显式 worktree 构建确定性 tar，写入逐文件 `RELEASE-MANIFEST.json` 和旁路 SHA-256；只包含公开运行时文件与 `frontend/dist`，排除 `.env`、数据库、Context/workspace、Node 依赖、缓存和日志，任何 symlink 输入失败关闭。
  - release `setup.sh` 在已有前端资产时只安装 Python；`start.sh` 永不调用 npm，缺资产返回 `missing_release_asset`。
  - `release-gates.json` 与两个 release 脚本强制八项 gate；CI 使用同名 job，缺项、脏 worktree、命令失败和 secret 命中均失败关闭。
  - H8-BOOT-001、H8-REL-001/002、H8-CI-001 四个 strict xfail 已删除；缺陷矩阵累计 87 fixed / 0 open。

- 已验证（均使用临时/合成数据）：
  - H8 onboarding/settings/release 定向为 `31 passed`；前端 Node `9 passed`，Vitest `6 files / 35 passed`。
  - revision 1–5 历史迁移定向为 `48 passed / 364.69s`；最终 release 的 `historical_migration` gate 运行完整 Python 套件，避免 H8 CI 丢失旧业务回归。Evidence gate `175 passed / 161.73s`；Context gate `43 passed / 39.70s`。
  - H8 独立提交候选的完整 Python 回归为 `1062 passed / 0 failed / 0 xfailed`；提交后的八项 `release-gate` 返回 `{"ok": true, "missing_gates": [], "failures": [], "error": ""}`。
  - Python `pip check` 与 `pip-audit` 为 0 已知漏洞；前端 production audit 为 0 vulnerabilities；Ruff 高置信静态规则、Python compileall、JS syntax 和 secret scan 通过。
  - 首启向导真实 Chrome 为 375/768/1280/1440 四个冷启动 viewport；完整产品矩阵继续为 375/768/1280/1440/2560、`43 route checks`，两者均从临时代码副本和临时 SQLite 启动。
  - worktree release 的确定性、manifest、checksum、排除项、symlink fail-close、内置前端、无 Node 启动与缺资产错误均有自动化回归。

- H8 收口时冻结与未执行（历史边界，第三阶段评测已于 2026-08-31 另行启动）：
  - 未邀请外部用户，不记录安装完成率、完整学习闭环或 7 日留存；这些不能被上面的自动化结果替代。
  - 未创建 tag、GitHub Release、`main` 合并或正式 2.0 Alpha 声明。
  - 当时不实现 M15–M20，也不编写第三阶段比赛评测/报告/Demo 方案；H8 独立提交后停止。该停止条件已经由当前第三阶段计划取代，但 M15–M20 仍不在本阶段范围。

## V2 已验收的产品底座

- `EvidenceObservation` 是追加式事实层，带来源、计划/任务/Run/Session、评分、提示/迁移等级、Rubric 快照、因果链和幂等键；没有编辑或物理删除路径。
- `submission_create`、`submission_check`、`quiz_grade` 和带证据的 `task_patch` 会双写账本；同一幂等键重试只返回原观察。
- `study_state_get`、计划 Context、CLI、tool 和 HTTP API 已接入同一个无截断 Evidence projection；全量/增量 digest、撤销和一次行为一份主观察语义已由 H4 验收。
- `scripts/rebuild-evidence.py` 已提供重建、审计、回填和派生快照命令；H1 已证明纯 audit 不建表、不迁移、不写入，任何回填/重建写操作都会先取得协调 lease 并完成全量验证备份。
- Artifact 保存 canonical envelope、耐久 bytes snapshot、size、hash 和 scope；H2 request identity 与 H4 完整性审计共同覆盖同键冲突、源文件删除、篡改和不可用 legacy 来源。
- 当前 41 个场景均有独立 production baseline 与对应 mutant；这证明 H4 事实层回归灵敏度，不替代被冻结的真实用户采用验证。M14 最小只读前端已由 H7 验收。

## 当前结论

Learning Agent 已形成真实可运行的个人学习 Harness 原型，而不是一次问答式聊天页面。Hy3 在统一 Runtime 中读取分层上下文、调用工具、观察结果并继续决策；H1–H8 工程门禁已验收迁移、事务、Runtime、Evidence、长期 Context/提醒线程、应用安全边界、前端闭环、首启和发布工程。真人采用验证仍未执行，当前不作 V2 Alpha 发布声明。

`main` 是可发布分支，`develop` 用于集成下一版本。发布前必须在 `develop` 完成测试、浏览器回归和文档同步，再合并到 `main`；不再保留“main 固定为旧归档快照”的历史约定。

## 已实现的正常路径能力

以下条目描述可运行能力；H1–H8 工程门禁已通过对应崩溃恢复、并发、迁移、长期上下文、应用安全、前端和发布验证：

- 对话式计划制定：需求不充分时由 Agent 生成结构化提问卡；充分后可委派只读子 Agent 调研，汇总成可审阅提案，用户采用后才创建正式计划。
- 计划执行与调整：Agent 能读取学习位置、计划版本、资源、事件和提交，教学下一步、修改阶段/任务、安排复习和日历；H2 已验收 action identity 与统一 UoW，H4 已验收 Evidence/Competency 作用域 Guard、invalidation 和图依赖撤销。
- 学习证据与考核：支持文字、文件、代码和链接提交；Agent 可读取文件并按可证明内容/Rubric 验收、评分和安排复习。当前没有 sandbox Provider，不能执行提交中的代码。
- 主动性：单实例心跳先筛选候选，再启动计划级 Hy3 Run 决定是否干预；终态 ProactiveDecision 区分成功、quiet hours、Guard/model/runtime failure。提醒以唯一 Intervention/canonical message 为身份，多渠道 delivery、活动 Run reply target 与 IMAP ack 已由 H5 验收。
- Session 管理：对话自动语义命名，支持手动改名、归档、恢复和非破坏式消息编辑。全局 Session 创建计划后通过 `SessionPlanLink + handoff_summary` 显式过渡到新的计划 Session，不静默改绑。
- Harness 可观察性：每条 Agent 消息内包含可折叠 Run；工具、审批、子 Agent、失败和预算可以逐项展开。H3 已验收主/子 Run 的版本化 checkpoint、lease、耐久 retry、原子终态/父投影和共享预算；H5 已验收提醒线程事实，H7 已验收 SSE/UI durable REST 对账与事件排序。
- 产品状态一致性：同一 Session/计划根 Run 由统一 scope 仲裁，Queue/late steer/审批和终态 successor 已耐久闭环。Evidence 与 graph dependency 已由 H4 验收；H5 又使 reply target、Context 来源图和派生失效成为耐久事实。

## Context 与 Memory

- Context 按 Global/Profile/Plan/Session 分层组装；SessionPlanLink 只参与关系和排序，global Session 不会据此读取计划私有事件、测验、提醒、复习或日历。
- V2 Evidence 账本与统一投影已通过 H4：0/1/500/501/10,000 条在在线、Context、CLI、tool、HTTP、关闭重开和备份恢复后具有一致 digest。
- 长 Session 保留原文，摘要通过 claim、完整分块和连续 coverage CAS 写入；模型失败、并发丢 claim 或消息版本变化都不会推进 cursor。不可变 handoff 冻结创建时来源，不受源 Session 后续对话改写。
- 长期记忆先以 proposal 存在；确认后才进入检索。重复内容会在同一版本化来源图上强化原记录，用户纠正保留替代链，状态/pointer/digest/lifecycle event 原子推进。
- 记忆归档/恢复保留仍在未来的 `expires_at`；真正过期后的恢复是显式 renewal。无法验证来源的 legacy/pointerless 行不会进入检索、强化或维护。
- 检索在 BM25/本地 SimHash 与 RRF 排序前应用绝对相关性门槛，再执行 scope/layer 配额；provider 等待后重验版本、有效期和来源，只有最终命中项增加访问遥测。
- 每个 Run 保存带 generation fence 的 `ContextSnapshot` 和 normalized blocks；retained/dropped 来源、原因、版本、digest 与完整 PromptEnvelope 预算均可审计。消息编辑沿 verified 来源闭包失效旧派生物。
- SQLite 是事实来源，`data/context/global.md` 与 `data/context/plans/{id}.md` 是不含 Session 对话的最新可读投影，`data/context/runs/{run_id}.md` 是该轮精确输入副本；原始对话、事件、摘要版本和历史快照仍保留在数据库。

## H0 验证

- 修改前基线：`pytest -q` 为 126 passed；该数字只证明旧正常路径测试通过，不是领域正确性证明。
- H0 定向：`pytest -q tests/hardening -rxX` 为 20 passed、113 xfailed、0 XPASS、0 failed。节点覆盖拒绝/崩溃/二次恢复、真实 SQLite 锁、重复请求、外部副作用不确定、跨计划隔离、编辑/归档/恢复、安全与前端状态契约。
- 解除登记验证：`pytest -q tests/hardening --runxfail --tb=no` 为 20 passed、113 failed；113 个登记节点全部在旧实现失败，没有被夹具错误伪装成通过。
- 全量回归：`pytest -q -rxX` 为 146 passed、113 xfailed、0 XPASS、0 failed；唯一警告是现有 FastAPI TestClient 的 `StarletteDeprecationWarning`。
- 静态与依赖门禁（H0 历史）：当时 Python `compileall`、`pip check`、前端生产构建、完整 `npm audit` 和 `npm audit --omit=dev` 均通过；两次 npm audit 均为 0 vulnerabilities。H7 后续补 Vitest/system-Chromium，H8 又完成可执行 release gate。
- 数据库夹具 passing gate 校验 manifest/hash、无个人数据扫描、`integrity_check`、`foreign_key_check`、schema digest 与边界行数；所有物化副本都在 pytest 临时目录。
- 真实 Chrome 冷启动（H0 历史）：当时使用独立临时 SQLite 和精确双 Session 合成令牌，稳定复现 H7-UI-001 的 3 项 expected failure。H7 已用新的公开 seed 和五宽 43 项普通断言取代该失败基线，当前结果见 H7 节。
- H0 没有调用真实模型、SMTP、IMAP 或公网，也没有用 SQLite 打开、查询、迁移、复制或修改正式用户数据库，没有输出、复制或修改 `.env`/个人数据。应用配置导入仍可能按生产启动方式读取本地 `.env`，但测试会先用合成环境值覆盖外部凭据且不打印内容；保护夹具另在本机计算不输出的单向完整性指纹。

## 明确边界

- 产品只面向单个 owner；local 模式只接受 loopback，高级 server 模式必须认证且不是多用户账号系统。local 首次配置与安装包已由 H8 验收；普通用户 server 登录页仍未提供。
- 当前没有安全 sandbox Provider，`code_execute` 在所有部署模式保持不可用；内部 host runner 不能作为公开能力启用。
- 当前日历是应用内日历，不宣称与系统/Google/Outlook 双向同步。
- Service Worker 通知需要浏览器仍在运行；电脑关机或浏览器完全退出时不能被本地服务唤醒。
- SMTP/IMAP、VAPID 和模型调用依赖用户自己的供应商配置及本地进程持续运行。
- 子 Agent v1 默认只读；业务写操作回到主 Agent，避免多个执行体竞争修改计划和长期记忆。
- 外部用户安装完成率、连续学习闭环和 7 日留存均为**待真实用户验证**；H0 的合成夹具、自动化测试和 Chrome 冷启动不能替代 H8 的真实样本记录，也不能由 Agent 自行宣布完成。

上述边界不影响受控本机 Demo；H2–H8 已关闭工具事务、幂等、外部副作用围栏、Run 恢复、Evidence/Competency、Context/提醒线程、应用安全、移动前端、首启和发布协议。外部采用验证尚未执行，不能在界面或文档中写成已完成；第三阶段评测按本文件顶部的当前状态继续推进。
