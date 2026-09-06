# DecisionBench v1 · E6 正式测试输入

12 个新家族、48 个 Primary 输入，四轨各 12。方法复核先于构建，采用 E5 V2 基线；这些输入未用于 Agent/Judge 调优，没有附带模型输出。现已按仓库信任根登记，正式运行因原共享预算不足完整批次尚未开始，**没有正式能力结果**。

| 家族 | 主题 | 分层（每轨） |
| --- | --- | --- |
| F01–F04 | DNS 缓存、WAV 分段、售票状态机、图路径 | 标准 4 |
| F05–F08 | Unicode 计数、时区转换、日志解析、依赖版本 | 困难 4 |
| F09–F10 | 游程编码、定点分摊 | 边界 2 |
| F11–F12 | CSV 转义、离线链路教学 | 对抗 2 |

每个家族有独立 P/I/A/R 初态，不代表连续学习故事。Scenario Family 为 `formal-f01` 至 `formal-f12`；与旧 S01–S12、C01–C08 和工程家族隔离，旧 Episode/Failure 均未复用。构造复用了 CaseSpec 和身份绑定脚手架；主题、输入、证据、资源和参数新写，但共享行动类型和部分 Guard 机制，不声称 12 种全新错误机制，也不证明训练语料未见。

资源均为自制合成材料；成功验收例直接给出题目输入和断言，缺证据例明确保留缺失。对抗文字保留在待评提交或不可信材料中。材料中的数值不是实际学习者或真实实验测量。所有实体为合成身份，资源限定 `learning.example.test`，通知走隔离 Outbox。

构造源是 [家族设计](../../case-design/e6-family-design.json)，48 条逐例裁决在 [AI 内容复核](../../case-design/e6-content-review.json)。作者和复核者为同一主 AI、共享上下文；没有独立人类盲标或 Kappa。机械校验检查摘要、资源、覆盖、显式家族及完整输入重复，语义判断见各行裁决。F01/F02/F09 的具体证据补齐、F08 的已知周时间对齐均在冻结前完成，没有观察模型结果后改题。

[Benchmark](benchmark-release.json) 固定有序 Case、资源、lineage 与协议；[Suite](manifest.json) 与仓库 [production registry](../../releases/trusted-benchmark-registry-v1.json) 对齐。[运行预设](../../case-design/e6-run-plan.json) 固定源代码边界、Agent/Judge 配置、48 分母、顺序、费用与失败政策。`released` 表示本地版本化登记，不表示已推送、公开发布或正式实验已完成。

```bash
PYTHONPATH=evaluation/src .venv/bin/python evaluation/scripts/check_e6_freeze.py
PYTHONPATH=evaluation/src .venv/bin/python evaluation/scripts/build_e312_releases.py
```

重建审核后的输入需使用一个不存在的新目录：

```bash
PYTHONPATH=evaluation/src .venv/bin/python evaluation/scripts/build_e6_test.py \
  --output /tmp/my-e6-rebuild \
  --review-records evaluation/case-design/e6-content-review.json
```

构造器只生成未登记的 `engineering` Release；Case/资源可逐字节对照本数据集，构造器不能替代仓库审查提交授予信任。可信登记后 `--refresh-untrusted-candidate` 会拒绝刷新协议；未来方法/来源变化必须另版处理。

后续执行方式及原账本见 [E6 工作记录](../../../docs/E6验收工作记录.md)，当前 48 个未运行状态见 [执行清单](../../artifacts/e6-readiness-20260906/execution-status.json)。未运行不伪造为 RuntimeFailure，也不记零分。E5 的 24 Calibration/88 Judge 结果继续使用其 `40fddb8` 档案身份。
