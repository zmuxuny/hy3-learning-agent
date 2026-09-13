# 第三阶段Demo素材与复建

[观看视频](../../../第三阶段Demo.mp4) · [封面](cover.png) · [逐段时间线](learning-agent-stage3.json) · [交互结果页](viewer.html)

视频为1920×1080、25fps、108秒，无音轨。除开头和结尾外，98秒均为实际页面操作：审阅与采用计划、接收后台主动支持、展开案例证据与评测结果。

| 时间 | 内容 | 观看重点 |
| --- | --- | --- |
| 0—2秒 | 项目理念 | 从学习目标到持续行动 |
| 2—8秒 | 输入目标 | 一周完成目录统计工具，每天30分钟 |
| 8—26秒 | 审阅与采用计划 | 查看任务、验收要求与学习安排 |
| 26—38秒 | 学习计划页 | 进入计划，浏览任务、截止与复习时间、成果提交入口 |
| 38—62秒 | 主动支持 | 查看临近截止的任务、后台发出的提醒，并进入对话继续学习 |
| 62—100秒 | 查看评测结果 | 展开Luhn校验案例的情境与评分依据，再查看质量对照图及四类应用结果 |
| 100—108秒 | 结果与交付入口 | 应用实验、方法验证和仓库材料 |

## 数据与画面来源

规划与采用画面来自`sources/goal-daytime.webm`、`sources/application-daytime.webm`及`sources/plan-daytime.webm`，对应[白天规划与提交记录](planning-daytime-evidence.tar.gz)。场景从9月11日15:00开始，真实Hy3提出30、60、60分钟的三项任务，用户审阅采用后进入计划页。主动支持画面来自`sources/proactive-daytime.webm`，对应[主动支持记录](../../readme/proactive-daytime-evidence.tar.gz)：在9月11日15:00的仓外临时学习场景中，后台调度器发现任务将在16:00截止，真实Hy3结合15:00—17:00的学习时段发送站内提醒，建议完成30分钟练习并提交运行结果。点击提醒可进入对话，免打扰保持23:00—08:00的默认设置。

评测画面来自`sources/evaluation-walkthrough.webm`，录制[交互结果页](viewer.html)中的实际操作。先展示自动与人工参考均为100分的Luhn校验案例，再展开七维依据，切换到方法验证：8个情境各设三种质量版本，每份评分3次，得到24组排序比较，其中23组顺序正确。全部案例与统计解释见[报告](../../../第三阶段项目与评测报告.md)。

产品与评测录制均保留完整1440×900视口，以原尺寸放在1920×1080画框的（240，140）位置；顶部、侧栏与底部完整保留。说明文字位于画面外，使用Noto Sans CJK SC字体、深蓝与灰白配色，章节间采用0.6秒渐变。主动片段将18.32秒原始操作按约0.74倍速完整播放，便于读清提醒和后续对话；成片仍为108秒。素材与证据摘要见[timeline.json](timeline.json)。

## 重建命令

从仓库根目录执行，无模型调用。需要Python、Pillow、Matplotlib、Noto中文字体、ffmpeg以及前端Playwright依赖和Chromium。

```bash
python scripts/plot-final-method-results.py
python scripts/build-final-study-viewer.py
python scripts/build-stage3-demo.py
```

成片使用H.264高效率编码并将播放索引置于文件开头，保持1080p、25fps。最终视频位于仓库根目录`第三阶段Demo.mp4`；本目录保存素材、封面与视频清单。

`build-stage3-demo.py`按[timeline.json](timeline.json)验证素材与实验摘要，合成后检查时长和分辨率，输出根目录视频并更新本目录的`learning-agent-stage3.json`。归档视频可直接合成，无需再次调用模型；完整108秒成片的时间和摘要见[视频清单](learning-agent-stage3.json)。

## README运行截图

[截图来源与摘要](../../readme/screenshots/sources.json)记录素材版本、完整画面尺寸及对应证据。README中的产品图保留完整1440×900视口，验收案例图保留结果页的完整页面。

| 截图 | 来源与呈现内容 |
| --- | --- |
| [计划审阅](../../readme/screenshots/01-plan-review.png) | 白天录屏中的三项任务提案，预计150分钟，待用户采用；保留完整视口。 |
| [主动支持](../../readme/screenshots/02-proactive-support.png) | 9月11日15:00的临时学习场景，默认免打扰23:00—08:00；后台触发真实Hy3生成提醒。完整视口录屏、通知记录、模型调用与场景配置见[主动提醒素材](../../readme/proactive-daytime-evidence.tar.gz)。 |
| [验收实验归档](../../readme/screenshots/03-assessment-archive.png) | 在既有结果页选择A33，滚动输出框到失败测试记录，保留页面标题、案例选择、统计与评分依据。 |
| [学习计划](../../readme/screenshots/04-learning-plan.png) | 同一计划采用后的任务工作区，包含截止与复习安排、当前建议及成果提交入口。 |
| [提交成果](../../readme/screenshots/05-submit-work.png) | 从任务进入提交对话，已上传真实脚本并填写运行结果，画面处于待发送状态。 |
