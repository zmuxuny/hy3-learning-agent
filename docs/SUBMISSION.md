# 参赛提交清单

目标 Issue：<https://github.com/Tencent-Hunyuan/Hy3/issues/4>

现有提交关系已经确认：

```text
完整应用源码：zmuxuny/hy3-learning-agent (main)
提交说明文件：zmuxuny/Hy3 (rhinobird2026)
现有 PR #220：zmuxuny/Hy3:rhinobird2026
             → Tencent-Hunyuan/Hy3:rhinobird2026
```

不得把独立应用仓库的 `main` 当成 Hy3 上游 PR 的 head，也不得把 PR 目标改成上游默认分支。

## 官方硬性要求

- [x] 全部模型能力通过 TokenHub Hy3 API 使用，不训练、不微调、不做本地推理部署
- [x] 有可运行的交互 Web 前端
- [x] README 明确说明 Hy3 在系统中承担规划、工具选择、上下文使用和主动决策角色
- [ ] 录制并跑通 Demo 1：模糊目标 → 澄清 → 规划子 Run → 资源核验 → 提案采用 → 正式计划
- [ ] 录制并跑通 Demo 2：进度感知教学 → 文件/代码证据 → 验收 → 进度/复习 → 主动检查
- [ ] 将两条流程剪为同一段 **≤ 2 分钟** 视频或 GIF，并提供可公开访问的链接
- [x] 项目使用 MIT License 开源
- [ ] 对公开仓库执行最终密钥、数据库、日志、快照和个人数据扫描

## GitHub 交付状态

- [x] 已在 Issue 认领任务
- [x] 已创建独立仓库 <https://github.com/zmuxuny/hy3-learning-agent>
- [x] 已创建上游 PR <https://github.com/Tencent-Hunyuan/Hy3/pull/220>
- [x] PR head/base 是 `zmuxuny:rhinobird2026 → Tencent-Hunyuan:rhinobird2026`
- [x] 把本地完整应用源码推送到独立仓库 `main`
- [x] 更新 fork 的 `submissions/hy3-learning-agent.md`，包含准确能力、两条 Demo 和运行方法
- [x] 推送更新到 `zmuxuny/Hy3:rhinobird2026`，现有 PR #220 已自动增加提交，没有新开错误 PR
- [x] 通过 GitHub API 核对 PR 只改动 `submissions/hy3-learning-agent.md` 一个文件
- [ ] 视频完成后把公开链接补入 README、submission 文件与 PR 描述

## AI Coding 协作说明

Issue 期望参与者使用 CodeBuddy/WorkBuddy，并鼓励 README 记录协作代码范围。只能记录实际发生过的协作：

- [ ] 用 CodeBuddy 或 WorkBuddy 完成一次真实、可复述的代码评审或小范围修改
- [ ] 保存协作截图或记录
- [ ] 在 README 和 submission 文件中写明准确文件/职责，不把 Codex 工作冒充为 CodeBuddy/WorkBuddy

推荐范围：检查 `backend/app/runtime/agent.py` 的 TokenHub Hy3 多轮工具调用适配，验证 `reasoning_content` 回填、tool message 格式和失败事件记录。

## 录制前命令

```bash
# 先停止正在运行的服务
./scripts/demo-data.sh reset
./scripts/start.sh

# 另一个终端
./scripts/demo-preflight.sh
```

完整 115 秒分镜见 [DEMO.md](DEMO.md)。视频上传后，必须同时更新独立仓库 README、fork submission 文件和 PR #220 描述/评论中的视频链接。
