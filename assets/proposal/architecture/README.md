# 架构图

项目提供两种视图，分别用于理解学习闭环和查看技术实现。

| 视图 | 内容与阅读顺序 | 文件 |
| --- | --- | --- |
| Learning Agent · Hy3 整体架构 | 报告图1，提供闭环概览。由学习目标、计划、提交与验收进入状态更新，再看主动判断的“保持安静”和“提醒、抽查、调整”分支；底部连接离线评测。 | [SVG](../../stage3/application-evaluation-architecture.svg) · [PNG](../../stage3/application-evaluation-architecture.png) · [draw.io源文件](../../stage3/application-evaluation-architecture.drawio) |
| 系统技术架构详图 | README与技术文档。从工作台进入Agent Runtime，展开上下文、Hy3决策循环、工具审批、状态保存与效果投递；下方展开Decision Episode和评分流程。 | [SVG](learning-agent-system-architecture.svg) · [PNG](learning-agent-system-architecture.png) · [draw.io源文件](learning-agent-system-architecture.drawio) |

两张图均固定为白底，浏览器深色主题不改变图内颜色。混元图形标识来自官方素材，来源见[素材说明](THIRD_PARTY_NOTICES.md)。

在仓库根目录运行以下命令可从draw.io源文件重新导出两张图，并更新报告：

```bash
node scripts/export-architecture.mjs
node scripts/render-stage3-report.mjs
```

导出依赖前端已安装的Playwright、Chromium，以及可访问diagrams.net的网络；可通过`CHROMIUM_PATH`指定浏览器路径。导出脚本也接受单个draw.io文件的仓库相对路径。
