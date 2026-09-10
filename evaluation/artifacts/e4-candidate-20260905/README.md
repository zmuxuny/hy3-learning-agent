# E4 候选输入与运行记录 · 2026-09-05

本目录是本地候选运行档案，**不是正式评测或能力报告**。production registry 为空；没有运行真实 Judge 或 E5 有效性实验。

- 48 Primary 在干净 `63be8da` 执行：45 Episode + 3 RuntimeFailure，P/I/A/R 分别为 11/12/12/10 个 Episode。全部 48 个唯一终态保留。
- `63be8da` 的 24 Calibration 是受控作者输出。扩展检查发现画像文字中含 Calibration 场景族 ID，因此该批不能用于正式 Judge 校准；初次有限标识扫描漏掉了此问题。
- `df8e8e7` 从模型可见输入中移除控制 logical_id，重新离线导出 24 Calibration，0 Failure；模型上下文和 Judge 投影的场景族前缀检查均通过。Good/Mild 的 Rule Gate 为 0/8，Severe 为 8/8；这不证明真实 Judge 能正确排序。
- 72 个 Case 的业务输入、资源、Mutation/Split 内容没有改写；重新绑定的仅是 Protocol/Benchmark Manifest。48 Primary 未重跑，未据其结果调优 Agent/Oracle/Judge。
- 72 行独立人工复核均 pending。详细修复、限制和两份方案的复核见 [E4 收口记录](https://github.com/zmuxuny/hy3-learning-agent/blob/1295254e2f8f792da30eb331cb2d0ea12437fa0b/docs/E4%E5%80%99%E9%80%89%E6%95%B0%E6%8D%AE%E6%94%B6%E5%8F%A3%E4%B8%8E%E6%96%B9%E6%A1%88%E5%A4%8D%E6%A0%B8.md)。

## 文件

| 文件 | 内容与使用条件 |
| --- | --- |
| `protocol-trials.tar.gz` | 9 轮真实工程试跑的输入与全部成功/失败；每轮按各自 Manifest Git commit 复查，不纳入 Primary/Calibration |
| `candidate-inputs-63be8da.tar.gz` | 首批 48/24 的精确输入、来源、资源、变异与绑定 |
| `primary-runtime-63be8da.tar.gz` | 首批 48 个真实终态；只能作为探索/工程记录 |
| `calibration-runtime-and-rules-63be8da.tar.gz` | 旧 24 个受控终态与规则结果；保留已发现的族标识泄漏证据 |
| `candidate-inputs-df8e8e7.tar.gz` | 控制标识投影修复后的发布绑定；Case 业务内容不变 |
| `calibration-runtime-and-rules-df8e8e7.tar.gz` | 当前 24 个受控终态与规则结果，独立标签复核仍待办 |
| `stub-smoke-63be8da.tar.gz` / `stub-smoke-df8e8e7.tar.gz` | 两个源码检查点各自两轮全链；每轮 11 Episode + 1 Failure，64 文件逐字节一致 |
| `*.inventory.json` | 对应压缩包及所有内部文件的原始字节数、SHA-256 与相对路径 |
| `run-summary.json` / `calibration-replacement-df8e8e7.json` | 唯一终态、来源、分轨数量、隔离及最新受控检查；不包含能力分数 |
| `calibration-identity-leak-63be8da.json` | 扩展扫描发现的旧族标识泄漏及修复位置 |
| `budget-summary.json` | 本轮 190 次真实请求的公开 usage 估计和未知费用预留；不含凭据或私有账本 |
| `validation-summary.json` / `validation/` | 分提交测试、真实源码变异重算、Ruff 基线、双轮检查与相关日志 |

这些 tar.gz 只包含已扫描的公开 JSON/Markdown/CSV，没有数据库、WAL/SHM、Capture、`.env`、密钥、私有思维链、Worker 目录或 Provider 原始响应。压缩使用固定时间戳；每份清单可以独立核对原始字节。

测试文本日志中 `profile-ref-red.log` 仅去除了 traceback 行尾空白，以通过 Git 空白检查；
原始与发布字节摘要记在 `validation-summary.json`。Runtime、Case 和压缩包成员未因此改写。

## 只读核对

在仓库根目录运行以下标准库检查，不解包，不调用模型、数据库或网络：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import hashlib
import json
import tarfile
from pathlib import Path

root = Path('evaluation/artifacts/e4-candidate-20260905')
for path in sorted(root.glob('*.tar.gz.inventory.json')):
    manifest = json.loads(path.read_text())
    archive = root / manifest['file']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['sha256']
    expected = {item['path']: item for item in manifest['members']}
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        assert len(members) == len(expected) == len({m.name for m in members})
        assert {m.name for m in members} == set(expected)
        for member in members:
            assert member.isfile() and not member.name.startswith('/')
            assert '..' not in Path(member.name).parts
            data = stream.extractfile(member).read()
            assert len(data) == expected[member.name]['bytes']
            assert hashlib.sha256(data).hexdigest() == expected[member.name]['sha256']
    print(archive.name, 'verified')
PY
```

需要运行 `validate-dataset` 时，在仓库外新建临时副本并检出 Manifest 中记录的原始 Git commit，
设置该副本的 `evaluation/src` 与 `backend` 为 PYTHONPATH，再校验临时解包目录。
活动 Validator 会校验当前协议与源码，所以不能在新提交上改写旧制品来消除版本差异。
历史协议试跑的不同摘要不是多个生产入口；对应公开旧执行入口仍禁用。

## 失败与费用边界

`primary-s04-r` 的 profile 逻辑引用和 `primary-s10-r` 的缺失 intake 空引用问题已离线修复，
并保留原失败。`primary-s06-p` 的 `runtime.non_exportable_terminal` 尚未得到完整根因：
保留了 3 次模型调用，最后一次 completion 达 16,000 Token，返回的两条工具参数均为 invalid_json；
这些是观察事实，不能据此断言框架终止的唯一原因，也不能删除该样本后宣布正式成功。

公开 usage 估算 4.355213 元，加 4 次未知请求预留 1.042432 元，共占用 5.397645 元，未超过 14 元。
没有查询账户账单，不能将估计写成实际余额。原始共享账本保存在私有本地状态目录；后续恢复必须复用，禁止重置本轮额度。
