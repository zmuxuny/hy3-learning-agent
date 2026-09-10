# DecisionBench v1.4 E6 新测试

2026-09-08，在另版 Development/Calibration 决定结束方法调优后编写 G01–G12 × 四轨共48个输入。各轨12例；不是旧 F01–F12 的重新编号或重跑。F/S/C/Development 家族均保留已见身份。12个主题涉及矩阵旋转、逆波兰栈、括号深度、中值滤波、分数约分、网格障碍、字节序、多边形面积、整数分桶、奇偶校验、棋盘马步和直方图前缀。

[家族设计](../../case-design/e6-repair-family-design.json)、[主AI内容确认](../../case-design/e6-repair-content-review.json)、[第二AI内容复核](../../case-design/e6-repair-secondary-content-review.json)绑定实际输入与资源摘要。两名AI都参与开发，复核不是独立人类盲标。首次模型调用前修正了网格测试独立初态、奇偶校验模式的表述歧义；原始草稿与纠正过程在本轮证据档案保留。没有用G家族模型输出调优这版方法。

家族难度为4 standard、4 hard、2 boundary、2 adversarial。Schema没有boundary枚举，因此案例字段计数为16 standard、24 hard、8 adversarial；boundary另以tag保留。所有初态、教学材料、提交证据都是作者公开编写的合成内容，不代表外部网站实录、实际程序测量或真实学习成效。对抗文本中出现的外发/扩大修改指令仅为不可信材料。

本测试冻结协议为Evaluation Protocol Release 1.4，Benchmark为`decisionbench-v1.4-e6-test-release`，已登记仓库信任根。源码在`1f1f35f`提交后从干净仓外副本运行；完整固定顺序、预算、参数及失败保留策略见[运行预设](../../case-design/e6-repair-run-plan.json)。每Case一个终态，禁止替换失败或以过滤恢复正式资格。保留合法审批，Benchmark不自动批准；实际批准续跑另在隔离产品体验验证。

登记仅确认输入和协议身份。正式结果与剩余缺陷见[E6新版记录](https://github.com/zmuxuny/hy3-learning-agent/blob/1295254e2f8f792da30eb331cb2d0ea12437fa0b/docs/E6%E6%96%B0%E7%89%88%E4%BF%AE%E5%A4%8D%E4%B8%8E%E9%AA%8C%E8%AF%81%E8%AE%B0%E5%BD%95.md)，不能用本数据卡或内容自审宣称能力验收通过。旧E6的48 Episode、46有效Judge/2失败、formal=false不覆盖。

本次运行完成48 Episode/33有效Judge/15失败，formal=false。活动源码后续另有1.5离线候选；该G家族现已见，不是后续版本的未见测试。
