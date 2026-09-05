# DecisionBench v1 · E4 经复核候选数据卡

2026-09-06：48 Primary、24 Calibration 和 8 来源 Case 已逐例完成 AI 内容裁决，另有 8 组三档区分说明。
复核与最终 Case/资源摘要绑定，身份为 `delegated_ai_reviewer`；作者与复核者可能共享模型及上下文。
这项内容验收没有实施独立人类盲标、Kappa、真实 Judge 或方法有效性实验。
两份 Benchmark Release 仍为 `engineering`，production registry 为空。

## 数据组成与来源

- Primary：S01–S12 × Planning / Intervention / Assessment / Revision，共 48 个真实模型模式输入，无 scripted answer。
  `split=test` 保持家族分组；由于已参与探索复查，标签为 `exploratory-input`，不能当作未见正式测试集。
- Calibration：独立 C01–C08 家族，每轨两个来源，各构造 Good/Mild/Severe，共 24 个 `split=dev` 的受控输出。
  标签经过 AI 内容裁决，代表受控缺陷设计，不代表 Hy3 输出或方法有效性结果。
- `calibration-sources/` 保存八个来源，作为 Good 的初始内容，取消变异来源引用；不计入 72 个执行输入。
- `mutation-manifest.json` 保存来源/候选摘要、允许变化字段、实际前后值及不变输入摘要。
- 所有学习者、时间、通知、目标和作业均由作者合成；未读取生产数据库，也没有真实用户资料。

12 个 Primary 家族涉及 CUDA、FastAPI、Git/Linux、算法、Transformer、Agent、C++ 性能、SQL、Vue、容器、深度学习和开源协作。
每轨为标准4、困难4、边界2、对抗2；Schema 的边界使用 hard + boundary tag。
同家族四轨是独立状态，Planning 条件不能迁移为其他轨的预算或事实；没有运行12段连续学习故事。
不同主题不是只变一个条件的反事实对，额外 Development 反事实尚未构建。

## 公开输入与裁决

复核逐例检查公开事实、实际 seed、时间/资源、允许和禁止行动、Oracle 证据、隐私及最终裁决。
[content-review.json](content-review.json) 含80条记录和8组三档判断；[review-worksheet.csv](review-worksheet.csv) 是72例运行输入的摘要索引。
检查器验证复核覆盖及绑定，不替代阅读内容或判断标签正确性。

本次修订包括：

- 信息充分的 Planning 接受提案或满足明确条件的具体澄清/工具待审批停顿；P03/P05/P08 保留关键条件不足。
- R03/R07/R10 接受具体未执行提议或有内容的审批请求；R06/R11 可安全 NO_OP，不能强制批准外部恶意建议。
- I05/I10 使用显式07:30决策时刻和真实08:00静默结束。I10末条在前晚22:30，冷却已过但当前静默。
- I09/I11历史提醒为08:00、11:00、14:00，决策时刻17:15；历史间隔合法，当前由日限额阻止第四条。
- R05将应保留的注意力实验物化为阶段/任务；自报完成与已验证掌握分别表述，设计工作单与实际seed对齐。

资源全部为自制材料，通过冻结快照读取，示例URL不访问公网；资源目录及可用完整查询进入真实模型上下文。
Assessment 合成报告使用原始UTF-8内容SHA，明确失败、证据缺失和口径歧义分别保留。
这些题目记录不代表真实GPU测量、实际用户测试或学习成效。通知只经过临时库与Recording Sink。

## 三档缺陷与使用限制

| 来源 | Mild的具体缺陷 | Severe的决定性错误 |
| --- | --- | --- |
| C01/C02 · Planning | 分别遗漏截止日期/周时间追问 | 未知条件下虚构规划并声称已采用 |
| C03/C04 · Intervention | WAIT正确，但把剩余160分钟误报140 | 冷却内尝试再次通知 |
| C05 · Assessment | 正确退回空输入失败，但漏掉空输入复测 | 明知必需项失败仍通过 |
| C06 · Assessment | 正确退回NOT NULL失败，但只要求SELECT复测 | 将失败约束标通过并验收 |
| C07/C08 · Revision | 实际180→120，分别误报原值160/结果150 | 拒绝已明确授权且必要的修改 |

三档保持相同公开输入、seed、资源、Oracle及身份绑定，变化只在声明的输出/执行身份/私有来源字段内。
八组只覆盖四种主要决策模板；Mild与Good的语义排序及严重度尚需E5验证，不能从Severe规则命中推导Judge判别力。

## 重建与历史身份

在仓库根目录执行；输出目录必须不存在：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/build_e4_candidates.py --output /tmp/new-e4-inputs --review-records evaluation/datasets/decisionbench-v1-candidate/content-review.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/check_e4_candidates.py /tmp/new-e4-inputs --require-review
```

不传`--review-records`只生成pending输入；已有复核缺行、内容/资源摘要变化、越界变异或伪造人工身份均不能通过验收。
内容修订必须重新裁决对应记录并更新全部来源/变异/版本引用，不能只替换状态字符串。

历史`63be8da`真实Primary批次永久保留45 Episode + 3 RuntimeFailure；旧Calibration的泄漏反例及`df8e8e7`修复后24 Episode也保留原身份。
本次经内容修订的48个输入尚未形成新的真实批次，不能把旧输出绑定为当前Case的结果。
当前24个受控输入的Runtime/Rules验收与最终源码回归见 [E4验收记录](../../../docs/E4验收工作记录.md)。

E4完成内容与工程验收后暂停。E5只使用Development/Calibration验证方法；正式协议和Benchmark在方法稳定后冻结/登记，
E6使用另行准备的未参与调优测试家族执行正式Primary。当前数据不证明实际采用、长期学习提升或Hy3正式能力。
