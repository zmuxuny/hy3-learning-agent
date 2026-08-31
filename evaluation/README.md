# Learning Agent Evaluation

`evaluation/` 是腾讯犀牛鸟第三阶段的离线评测平面。E0 只交付版本化协议、确定性校验、规范化摘要和四个手工 Mini Episode；它不导入产品 Runtime，不创建或读取 SQLite，不调用模型，也不触发网络、SMTP、IMAP、Web Push 或生产 Outbox。

## 当前可用能力

- `DecisionEpisode v1`、`Acceptable Action Envelope v1` 和 `Environment Manifest v1` 的严格 JSON Schema；
- 单一 canonical JSON / SHA-256 实现，以及 Context、Environment、Oracle、Episode 的明确自摘要规则；
- 递归数据集校验、结构化错误码、稳定错误排序、逻辑 ID/引用/Evidence Path/摘要/Split 完整性检查；
- 对私有推理、凭据形态、认证字段、非保留邮箱和个人身份字段的失败关闭检查；
- Planning、Intervention、Assessment、Revision 各一个手工协议夹具。

四个 Mini Episode **不是**真实 Hy3 Runtime 轨迹、DecisionBench Primary Episode、Calibration Output 或正式模型评测结果。它们的 `provenance`、`observable_trace.capture_mode` 和离线环境清单均明确声明这一点；不得据此生成分数或模型能力结论。

## 安装与运行

源码仓库的常规安装脚本会以 editable 模式安装评测包：

```bash
./scripts/setup.sh
.venv/bin/python -m learning_agent_eval validate-dataset \
  --dataset evaluation/datasets/decisionbench-v1
```

只安装评测包时可运行：

```bash
.venv/bin/pip install --editable evaluation
```

成功输出固定轨道顺序的样本统计并返回 `0`：

```text
dataset_valid episodes=4 tracks=planning:1,intervention:1,assessment:1,revision:1
```

失败返回 `1`。每条错误只包含相对文件名、字段路径、稳定错误码和公共说明，不包含被拒绝的字段值或完整载荷。命令参数错误由 `argparse` 返回 `2`。

## 目录规范

```text
evaluation/
├── README.md
├── pyproject.toml
├── schemas/
│   ├── decision-episode-v1.schema.json
│   ├── acceptable-action-envelope-v1.schema.json
│   └── environment-manifest-v1.schema.json
├── datasets/decisionbench-v1/
│   ├── dataset-card.md
│   └── episodes/mini/
│       ├── P-MINI-001.json
│       ├── I-MINI-001.json
│       ├── A-MINI-001.json
│       └── R-MINI-001.json
├── src/learning_agent_eval/
└── tests/
```

校验器递归读取指定路径中的 `*.json`，拒绝符号链接、重复 JSON Key、未知 Schema 版本、超过 2 MB 的单文件和非有限数值。合同对象全部采用 `additionalProperties: false`；只有明确命名的 JSON 载荷映射（例如 `payload`、实体 `data`、`facts`、工具参数/结果和 Patch）允许领域扩展，这些扩展仍经过 canonicalization 与隐私检查。

## Canonical JSON 与摘要

所有评测摘要只调用 `learning_agent_eval.canonical` 中的一份实现：

- 对象 Key 排序，移除无意义空白，UTF-8 直写；
- Key 与字符串使用 Unicode NFC；
- `1.0` 规范为 `1`，`-0.0` 规范为 `0`；
- 数组顺序保留；非有限数值、非字符串 Key 和非 JSON 类型失败关闭；
- SHA-256 是 canonical JSON bytes 的小写 64 位十六进制摘要。

自摘要字段不参与自身摘要：Context 排除 `context_sha256`，Environment 排除 `manifest_sha256`，Oracle 排除 `envelope_sha256`，Episode 只排除 `provenance.episode_sha256`。嵌套 Environment 和 Oracle 摘要仍属于 Episode 摘要输入。

## 隐私与隔离边界

Episode 只允许合成事实、公开输出、Function Call、工具结果和状态差异。以下内容在 Schema 校验和错误输出之前即被拒绝：

- `reasoning_content`、Chain-of-Thought、私有推理或同义字段；
- API Key、Bearer/JWT、密码、认证 Token、Cookie、私钥等字段或疑似值；
- 非 RFC 保留域邮箱、真实通知地址、电话号码、证件号及个人身份字段；
- 真实用户数据库、`.env` 内容和生产通知配置。

校验器不会“脱敏后接受”这些内容，而是返回结构化错误并让整个数据集失败。保留域仅用于无个人归属的文档样例；当前 Mini Episode 不包含邮箱。

## E1 接口边界

E1 才能在明确的评测开关后增加临时 SQLite、冻结时钟、公开资源快照、假 Outbox 和 Evaluation Model Recorder。Recorder 只能把模型可见输入、公开输出与 Function Call 交给本包定义的 `DecisionEpisode v1` 入口，写盘前必须移除并拒绝 `reasoning_content`。

E1 不得复用生产数据库、真实通知目标或隐式应用配置；真实 Runtime Episode 还必须把 `capture_mode` 改为 `runtime_recording`、记录实际临时库摘要和受控环境，并使用新的 `runtime_export` provenance。若 E1 需要改变字段语义，必须发布新 Schema 版本，不能无版本改写 v1。
