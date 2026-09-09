# 复算与重新运行

在仓库根目录执行。环境安装见根README；Python依赖使用已锁定的项目环境。

## 离线复算已报告结果（不调用模型）

```bash
work=$(mktemp -d /tmp/decisionbench-recompute-XXXXXX)
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/summarize_decisionbench_final.py \
  --evidence evaluation/artifacts/decisionbench-study-20260910/evidence --output "$work/automatic"
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/summarize_decisionbench_review.py \
  --evidence evaluation/artifacts/decisionbench-study-20260910/evidence \
  --notes evaluation/artifacts/decisionbench-study-20260910/evidence/review-notes.json --output "$work/review"
diff -r evaluation/artifacts/decisionbench-study-20260910/automatic "$work/automatic"
diff -r evaluation/artifacts/decisionbench-study-20260910/review "$work/review"
python scripts/plot-stage3-results.py --output "$work/figures"
```

复算逐一验证48条公开证据与原始运行、规则和参考的连接，验证每次请求、原始响应与解析结果的一致性，核对失败恢复链和128个固定评分位置，重新计算分数、判别力与重复波动。复核意见另有原始结果和证据摘要绑定。

图表需要matplotlib及Noto Sans CJK字体；PNG和SVG已一并提供，读取结果不依赖绘图库。

## 查看全部应用运行

```bash
tar -xzf evaluation/artifacts/decisionbench-study-20260910/application-runs.tar.gz -C "$work"
```

`product-final`四类索引保存所有首次及故障后重试；`product-final-recovery`保存补充成功运行。每次运行的manifest、episode、故障和模型调用均可查看。选择表列出的相对路径对应这些目录。

## 新的真实模型实验

新的模型调用会产生新的输出，应写入新的仓外目录，使用自己的API配置和费用账本。命令格式：

```bash
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/run_learning_quality.py --help
PYTHONPATH=evaluation/src:backend .venv/bin/python -m learning_agent_eval --help
```

本轮固定评分输入为`evidence/full/suite.json`（48×1）、`evidence/validation/suite.json`（24×3）、`evidence/adversarial/suite.json`（8×1）。运行器要求源码干净、依赖与锁文件一致，密钥通过环境变量传入。`design.json`包含方法全文、结构约束与样本摘要；原始评分响应记有实际模型参数和调用凭证。

应用生成源码为500f280，评分源码为2a9c38a，解析恢复为7ac56ed，统一无分恢复为500622e。可用`git worktree add --detach <仓外路径> <提交>`创建对应副本。开发记录压缩包保留本轮实际调度脚本和参数；其中绝对路径是当时的执行位置，迁移时需改为本机路径。新实验不能替换本档案的原始响应。
