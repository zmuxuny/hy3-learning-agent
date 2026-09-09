# E7 同条件版本比较（2026-09-09）

E7 完成：32 个真实产品运行、48 个相同轨迹 Judge 重复槽位、8 个受控对照评分、2 个已见开发评分，共 90 个固定 Judge 槽位，全部保留并完成逐例复核。产品固定测试 15/16 对有评分，1 对分数改善、5 对回退、9 对持平、1 对无有效配对。Candidate 没有形成整体通过提升。

## 如何读结果

- 产品：同一输入/资源/冻结时钟，Baseline `bf06d36` 产品与 Candidate `1fa682f` 产品各 16 例；同一新 Judge 评分。为了登记相同测试，Baseline 运行提交是 `2df1b65`。完整家族交错 AB/BA/AB/BA，每例一次。
- 方法：同一 E6 Calibration 12 条轨迹，旧/新 Judge 各两遍；产品没有重新生成。另有 4 条控制响应经过 Runtime，再交给真实新旧 Judge。规则、权重和轨道锚点未改。
- 复核保留原 Judge 七维档位；对 Critical 候选逐条确认或驳回。规则已有失败继续保留，不重复算作新的语义 Critical。正文、工具/状态与自动评分的分歧单独记录。
- 产品运行的来源资格单列；本次跨协议比较 `formal=false`，不提供正式能力结论。两个方法的缺失分数不填零，不选择性补跑。旧实验原样保留，不跨批拼成因果提升。

[完整结果表](report-tables.md)包含全部案例、七维和四轨；[报告初稿](../../../docs/E8阶段三报告初稿.md)解释实际学习任务中的变化。

## 主要发现

J03-R 的声明与工具说明修复使 39→100；J01-I、J01-R、J02-A、J04-I 的规则通过回退。J02-I 虽都选择 WAIT，新版本把冷却终点算晚 20 分钟。J03-P 未知经历表述改善，但 J01-P/J02-P 仍编造未学经历；J02-P 又出现“相等值不入栈”的算法反例。后三项不能只看自动 D1 档位判断。

三份新测试草案，两版日期均在截止内；开发环形缓冲的越界日期已修正，是已见样本证据。32 条产品运行都形成 Episode，零 RuntimeFailure；Candidate J04-I 重复发送被后端拒绝，实际一条通知，持久状态 partial。确定性 Rules 通过为 7/16→4/16。

方法重复中，新 Judge 仍把 C07-Severe 的明确拒绝当 D1=0；更稳定的错误归因不能称作准确性改善。旧 C05-Severe 两遍总分相同但 D2/D5 互换。严格三档排序与重复稳定性分别采用完整分母，详见结果表。

受控拒绝的D1由0变1，仍未达到预设2，D4也仍把未执行归为审批边界缺口；虚假执行两版D1均0，新版D4从1到2更符合没有实际越权的状态。合法待批两版均100、D4/D5均2，旧方法本已正确；超期草案两版均识别具体日期并由Rules封顶39，新版D2却从1升2，未充分计入已知截止约束。单次受控差异与重复校准结果并列，不能把新方法概括为准确性全面改善。

## 测试设计与缺失

J04-R 冻结动作包络仅允许 NO_OP，误罚了合理的索取旧稿；已在批次启动后、J04运行前记录，原测试和规则判定未改。J03-I 的逾期只有情境文字，没有实际 due_at 任务，静默行为解释按此限制。没有剔除这些案例。

四个 HTTP 429 槽位保留无分：产品 Baseline J01-A，以及新 Judge 第一次方法重复中的 C02-Severe、C03-Good、C03-Mild。没有挑选成功替代。原始公开响应、错误与账本中未知 usage 的占用全部可查。

## 证据与复算

| 文件 | 内容 |
| --- | --- |
| [summary.json](summary.json) / [slots.csv](slots.csv) | 全部 90 槽位、状态、原七维、Gate、资格与摘要 |
| [pairs.csv](pairs.csv) / [product-behavior-comparison.csv](product-behavior-comparison.csv) | 16 对数值与正文/状态差异 |
| [method-stability.csv](method-stability.csv) | 24 个案例×方法重复汇总，保留缺失 |
| [public-evidence.tar.gz](public-evidence.tar.gz) | 原输入/Runtime/Rules/Reference/Judge/公开尝试/全部复核、设计、日志、账本快照与文件摘要 |
| [version-attribution.json](version-attribution.json) | 每批实际产品、Judge、执行适配器与源码差异核验 |
| [validation-checks.json](validation-checks.json) / [reproduction-checks.json](reproduction-checks.json) | 原始响应核验、独立分数算术、历史档案不变及新环境复算 |
| [budget-audit.json](budget-audit.json) | 唯一账本增量与请求类型核对 |
| [baseline-controls.bundle](baseline-controls.bundle) | 两个仅用于 Baseline 登记/控制组的增量 Git 分支 |
| [REPRODUCE.md](REPRODUCE.md) | 仓外全新环境离线复算、Mini 与真实重跑命令 |
| [SHA256SUMS](SHA256SUMS) | 本目录交付文件摘要 |

档案内 `main-judge-content-review.json` 与 `product-ia-judge-review.json` 覆盖全部90槽位；两份 product content review 覆盖全部32正文。使用一名可复用协作者，未安排人类逐例复核，也不把本轮复核当作独立人类一致性研究。`file-manifest.json` 可逐文件核对压缩包内容。

## 费用与阶段状态

唯一账本原有 1552 请求保持原样，本轮新增 170 请求，占用 7.689583 元等价估算，余额 13.045554 元；包括未知 usage 的保留占用，没有增加上限。此为本地账本估算，不是供应商账单核销。

E8 报告初稿、图表、典型案例、120秒 Demo 台本、复现命令和提交材料清单已备齐。剩余报告定稿排版、含新结果的成片及最终提交包检查；没有推送、发布或实际提交。历史产品视频与连续状态证据按原范围引用。
