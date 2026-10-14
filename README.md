# Model RPS

使用 Python 和 CLI（命令行界面），让两个独立的大语言模型通过交流和自主出拳完成一局猜拳博弈。

当前版本 v0.2 提供 DeepSeek V4.1 Flash 和 GPT-6 Luna 的接口适配、BO 比赛、运行日志和赛后复盘。模型负责策略、交流与复盘，Python 程序负责规则、状态、信息可见性和比赛判定。

详细规则见 [项目协议 v0.2](model-rps-protocol-v0.2.md)。

## 当前进度

裁判规则、Tick 并发调度、响应校验与重试、模拟玩家、模型适配器和 CLI 均已实现。支持五份运行日志、BO 赛制、同场上下文保留和赛后公开复盘。默认首次提交后锁定出拳；原来的允许改拳模式仍可选择。本地 76 项自动化测试已通过。

两种服务均已验证接受 20,000 token 输出上限。模型决策说明与复盘可能包含事实或判断错误，实际动作与结果以裁判记录为准。思考模式仍待后续开发。

2026-10-15 已完成锁定模式真实验证：双方第 1 Tick 提交，GPT 的布胜 DeepSeek 的石头，当 Tick 结算，无额外结算请求；随后双方复盘成功。该场仍快速提交，不能据此推断锁定模式会增加交流或等待。

## 安装与首次运行

在项目根目录打开 PowerShell，使用现有 `.venv`（Python 3.12.6）。默认启动真实模型，首次运行前请按下方“接入真实模型”说明填写 `local_secrets.py`；当前工作区已配置密钥。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m model_rps
```

当前工作区已完成安装。`-e .` 表示以可编辑方式安装，修改源文件后无需重复安装；依赖声明变化时再执行安装命令。

默认是真实模型模式、BO1、锁定出拳规则，使用 DeepSeek 官方接口与 GPT 兼容服务。每次运行自动创建一个独立的日志目录。需要无 API 请求的演示时，显式使用 `--mode demo`。

## BO 比赛

通过 `--move-policy` 选择整场比赛的规则；一场 BO 中所有局采用同一模式。

| 规则 | `locked`（默认） | `revisable`（对照模式） |
| --- | --- | --- |
| 出拳 | 首次有效 MOVE 锁定 | 最后一次有效 MOVE 生效 |
| 重复 MOVE | 拒绝，包括同一拳型；整份响应纠错重试 | 允许跨 Tick 或同一响应多次 MOVE |
| 已提交后的行动 | 可以 SAY 或单独 WAIT | 可以 SAY、WAIT 或 MOVE |
| 双方提交齐全 | 当前 Tick 全部动作处理后直接结算 | 下一 Tick 为最后行动机会，之后结算 |
| 第 20 Tick 才提交齐全 | 当 Tick 结算，无第 21 Tick | 追加第 21 Tick 结算 |

两种模式都在第 20 Tick 判定尚未出拳者弃权、双方均未出拳则平局；已结束的对局不再请求动作。锁定模式中，一份响应最多一个 MOVE，多个 MOVE 会使整份响应无效，不会先执行第一个。违规重试耗尽按技术异常处理，不记弃权。

```powershell
# 模拟 BO3，先赢 2 局结束
.\.venv\Scripts\python.exe -m model_rps --mode demo --bo 3

# 真实 BO5，先赢 3 局；实际最多进行 50 局
.\.venv\Scripts\python.exe -m model_rps --mode live --bo 5

# 保留原来的改拳规则作对照
.\.venv\Scripts\python.exe -m model_rps --bo 3 --move-policy revisable

# 自定义实际总局数上限
.\.venv\Scripts\python.exe -m model_rps --mode live --bo 3 --max-games 10
```

- `--bo N` 接受任意正奇数，默认 1；目标胜局数为 `N // 2 + 1`。
- 平局不加分，继续下一局；弃权正常计分。
- `--max-games` 包含平局，默认 `10 × N`，不能小于目标胜局数。达到上限无人赢得足够胜局时记为“未决”，保留比分。
- 技术故障立即中止整场比赛，失败局不算胜负或平局。
- **同一场 BO 内保留上下文**：模型记得此前的公开交流、结算拳型、结果、比分，以及自己的响应和决策说明。每局只重置 Tick、出拳及当前局状态。
- 比赛期间双方上下文独立，对手的决策说明和纠错信息不可见；开始新的比赛时清空双方上下文。

## 赛后公开与复盘

整场 BO 产生冠军或达到实际总局数上限后，程序自动进行以下步骤：

1. 展示最终比分和比赛结果。
2. 按局号、Tick、玩家顺序公开双方实际执行的动作及其决策说明，改拳模式下包括每次有效改拳。
3. 向两位模型提供相同的完整赛后记录，各自从自己的视角分析双方策略，给出**整体评价、关键决策和改进建议**。
4. 在 CLI 和 `chat.log` 展示双方复盘。

公开的是模型在对局时生成的 `decision_summary`，不包含内部思考。整个 BO 结束前不会向对手公开这些说明；无效响应、纠错信息、原始 HTTP 数据和未执行的动作不进入赛后公开记录。

复盘使用独立提示和完整比赛记录，双方互相看不到尚未完成的复盘。复盘不执行游戏动作，也不改变已确定的比分。真实模式通常额外请求两次，每位模型失败时最多请求三次，沿用超时设置和每次 20,000 token 输出上限。某一方复盘失败仍保留另一方成功的内容，并返回退出码 `4`。

技术异常或用户中止的比赛不自动进入复盘。模拟模式展示明确标注的预设复盘，不发起 API 请求。

## 提示词与自由选择

双方使用相同的规则提示，明确拳型克制关系、系统比分、动作效果和信息可见性。SAY 可以单独使用；一方提交不会独自触发结算，仍未提交的一方可以继续发言或等待，直到提交截止。提示中只注入本场所选模式的出拳和结算规则。

提示不要求模型尽快出拳，也不要求为了展示功能而聊天、等待或改拳。格式示例覆盖单独 SAY、WAIT 和三种 MOVE，不指定默认拳型。决策说明要求区分事实与猜测、核对比分和规则；复盘要求指出自己的事实错误并引用局号与 Tick。提示词不能保证模型完全正确，也不能保证出现更多交流；具体行为需从多场记录中观察。

单次输出上限现为 20,000 token，对局和复盘共用此设置。它是允许的最大输出量，不要求模型写满，也不代表开启思考模式。

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

`chat.log` 是人类可读的公开过程，包含系统消息、聊天、结算揭晓、比分、比赛结果，以及赛后公开的动作、决策说明和双方复盘。结算前不显示实际拳型，整场比赛结束前不公开决策说明。

`thought.log` 记录模型用一至三句给出的**决策说明**，不是内部思考；比赛期间只保留在该模型自身的对话历史中，整场比赛结束后才向双方公开实际执行动作的说明。无有效说明的失败或取消请求记录明确状态。`validated` 表示响应格式和本 Tick 的动作规则均有效；对手在同 Tick 失败时，该响应的动作仍不会执行。成功的赛后复盘也写入该玩家的 `thought.log`。

`debug.log` 记录该玩家的输入、请求正文、HTTP 状态和响应正文、原始模型输出、接口提供的 token 用量、耗时、错误、重试与取消，包含赛后复盘请求。比赛开始记录 `move_policy`，游戏快照包含模式及自己的锁定状态，复盘记录也包含比赛模式。两类结构化日志每行一个 JSON 对象，用局号、Tick、请求次数区分；请求记录的 `phase` 为 `game` 或 `review`。复盘请求的局号和 Tick 均为 `0`，不增加比赛局数或 Tick。用量缺失记录为 null，模拟模式不产生 HTTP 事件。

日志使用 UTF-8，每条记录立即 flush，异常退出时保留已经产生的记录。创建或写入失败会停止比赛。不保存鉴权头，并过滤本次运行的两个 API key。默认 `logs/` 目录不纳入 Git；自定义日志目录时需自行设置对应忽略规则。

使用 `--log-dir 路径` 更改日志根目录，每次运行仍创建新子目录。

## 模拟场景

```powershell
.\.venv\Scripts\python.exe -m model_rps --mode demo --scenario deadline
```

| `--scenario` | 展示内容 |
| --- | --- |
| `locked` | 锁定模式默认场景：A 先提交，下一 Tick 继续发言，B 提交后当刻结算 |
| `change` | 改拳模式默认场景：出拳后修改，按最终拳型判定；需 `--move-policy revisable` |
| `settlement` | 第 1 Tick 双方出石头；第 2 Tick 结算刻 A 改为布，A 获胜；需 `--move-policy revisable` |
| `draw` | 双方一直等待，每局 20 Tick 后平局；反复开局，直至比赛达到实际总局数上限 |
| `forfeit` | 仅 A 出拳，B 到期弃权 |
| `deadline` | 第 20 Tick 双方出拳：锁定模式当刻结算，改拳模式第 21 Tick 结算 |
| `retry` | WAIT 混用被拒绝，在同一 Tick 纠正后完成对局 |

未指定 `--scenario` 时随模式选择默认场景；锁定模式指定 `change` 或 `settlement` 会在运行前报配置错误。模拟场景在每局重新开始，会重复相同结果。只想观察一局平局时，可运行 `--mode demo --scenario draw --max-games 1`，最终比赛状态为未决。`--scenario` 只用于模拟模式，不会自动切换运行模式。

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

程序自动在基础地址后追加 `/chat/completions`。可以通过 `--deepseek-base-url`、`--openai-base-url`、`--deepseek-model`、`--openai-model` 覆盖地址或模型标识。覆盖模型名称仍使用该玩家的参数方案；适配不兼容的模型时，需要在 `players.py` 中调整 `ModelSpec` 或新增实现 `MatchPlayer` 的适配器，包含对局响应、比赛上下文管理与赛后复盘。

本版 DeepSeek 使用非思考模式，GPT-6 Luna 使用 `reasoning_effort="none"`。两者都请求 JSON 输出，单次请求输出上限统一为 **20,000 token**，决策说明和动作共用额度。响应必须包含非空的 `decision_summary` 字符串和非空的 `actions` 列表；缺少决策说明也会纠错重试。思考模式留待后续开发。调用方式依据 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 和 [OpenAI Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

## 超时、重试与退出状态

- 每次请求总时限默认 60 秒，可用 `--timeout 120` 调整。
- 每位玩家每个 Tick 最多请求 3 次，网络错误和格式纠错共用额度。
- 真实模式每次重试间隔 1 秒，模拟模式不增加等待时间。
- HTTP 408、409、429 和 5xx 可重试；其他 HTTP 错误（例如 401）直接中止，提示状态码。
- 重试时只请求出错的一方，仍使用同一份 Tick 开始状态。
- CLI 错误概况不会打印密钥、服务器原文或结算前的拳型；私有 debug 日志保留脱敏后的响应正文。

退出码：`0` 表示产生比赛冠军且复盘完成，`1` 表示技术异常或日志故障中止，`2` 表示参数或本地配置错误，`3` 表示达到总局数上限仍未决且复盘完成，`4` 表示比赛已结束但至少一方复盘失败，`130` 表示用户按 Ctrl+C 中止。复盘失败不改变比赛的冠军或未决状态。

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
│     ├─ postmatch.py           赛后记录、公开说明与双方独立复盘
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
   ├─ test_logs.py              日志内容、脱敏、取消与写入故障
   ├─ test_postmatch.py         赛后公开、复盘输入、重试和故障隔离
   └─ test_locked.py            锁定规则、违规纠错、即时结算与模式切换
```

## 运行测试

测试使用 Python 标准库 `unittest`；模型请求通过 HTTPX 的模拟传输验证，不需要真实密钥。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

核心覆盖：两种模式的九种拳型组合、锁定后重复提交拒绝、同响应多个 MOVE 拒绝、锁定后聊天与等待、当 Tick 结算、模式切换与日志，跨 Tick 与结算刻改拳、同一响应多个动作、结算前保密、10/18/19 Tick 提醒、到期弃权或平局、第 20/21 Tick 边界、整份响应拒绝、同 Tick 信息隔离、结算刻重试与故障、HTTP 错误、请求取消、BO 比赛、同场比赛记忆保留、新比赛清空、日志脱敏与故障处理，以及赛后公开时机、双方复盘输入、纠错重试和复盘失败不改变比分。
