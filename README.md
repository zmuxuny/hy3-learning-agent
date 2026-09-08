# Learning Agent · Hy3

### 一个持续理解学习状态、主动判断并推动目标完成的个人学习 Agent

[在线 Demo](https://zmuxuny.github.io/hy3-learning-agent/) · [第三阶段项目方案](docs/腾讯犀牛鸟开源实习第三阶段项目方案.md) · [产品定义](docs/PRODUCT.md) · [系统架构](docs/ARCHITECTURE.md)

Learning Agent · Hy3 面向编程与技术学习，把 AI 从一次性问答扩展为贯穿目标、计划、执行、验收与调整的持续学习伙伴。

用户可以从一句并不完整的目标开始，例如：“我有 C++ 基础，希望两个月内学会 CUDA，并完成一个矩阵乘优化项目。”Hy3 会澄清真正影响路径的条件、调研并核验资源、提出可审阅的计划；当学习开始后，它继续跟踪任务与证据、验收成果，并根据新的学习事实选择保持安静、教学、抽查、提醒或调整计划。

> **让 AI 从一次性回答走向持续行动：理解目标、跟踪进度、验证掌握，并在恰当的时机主动介入。**

![Learning Agent · Hy3 整体架构](assets/proposal/architecture/learning-agent-system-architecture.svg)

## 核心体验

| 能力 | Hy3 如何工作 | 用户获得什么 |
| --- | --- | --- |
| 目标澄清与规划 | 围绕基础、期限、时间预算和期望作品提出高信息量问题，生成可讨论、可修改、可采用的计划提案 | 从模糊愿望走向一条真正可执行的学习路径 |
| 资源调研与教学 | 搜索、打开并核验课程与资料，结合当前任务提供讲解和练习 | 资源与目标、难度和阶段相匹配 |
| 学习成果验收 | 依据任务 Rubric 检查回答、代码、文件和项目作品，记录结论、证据与反馈 | “完成任务”建立在可查看的学习证据上 |
| 主动介入 | 综合截止时间、近期活动、复习安排、历史提醒和用户偏好决定 `WAIT` 或行动 | 需要帮助时得到具体支持，不需要时保持安静 |
| 计划调整 | 在进度、能力或现实条件变化后提出范围合适的修改，并保留用户确认边界 | 计划随真实学习过程持续演进 |
| 连续上下文 | 分层维护对话、计划、任务、事件、证据、记忆和运行轨迹 | 每一次决策都基于可追溯的最新状态 |

## 主动学习闭环

Learning Agent 不把计划视为静态日程，也不把一次对话当作任务终点。系统围绕六个阶段持续运行：

```text
目标与约束 → 规划与资源 → 学习与提交 → 成果验收
     ↑                                  ↓
主动判断 ← 提醒 / 抽查 / 调整 ← 学习状态与证据
```

用户消息、后台心跳、任务事件与复习到期进入同一个 Agent Runtime。Hy3 读取与当前目标相关的上下文，选择工具、观察结果并继续决策；确定性的权限、频率、冷却、免打扰和审批规则负责守住行动边界。

这里的“主动”不以通知数量衡量。`WAIT` 是与提醒、抽查和调整同等重要的决策：系统追求的是一次有依据、及时、可执行的介入。

## 系统设计

整体系统由五部分组成：

- **交互工作台**：连续对话、计划工作区、学习任务与收件箱共同呈现目标、过程和下一步。
- **Hy3 Agent Runtime**：统一处理用户请求与后台主动任务，通过多轮工具调用完成真实操作。
- **学习能力层**：覆盖规划、资源调研、教学、成果验收、复习、提醒与计划调整。
- **学习事实层**：持久化画像、计划、任务、证据、事件、记忆和可审计操作，为后续决策提供连续状态。
- **离线评测平面**：从关键决策导出可回放样本，在隔离环境中完成评分、有效性验证和结果归因，不进入用户运行时。

架构图提供 [draw.io 可编辑源文件](assets/proposal/architecture/learning-agent-system-architecture.drawio) 与 [PNG 版本](assets/proposal/architecture/learning-agent-system-architecture.png)。

## 第三阶段：评测 Agent 的关键决策

本项目参加 2026 腾讯犀牛鸟开源人才培养计划混元大语言模型实战题目一。第三阶段围绕开放式输出建立统一的关键决策评测框架，覆盖完整的主动学习价值链：

- `P · Planning`：规划是否满足目标、约束、顺序、时间预算与成果要求；
- `I · Intervention`：此刻应该保持安静还是介入，时机、强度与内容是否合适；
- `A · Assessment`：成果验收结论是否正确，证据是否充分，反馈是否有效；
- `R · Revision`：计划是否需要改变，修改是否解决问题且不过度。

评测单元采用可回放的 `Decision Episode`，保存决策所需的状态、触发、可见上下文、工具轨迹、结构化结果与状态差异。规则检查负责硬约束，Hy3 Judge 评价开放语义。方法验证将先检查判别力与重复一致性，独立人工盲标和反事实等实验另行实施。

产品内的“学习成果验收”判断用户是否学会；第三阶段评测判断 Hy3 的规划、介入、验收和调整是否做得正确。两者共享可观察事实，但职责清晰分离。整体构思见[第三阶段项目方案](docs/腾讯犀牛鸟开源实习第三阶段项目方案.md)，工程协议见[第三阶段评测实施方案](docs/腾讯犀牛鸟开源实习第三阶段评测实施方案.md)。

第三阶段已实现隔离Runtime、轨迹、Rules、盲化Judge、聚合与版本治理，E4及S06/E5已完成。E5实验固定`40fddb8`、88有效；首批E6固定`b0b0d02`的48 Episode/46有效Judge/2失败保留。随后实际修复通知、审批归因、行动声明、时区、证据不足决策及Judge，完成另版方法验证，并在`1f1f35f`冻结登记新的G01–G12四轨48输入后运行：33有效Judge、15失败，formal=false。输入超限另记Protocol1.5候选，45项仓外回归及79请求等价验证通过，未真实复测；动作格式和规划内容仍有反例。当前共享账本剩余0.216953元，已停止付费。完整结果、版本、失败与费用见[新版档案](evaluation/artifacts/e6-repair-20260908/README.md)和[工作记录](docs/E6新版修复与验证记录.md)。E7/E8仍待完成；方案中的人工审核由主AI承担，按实际AI身份记录。


## Demo

[![观看 Learning Agent · Hy3 92.6 秒完整 Demo](assets/demo/learning-agent-hy3-demo-cover.jpg)](https://zmuxuny.github.io/hy3-learning-agent/)

[▶ 在线播放完整 Demo（92.6 秒 · 1080p）](https://zmuxuny.github.io/hy3-learning-agent/)

Demo 展示两条真实端到端流程：

1. 模糊目标 → 结构化澄清 → 规划调研 → 可审阅提案 → 用户采用 → 计划 Session；
2. 读取真实进度 → 当前任务教学 → 文件与代码检查 → 证据验收 → 进度更新 → 心跳主动提醒。

模型决策来自 TokenHub Hy3 API，工具调用、计划进度、验收结果和站内通知均来自真实运行状态；视频剪辑移除了模型与网络等待时间。

## 快速开始

源码运行需要 Python 3.11+ 与 Node.js 20+。

```bash
git clone https://github.com/zmuxuny/hy3-learning-agent.git
cd hy3-learning-agent
./scripts/setup.sh
cp .env.example .env
./scripts/start.sh
```

打开 <http://127.0.0.1:8000>，首次设置向导会验证模型连接并创建第一条 Session。也可以提前在 `.env` 中配置：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_API_BASE=https://tokenhub.tencentmaas.com/v1
MODEL_NAME=hy3
```

开发前端时运行：

```bash
cd frontend
npm run dev
```

## 验证

```bash
source .venv/bin/activate
pytest -q
python -m learning_agent_eval validate-dataset --dataset evaluation/datasets/decisionbench-v1
npm --prefix frontend test
npm --prefix frontend run build
python scripts/check_doc_links.py .
python scripts/release-gate.py --repository . --format json
```

测试覆盖持久化迁移、事务与副作用、Runtime 恢复、学习证据、上下文与记忆、主动介入、安全边界、前端状态对账、响应式浏览器路径和首次设置。详细结果与复现入口见[当前状态](docs/STATUS.md)。

## 数据与安全

- 默认 `local` 模式只监听本机回环地址；个人服务器部署需要认证令牌、精确 HTTPS Origin 与一致的 CORS 配置，详见[安全与部署边界](SECURITY.md)。
- SQLite、上下文快照、工作区文件和备份位于本地 `data/`，不会进入 Git。
- 长期记忆保留来源与生命周期，用户可以确认、纠正、归档和恢复。
- 外部内容始终作为不可信输入处理；没有可信沙箱 Provider 时，代码执行能力不会开放。
- 低风险数据库操作保留审计与撤销信息，外部发送通过耐久 Outbox 记录意图和结果。

## 项目文档

- [第三阶段项目方案](docs/腾讯犀牛鸟开源实习第三阶段项目方案.md)
- [第三阶段评测实施方案](docs/腾讯犀牛鸟开源实习第三阶段评测实施方案.md)
- [离线评测包与 E0 运行说明](evaluation/README.md)
- [第三阶段开发交接](docs/第三阶段开发交接.md)
- [产品定义](docs/PRODUCT.md)
- [系统架构与上下文](docs/ARCHITECTURE.md)
- [Harness 完整性标准](docs/HARNESS.md)
- [工具与权限协议](docs/TOOL_PROTOCOL.md)
- [安全与部署边界](SECURITY.md)
- [路线图](docs/ROADMAP.md)
- [当前状态](docs/STATUS.md)

## License

[MIT](LICENSE)
