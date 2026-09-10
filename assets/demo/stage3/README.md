# 学习助手与评测：108秒Demo

[观看视频](learning-agent-stage3.mp4) · [封面](cover.png) · [逐段时间线](learning-agent-stage3.json) · [交互结果页](viewer.html)

视频为1920×1080、25fps、108秒，同时展示真实Hy3学习规划操作和学习决策评测。应用片段包含作品目标、计划审阅、采用和任务工作区；评测片段展示卷积条件遗漏的原句、七维判断、三档排序及四类应用结果。

| 时间 | 内容 | 读者可以核对的重点 |
| --- | --- | --- |
| 0—8秒 | 项目场景与目标 | 面向编程自学者的计划、反馈与过程支持 |
| 8—16秒 | 输入学习目标 | 一周完成目录统计工具，每天30分钟 |
| 16—42秒 | 真实计划审阅与采用 | 展开任务与验收要求，采用后查看学习安排 |
| 42—52秒 | 评测方法 | 用户条件、完整内容、工具结果与状态共同支撑七维判断 |
| 52—92秒 | 评测证据与实验结果 | 卷积条件遗漏、自动与人工参考、三档23/24排序、重复波动与成对识别 |
| 92—108秒 | 交付与主要结果 | 116份输入，主体200次与条件专项36次评分，报告与数据入口 |

## 数据与画面来源

应用画面来自真实录制的`sources/goal.webm`和`sources/application.webm`。对应[证据压缩包](recorded-evidence.tar.gz)保存计划、任务、消息、工具操作及采用记录；源码与素材摘要见[timeline.json](timeline.json)。

评测页面从[主体结果表](../../../evaluation/artifacts/decisionbench-final-method-20260910/automatic/)和[条件验证结果](../../../evaluation/artifacts/condition-validation-20260910/automatic/)生成。它展示同一份应用输出的自动分数与固定人工参考；卷积原文和反例用于解释具体差异。主体三档严格排序23/24，重复加权分标准差均值3.70，操纵误通过1/8；正常/严重对照36/36与36/36，条件对照18/18与18/18。详细解释见[报告](../../../第三阶段项目与评测报告.md)。

浏览器录制完整保留1440×900视口，等比放入成片，下方不裁剪，字幕使用额外的顶部区域。页面导航、对话标题及正文均保留；图表与标题采用深蓝、灰白配色。评测页在375、768、1280、1440四种宽度、两个页面下检查无横向溢出。

## 重建命令

从仓库根目录执行，无模型调用。需要Python、Pillow、Matplotlib、Noto中文字体、ffmpeg以及前端Playwright依赖和Chromium。

```bash
python scripts/plot-final-method-results.py
python scripts/build-final-study-viewer.py
node scripts/record-stage3-evaluation.mjs
python scripts/build-stage3-demo.py
```

`build-stage3-demo.py`按[timeline.json](timeline.json)验证素材与实验摘要，合成后检查时长和分辨率，输出视频及同名JSON。录制脚本保存视口检查与实际片段时间；完整108秒成片的时间和摘要见[视频清单](learning-agent-stage3.json)。
