# Hy3 接口切换与兼容验证（2026-09-08）

产品、评测Agent与Judge现统一使用 `.env` 的 `OPENAI_API_BASE=https://cf.hetune.top/v1`、`MODEL_NAME=hy3` 和密钥；密钥仅在本机忽略文件中。开发提交 `a4766b6`，已见Development绑定及真实验证源码 `de8f35c`。Protocol1.6是未登记候选，继承1.5完整证据输入编码修复，未改变本轮评分Prompt/Rubric；旧1.0–1.5资产及历史结果保持原字节。

## 验证结果

- 仓外回归45 passed；初次36 passed/9 failed的日志保留。初次失败来自新增CLI测试缺manifest和stub attestation未在签名前显式含api_base空值，均已修正。
- 流式请求成功；独立强制tool_choice探测90秒超时，未重试、usage未知，保留0.010650元等价预留。
- 干净仓外副本、临时库运行已见 `dev-afternoon-i`：1 Episode/0 Failure，2真实模型调用、1次站内通知实际提交，声明与效果一致；Rules与真实Judge链路完成，Judge1有效/0失败/0修复。
- Agent与Judge均记录实际第三方api_base和openai-compatible来源，不能据此证明中转站内部模型来源。主AI核对完整公开轨迹和时区事实；本次是工程兼容验证，formal=false，不代替新版方法验证或E6正式批次。
- 当前协议/注册表只读重建通过；463份旧制品、Release与Schema文件与905ac3c逐字节一致；21个归档文件回验一致。1.5历史源码只读验证也通过。

## 费用与接续

唯一账本：`/root/.local/state/learning-travel/e4-hy3-20260905-osj4FCZZ-budget.json`。本次约50元可用额度已登记一次，授权ID `hy3-configurable-provider-50-cny-equivalent-20260908`；累计上限84.393029元，原907请求完全保留。新增5请求占用0.105481元，累计912请求/占用34.498510元，剩余49.894519元。后续读实际账本，不再加50/17元。

新站公开的是倍率，实际人民币换算未确认；以上暂沿用输入1/输出4元每百万token作为官方价格等价估算，非账户实扣。代码支持小数费率，每次请求冻结费率、接口和依据；未知usage继续保留预留。没有为了本次接口切换重跑整批E6。

[完整证据包](provider-evidence.tar.gz)保存两次回归、所有探测（含失败）、运行源码脚本、Episode/Rules/Judge、Judge尝试和账本前后快照；[文件摘要](evidence-manifest.json)、[核验结果](checks.json)可复核。E6待办与具体反例见[接续提示词](https://github.com/zmuxuny/hy3-learning-agent/blob/1295254e2f8f792da30eb331cb2d0ea12437fa0b/docs/E6%E6%8E%A5%E7%BB%AD%E6%8F%90%E7%A4%BA%E8%AF%8D.md)，旧修复批次见[原档案](../e6-repair-20260908/README.md)。开发与本次审计均为主AI。

本轮仓外副本、pytest临时库及探测目录已归档后清理，见[清理回执](cleanup.json)。仅本地提交，不推送、不发布。
