# Model RPS

使用 Python 和 CLI（命令行界面），让两个独立的大语言模型通过交流和自主出拳完成一局猜拳博弈。

当前版本 v0.2 提供 DeepSeek V4.1 Flash 和 GPT-6 Luna 的接口适配、BO 比赛和运行日志。模型负责本局策略与交流，Python 程序负责规则、状态、信息可见性和比赛判定。

详细规则见 [项目协议 v0.2](model-rps-protocol-v0.2.md)。

## 当前进度

裁判规则、Tick 并发调度、响应校验与重试、模拟玩家、模型适配器和 CLI 均已实现。第二版增加私有决策说明、五份运行日志和 BO 赛制。同一场比赛内保留模型上下文，新的比赛才清空；双方私有上下文彼此隔离。本地 53 项自动化测试已通过。

2026-10-14 已完成保留上下文的真实 BO3 验证：DeepSeek 以 2:0 获胜，两局后立即结束。第二局实际请求保留第一局的交流、模型自身响应、公开拳型、结果和比分，同时重置当前局 Tick 与出拳；对手的私有决策说明未泄漏。新比赛清空以及 BO5 第三局保留此前两局的记忆已通过模拟 HTTP 请求检查。两种服务的 15,000 token 参数与日志也已验证。

## 安装与首次运行

在项目根目录打开 PowerShell，使用现有 `.venv`（Python 3.12.6）。默认启动真实模型，首次运行前请按下方“接入真实模型”说明填写 `local_secrets.py`；当前工作区已配置密钥。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m model_rps
```

当前工作区已完成安装。`-e .` 表示以可编辑方式安装，修改源文件后无需重复安装；依赖声明变化时再执行安装命令。

默认是真实模型模式、BO1，使用 DeepSeek 官方接口与 GPT 兼容服务。每次运行自动创建一个独立的日志目录。需要无 API 请求的演示时，显式使用 `--mode demo`。

## BO 比赛

```powershell
# 模拟 BO3，先赢 2 局结束
.\.venv\Scripts\python.exe -m model_rps --mode demo --bo 3

# 真实 BO5，先赢 3 局；实际最多进行 50 局
.\.venv\Scripts\python.exe -m model_rps --mode live --bo 5

# 自定义实际总局数上限
.\.venv\Scripts\python.exe -m model_rps --mode live --bo 3 --max-games 10
```

- `--bo N` 接受任意正奇数，默认 1；目标胜局数为 `N // 2 + 1`。
- 平局不加分，继续下一局；弃权正常计分。
- `--max-games` 包含平局，默认 `10 × N`，不能小于目标胜局数。达到上限无人赢得足够胜局时记为“未决”，保留比分。
- 技术故障立即中止整场比赛，失败局不算胜负或平局。
- **同一场 BO 内保留上下文**：模型记得此前的公开交流、结算拳型、结果、比分，以及自己的响应和决策说明。每局只重置 Tick、出拳及当前局状态。
- 双方上下文独立，对手的私有决策说明和纠错信息不可见；开始新的比赛时清空双方上下文。

## 运行日志

启动后会打印本次日志目录。默认目录结构：

```text
logs/20261014_153000_唯一编号/
├─ chat.log
├─ player_a/
│  ├─ thought.log
│  └─ debug.log
└─ player_b/
   ├─ thought.log
   └─ debug.log
```

`chat.log` 是人类可读的公开过程，包含系统消息、聊天、结算揭晓、比分与比赛结果。结算前不显示实际拳型。

`thought.log` 记录模型用一至三句给出的**决策说明**，不是内部思考；说明只保留在该模型自身的对话历史中，不提供给对手。无有效说明的失败或取消请求记录明确状态。`validated` 表示响应格式有效；对手在同 Tick 失败时，该响应的动作仍不会执行。

`debug.log` 记录该玩家的输入、请求正文、HTTP 状态和响应正文、原始模型输出、接口提供的 token 用量、耗时、错误、重试与取消。两类私有日志每行一个 JSON 对象，用局号、Tick、请求次数区分。用量缺失记录为 null，模拟模式不产生 HTTP 事件。

日志使用 UTF-8，每条记录立即 flush，异常退出时保留已经产生的记录。创建或写入失败会停止比赛。不保存鉴权头，并过滤本次运行的两个 API key。默认 `logs/` 目录不纳入 Git；自定义日志目录时需自行设置对应忽略规则。

使用 `--log-dir 路径` 更改日志根目录，每次运行仍创建新子目录。

## 模拟场景

```powershell
.\.venv\Scripts\python.exe -m model_rps --mode demo --scenario deadline
```

| `--scenario` | 展示内容 |
| --- | --- |
| `change` | 默认场景：出拳后修改，按最终拳型判定 |
| `draw` | 双方一直等待，每局 20 Tick 后平局；反复开局，直至比赛达到实际总局数上限 |
| `forfeit` | 仅 A 出拳，B 到期弃权 |
| `deadline` | 第 20 Tick 双方出拳，先处理动作再判定 |
| `retry` | WAIT 混用被拒绝，在同一 Tick 纠正后完成对局 |

模拟场景在每局重新开始，会重复相同结果。只想观察一局平局时，可运行 `--mode demo --scenario draw --max-games 1`，最终比赛状态为未决。`--scenario` 只用于模拟模式，不会自动切换运行模式。

## 接入真实模型

在项目根目录的 `local_secrets.py` 中填写两个字符串常量：

```python
DEEPSEEK_API_KEY = "你的 DeepSeek API key"
OPENAI_API_KEY = "你的 OpenAI API key"
```

该文件由 `.gitignore` 排除；重新克隆仓库时需要自行创建。程序只读取这两个字符串常量，不执行密钥文件里的其他 Python 语句。

```powershell
.\.venv\Scripts\python.exe -m model_rps --mode live
```

`live` 是默认模式，会向两个服务发起实际 API 请求，因此上面的 `--mode live` 可以省略。密钥缺失、为空或格式不正确时，程序会在发起请求前退出。

| 玩家 | 默认 API 基础地址 | 默认模型标识 |
| --- | --- | --- |
| A | `https://api.deepseek.com` | `deepseek-flash` |
| B | `https://ai.yuhuantin.icu/v1` | `gpt-6-luna` |

DeepSeek 使用官方服务，GPT 使用用户指定的兼容服务；每个玩家分别使用对应服务的密钥。DeepSeek 官方使用 `deepseek-flash` 标识 V4.1 Flash，见 [官方接入文档](https://api-docs.deepseek.com/)。2026-10-14 已完成以上组合的真实验证。

此前第三方 DeepSeek 在真实联调中出现超时和缺少 JSON 结尾的响应，因此改用官方服务。原服务使用的 DeepSeek 标识为 `deepseek-v4.1-flash`；切换地址时必须同时匹配服务的模型标识和密钥，不能仅替换地址。当前本地密钥文件保留原密钥为 `DEEPSEEK_GATEWAY_API_KEY`，程序不读取该备份字段。

程序自动在基础地址后追加 `/chat/completions`。可以通过 `--deepseek-base-url`、`--openai-base-url`、`--deepseek-model`、`--openai-model` 覆盖地址或模型标识。覆盖模型名称仍使用该玩家的参数方案；适配不兼容的模型时，需要在 `players.py` 中调整 `ModelSpec` 或新增实现 `Player.respond` 的适配器。

本版 DeepSeek 使用非思考模式，GPT-6 Luna 使用 `reasoning_effort="none"`。两者都请求 JSON 输出，单次请求输出上限统一为 **15,000 token**，决策说明和动作共用额度。响应必须包含非空的 `decision_summary` 字符串和非空的 `actions` 列表；缺少决策说明也会纠错重试。思考模式留待后续开发。调用方式依据 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 和 [OpenAI Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

## 超时、重试与退出状态

- 每次请求总时限默认 60 秒，可用 `--timeout 120` 调整。
- 每位玩家每个 Tick 最多请求 3 次，网络错误和格式纠错共用额度。
- 真实模式每次重试间隔 1 秒，模拟模式不增加等待时间。
- HTTP 408、409、429 和 5xx 可重试；其他 HTTP 错误（例如 401）直接中止，提示状态码。
- 重试时只请求出错的一方，仍使用同一份 Tick 开始状态。
- CLI 错误概况不会打印密钥、服务器原文或结算前的拳型；私有 debug 日志保留脱敏后的响应正文。

退出码：`0` 表示产生比赛冠军，`1` 表示技术异常或日志故障中止，`2` 表示参数或本地配置错误，`3` 表示达到总局数上限仍未决，`130` 表示用户按 Ctrl+C 中止。

更多参数：

```powershell
.\.venv\Scripts\python.exe -m model_rps --help
```

## 项目结构

```text
model-rps-protocol/
├─ README.md                    项目介绍、环境与进度说明
├─ model-rps-protocol-v0.2.md    游戏规则、日志与比赛协议
├─ pyproject.toml               Python 项目信息与依赖声明
├─ .gitignore                   Git 忽略规则
├─ local_secrets.py             本地 API key，不纳入 Git
├─ .venv/                       已有的本地 Python 环境，不纳入 Git
├─ logs/                        每次运行的日志，不纳入 Git
├─ src/
│  └─ model_rps/
│     ├─ __init__.py            Python 包标记与说明
│     ├─ __main__.py            python -m model_rps 入口
│     ├─ game.py                状态、动作执行与结果判定
│     ├─ runner.py              Tick 推进、并发请求与重试协调
│     ├─ match.py               BO 比赛、比分和比赛内上下文管理
│     ├─ diagnostics.py         并发请求诊断与标识
│     ├─ logs.py                文件记录、脱敏和日志生命周期
│     ├─ players.py             模型接入与响应处理
│     └─ cli.py                 命令行入口与事件展示
└─ tests/
   ├─ test_game.py              裁判、动作校验、可见性和到期判定
   ├─ test_runner.py            并发、重试、超时与取消
   ├─ test_players.py           模拟 HTTP 请求与模型响应处理
   ├─ test_cli.py               命令行场景、密钥读取和展示
   ├─ test_match.py             BO 判定、跨局记忆和新比赛重置
   └─ test_logs.py              日志内容、脱敏、取消与写入故障
```

## 运行测试

测试使用 Python 标准库 `unittest`；模型请求通过 HTTPX 的模拟传输验证，不需要真实密钥。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

核心覆盖：九种拳型组合、跨 Tick 改拳、同一响应多个动作、结算前保密、10/18/19 Tick 提醒、到期弃权或平局、最后 Tick 行动、整份响应拒绝、同 Tick 信息隔离、有限重试、HTTP 错误、请求取消、BO 比赛、同场比赛记忆保留、新比赛清空、日志脱敏与故障处理。
