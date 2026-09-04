# Evaluation Release Assets

本目录是 Evaluation Protocol Release 1.0 的仓库固定信任根，不是可由 CLI 指定的配置目录。
设计与边界见 [`../../docs/E3.1.2正式评测准入与版本治理.md`](../../docs/E3.1.2正式评测准入与版本治理.md)。

| 文件 | 作用 |
| --- | --- |
| `evaluation-protocol-release-1.0.json` | 固定活动制品链、Schema、规则、Judge、聚合、盲化、Evidence 和 formal 政策 |
| `trusted-benchmark-registry-v1.json` | 生产可信 Benchmark 注册表；当前必须为空 |
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

未来正式 Benchmark 必须经过案例/资源/隐私/数量/Split 审核，使用状态 `released` 的
`benchmark-release-manifest-v1`，在单独审查提交中加入生产注册表。完成可信注册后，任何
协议或来源变更都必须创建新的 Protocol Release 与活动锁，不能更新 Release 1.0 或历史锁。
测试注册表只能通过明确的
test-only Python seam 注入，生产 CLI 不提供对应参数或环境变量。
