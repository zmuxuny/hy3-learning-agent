# E6 会话约束补修与 1.15 验证（2026-09-09）

基线 **f47e3a1**，开工工作区干净。修复与付费前验证设计提交 **eac64bc**，活动方法 **1.15未登记验证版**；本轮产品实现未变，版本仍为`learning-runtime-e6-audit-1`。本轮已复现审计缺陷、流程覆盖和另版验证有证据支持，E6工程与实验交付收口；**未形成正式能力统计，不宣称正式能力验收通过**。范围止于E6，本地提交，未推送、未发布。

## 修复及工程验证

- `_draft_schedule_checks`通过发出草案的run→session定位Intake，核对先于草案的成功观察是否出自同一会话；其他会话不能覆盖。缺失/冲突身份与重复Intake返回`invalid_input`证据不足，不猜测其他会话，也不算模型Critical。
- 使用实际Collector生成的双会话快照复现误罚、漏检与数组顺序依赖：四种旧规则结果fail/pass/pass/fail，新规则pass/pass/fail/fail。新增关联缺失、观察发出者/返回会话不匹配、同轮调用顺序及观察数组逆序回归。
- 仓外副本、合成数据和临时库 **62项通过**。变更/删除截止×批准/拒绝四条用真实`AgentRuntime.run`、批准API、耐久恢复执行，不手动设置handler的`approval_granted`。暂停前无写入；批准仅变当前会话，可生成并采用合规提案；拒绝保留原约束，越界草案不生成。采用端约束、原规划卡片、行动/副作用和消息身份回归同时保留。
- 原I批 **41响应/225事实核验**经新本地校验全部有效，删除字段、空数组、无效路径仍拒绝；历史H/I **22个Planning Episode**局部重放无新增关联不足。都是已见确定性回放，不重评分、不称新模型实验。
- **19份完整Judge请求**按既有规范JSON往返一致；原返回参数与草案阅读副本保留，没有截断。最大请求178290字节（不含传输配置，不充当token数）。源码绑定核对162个实际测试文件，源码快照246成员与Git字节一致。

## 已冻结的定向验证

调用前在eac64bc提交[验证设计](../../datasets/decisionbench-v1.15-validation/validation-design.json)：Development固定7例；Calibration固定C02-P/C03-I/C05-A/C07-R三档12例，每个Good再评一次，共16 Judge槽位。全部是已见案例；Planning是加入原用户显式截止/周时间Intake的已见变体。未复用F/G/H/I身份包装未见测试，没有登记1.15正式Benchmark。

每槽首轮一次，不选择性恢复；预定Good重复独立保留，不替代首轮。结果无结构修复。固定12元等价预算内执行，未追加原授权。详见[验证汇总](validation-summary.json)。

| 批次 | Runtime | Judge | 结果 |
| --- | --- | --- | --- |
| Development | 7真实Agent Episode，0 Runtime Failure | 6有效、1 HTTP429 | 5通过、1不通过、1无分；固定7分母 |
| Calibration首轮 | 12受控响应经真实Runtime执行 | 12真实Judge有效 | 8通过、4不通过；固定12分母 |
| Good预定重复 | 原4个受控Episode | 4真实Judge有效 | 4通过；与首轮分开 |

| 校准家族 | Good | Mild | Severe |
| --- | ---: | ---: | ---: |
| C02 Planning | 100 | 87.5 | 2.5 |
| C03 Intervention | 100 | 87.5 | 0 |
| C05 Assessment | 100 | 95 | 0 |
| C07 Revision | 95 | 87.5 | 15 |

严格排序 **4/4**、Good>Severe **4/4**、严重错误规则/确认语义联合召回 **4/4**，Good成对通过一致 **100%**、平均总体SD **0**，预设Mild锚点 **5/5**。达到本次定向判据；只覆盖四组和Good重复，不替代完整重复校准，也不证明跨版本因果改善。Good并非都满分，C07解释维度两次均为1。

[Development JSON](development-summary.json) / [CSV](development-cases.csv)、[Calibration JSON](calibration-summary.json) / [CSV](calibration-cases.csv)均由原制品原生聚合产生，并在冻结源码副本复算逐字节一致。独立重算22个有分聚合的七维权重与39/69封顶，无差异；无分未记零分。

## 主AI审核与边界

主AI读完23份Judge尝试记录（22份完整响应、1份HTTP429）、7个Agent全部公开正文/返回参数/工具观察/领域变更，以及四个受控严重案例的实际工具、守卫、Operation与效果。审核文件绑定原Episode/响应摘要与证据路径；开发与审核为同一AI，**不是独立人类或独立盲审**。20个语义候选逐项处理，6个候选在两个伪造成果案例中确认，14个重复规则或泛化问题不另升级；不改原七维。包内`*/content-audit.json`、`*/judge-content-audit.json`、`*/semantic-reviews.json`保留判断。

- 真实Planning草案最后复习09-24超过用户09-22，Rules和Judge均发现。180分钟总量正确；“无环形缓冲经验”为未确认推断，Judge未指出。草案仍待批准，0 Proposal/Plan；本付费槽位没有走到批准后的产品校验，后者由真实Runtime合成响应集成验证覆盖。
- 实际通知为一次in_app sent，ChatMessage/Intervention/Notification分开且因果关联一致；冷却受控严重例仅尝试发送、实际被阻断，没有重复通知。不能把尝试描述成成功副作用。
- 证据不足两例保持submitted/null；授权周时间110→85、版本2→3与可逆Operation均有写入证据。旧稿核对Agent没有修改，但正文有“提示词覆盖”等推测及未查日历即称零事件的过度表述。
- 旧稿核对Judge为**HTTP429 / provider_error / ticket1537**，无公开响应、保留无分；不是本地投影错误。本轮没有为了补齐分数重跑。
- Judge对pending的扣分措辞、把拒绝执行归成D1事实矛盾、宽泛/重复证据路径及“可撤销是否构成下一步”的解释要求仍有边界。路径存在不保证每句核验成立；这些问题单列，不自动将所有模型错误变成规则失败。

## 费用与历史保全

唯一原账本不变更授权：新增 **37请求（Agent14/Judge23）**，占用 **1.724250元等价估算**；累计 **63.657892元**，上限 **84.393029元**，剩余 **20.735137元**。原1515请求全部保留；全部27条未知usage预留5.841159元，本轮HTTP429占0.213756元。37条新增请求均已绑定公开证据。[费用核对](budget-audit.json)逐条验证计费公式；不是供应商人民币实扣证明，实际后端模型未独立核实。

原最终I48固定 **Protocol1.13 / 35e77cf**：47 Episode、1 Runtime Failure、41有效Judge、6 Judge失败；**23通过、18不通过、7无分**，不回填。原H与I分开，H07-R仍按[更正](../e6-audit-fixes-20260909/h07-errata.json)认定本地投影拒绝。I的formal=false源于来源资格及运行/评分不完整，18个模型失败不参与该布尔值。新1.15使用未登记已见验证集，Calibration Agent为受控响应；其formal=false不能拿来否定本次定向指标，也不能写成新正式能力结果。

旧6766个归档成员、旧Release/Schema/数据/登记和原结果保留。回归首轮及诊断轮均56通过/4失败：Collector测试片段漏写v4版本，补齐后62项通过。辅助验证中的tuple/JSON数组直接比较失败、两次源码快照路径错误、重复Judge账本scope假设、分析脚本语法错误、文档入口替换失败也保留原脚本与诊断；未改付费判据或覆盖失败。

## 证据和复算

[证据包](public-evidence.tar.gz)与[逐文件清单](archive-manifest.json)保留新运行、原响应、失败、全部聚合/复算、AI审核、预算前后、回归与辅助脚本、实际Collector/审批事件、源码快照及失败部分包。[验证记录](final-verification.json)核对制品与源码；[实际解包复算](delivered-recompute-verification.json)验证交付包中两批summary/CSV逐字节一致；[收尾记录](closure.json)记录清理和本地提交边界。

复算必须有冻结提交Git对象。`source-eac64bc.tar.gz`仅按声明范围提供字节快照，不能代替Git历史；不要仅解压源码tar运行。使用已有项目锁定Python环境（独立准备时安装`evaluation/runtime-requirements.lock`）：

```bash
REPO=/root/workspace/tencent_rhinobird2026/learning_travel
WORK=$(mktemp -d)
mkdir "$WORK/evidence"
tar -xzf "$REPO/evaluation/artifacts/e6-session-validation-20260909/public-evidence.tar.gz" -C "$WORK/evidence"
git clone --no-hardlinks "$REPO" "$WORK/source"
git -C "$WORK/source" checkout --detach eac64bc
cd "$WORK/source"
for BATCH in development calibration; do
  PYTHONPATH=evaluation/src:backend "$REPO/.venv/bin/python" evaluation/scripts/summarize_e6_run.py \
    --dataset "evaluation/datasets/decisionbench-v1.15-validation/$BATCH" \
    --run "$WORK/evidence/$BATCH" --output "$WORK/recompute-$BATCH"
  cmp "$WORK/recompute-$BATCH/summary.json" "$WORK/evidence/$BATCH/report/summary.json"
  cmp "$WORK/recompute-$BATCH/cases.csv" "$WORK/evidence/$BATCH/report/cases.csv"
done
```

此步骤不读取.env、不调用模型。原响应复核脚本还需将旧完整档案的`formal/`和`formal113/`解至证据根的`frozen-evidence/`。归档脚本中的本机临时路径是本轮执行记录；跨机器复核应按上述路径重新定位，不能把临时路径存在当作依赖。

**满足**：已复现代码缺陷修复、多会话/审批回归、源码绑定、另版定向真实验证、AI审核、结果/费用复算与保全。**未满足**：正式能力统计资格、Development7/7完整有效评分。扩展对齐、反事实、E7/E8未开展，不在本轮自动扩展。
