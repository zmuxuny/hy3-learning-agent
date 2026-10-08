# 架构图

项目提供闭环概览、完整技术详图和面试讲解版，分别用于介绍产品理念、查看完整架构和讲解Agent实现。

| 视图 | 内容与阅读顺序 | 文件 |
| --- | --- | --- |
| Learning Agent · Hy3 整体架构 | 报告图1，提供闭环概览。由学习目标、计划、提交与验收进入状态更新，再看主动判断的“保持安静”和“提醒、抽查、调整”分支；底部连接离线评测。 | [SVG](../../stage3/application-evaluation-architecture.svg) · [PNG](../../stage3/application-evaluation-architecture.png) · [draw.io源文件](../../stage3/application-evaluation-architecture.drawio) |
| 系统技术架构详图 | README与技术文档。从工作台进入Agent Runtime，展开上下文、Hy3决策循环、工具审批、状态保存与效果投递；下方展开Decision Episode和评分流程。 | [SVG](learning-agent-system-architecture.svg) · [PNG](learning-agent-system-architecture.png) · [draw.io源文件](learning-agent-system-architecture.drawio) |
| 面试讲解版 | 按①主动决策、②上下文与记忆、③可恢复运行、④工具契约展开。保留学习事件触发、上下文装配、记忆生命周期、工具契约、审批暂停、检查点恢复和状态写回；不包含离线评测和Outbox。 | [SVG](learning-agent-interview-architecture.svg) · [PNG](learning-agent-interview-architecture.png) · [draw.io源文件](learning-agent-interview-architecture.drawio) |

网页中的架构图均固定为白底，浏览器深色主题不改变图内颜色。PDF报告中的整体架构图沿用浅灰蓝底色（`#E8ECF1`），由报告导出脚本切换背景，布局与内容保持一致。技术详图仅在Hy3决策循环模块使用混元图形标识；概览图中央代表整个学习助手，使用项目自己的书页路径Logo。混元标识来源见[素材说明](THIRD_PARTY_NOTICES.md)。

在仓库根目录运行以下命令可从draw.io源文件重新导出两张图，并更新报告：

```bash
node scripts/export-architecture.mjs
node scripts/render-stage3-report.mjs
```

导出依赖前端已安装的Playwright、Chromium，以及可访问diagrams.net的网络；可通过`CHROMIUM_PATH`指定浏览器路径。导出脚本也接受单个draw.io文件的仓库相对路径。

## 面试讲解版

这张图独立于README和报告中的原图，适合在PPT中横向展示。四个编号对应以下指示路径：

1. **主动决策**：从左下持续学习状态向上，经过后台事件筛选与上下文装配，指向Hy3循环中的“介入或等待”。
2. **上下文与记忆**：从业务状态、记忆生命周期，指向“当前学习事实 / 对话与长期记忆”，再沿检索排序、预算控制、Context Snapshot进入模型。
3. **可恢复运行**：沿Hy3与工具之间的往返连线，说明模型决策和真实执行结果如何构成循环，再向下指到持久化检查点，解释运行位置、已执行结果和恢复。
4. **工具契约**：从右下工具能力向上，展开参数与结果契约、权限检查及审批暂停分支；最后沿底部状态写回线回到学习状态，说明后续运行如何接续进展。

业务状态和执行检查点分别保存学习进展与运行位置。长期记忆按全局、计划和会话范围管理，确认且有效的记忆才进入检索。图中的学习工具能力列举实际操作类别，审批是否必需由工具及当前授权条件决定。

单独重新导出面试版：

```bash
node scripts/export-architecture.mjs assets/proposal/architecture/learning-agent-interview-architecture.drawio
```
