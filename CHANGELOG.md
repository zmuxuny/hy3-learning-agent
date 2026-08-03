# Changelog

## 0.5.0 (2026-08-03)

### 硬化队列（M7）

- 混合语义记忆检索：纯 Python BM25 + 本地 SimHash 向量，RRF 融合并输出可解释的 `score_breakdown`；无向量/无检索词时回退关键词排序。
- 可恢复 Run 状态机：工具轮次检查点、阻塞审批的 `waiting_approval` 暂停—批准—恢复、拒绝结果回填模型、启动时自动续跑有检查点的 Run。
- 写工具幂等：`ToolInvocation` 持久化 `idempotency_key`，重复调用返回原结果并带 `replayed` 标记。
- Run 预算：模型调用、Token、工具调用、网络请求、耗时与估算费用记录在 `budget_usage`，超限产生 `run.budget_exceeded` 事件并安全停止。
- Service Worker 通知：通知经 `showNotification` 展示并路由回收件箱；可选 VAPID Web Push，失效订阅自动清理。
- 通用受限子 Agent：`subagent_spawn/status/join/cancel`，v1 强制只读白名单、独立轮次上限与结构化报告。
- 设置页：模型连接、SMTP/IMAP 凭据与测试、通知策略（免打扰/每日上限/冷却时间）；凭据只写本地 `.env`（0600），API 不回传密码。
- CI：pytest + 前端构建 + `npm audit --omit=dev`；工具契约从 37 个扩展到 41 个（输入/输出 Schema 双向校验）。

### 文档与工程

- STATUS/ROADMAP/HARNESS/TOOL_PROTOCOL/EMAIL/PRODUCT/README 同步更新。
- 新增 `scripts/seed-fixture.sh`（从备份恢复浏览器回归夹具）与 `scripts/reset-data.sh`（备份并清空本地运行数据）。
