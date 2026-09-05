# DecisionBench v1 · E4 候选数据卡

状态：输入已构建并通过机械校验；独立人工内容复核待办。两份 Benchmark Release 均为
`engineering`，production registry 为空。这里没有正式能力结论。

2026-09-05 已在干净 `63be8da` 执行本候选：Primary 保留 45 Episode + 3 RuntimeFailure，
Calibration 为 24 个受控 Episode。见 [原始运行档案](../../artifacts/e4-candidate-20260905/README.md)。
后续 `bd89530` 仅修复两类读引用并刷新 4 份发布绑定；72 个 Case、资源与变异内容字节不变。
当前暂停，未完成独立人工复核或进入真实 Judge 实验。

随后 `df8e8e7` 修复公开画像嵌入控制 logical_id 的场景族泄漏，重新离线导出 Calibration；旧批次保留为探索记录，不能进入正式 Judge 校准。

## 范围与来源

- Primary：12 个主题家族 × Planning / Intervention / Assessment / Revision，共 48 个输入。
  全部位于 test split；作者只提供目标、初态、资源、授权边界和 Oracle，不提供模型回答。
- Calibration：另外 8 个家族，每轨 2 个种子，各构造 Good / Mild / Severe 三档，共 24 个输入。
  位于 dev split，使用受控输出；三档是待复核的作者标签，不是 Hy3 输出或人工裁决。
- `calibration-sources/` 保存 8 个源 CaseSpec，不计入 72 个运行输入。
  `mutation-manifest.json` 固定源摘要、允许变化字段、实际前后值及未变输入摘要。
- 所有学习者、学习过程、通知历史和提交记录由作者构造。没有读取生产数据库或收集真实用户资料。
  现有工程 Case 仅作契约结构模板；具体输入和 Oracle 单独编写。

12 个 Primary 家族分别涉及 CUDA、FastAPI、Git/Linux、算法、Transformer、Agent、C++ 性能、
SQL、Vue、容器、深度学习和开源协作。同一家族的四轨是独立初态，不能称为一段已经跑通的连续学习旅程。
每轨包含标准 4、困难 4、边界 2、对抗 2；Schema 把边界记为 hard 并附 boundary tag。
题目意图覆盖全部 14 类规范行动。难度等级和唯一允许行动仍须人工复核。

## 可见证据与资源

资源全部是自制合成材料，运行时通过冻结快照读取，不访问这些示例 URL 或公网搜索。
Primary 和 Calibration 使用独立资源包；资源目录、可用完整查询及 URL 会进入模型上下文。

Assessment 中有依据的正反例附题目提供的合成验收记录 URL 和原始 UTF-8 内容 SHA-256。
它们用于测试 Agent 如何依据给定记录验收，不代表作者执行过真实 GPU 性能实验、用户测试或学习效果实验。
缺证据的场景故意没有附件；歧义场景保留单位或口径冲突。不能把这些构造记录当作真实用户能力证据。

通知只在临时库中记录并由 Recording Sink 模拟投递。时钟、静默窗口、冷却和历史通知均进入真实产品状态。
计划修改依赖用户在题目中明确给出的权限；Operation 与 Delta 记录实际结果。

## 构造和复核

运行仓库根目录下的命令，可以在新目录重建候选输入并核对结构、摘要、资源与 Mutation：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/build_e4_candidates.py --output /tmp/new-e4-inputs
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python evaluation/scripts/check_e4_candidates.py /tmp/new-e4-inputs
```

`check_e4_candidates.py` 不调用 Provider，不判断标签正确，也不授予 formal 资格。
`review-worksheet.csv` 是待填写工作单；没有已完成的独立标注或裁决。
复核必须检查：公开事实与实际 seed 一致、合理行动没有被误排除、正反约束方向正确、材料足以支持结论、
质量退化只在声明字段发生、隐私与版权符合数据来源。争议应修改输入并重新生成摘要，不能覆盖已执行的历史结果。

## 使用边界

Primary 不参与 Prompt、Rules 或 Judge 调优。若查看 Primary 结果后修改相关协议，这一轮应保留为探索记录；
新的正式比较需要重新确定未用于调优的测试输入。Calibration 可用于后续 E5，但当前未运行真实 Judge、
判别力排序实验、重复一致性或人工一致性实验。

本数据卡不证明真实用户采用效果、长期学习提升、开放网络调研能力或 Hy3 的正式能力。
production registry 登记必须等待独立内容复核、协议冻结和完整终态检查。
