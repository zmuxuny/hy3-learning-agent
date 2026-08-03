# Changelog

## 0.7.0 (2026-08-03)

### 对话体验与上下文可视化

- Codex 式可展开记录：计划澄清问答提交后可点击展开查看回答；运行工作记录内展示“本次使用记忆”与子 Agent 活动（角色、状态、工具、结论）。
- 输入框对标基础 Chatbot：运行中显示停止按钮；运行中发送可选择“排队等待”或“打断当前运行并发送”；排队提示可取消。
- 复制增强：代码块一键复制，助手回答可整体复制。
- 上下文占用可视化：新增 `MODEL_CONTEXT_WINDOW` 配置并在设置接口暴露；输入栏与运行轨迹显示当前上下文占用（k / 模型窗口）。
- 记忆亮点：`context.built` 事件携带本次使用记忆 ID；对话内展示记忆引用，首页新增记忆概览，记忆页支持搜索与作用域/状态计数。

## 0.6.0 (2026-08-03)

### 稳定性与体验

- Web 搜索新增 Bing HTML 备选源与自动降级：主源失败、超时或返回空结果时切换，结果带 `fallback_used` 标记；`WEB_SEARCH_FALLBACK_PROVIDER` 可设为 `none` 关闭。
- 邮件渠道纳入每日上限与冷却统计；邮件发送失败的错误详情完整回填给模型。
- 连续天数真实计算：以 `ActivityDay` 为准，任务完成/测验通过事件驱动 `streak_days` 刷新。
- 规则成就引擎：首次建计划、首次完成任务、首次测验通过、3/7 天连续、100/500 XP 共 7 条规则，幂等解锁并展示在首页成就墙。
- 设置页冷却时间预填；浏览器回归脚本增加成就墙检查。
- 文档事实修正：M6 版本标签/CHANGELOG 标记完成，明确 `main` 存档 / `develop` 开发线分支约定。

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
