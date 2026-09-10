# E4 内容验收与回归证据

本轮从 `a875ea0` 继续，在 `946d735` 完成 48 Primary、24 Calibration、8 来源的 AI 逐例裁决，以及 8 组三档区分判断。`4c9a632` 随后修正一处并发通知测试对立即成功的假设，产品/评测执行源码不变。复核身份为 `delegated_ai_reviewer`；没有独立人类标注或方法有效性实验。当前输入与裁决见 [候选包](../../datasets/decisionbench-v1-candidate/README.md)，验收与方案结论见 [E4 工作记录](https://github.com/zmuxuny/hy3-learning-agent/blob/1295254e2f8f792da30eb331cb2d0ea12437fa0b/docs/E4%E9%AA%8C%E6%94%B6%E5%B7%A5%E4%BD%9C%E8%AE%B0%E5%BD%95.md)。

| 证据 | 内容 |
| --- | --- |
| [交接输入归档](inputs-at-handoff-a875ea0.tar.gz)及[清单](inputs-at-handoff-a875ea0.tar.gz.inventory.json) | 93 个原始输入/说明文件，保留旧绑定和 pending 状态，逐字节核对 `a875ea0` |
| [当前 Calibration](calibration-runtime-and-rules-946d735.tar.gz)及[清单](calibration-runtime-and-rules-946d735.tar.gz.inventory.json) | 74 个 JSON：24 Episode、24 JudgeReference、24 Rules 和两份 Manifest；0 RuntimeFailure，8 Severe 全部触发 Gate |
| [两轮离线全链](two-offline-full-chains-946d735.tar.gz)及[清单](two-offline-full-chains-946d735.tar.gz.inventory.json) | 每轮 64 个文件、11 Episode + 1 故意 Failure；Runtime/Rules/固定 Judge/Aggregate 均保留，两轮逐字节一致 |
| [独立绑定核验](independent-checks.json) | 44 Schema、4 来源包与真实源文件变异、当前 Suite、盲化投影以及旧 8 个归档的摘要核验 |
| [双轮比较](two-smokes.json) | 逐阶段预期/实际退出码及每个输出的摘要 |
| [环境与预算](environment-and-budget.json) | 实际 Python/Node/依赖版本、前端离线构建、隔离边界、原账本聚合费用与未改写摘要 |
| [源码与输入清单](source-inventory.json) | 511 个代码/输入/配置文件与 `4c9a632` 和主工作树逐字节一致，并记录相对 `946d735` 唯一的测试文件差异；后续只收尾文档与证据 |
| [E4 Ruff 基线](ruff-baseline.json)及[通知测试基线](outbox-ruff-baseline.json) | 三个 E4 Python 文件默认规则 0 条；通知测试文件修改前后均为 16 条，未新增，不代表仓库默认 Ruff 全绿 |
| [失败测试夹具摘要](outbox-failure-fixture-summary.json) | 仅从外部合成测试库只读汇总状态：Invocation 为 retry_pending，没有通知、Outbox 或回执；未复制数据库 |

提交前定向内容/负例 18 passed；`946d735` 全部 evaluation 269 passed，再次包含这 18 项；产品边界、前端及发布复验 24 passed。最终 `4c9a632` 全仓 1350 passed, 2 warnings in 3806.65s (1:03:26)。完整命令与耗时见 [验证清单](validation-summary.json)，原始日志及验证脚本见 [日志归档](validation-logs.tar.gz)与[逐成员清单](validation-logs.tar.gz.inventory.json)。

`946d735` 首个完整回归为 1348 passed、1 failed、2 warnings（4344.99 秒），唯一失败为通知并发测试的 `NoResultFound`，单例复跑曾通过；独立强制 writer busy 反例进一步复现了旧测试立即查询不存在 Outbox 行的假设。修正后四项并发/重试检查通过，包括原场景与强制退避分支；最终完整回归在 `4c9a632` 通过，旧完整批次与独立 red 反例完整保留。

首次临时副本缺少前端依赖和构建产物，定位并离线补齐后已通过前端/发布复验。独立检查脚本参数/字段取法的错误也保留原退出码，随后按实际接口校正；日志归档不会只保存成功尝试。首次尝试之后增加 Node 网络限制，完整重跑同时启用 Python 文件/网络审计、Node 网络限制与 npm offline。副本中的 221 个已安装包版本与锁文件相符，缺少的 28 个包均为 optional。

本轮没有新增真实请求或真实 Judge。原 14 元账本仍占用 **5.397645 元**，保守剩余 **8.602355 元**，保留 4 次未知 usage 的预留；原文件摘要不变。历史真实 Primary 仍为 45 Episode + 3 Failure，属于旧输入。[旧运行档案](../e4-candidate-20260905/README.md)与[诊断修复历史](../e4-handoff-20260905/README.md)均未改写。所有当前结果均 non-formal，production registry 为空。

日志密钥扫描的 4 处匹配来自同一个公开的合成凭据测试参数 ID，已逐项核对源码并保留原文。精确匹配摘要与位置在日志归档清单中；首次归档拒绝及核对记录保存在归档的 `harness/archive-scan-review.json`。这项例外只作用于已确认的测试 ID，仓库 Secret Scan 规则未变。

最终文档、Secret 和 staged diff 检查的命令、原始输出及收尾完整性核对见 [最终门禁记录](final-gates.json)。这份记录在日志归档之后生成，原始输出直接保存在 JSON 中。
