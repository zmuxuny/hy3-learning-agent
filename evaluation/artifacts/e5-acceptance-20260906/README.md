# S06 与 E5 验收证据 · 2026-09-06

从 `fix/evaluation-audit-readiness@5e7a4fb` 接续。S06 机制修复、E5 两项必需实验、真实浏览器连续流程与实际问题修复完成。主 AI 开发、体验及内容裁决，同一个子 agent 参与部分 Runtime 修复和 AI 审计；没有独立人类盲标。本轮仅本地提交，未推送或发布。

## 结果与入口

V2 固定干净 `40fddb8`：24 个 Calibration 首轮，预设 Good/Mild 16 例各 5 次（复用首轮，加 64 次），**88/88 有效，88 个请求，0 修复/最终失败**。严格排序 **6/8**，Good>Severe **8/8**，构造 Critical 联合召回 **8/8**；结论一致 **96.25%**，平均总体标准差 **2.243425**。实验完成与最低预设方法目标达标分别计算，均为真。

Assessment 两个 Mild 五轮均被误评为 100；Planning C01/C02 Mild 结论一致性只有 80%/60%，Planning 轨平均标准差 5.942769。重复集 80 条均无 Gate 建议，Gate 一致 100% 不证明正例稳定识别。全部真实 Severe 同时有 Rule Gate，纯语义 Critical 机制单独由工程反例验证。当前为 Calibration 方法结果，正式未见 Primary、可信登记和 E6–E8 未完成。

| 文件 | 内容 |
| --- | --- |
| [完整公开证据](e5-evidence.tar.gz) | V1/V2 全部运行、结果、失败、公开响应、裁决、CSV、源码绑定、计费、测试日志、浏览器截图及脱敏事实 |
| [文件清单](artifact-index.json) | 压缩包及每个成员的字节数、SHA256；原始字节可核对 |
| [验收清单](validation-summary.json) | 里程碑、分版本回归、预算、浏览器及实际限制 |
| [方法判断](method-validation.json) | 分开计算实验完成与最低指标达标，绑定指标和缺陷内容裁决摘要 |
| [指标与分母](metrics.json) / [逐次结果](per-evaluation.json) | 8 组三档、16 例重复的每次分数、维度、结论、Gate、标准差、极差及失败分母 |
| [缺陷内容复核](defect-content-review.json) | 16 个注入缺陷逐项 AI 证据裁决，Rules/Judge/联合召回分别统计 |
| [AI 审计](ai-audit.json) | 固定 V2 源码、88 个结果/请求及 53 个语义提名等 2765 项重算无差异；明确同一子 agent 的身份及边界 |
| [收尾检查](closure-checks.json) | 文档链接、原账本摘要、服务停止、私有状态权限及已清理的本轮临时路径 |
| [费用](budget-summary.json) | 原账本与本轮每个 ticket 归属；余额用整数微元计算后展示 |
| [公开检查](archive-validation.json) | 627 份结构化记录隐私检查、实际密钥零命中、历史账本未变、三个源码快照绑定 |
| [工作记录](../../../docs/E5验收工作记录.md) | 完整“问题—影响—修改文件与行为—验证—剩余缺口”、连续体验与第三阶段余项 |
| [S06 机制记录](../../../docs/S06机制修复记录.md) | 历史可确认事实、独立反例、修复与归因边界 |

## 压缩包布局与版本

解压后根目录为 `e5-evidence/`。

| 目录 | 说明 |
| --- | --- |
| `calibration-runtime/`、`calibration-rules/`、`e5-v1/` | `3ed0541` 的 V1：9 有效、5 最终 response_invalid、10 输入准入拒绝；19 请求含5修复，64次重复未运行，实验未完成 |
| `calibration-runtime-v2/`、`calibration-rules-v2/`、`e5-v2/` | 干净 `40fddb8` 的完整 V2；原 Aggregate 和 `adjudicated-r1-*` 同时保留，53提名为45确认/8驳回/0未决 |
| `sources/v1-3ed0541/`、`sources/v2-40fddb8/`、`sources/final-d8fa199/` | 各版 source bundle 的全部源码文件、协议/候选/资源/engineering资产、设计时工作记录及逐文件摘要；快照文档代表当时进度 |
| `budget/` | 起始190请求的数量/金额/摘要记录、最终330请求完整原账本快照、对账与价格核对；起始摘要记录不是完整起始账本备份 |
| `browser/` | 真实页面截图、后期自动DOM观察、公开数据库投影、后两轮18次调用JSONL、实际Git终端证据、体验AI说明；私有本地目录仅列文件摘要 |
| `intermediate/`、`frozen-schemas/` | 中间候选绑定与诊断目录、44 个原字节不变的 Schema；不赋予中间输出正式身份 |
| `verification/`、`logs/` | S06、SSE、审批等待、摘要的修复前后记录；V1失败、构建与所有阶段测试，包括中间失败/中止 |
| `drivers/`、`preflight/`、`review/` | 实际使用的外部实验/浏览器/内容复核脚本、无损请求预检与内容检查记录；脚本中的临时绝对路径是原运行位置，重用时明确改为自己的位置 |

V2 Prompt 固定为 `hy3-judge-prompt-v3-e5-2`，配置 `hy3-judge-config-v3-budgeted-2`，设计摘要 `2aa3b4a8a45d46fe1465fd19261851ca739b7732ed9cfffa3aaab7a7188e9c74`。Judge 输入完整可逆还原，实际证据路径不变；固定 196608 输入上界与 8192 输出上限，HTTP 不自动重试，最多一次结构修复。后续最终产品修复在 `d8fa199`，当前源码/候选重新绑定，旧实验保持 `40fddb8` 身份。

完整基线是 `3ed0541` 的 **1378 passed / 2 warnings / 4200.96 秒**。最终 `d8fa199` 的 evaluation **292 passed / 1281.85 秒**、产品定向 **34 passed / 17.66 秒**，前端 **45 Vitest + 9 Node contracts** 和构建通过。不同集合有重合，不能相加为最终独立总数。S06 的102项广回归只有当时工具输出摘要摘录，28项定向保留完整简短工具输出；不把摘录伪装成连续完整 stdout。旧 Ruff 全仓基线不是零。

`e5-v2/metrics.json` 中缺陷召回提示由摘要绑定的 `defect-content-review.json` 和 `method-validation.json`补齐。可在固定 `40fddb8` 源码及相应依赖下，对解压后的 `e5-v2` 运行 `evaluation/scripts/run_e5_experiments.py summarize --output <解压路径>/e5-evidence/e5-v2` 离线重算；归档前已核对 `metrics.json` 与 `per-evaluation.json` 重算逐字节一致，零 Provider 调用。真实重跑命令见 [评测包说明](../../README.md)，必须使用新的输出目录和原共享账本，不覆盖本档案。

## 费用与历史保全

原14元账本累计 **11.606238 元**，保守剩余 **2.393762 元**；本轮新增140请求/6.208593元：V1 Judge 19请求/0.906724元、V2 Judge 88请求/4.137979元、真实浏览器33请求/1.163890元。已知usage估算10.042590元，6个未知usage保留1.563648元预留，无待处理reserved。原190请求/5.397645元内容未变；未核对云端账单实扣。原始快照 SHA256 为 `18bf439406ceee17967cba6f5dfd96e3f0deae99a256a7a671cf907b3245ad87`。

V1 当时未保留被拒绝响应原文，不能补造五次无效证据路径。初始15次浏览器调用有账本与产品状态，但没有逐请求公开JSONL，其中两次未知usage继续预留。早期DOM观察只在当时工具返回中，后期才自动存文本。初始17分钟审批等待造成一次completed预算fallback，未完成该用户目标；保留原对话和随后恢复。`browser-approval-after.png` 实际是重启后的首页，不证明审批成功；`browser-revision-reconnected.png` 是终态断连，活跃恢复证据为 `browser-active-reconnect.png`、`browser-final-restored.png` 及对应Run事件。

所有截图和学习证据来自主AI操作的隔离合成学习者。原始隔离数据库、上传文件、展开上下文与练习Git仓库完整保存在本机 `/root/.local/state/learning-travel/e5-browser-20260906`（目录0700、文件0600），不进入公开压缩包。原预算账本仍在其既有路径。公开档案不含 `.env`、数据库、展开Judge输入或私有推理；原产品数据库未用于回归或体验。体验服务及浏览器已关闭，本轮临时副本在归档校验后清理。
