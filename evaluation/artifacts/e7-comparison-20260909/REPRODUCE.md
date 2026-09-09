# E7 复现

本目录的 `public-evidence.tar.gz` 保存原始输入、运行结果、Judge 公开响应与复核；`summary.json`、CSV 和图表从它复算。源码历史及增量 Git bundle 一起定位当时实现。所有命令在仓外生成结果。

## 离线复算（不调用 API）

从包含本档案的本地分支创建全新副本，保留 Git 历史；先把结果复制到仓外，再检出固定复算源码，避免后续项目升级改变活动协议：

```bash
REPO=/root/workspace/tencent_rhinobird2026/learning_travel
E7_REPRO=$(mktemp -d /tmp/learning-travel-e7-repro-XXXXXX)
git clone --no-hardlinks "$REPO" "$E7_REPRO/source"
cd "$E7_REPRO/source"
cp -a evaluation/artifacts/e7-comparison-20260909 "$E7_REPRO/expected"
git fetch "$E7_REPRO/expected/baseline-controls.bundle" 'refs/heads/*:refs/remotes/e7-archive/*'
git checkout --detach e8c3e2d
python3 -m venv "$E7_REPRO/venv"
"$E7_REPRO/venv/bin/pip" install -r evaluation/runtime-requirements.lock
export PYTHONPATH="$E7_REPRO/source/evaluation/src:$E7_REPRO/source/backend"
tar -xzf "$E7_REPRO/expected/public-evidence.tar.gz" -C "$E7_REPRO"
"$E7_REPRO/venv/bin/python" evaluation/scripts/summarize_e7.py \
  --evidence "$E7_REPRO/evidence" --output "$E7_REPRO/recomputed"
cmp "$E7_REPRO/expected/summary.json" "$E7_REPRO/recomputed/summary.json"
cmp "$E7_REPRO/expected/pairs.csv" "$E7_REPRO/recomputed/pairs.csv"
cmp "$E7_REPRO/expected/slots.csv" "$E7_REPRO/recomputed/slots.csv"
cmp "$E7_REPRO/expected/method-stability.csv" "$E7_REPRO/recomputed/method-stability.csv"
"$E7_REPRO/venv/bin/python" evaluation/scripts/run_e7_offline_mini.py --output "$E7_REPRO/mini"
```

复算验证原 Episode/Rule/Reference/Manifest/Judge 的摘要和连接；从原始七维和确定性 Gate 重建分数，应用绑定到 Judge 摘要的语义裁决，再重建配对及方法重复统计。`mini/receipt.json` 是四轨固定响应工程链验证，不进入真实实验计数。

图表在独立绘图环境生成，避免改变 Runtime 锁定依赖：

```bash
python3 -m venv "$E7_REPRO/plot-venv"
"$E7_REPRO/plot-venv/bin/pip" install matplotlib==3.10.6
"$E7_REPRO/plot-venv/bin/python" evaluation/scripts/plot_e7.py \
  --summary "$E7_REPRO/recomputed/summary.json" --output "$E7_REPRO/figures"
```

## 真实重新运行

真实 API 输出会波动。重新调用是一次新实验，不能覆盖本档案，也不能替换失败槽位。沿用已有配置文件和同一账本，保留已发生请求，不重置额度。

固定产品源码：Baseline `2df1b658f727e757aa2da0d10df072ee93589bed`（`bf06d36` 产品加相同测试登记）、Candidate `1fa682fd0ea6a8d23b14f6fe744ddae489bb2331`。Judge 源码：旧版 `bf06d3697e67bcd1c7e115fd1d5ba9c56f214b1e`，新版实际执行 `deb3d75f399a541ab301b711bc0d29ea7eb8d65c`。方法设计固定 `848dbb8` 后增加的开发数据提交没有改变 Judge 源码或 Prompt。详见 `evidence/*-design.json` 和逐调用 Attestation。

在独立干净 clone 检出对应产品提交后，每个完整家族使用以下命令；按 J01 Baseline→Candidate、J02 Candidate→Baseline、J03 Baseline→Candidate、J04 Candidate→Baseline 执行。`E7_OUT` 必须是不存在的仓外目录。

```bash
E7_ENV=/root/workspace/tencent_rhinobird2026/learning_travel/.env
E7_LEDGER=/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json
E7_DATASET=evaluation/datasets/e7-test-e7-j01
E7_OUT="$E7_REPRO/new-product-baseline-j01"
export PYTHONPATH="$PWD/evaluation/src:$PWD/backend"
"$E7_REPRO/venv/bin/python" -m dotenv -f "$E7_ENV" run -- \
  "$E7_REPRO/venv/bin/python" -m learning_agent_eval run-agent \
  --dataset "$E7_DATASET" --manifest "$E7_DATASET/manifest.json" \
  --output "$E7_OUT/runtime" --model-mode real --allow-real-model --budget-ledger "$E7_LEDGER"
"$E7_REPRO/venv/bin/python" -m learning_agent_eval evaluate-rules \
  --input "$E7_OUT/runtime" --output "$E7_OUT/rules"
```

Rules 出现行为失败时 CLI 可以返回 2；读取已输出 Manifest，保留失败后继续评分。在另一个检出 `deb3d75` 的干净 clone 中，用本交付版 `evaluate_e7_trace.py` 和同一新 Judge 对两侧原始轨迹评分：

```bash
export PYTHONPATH="$PWD/evaluation/src:$PWD/backend"
"$E7_REPRO/venv/bin/python" -m dotenv -f "$E7_ENV" run -- \
  "$E7_REPRO/venv/bin/python" "$E7_REPRO/source/evaluation/scripts/evaluate_e7_trace.py" \
  --run "$E7_OUT" --output "$E7_OUT/common-judge" --budget-ledger "$E7_LEDGER"
```

方法重复使用 `evidence/e6/calibration` 同一轨迹，顺序为旧版第 1 次、新版第 1 次、新版第 2 次、旧版第 2 次，每次 12 例；控制组使用 `evidence/controls`，新旧各 4 例。分别在对应 Judge clone 运行上述评分脚本，输出到新目录。原始控制组是受控响应经过 Runtime 导出的轨迹，Judge 为真实调用；不能将它记作真实产品生成。

复用测试会成为已见复测。需要新的泛化结论时，应另行固定新输入与实验设计；本次全部原始失败、无分与旧版本结果仍保留。

如需重新生成控制组 Runtime，在已导入 bundle 的干净仓外副本检出 `e0d0b9bf62a30b0d0ef466742643118ad15e0b56`，运行固定响应模式；这一阶段不调用 API：

```bash
E7_CONTROL_DATA=evaluation/datasets/e7-method-controls
"$E7_REPRO/venv/bin/python" -m learning_agent_eval run-agent \
  --dataset "$E7_CONTROL_DATA" --manifest "$E7_CONTROL_DATA/manifest.json" \
  --output "$E7_REPRO/new-controls/runtime" --model-mode stub
"$E7_REPRO/venv/bin/python" -m learning_agent_eval evaluate-rules \
  --input "$E7_REPRO/new-controls/runtime" --output "$E7_REPRO/new-controls/rules"
```

随后按前述新旧 Judge 命令各评一次。开发两例的输入已随 Candidate `deb3d75` 保存于 `evaluation/datasets/e7-development/`；开发轨迹与固定 Test 分开，不进入产品配对数。
