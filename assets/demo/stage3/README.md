# 学习助手与评测：108秒Demo

[观看视频](learning-agent-stage3.mp4) · [封面](cover.png) · [逐段时间线](learning-agent-stage3.json) · [交互结果页](viewer.html)

视频为1920×1080、25fps、108秒，无音轨，同时展示真实Hy3学习规划操作和学习决策评测。应用片段包含作品目标、计划审阅、采用和任务工作区；评测部分依次说明完整决策的评分流程、错误验收识别案例，以及用固定质量对照检验评分方法的实验。

| 时间 | 内容 | 读者可以核对的重点 |
| --- | --- | --- |
| 0—8秒 | 项目场景与目标 | 围绕学习目标提供计划、反馈与主动支持 |
| 8—16秒 | 输入学习目标 | 一周完成目录统计工具，每天30分钟 |
| 16—42秒 | 真实计划审阅与采用 | 展开任务与验收要求，采用后查看学习安排 |
| 42—60秒 | 评测方法 | 完整决策证据、规则核对、Hy3内容核验与七维评分 |
| 60—84秒 | B15去重函数验收 | 测试仍有重复项却被虚称成功；评测器指出证据矛盾并给30分、严重错误 |
| 84—108秒 | 方法验证与数据入口 | 去重验收三种质量的具体得分、23/24组排序正确、正常与严重错误对照 |

## 数据与画面来源

应用画面来自真实录制的`sources/goal.webm`和`sources/application.webm`。对应[证据压缩包](recorded-evidence.tar.gz)保存计划、任务、消息、工具操作及采用记录；源码与素材摘要见[timeline.json](timeline.json)。

评测展示从[主体结果表](../../../evaluation/artifacts/decisionbench-final-method-20260910/automatic/)和[条件验证结果](../../../evaluation/artifacts/condition-validation-20260910/automatic/)生成。视频选取B15错误验收记录展示具体判断，再用B13—B15三种处理说明质量差异；交互结果页可查看全部应用案例和自动评分与人工标注的对齐。主体三档严格排序23/24，重复加权分标准差均值3.70，操纵误通过1/8；正常/严重对照36/36与36/36，条件对照18/18与18/18。详细解释见[报告](../../../第三阶段项目与评测报告.md)。

产品录制保留完整1440×900视口，以原尺寸放在1920×1080画框内的（240，140）位置。页面顶部、侧栏与底部均保留，说明文字位于画面之外。全片使用书页与学习路径构成的[项目Logo](../../brand/learning-agent-logo.svg)、深蓝与灰白配色；章节间采用0.6秒渐变，评测画面分别停留18、24、24秒。交互结果页另供查看完整记录。

## 重建命令

从仓库根目录执行，无模型调用。需要Python、Pillow、Matplotlib、Noto中文字体、ffmpeg以及前端Playwright依赖和Chromium。

```bash
python scripts/plot-final-method-results.py
python scripts/build-final-study-viewer.py
python scripts/build-stage3-demo.py
```

`build-stage3-demo.py`按[timeline.json](timeline.json)验证素材与实验摘要，合成后检查时长和分辨率，输出视频及同名JSON。评测画面由已保存的结果生成，无需重新录制浏览器；完整108秒成片的时间和摘要见[视频清单](learning-agent-stage3.json)。
