# E6 修复、另版验证与新版批次档案

从 `fix/evaluation-audit-readiness@c83c0ef` 接续。已实施产品和方法修复、多版 Development/Calibration、新 G01–G12 测试冻结登记及全批运行。**新版为48 Episode、33有效Judge、15失败；15通过、18不通过、15无分，formal=false。** 正式能力资格未通过，不能将自审、离线验证或高原始分替代它。开发和审计均为AI，一名可复用第二AI参与内容/治理复核，无独立人类盲审。

旧E5实验固定 `40fddb8`；首批E6固定 `b0b0d02`（48/46/2，formal=false），[原档案](../e6-formal-20260908/README.md)及旧Release不覆盖。完整执行预设和逐版修复见[工作记录](https://github.com/zmuxuny/hy3-learning-agent/blob/1295254e2f8f792da30eb331cb2d0ea12437fa0b/docs/E6%E6%96%B0%E7%89%88%E4%BF%AE%E5%A4%8D%E4%B8%8E%E9%AA%8C%E8%AF%81%E8%AE%B0%E5%BD%95.md)。G家族在本轮方法决定结束后才编写、复核、冻结；它们现已见，后续不能冒充新版本未见测试。F01–F12始终是已见回归。

## 实际修改与验证来源

| 版本 / 源码 | 实际变化 | 实验及结论 |
| --- | --- | --- |
| 1.1 / `267127d` | 站内通知按Notification/Intervention事实验证，外部渠道仍查回执；审批意图/实际提交分层；Owner时区统一；证据缺失/歧义与失败分开；Judge可见路径枚举、输出12288、超时180秒、安全失败类别 | Dev7例6 Gate；Calibration88有效，新增复测/Planning判据未全过；全部保留 |
| 产品体验 / `4e6c1c1` | 按两个具体action摘要真实批准intake和proposal，保留采用边界 | 产生1 pending Proposal/0 Plan，暴露3核心任务不要求证据 |
| 1.2 / `2e7a4db` | 核心任务强制证据完整性；弃权规则与包络分层；Planning D3/D5锚点分开 | Dev7例4通过3 Gate；第二个88次校准；真实批准后1 pending Proposal/0 Plan、5核心均需证据 |
| 1.3 / `1ae8cd2` | 写工具Schema接收模型自己生成的原生行动数组，原参数保留，交给业务Guard前只剥离评测字段；memory_search纯读；ready intake继续提案 | 已见Dev7/7无Gate、7/7真实Judge全维2；14模型轮，3原生写声明，0推断声明；保留待审批 |
| 1.4 / `c25da44` | Assessment允许引用已定义的同一失败测试及预期，不能用通过输入替代复测 | 固定20次Assessment与6次时区对照全部满足预设，0失败/repair；并非又跑88次 |
| 冻结1.4 / `1f1f35f` | 方法决定后建立G家族，逐项内容检查、冻结登记并干净提交，按48固定分母运行 | 本页新版批次；新暴露问题保留，未事后改分 |
| 候选1.5 / `3d20c8b` | shared-json最小共享长度512→256、证据路径enum共享引用；输入超限改为typed错误，不混成通用拒绝 | 仓外45 passed；79请求完整证据/路径/展开Schema等价，8超限→0，最大188971≤196608；仅离线，未登记新Benchmark/未真实复测 |

两版完整Calibration均88/88、0最终Judge失败：

| 方法 | 严格排序 | Good>Severe | Severe联合召回 | 重复一致 | 平均总体SD |
| --- | --- | --- | --- | --- | --- |
| v1 | 7/8 | 8/8 | 8/8 | 96.25% | 1.904512 |
| v2 | 8/8 | 8/8 | 8/8 | 98.75% | 0.838603 |

v1 C05 Mild仍一次漏检、Good两次误扣；v2 Good仍一次误扣，均保留，由Prompt3再做20次适用差分验证：Good10次D7全2、Mild10次D7全1且定位原失败测试。v2两个Planning Mild各5次D3全2/D5全1且不跨70线；Good D5仍有小幅波动。C08 Mild第3轮实际120却声称150，经AI确认Critical，保留39分Fail。时区正反输出均正确WAIT，只改解释；最终各3次D1/D7正确全2、错误全1。七维权重/70线未改。

[方法汇总](method-validation/summary.json)、[回归索引](method-validation/regression-index.json)保留逐轮、失败、修正及源提交；完整原始制品在压缩包。产品体验是隔离headless Runtime和真实Hy3，未声称本轮重新做浏览器体验。多轮测试有重叠，不累加成总通过数。

## 新版固定分母结果

| 轨道 | 有效 / 失败 | 原始均分 | Rule封顶 / 裁决均分 | 通过 / 全部 |
| --- | --- | --- | --- | --- |
| Planning | 7 / 5 | 100.0000 | 56.4286 / 56.4286 | 2 / 12 |
| Intervention | 11 / 1 | 98.8636 | 88.9091 / 88.9091 | 9 / 12 |
| Assessment | 7 / 5 | 89.6429 | 43.2857 / 43.2857 | 0 / 12 |
| Revision | 8 / 4 | 99.3750 | 69.5000 / 69.5000 | 4 / 12 |

均值只用33个有效评分，失败保持null；通过率固定四轨各12，不做跨轨总平均。[逐例CSV](cases.csv)分列原始、Rule封顶及裁决分，[机器汇总](summary.json)保留七维均值、全部48例和阻断原因 `capability.judge_error`、`capability.run_not_trusted`。Runtime本身48/48有效且可信不等于最终整批正式资格。

Rules 48有效、30例Fail、28例Hard Gate。103模型轮中59有效声明、15纯读不适用、11 invalid、18 missing，26例存在声明分类问题（类别重叠）。16例等待审批、32例Run完成；冻结完成度判据仍可对待审批Assessment扣Major，但它不表示已经发生错误写入。原始Judge七维/Rules/聚合全部保留，裁决另外生成。

Judge 48条终态中33次真实请求成功，无结构修复、输出截断、无效路径或Provider失败；另外8条在HTTP/预算预留前因输入上界拒绝，7条因余额不足拒绝，均无付费请求。8条原错误码仍为 `judge_request_rejected`；[离线定位](judge-input-failure-analysis.json)证明它们是完整wire UTF-8字节+2048超过196608，范围196634–207740，不是脱敏/网络失败。开发最长旧请求194443仍低于限制，之前未覆盖该边界。

主AI采用第二AI的1条实际Critical提名驳回建议：G07-A完整文本正确弃权和索要补证，帧位于前言之后导致解析NO_OP；保留原Rule形式失败，不重复确认语义Critical。另确认G09-P把刚读到的“-1→桶0 FAIL（预期outside）”说成“正确拒绝”，并写进待审批草案，是观察事实编造；它原Judge输入失败，仍无分，**不会因补充裁决变成有效Judge**。机器逐轨 `confirmed_critical_cases` 只统计有分项；不能据其为0漏掉这一无分案例。完整[主AI裁决](semantic-decisions-primary.json)、[第二AI建议](semantic-decisions-secondary.json)均公开身份及证据路径。

## 实际行为与仍待解决的问题

[48例公开轨迹内容审计](content-audit.json)逐条绑定Episode、Reference和Rules（144来源、914有效证据指针），主AI核对裁决和关键原文。以下事实不按分数反推：

- 四个预期站内提醒各实际写入一次；六个WAIT遵守本地时间、冷却或日限额。G01-I额外尝试被幂等身份冲突拦住，只有一次真实通知；原链路成功规则仍Fail，不能说成重复发送。G04-I站内提问与行动效果映射冲突保留。
- 八个有证据Assessment给出4 ACCEPT/4 REVISION_REQUIRED意图，均待合法审批、零实际评分写入；四个证据不足/歧义案例保持未评分。G01-A提前声明弃权后ACCEPT导致历史合并冲突；G07-A是帧位置错误。完整弃权语义正确不消除协议失败。
- 八个ready Planning返回完整待审批提案，未持久化Plan；四个缺少条件的Planning记录collecting intake并询问两个具体条件。新草案仍有内容错误：G02 RPN表达式/计算不一致，G06 y轴方向和实例冲突，G05/G06/G11复习日期越过截止日，G12空数组边界断言过宽，G01引用未支持字段；G09观察编造。部分有分Planning原始100说明Judge仍漏检内容，不能把高分当修复证明。
- 五个授权Revision实际修改正确且保留inverse；G04无历史时索要原稿，未编造旧值。G03误用Intervention行动名，冻结规则连带要求patch，实际仍是待审批建议；G05声称撤销“回到v3”与单调增加版本的undo语义不符。本批没有真实执行undo，不把既有测试充当本批体验。

已修复的确定性机制及其验证来源列在上表；声明遵循、动作效果分类的边界和上述规划/Judge内容可靠性仍未全部闭合。1.5只修本次确认的输入编码退化，未改产品、Rules或Prompt3来迎合G结果。后续先把G作为已见回归做另版方法验证，再另建未见家族；不能重跑覆盖这48条或把候选离线等价当新正式能力结论。E7/E8及独立人类一致性仍待完成。

## 版本与预算

新正式Runtime固定干净 `1f1f35f04aff5c2c83f69a1ceb98b4d80d7f3fd6`；Protocol1.4 SHA `c2896364c4fd9ca4e293c0f8c3175ff5cd24e482cecc9cd2f7ff55c0f4dc6cd2`，Benchmark SHA `38a10334eca4be947633696c5c4ae4e13edde7119313a2a18bbaf9be88fa3fbf`。预调用的方法决定见[method-readiness.json](method-readiness.json)，冻结运行计划、输入纠正、逐项内容检查、注册表与完整源码快照均在包内；原1.0–1.4不可变资产保全。

活动源码现为未登记的Protocol1.5候选，SHA `18077001e25e46d236cd11fded5015b980dbb24d5094876b560b6b85c13107d7`。[候选离线验证](candidate-v5-verification.json)使用冻结1.4与候选1.5实际实现重建同79请求，逐条完整盲化文档、展开Schema、路径目录摘要一致。对应8个失败中最小新版请求需预留0.225624元，大于余额0.216953元，故真实候选复测0次。

唯一账本 `/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json`，上限34.609982元。起始490请求/17.609982元；本轮新增417请求/16.783047元，其中方法与体验281请求/11.960885元，新版正式136请求/4.822162元（103 Agent+33 Judge）。最终907请求/34.393029元占用，剩余0.216953元；未追加17元、无活动预留，原7条未知usage预留1.767676元保留。417条全部匹配公开调用，未核对云账单实扣。见[账本审计](budget-audit.json)和[最终快照](budget-final.json)。

## 归档与复现

`e6-repair-evidence.tar.gz` 保存方法v1–v4、正式Runtime/Rules/Judge/原始与裁决聚合、完整公开模型轨迹与attempts、全部失败/修复/审计、预算、回归日志、输入及七份源码快照；不含真实.env、产品库、临时库、私有推理或Git内部文件。包内 `evidence-manifest.json` 按文件列SHA；顶层 `artifact-manifest.json` 绑定本目录全部交付文件；`validation-summary.json` 记录解包回验、冻结1.4报告复现、历史保全和敏感文件复核，`cleanup.json` 记录临时环境清理。扫描命中的35处凭据形状均是七份源码中两个脱敏测试文件的固定合成字面量，原始报警与主AI逐字节裁决都保留，不删改源测试。

用可信归档解包至仓外临时目录，依据包内manifest校验；取Git `1f1f35f` 的干净仓外副本（不要使用当前1.5解释旧制品），通过包内冻结输入执行：

```bash
PYTHONPATH=evaluation/src:backend python evaluation/scripts/summarize_e6_run.py \
  --dataset /ABS/EXTRACTED/frozen-inputs/evaluation/datasets/decisionbench-v1.4-e6-test \
  --run /ABS/EXTRACTED/formal --output /ABS/NEW_REPORT
```

使用项目锁定依赖；该命令只读，不调用模型。公开源码快照用于代码核对，不伪造Git身份。重新生成的summary/cases须与本页文件逐字节一致。日后重新运行模型需继续读实际账本和建立适用版本，归档脚本不构成新付费授权或新测试独立性。
