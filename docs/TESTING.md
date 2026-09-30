# 产品验证与冻结评测

在仓库根目录使用已安装开发依赖和 `evaluation/runtime-requirements.lock` 的 Python 环境运行：

```bash
python -m pytest -q tests
python scripts/frozen-evaluation.py
# 发布时，两部分由同一个必需门禁顺序执行：
python scripts/release-check.py historical_migration
```

`pytest -q` 默认只运行当前产品的 `tests/`，其中保留 `test_e1_evaluation_seams.py`、`test_evaluation_audit_seams.py` 和全部迁移测试。

历史实验使用协议 1.20 固定的产品源码，不能用后续产品修复替换其来源。冻结评测脚本先核对当前 `evaluation/` 的已跟踪文件清单、暂存内容及工作区内容、文件类型和 Git 模式，要求与比赛提交 `7f31816b29bbb35390343db032af889d67efb0e7` 完全一致，也拒绝该目录中未忽略的未跟踪文件。然后在该提交的临时 detached Git worktree 中运行原始 `python -m pytest -q evaluation/tests`，保留真实提交与来源归属。

脚本不改写评测源码、数据、发布记录或档案哈希，不复制当前 backend 或 `.env`，不调用真实模型。子进程使用精简环境，清除继承的凭据、Python 路径和 pytest 选择参数；结束或测试失败后清理临时 worktree。

本地必须已有该比赛提交；缺少时直接失败，不自动抓取，也不回退到当前版本。浅克隆需要先显式补齐历史。CI 的 `historical_migration` checkout 使用 `fetch-depth: 0`，并强制依次通过当前产品与历史评测两部分；发布门禁仍是原来的八项。
