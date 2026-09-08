# E6最终修复、验证与评测档案（2026-09-09）

E6要求已执行完成：修复第三阶段实际代码问题、另版方法验证、重新建立未见家族并冻结登记、完整运行、主AI全轨迹审核及归档。最终I48固定Protocol1.13 / 源码35e77cf；结果formal=false，不能称正式能力验收通过。E7/E8未开展，未推送、未发布。

## 最终固定48分母结果

| 轨道 | 有效Judge | 通过 | 不通过 | 无分 | 原始均分 | Rule cap均分 | Critical裁决均分 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| assessment | 9/12 | 2 | 7 | 3 | 94.167 | 59.222 | 59.222 |
| intervention | 12/12 | 10 | 2 | 0 | 97.083 | 88.792 | 88.792 |
| planning | 11/12 | 6 | 5 | 1 | 100.000 | 72.273 | 72.273 |
| revision | 9/12 | 5 | 4 | 3 | 99.444 | 72.889 | 72.889 |

状态分布：{"complete": 41, "runtime_failure": 1, "judge_error": 6}；正式能力阻断：capability.judge_error, capability.run_not_trusted, capability.runtime_failure。零分保留为零，运行或Judge失败保留无分；均分只以有分案例为分母，全案例通过率以每轨12为分母。七维均值和所有案例在[JSON](final-summary.json)与[CSV](final-cases.csv)。

最终I批Judge为41有效、5 Provider错误（I03-R/I05-A/I05-R/I06-R/I12-A）及1本地输入超限（I10-A，无Provider调用或计费）。47份完整请求离线往返均等价，只有I10-A上界201992超过冻结上限196608；旧79份0超限并不代表新输入均通过。原错误保留，不截断、不在看过新测试后改上限或选择性重试。

通过数是冻结Rules与原Judge加Critical裁决的结果。主AI另外发现的major教学/解释缺陷不直接改写七维，不自动升级为Critical，因此“通过”不表示该例内容经AI核验无瑕疵。

## 代码与方法

行动帧允许前导解释但仍要求唯一合法帧/数组；最终决策不再错误合并全部历史意图，模型漏声明仍触发Gate。实际询问送达按关联声明分类，通知失败尝试与重复实际副作用分开。规划工具检查任务/复习/截止顺序及核心证据，未知TaskCreate字段不再静默吞掉。产品提示明确数学、日期、实际失败与撤销版本语义。Judge保留原returned_tool_calls和完整草案，增加公开事实核验；lossless编码仍完整，无截断。

H03暴露规范消息外键误按Notification解析后，1.10–1.13补齐ChatMessage独立身份、Delta来源及旧/活动Exporter实际Intervention关联归属。无关联的消息和真正缺失声明仍失败，未泛化豁免。最终65项仓外回归通过，原失败日志保留。

1.9固定Calibration56首轮53有效/3接口失败；预先限定每个失败槽位一次恢复共3项有效。恢复槽位口径8/8严格排序、8/8 Good>Severe、8/8 Severe联合召回，重复结论一致100%、平均SD1.701561；原首轮排序6/8、Good>Severe7/8、一致95%，不将恢复当首轮成功。锚点29/30，仍有Planning Mild误高。1.10定向8真实Judge全有效、4/4 Good>Mild、7/8锚点；1.11 Development7 Episode/7有效Judge，1.13仅对相同公开证据做归属差分，不称独立真实复现。旧8超限真实响应均有效，79完整请求等价、最大192638字节、0超限；时区正负各3槽位正确，1次结构修复保留。详见[方法摘要](method-summary.json)及包内method*。

## 审核与真实产出

原方案人工职责均由本次主AI亲自履行，标识primary_ai_reviewer；开发、编题与复核同属主AI，不算独立人类一致性。H48与I48共96个终态逐例审核；对93个Episode完整读取公开模型正文、返回参数、工具回执、审批、Operation和产品Delta，对3个RuntimeFailure读取其保留的完整失败轨迹。所有Judge问题及建议Gate逐项复核，裁决精确绑定原Episode/Judge摘要和证据路径。包内formal*/content-audit.json、judge-content-audit.json、semantic-reviews.json保存完整审核。

残余例子包括I01/I05/I06/I09-R的旧版本号回退承诺、I02-I冷却剩余时长算错、I12-I把21:00判为23–08静默、I04-P把未选舍入模式记为用户事实、I09-P要求朴素exp(1000)不溢出的验收条件、I07-R把15分钟块说成多个10–20分钟“拆分”。这些内容缺陷及Judge漏检分别披露，不隐藏在正确状态写入后。对于NO_OP/REQUEST_USER_INPUT违反冻结动作包络的案例，保留Rule Gate；不因实际没有写入就豁免，也不把重复Rule条件再冒充新语义Critical。

隔离产品体验实际验证Planning具体批准得到pending Proposal，拒绝无Proposal；主AI修正草案后采用Plan，再90→75→90，版本1→2→3。Assessment具体批准accepted/100/1 Operation，同初态拒绝submitted/null/0 Operation。修正/采用/撤销由主AI模拟用户明确操作，不冒充Hy3自主完成；原错误草案、批准后错误解释和所有失败保留。Benchmark无自动批准，待批准草案与实际产出分开。

## H批次及历史保全

H48固定Protocol1.9/83fbf52，在当时方法决定后建立并冻结；后来发现消息身份框架缺陷，仍完成原版全流程，未回填或改名成1.13。状态分布：{"judge_error": 2, "complete": 44, "runtime_failure": 2}。H03-P原身份Failure和H07-R原Provider Failure保留；见[h-summary.json](h-summary.json)与[h-cases.csv](h-cases.csv)。F/G旧结果、各中间Release及失败全部保留，I家族在最终方法决定792a84b之后才编写。

## 费用

直接沿用原账本，未追加约50元授权。原912请求与授权记录不变；本轮新增603请求，占用27.435132元等价估算；累计61.933642元，上限84.393029元，剩余22.459387元。未知usage保留预留。按旧1/4元每百万token等价估算，第三方接口人民币实扣未核实。逐请求公式及证据匹配见[预算审计](budget-audit.json)；ticket1010为dev-history-r失败的已结算请求，缺少公开调用导出，仅有账本，不宣称全部导出闭合。

## 证据与复核

[证据包](public-evidence.tar.gz)含6766个文件；[清单](archive-manifest.json)逐文件记录大小/SHA256并绑定压缩包。包括全部原运行终态、Judge原响应/结构修复/失败、原始和裁决聚合、输入复核、方法统计、产品体验、账本、回归失败、辅助脚本，以及9个关键提交的精确tracked源码包。repair目录保留旧4904文件，便于独立复查跨版反例。已失败的首次源码打包部分文件也按原失败身份保留，不作完整源码。

最终批次可离线复核：从source-snapshots/35e77cf…tar.gz取对应源码，将evidence解压到仓外，使用锁定依赖，设置PYTHONPATH=evaluation/src；运行evaluation/scripts/summarize_e6_run.py --dataset evaluation/datasets/decisionbench-v1.13-e6-final-test --run <evidence>/formal113 --output <new-report>，比较summary.json和cases.csv逐字节。H批次须用83fbf52源码和1.9数据。包内report-recompute与执行日志保存实际复算证据。此过程不调用模型。

[归档校验](archive-verification.json)核对外层全部成员、嵌套源码清单和配置密钥缺席；[历史保全](historical-preservation.json)核对e491164旧不可变制品，注册表仅追加，Release入口README保留原历史正文并增加当前说明。实际数据库、.env、缓存不归档。[收尾清单](closure-checks.json)记录本轮仓外副本/临时库清理与本地提交状态。
