from pathlib import Path
import json,hashlib,re
repo=Path('/root/workspace/tencent_rhinobird2026/learning_travel');r=Path(__file__).parent;out=repo/'evaluation/artifacts/e6-audit-fixes-20260909';out.mkdir(exist_ok=True)
explanation='冻结批次已执行；因运行及评分不完整，未形成正式能力统计。I批formal=false源于I01-P调用失败使来源资格不完整，以及Runtime/Judge错误；18个有分失败案例不参与该布尔值计算。Benchmark登记与完整套件检查通过，第三方接口也不是一律不受信任。'
correction='H07-R为本地响应投影拒绝（framework_error / response_projection_rejected），具体触发原因无法从保留材料确认；不是已证实的Provider失败。原记录含服务端请求ID及21699输入/421输出usage，无分状态保持不变。'
targets=[]
for rel,pointer in [('formal/runtime/failures/failure:formal-h07-r.json','public_summary'),('formal/content-audit.json','rows[27].rationale'),('formal/report/summary.json','cases[27]')]:
 p=r/'frozen-evidence'/rel;targets.append({'archive_member':rel,'file_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'field_or_row':pointer})
errata={'baseline':'b2352dd','case_id':'formal-h07-r','reviewer_role':'primary_ai_reviewer','source':'user-supplied independent review, primary AI rechecked original failure','corrected_attribution':correction,'original_artifacts_unchanged':True,'denominators_and_scores_unchanged':True,'targets':targets,'evidence_paths':['failure_class','model_calls[0].status','model_calls[0].response_validation_errors[0].code','model_calls[0].token_usage','provider_attestation.calls[0].provider_request_id']}
(out/'h07-errata.json').write_text(json.dumps(errata,ensure_ascii=False,indent=2)+'\n')
p=repo/'evaluation/artifacts/e6-completion-20260909/README.md';s=p.read_text().replace('结果formal=false，不能称正式能力验收通过。','结果formal=false。'+explanation).replace('正式能力阻断：','正式统计完整性阻断：').replace('H03-P原身份Failure和H07-R原Provider Failure保留；','H03-P原身份Failure保留。'+correction+' 原Failure、汇总和旧AI审核文字不覆盖，归因读取[H07-R更正](../e6-audit-fixes-20260909/h07-errata.json)；');a=s.index('最终批次可离线复核：');z=s.index('[归档校验]',a);s=s[:a]+'''复算需要包含冻结提交的完整Git历史：来源校验会执行`git archive <commit>`。源码快照只用于核对字节，单独解压不能替代Git对象库。下面以I批为例；H批将提交换为`83fbf52`、数据版本换为`1.9`、运行目录换为`formal`。

```bash
REPO=/root/workspace/tencent_rhinobird2026/learning_travel
WORK=$(mktemp -d)
mkdir "$WORK/evidence"
tar -xzf "$REPO/evaluation/artifacts/e6-completion-20260909/public-evidence.tar.gz" -C "$WORK/evidence"
git clone --no-hardlinks "$REPO" "$WORK/source"
git -C "$WORK/source" checkout --detach 35e77cf
cd "$WORK/source"
# 使用项目锁定依赖；已有同一锁定环境时也可使用其Python绝对路径。
uv sync --frozen
PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/summarize_e6_run.py \\
  --dataset evaluation/datasets/decisionbench-v1.13-e6-final-test \\
  --run "$WORK/evidence/formal113" --output "$WORK/recomputed"
cmp "$WORK/recomputed/summary.json" "$WORK/evidence/formal113/report/summary.json"
cmp "$WORK/recomputed/cases.csv" "$WORK/evidence/formal113/report/cases.csv"
```

以上复算不调用模型，也不需要.env。H/I修正后步骤均已在带Git历史的仓外冻结副本验证，见[复算证据](../e6-audit-fixes-20260909/frozen-recompute-verification.json)。原包内错误归因用更正文件解释，不改原结果以维持原字节复算。

'''+s[z:];s+='\n独立审计后的工程修复为Protocol1.14未登记候选，完成离线验证，未做新付费方法实验或新测试批次；1.13原实验不移名。见[审计修复记录](../e6-audit-fixes-20260909/README.md)。\n';p.write_text(s)
for name in ['docs/STATUS.md','docs/E6接续提示词.md','docs/E6最终修复与验收记录.md','docs/腾讯犀牛鸟开源实习第三阶段项目方案.md','docs/腾讯犀牛鸟开源实习第三阶段评测实施方案.md']:
 p=repo/name;s=p.read_text();title,body=s.split('\n',1);s=title+'\n\n> 审计后口径：'+explanation+' 工程补修为Protocol1.14未登记候选，旧实验仍固定1.13；见[审计修复档案](../evaluation/artifacts/e6-audit-fixes-20260909/README.md)。\n'+body;s=s.replace('E6执行与审核归档完成，正式能力资格未通过','E6执行与审核归档完成；运行及评分不完整，未形成正式能力统计').replace('正式能力资格未通过','未形成正式能力统计');p.write_text(s)
p=repo/'docs/腾讯犀牛鸟开源实习第三阶段评测实施方案.md';s=p.read_text().replace('本轮修订输入没有新增真实执行','历史修订当时未新增执行；其后E6已完成F/G/H/I各冻结批次').replace('真实运行尚未开始，失败/未运行不记零分','E6全批真实运行及失败保全已完成，失败/未运行不记零分').replace('- [ ] 在 DecisionBench v1 完成正式全量评测；','- [x] 完成冻结测试全批执行：最终I48为47 Episode/1 RuntimeFailure、41有效Judge/6错误；formal=false，不代表模型必须全通过；').replace('- [ ] 输出逐 Episode、逐维度、逐轨和错误类型结果；','- [x] 输出逐Episode、逐维度、逐轨和错误类型结果；原始与裁决汇总、H07-R归因更正分别保留；').replace('- [ ] 每轨包含成功、失败与分歧 Case；','- [x] 每轨保留成功、失败和Judge/AI内容审核分歧案例，见E6完整逐例档案；').replace('- [ ] 形成 Hy3 失败模式、能力边界和版本对比。','- [x] 形成Hy3失败模式及本批能力边界；H/I等批次分开报告。\n- [ ] E7受控版本对比未开展，不把不同家族通过率当因果提升。');p.write_text(s)
p=repo/'docs/ROADMAP.md';s=p.read_text();s=re.sub(r'^> 状态口径.*$', '> 当前状态（2026-09-09）：E6冻结批次执行与审计完成；运行及评分不完整，未形成正式能力统计。独立审计的工程补修为Protocol1.14未登记候选，旧实验固定1.13；E7/E8未开展。当前入口见[STATUS](STATUS.md)及[E6记录](E6最终修复与验收记录.md)。',s,flags=re.M);s=re.sub(r'^- \[ \] E6 正式能力验收：.*$', '- [x] E6冻结批次执行与审计归档完成；模型失败作为评测发现保留。工程补修、方法限制与统计完整性分开报告，见[E6最终记录](E6最终修复与验收记录.md)。',s,flags=re.M);p.write_text(s)
p=repo/'evaluation/README.md';s=p.read_text().replace('当前活动协议为 **Evaluation Protocol Release 1.13**','当前活动协议为 **Evaluation Protocol Release 1.14（审计补修、未登记候选，仅离线验证）**').replace('| 活动 Protocol1.13 及历史版本分别绑定组件 |','| 活动候选Protocol1.14及历史版本分别绑定组件 |').replace('当前 `decisionbench-v1.13-regression/engineering`','以下冻结1.13示例需先检出`35e77cf`，不能直接用于1.14候选。`decisionbench-v1.13-regression/engineering`');s+='\n审计修复、原H07-R归因更正、正式统计口径与Git历史复现要求见[补修档案](artifacts/e6-audit-fixes-20260909/README.md)。当前候选可运行`PYTHONPATH=evaluation/src:backend .venv/bin/python evaluation/scripts/build_e6_repair_releases.py verify`检查绑定；旧测试不重新登记或回填。\n';p.write_text(s)
p=repo/'evaluation/releases/README.md';s=p.read_text().replace('活动协议为Evaluation Protocol Release 1.13；','活动协议为Evaluation Protocol Release 1.14未登记候选（仅离线验证）；历史最终实验仍为1.13。');p.write_text(s)
print('errata and current documents updated; frozen results unchanged')
