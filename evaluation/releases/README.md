# 当前Release入口（2026-09-09）

活动协议为Evaluation Protocol Release 1.13；最终I48 Benchmark为decisionbench-e6-final-test-release-1.13，固定源码35e77cf。E6修复、另版验证、新测试冻结登记、全批评测及主AI审核归档已完成。最终Protocol1.13 / I48固定源码35e77cf：48槽位、47 Episode、1 RuntimeFailure、41有效Judge、6 Judge错误，formal=false。H48保留Protocol1.9原版结果（48槽位、46 Episode、2 RuntimeFailure、44有效Judge、2 Judge错误），不混算。 旧版本与全部失败保留；审核者为主AI，不冒充独立人类审核。E7/E8未开展。 见[最终档案](../artifacts/e6-completion-20260909/README.md)。登记仅为本地可信基准登记，没有推送或公开发布。

以下完整保留原Protocol1.0登记说明及当时运行前状态，均为历史。

# Evaluation Release Assets

本目录是 Evaluation Protocol Release 1.0 的仓库固定信任根，不是可由 CLI 指定的配置目录。
设计与边界见 [`../../docs/E3.1.2正式评测准入与版本治理.md`](../../docs/E3.1.2正式评测准入与版本治理.md)。

这里的信任边界来自经审查的代码与 Git 版本。普通 Git 文件中的 hash lock 是回归与一致性约束，不能证明文件不可被共同篡改，也不能密码学证明远端模型实际执行过某段代码。活动制品另核对 Git 对象中的来源包，Provider Attestation 仍只提供可审计归因。

2026-09-05 审计修复纠正了四条历史锁的 `frozen_in` 阶段文字：CaseSpec v1、CaseSuite v1、JudgeReference v1、RuntimeFailure v1 实际首次出现在 E3.1 的 `2af42e3` / `16ad39b`，不是 E3。此次仅纠正元数据；全部 23 个历史 Schema 的原始字节、路径、版本和 SHA-256 保持不变。

| 文件 | 作用 |
| --- | --- |
| `evaluation-protocol-release-1.0.json` | 固定活动制品链、Schema、规则、Judge、聚合、盲化、Evidence 和 formal 政策 |
| `trusted-benchmark-registry-v1.json` | 生产可信 Benchmark 注册表；已登记 `decisionbench-v1-e6-test-release` |
| `schema-lock-v1.json` | 历史与活动 Schema 的独立原始字节锁 |
| `model-action-declaration-v2.json` | 14 类行动的模型可读规范词典 |
| `source-bundles/*.json` | Runtime、Rules、Judge、Aggregate 的保守来源包摘要 |

当前 `decisionbench-v4-engineering` Release 是未注册的 `engineering` 制品，不能通过复制、
CLI 参数、环境变量或自声明 formal 字段进入本注册表。

开发阶段使用：

```bash
.venv/bin/python evaluation/scripts/build_e312_releases.py
```

Release 1.0 尚未提交时，脚本生成活动 Schema、Schema Lock、source bundles、Protocol
Release 和 engineering Benchmark 绑定，同时拒绝历史 Schema/锁漂移。Release 1.0 一旦存在
于当前 HEAD，脚本默认切换为只读验证，任何漂移都会失败。只有在 production registry 仍为空、
随附 Benchmark 仍为 `engineering` 时，开发者才可显式使用
`--refresh-untrusted-candidate` 刷新尚未取得信任的来源绑定；该路径不允许更新历史 Schema 锁，
且一旦出现任何可信注册项便先验失败。

E6 的48个新测试输入已完成AI内容/资源/隐私/数量/Split审核，并使用状态 `released` 的
`benchmark-release-manifest-v1` 随本地冻结审查提交加入生产注册表。该登记不是公开发布或能力结果；运行尚未开始，见 [E6记录](../../docs/E6验收工作记录.md)。完成可信注册后，任何
协议或来源变更都必须创建新的 Protocol Release 与活动锁，不能更新 Release 1.0 或历史锁。
测试注册表只能通过明确的
test-only Python seam 注入，生产 CLI 不提供对应参数或环境变量。
