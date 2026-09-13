# 第三阶段Demo素材与复建

[观看视频](../../../第三阶段Demo.mp4) · [封面](cover.png) · [逐段时间线](learning-agent-stage3.json) · [交互结果页](viewer.html)

视频为1920×1080、25fps、108秒，无音轨。除开头和结尾外，98秒均为实际页面操作：审阅与采用计划、接收后台主动支持、展开案例证据与评测结果。

| 时间 | 内容 | 观看重点 |
| --- | --- | --- |
| 0—2秒 | 项目理念 | 从学习目标到持续行动 |
| 2—8秒 | 输入目标 | 一周完成目录统计工具，每天30分钟 |
| 8—32秒 | 审阅与采用计划 | 查看任务、验收要求与学习安排 |
| 32—62秒 | 主动支持 | 查看临近截止的任务、后台发出的提醒，并进入对话继续学习 |
| 62—100秒 | 查看评测结果 | 展开Luhn校验案例的情境与评分依据，再查看质量对照图及四类应用结果 |
| 100—108秒 | 结果与交付入口 | 应用实验、方法验证和仓库材料 |

## 数据与画面来源

规划与采用画面来自`sources/goal.webm`和`sources/application.webm`，对应[规划证据包](recorded-evidence.tar.gz)。主动支持画面来自`sources/proactive.webm`，对应[主动支持记录](proactive-evidence.tar.gz)：在仓外临时学习场景中，生产后台调度器自动发现临近截止的任务，真实Hy3读取状态并发送站内提醒。提醒指出前置任务尚未开始，建议先完成一个5—10分钟的小步骤；点击提醒可进入对话。场景明确允许夜间站内提醒。

评测画面来自`sources/evaluation-walkthrough.webm`，录制[交互结果页](viewer.html)中的实际操作。先展示自动与人工参考均为100分的Luhn校验案例，再展开七维依据，切换到方法验证：8个情境各设三种质量版本，每份评分3次，得到24组排序比较，其中23组顺序正确。全部案例与统计解释见[报告](../../../第三阶段项目与评测报告.md)。

产品与评测录制均保留完整1440×900视口，以原尺寸放在1920×1080画框的（240，140）位置；顶部、侧栏与底部完整保留。说明文字位于画面外，使用Noto Sans CJK SC字体、深蓝与灰白配色，章节间采用0.6秒渐变。素材与证据摘要见[timeline.json](timeline.json)。

## 重建命令

从仓库根目录执行，无模型调用。需要Python、Pillow、Matplotlib、Noto中文字体、ffmpeg以及前端Playwright依赖和Chromium。

```bash
python scripts/plot-final-method-results.py
python scripts/build-final-study-viewer.py
python scripts/build-stage3-demo.py
```

最终视频位于仓库根目录`第三阶段Demo.mp4`；本目录保存素材、封面与视频清单。

`build-stage3-demo.py`按[timeline.json](timeline.json)验证素材与实验摘要，合成后检查时长和分辨率，输出根目录视频并更新本目录的`learning-agent-stage3.json`。归档视频可直接合成，无需再次调用模型；完整108秒成片的时间和摘要见[视频清单](learning-agent-stage3.json)。

## README运行截图

[截图来源与摘要](../../readme/screenshots/sources.json)记录素材版本、裁剪范围及对应证据。图片通过截帧或现有页面截图生成，保留原始文字和状态；原视频与报告共用截图未被覆盖。

| 截图 | 来源与呈现内容 |
| --- | --- |
| [计划审阅](../../readme/screenshots/01-plan-review.png) | 从现有[应用截图](../../stage3/application-screenshot.png)裁出计划区域，保留“等待确认”和三个任务。 |
| [主动支持](../../readme/screenshots/02-proactive-support.png) | 主动录屏第25秒，保留计划标题与通知正文；使用已允许夜间提醒的临时学习场景。 |
| [验收实验归档](../../readme/screenshots/03-assessment-archive.png) | 在既有结果页选择A33，滚动到失败测试记录，截取案例区域；这是离线归档，素材含真实模型输出与预设作品证据。 |
