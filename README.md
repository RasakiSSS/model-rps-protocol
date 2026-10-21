# Model RPS

[![Release](https://img.shields.io/badge/release-v1.0.1-blue)](https://github.com/RasakiSSS/model-rps-protocol/releases/tag/v1.0.1)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)](pyproject.toml)
[![Interface](https://img.shields.io/badge/interface-CLI-4B8BBE)](docs/cli.md#终端操作)
[![Model adapters](https://img.shields.io/badge/model_adapters-6-6A5ACD)](docs/model-adapters.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

让人类与人类、人类与大语言模型，或两个独立的模型，在命令行中聊天、等待和出拳。Python 裁判负责规则、信息隔离和胜负判定；比赛结束后，双方沿用各自上下文继续聊天。

支持任意正奇数 BO、Red / Blue 席位、同模型对战、人类玩家、独立聊天、同时或轮流行动、局中模型接管，以及比赛之间换人。已接入 OpenAI、DeepSeek、GLM、Gemini、Grok 和 Claude 的参数方案。项目用于观察博弈行为，不预设模型必须聊天，也不把单场胜负当作能力排名。

当前 `codex/mcp-agent` 分支在 `main` 基础上增加共享房间、人类双终端和可选的 MCP 外部 Agent 入口；下面的 v1.0.1 下载仍是已发布的稳定版本。

**[下载 v1.0.1](https://github.com/RasakiSSS/model-rps-protocol/releases/tag/v1.0.1)** · [版本说明](CHANGELOG.md)

## 文档

| 要找的内容 | 位置 |
| --- | --- |
| 启动参数、终端指令、大厅、日志与排错 | [命令行与终端操作](docs/cli.md) |
| 配置字段、默认值、优先级与填写示例 | [config.example.toml](config.example.toml) 的注释 |
| 裁判规则、信息边界、指令与 BO 生命周期 | [游戏规则](docs/rules.md) |
| 响应校验、重试、模型输入输出、赛后聊天 | [模型交互](docs/model-io.md) |
| 日志文件、字段、脱敏与终端展示 | [日志与展示](docs/logs-and-display.md) |
| 接口适配、官方资料与实测限制 | [模型适配](docs/model-adapters.md) |
| 两个人类或 Agent 通过本机房间对战 | [共享房间](docs/shared-room.md) |
| Codex / DSH 通过 MCP 参赛 | [MCP 接入](docs/mcp.md) |
| 代码分层、目录与依赖规则 | [架构](docs/architecture.md) |
| 下一版计划与尚待确定的问题 | [路线图](docs/roadmap.md) |
| 版本记录 | [CHANGELOG.md](CHANGELOG.md) |
| 项目许可证与第三方依赖声明 | [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |

## 安装与首次使用

需要 Python 3.12 或更高版本。可在 Release 页下载并解压 **Source code (zip)**，或按下面的命令克隆仓库。进入项目根目录后创建环境并安装依赖；已有虚拟环境时跳过创建步骤。发布包为源码，仍需安装 Python 和依赖。

Windows PowerShell：

```powershell
git clone https://github.com/RasakiSSS/model-rps-protocol.git
cd model-rps-protocol
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

macOS / Linux：

```bash
git clone https://github.com/RasakiSSS/model-rps-protocol.git
cd model-rps-protocol
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

下载 ZIP 的用户从创建 `.venv` 的步骤开始。Python 启动器中的版本号按实际安装版本调整；以下示例直接调用虚拟环境，无需激活。

外部 Agent 通过 MCP 参赛时安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

MCP 和 CLI 共用原有裁判。仅运行 CLI 时无需安装 MCP；Agent 接入示例见 [MCP 接入](docs/mcp.md)。

**首次运行不会自动创建 config.toml。** 使用真实模型前，复制 [配置模板](config.example.toml)，然后按其中注释填写。已有配置时直接编辑，不要覆盖：

```powershell
Copy-Item config.example.toml config.toml
```

macOS / Linux 使用 `cp config.example.toml config.toml`。离线演示不需要配置或密钥。完成安装后，按下一节启动。

## 快速开始

Windows 推荐使用根目录的 [run.ps1](run.ps1)，它调用本项目 `.venv` 中的 Python，转发全部参数并保留退出码。macOS / Linux 将 `.\.venv\Scripts\python.exe` 换成 `./.venv/bin/python`。

```powershell
# 无需密钥的离线 BO3（先试这个）
.\run.ps1 --no-config --mode demo --bo 3

# 使用 config.toml：人类（Red）对战 gpt profile
.\run.ps1 --red-profile human --blue-profile gpt --bo 3

# 连续比赛：赛后双方退出后进入下一场大厅
.\run.ps1 --session

# 两个人类或 Agent 在本机对战：启动房间后按提示在另两个终端加入
.\.venv\Scripts\python.exe -X utf8 -m model_rps.room.server --bo 3

# 查看全部参数；无需有效配置或密钥
.\run.ps1 --help
```

真实模式会发起 API 请求。人类参与和连续比赛需要交互终端，不要重定向输入或输出。没有 `--session` 时，一场 BO 及赛后聊天结束后退出。完整参数、人类指令和排错见 [命令行与终端操作](docs/cli.md)。

## 命令行入口

| 入口 | 用途 |
| --- | --- |
| `model-rps`、`python -m model_rps` | 本地对局：人类、模型、模拟玩家任意组合 |
| `model-rps-room` | 共享房间服务，见 [共享房间](docs/shared-room.md) |
| `model-rps-join` | 人类终端加入共享房间 |
| `model-rps-mcp` | MCP stdio 服务，由宿主启动，见 [MCP 接入](docs/mcp.md) |

## 比赛规则概览

- BO N 先赢 N//2+1 局即结束；平局继续，实际局数达到上限而无人达标时为“未决”。
- 同时行动：双方都结束本 Tick 行动后执行出拳并公开提交状态。聊天即时公开，生成中的模型请求保持原快照。
- 轮流行动：只有当前方可行动，另一方仍可聊天；每局交换先手。
- 锁定模式：首次有效出拳锁定，之后的 MOVE 被忽略；双方提交齐全后，在当前完整行动边界结算。
- 改拳模式：允许跨 Tick 修改，双方提交齐全后的下一 Tick 是最后一次修改机会；仅支持同时行动。
- 每方常规最多 20 次行动。到期只有一方未出拳则弃权，双方未出拳则平局；改拳模式可能追加结算 Tick。
- 聊天不消耗行动次数。开局、权限变化和对手发言会提供交流机会，模型可以沉默；暂不设置聊天额度或自动行动期限。
- 结算前对手看不到真实拳型；整场 BO 结束前看不到对手策略分析。人类和模型共享信息权限。
- 同一 BO 保留完整记忆；新 BO 清空。当前没有自动摘要、截断或上下文窗口容量管理。

完整规则见 [游戏规则](docs/rules.md)，模型收发的 JSON 见 [模型交互](docs/model-io.md)。

## 开发与测试

源码在 `src/model_rps/`，按职责分为 `core`、`llm`、`players`、`engine`、`ui`、`room`、`mcp_agent` 七层，依赖只能向下，详见 [架构](docs/architecture.md)。提示词集中在 [core/prompts.py](src/model_rps/core/prompts.py)，裁判逻辑在 [core/game.py](src/model_rps/core/game.py)；修改提示词不会改变裁判规则。

目录约定：`tests/` 为自动测试；`validation/` 是会请求真实服务的验证脚本，`experiments/` 是一次性实验脚本，两者的输出写入已被 Git 忽略的 `artifacts/`；`logs/` 只存放每场比赛的日志。

以可编辑方式安装后修改源文件无需重装，依赖变化时重新执行安装命令。开发工具用 `dev` 可选依赖安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,mcp]"
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check src tests validation experiments
```

也可以用 `python -m unittest discover -s tests`。测试使用模拟模型响应和异步输入，不调用真实模型；共享房间测试会使用本机临时端口及模拟键盘，完整运行约需两到三分钟。安装 MCP 可选依赖后还会运行真实 stdio 子进程和人类 / Agent 混合对局，未安装时跳过 SDK 相关测试。`tests/test_architecture.py` 检查分层与模块路径引用。真实服务的测试日期与边界单独记录在 [模型适配](docs/model-adapters.md) 中。

## 许可证

本项目自身的代码和文档采用 [MIT License](LICENSE)，Copyright (c) 2026 RasakiSSS。

第三方依赖保留各自的版权及许可证。依赖清单、许可范围和原始声明副本见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 [LICENSES](LICENSES/)；本项目的 MIT 授权不替代这些第三方条款。
