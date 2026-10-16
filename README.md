# Model RPS

让两个大语言模型在命令行中聊天、等待、出拳，并在比赛结束后复盘自己的策略。

模型负责交流与决策，Python 裁判负责规则、信息隔离和胜负判定。项目用于观察模型在不同规则和提示词下的博弈行为，不预设模型必须聊天，也不把单场胜负当作模型能力排名。

当前代码版本为 **0.3.0**。Red、Blue 两个席位独立选择模型，支持同模型对战；现有适配器提供 OpenAI 与 DeepSeek 参数方案的 Chat Completions 兼容接口。默认同时行动、首次出拳锁定，另有允许改拳的对照模式和实验性轮流行动模式。支持可选流式响应，默认仍为非流式。

## 功能

- **自主行动**：每次必须选择出拳 `MOVE` 或等待 `WAIT`，可以同时公开发言 `SAY`。
- **BO 比赛**：支持任意正奇数 BO，先达到目标胜局数即结束；平局继续，设有实际总局数上限。
- **独立上下文**：同一场 BO 内保留各自历史，新比赛清空；结算前隐藏拳型。
- **TOML 配置**：设置双方密钥、服务地址、模型、显示名、思考级别与比赛参数。
- **Red / Blue 席位**：红蓝玩家标签，支持同模型、同地址、同密钥对战，各自上下文与日志独立。
- **独立调试窗口**：Windows 下 `--debug` 另开只读命令行窗口，前台显示与普通模式一致。
- **日志与复盘**：记录聊天、分析和调试信息；赛后公开正文分析，双方独立复盘。
- **离线演示**：提供预设模拟玩家，无需 API key。

完整规则与数据约定见 [项目协议 v0.2](model-rps-protocol-v0.2.md)。

人类玩家、更多模型适配与自由聊天的后续设计见 [开发方向与设计讨论](ROADMAP.md)，其中标明当前已完成的阶段。

## 快速开始

需要 **Python 3.12 或更高版本**。使用克隆命令还需要安装 Git；也可以从 GitHub 的 **Code → Download ZIP** 下载并解压，然后在项目根目录打开终端。

### 1. 下载并安装

Windows PowerShell：

```powershell
git clone https://github.com/RasakiSSS/model-rps-protocol.git
cd model-rps-protocol
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

如果安装的是其他受支持的 Python 版本，将 `py -3.12` 换成对应版本，例如 `py -3.13`。已有 `.venv` 时可跳过创建步骤。以下命令直接使用虚拟环境里的 Python，无需激活环境。

macOS / Linux（先用 `python3 --version` 确认版本至少为 3.12）：

```bash
git clone https://github.com/RasakiSSS/model-rps-protocol.git
cd model-rps-protocol
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

后续示例使用 PowerShell；macOS / Linux 将 `.\.venv\Scripts\python.exe` 替换成 `./.venv/bin/python`。

### 2. 先运行模拟比赛

```powershell
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --bo 3
```

这会运行无需密钥、不会调用模型的 BO3，展示聊天、出拳、比分和预设复盘，并打印本次日志目录。模拟玩家使用固定脚本，不能代表真实模型表现。

### 3. 手动创建 config.toml 并配置真实模型

**程序首次运行不会自动创建 `config.toml`。仓库只提供 `config.example.toml` 模板；首次使用真实模型前，需要在项目根目录复制模板，新建自己的 `config.toml`。** 上一步的模拟比赛不需要配置文件。

如果已经有 `config.toml`，直接编辑现有文件，不要再次复制覆盖。首次创建时执行：

```powershell
Copy-Item config.example.toml config.toml
```

macOS / Linux 使用 `cp config.example.toml config.toml`。也可以在文件管理器中复制模板并重命名为 `config.toml`；确认文件名不是 `config.toml.txt`。

用文本编辑器打开新建的 `config.toml`，填写双方密钥，并按实际服务修改以下字段。仅复制模板仍无法运行真实比赛，因为模板中的密钥为空。

| `[players.red]` 与 `[players.blue]` 下的字段 | 填写内容 |
| --- | --- |
| `adapter` | `openai` 或 `deepseek`，选择请求参数方案；两席可选相同值 |
| `api_key` | 对应服务提供的密钥，两方都需填写 |
| `base_url` | API 基础地址，不包含末尾的 `/chat/completions` |
| `model` | 该服务实际支持的模型标识 |
| `thinking` | 当前适配器允许、且模型服务支持的思考级别 |
| `name` | 可选名称，显示为 `Red · 名称` 或 `Blue · 名称`；省略时使用模型标识 |

模板中的 GPT 地址是第三方兼容服务，DeepSeek 地址是官方服务；它们是实验配置示例，不要求使用相同服务。更换服务时，应同时核对地址、模型标识与密钥。程序会在基础地址后追加 `/chat/completions`。

`config.toml` 已被 Git 忽略。共享配置时使用不含密钥的 [config.example.toml](config.example.toml)。

### 4. 运行真实 BO3

```powershell
.\.venv\Scripts\python.exe -m model_rps --config config.toml --mode live --bo 3
```

此命令明确读取刚创建的配置并运行真实 BO3，会发起 API 请求，费用由所用服务计收。显式指定配置可以在文件缺失时直接报错。之后在项目根目录不加参数运行，会使用 `config.toml` 中的比赛设置。正常结束后自动请求双方复盘；按 `Ctrl+C` 可以中止，已经写入的日志会保留。

## 配置与常用命令

程序默认读取**当前工作目录**中的 `config.toml`，优先级为 **命令行参数 > TOML 配置 > 内置默认值**。配置文件使用 UTF-8 编码。

| 配置节 | 字段 | 用途 |
| --- | --- | --- |
| `[app]` | `mode`、`log_dir` | live / demo，以及日志根目录 |
| `[app]` | `timeout_seconds`、`retry_delay_seconds` | 每次请求总时限、重试间隔 |
| `[app]` | `max_output_tokens` | 对局和复盘共用的单次输出上限 |
| `[app]` | `stream` | 是否启用流式响应，默认 `false` |
| `[app]` | `stream_first_timeout_seconds`、`stream_idle_timeout_seconds` | 首个流式数据事件等待上限、后续数据事件间的等待上限 |
| `[match]` | `best_of`、`max_games` | BO 正奇数、包含平局的实际总局数上限 |
| `[match]` | `move_policy` | locked 锁定出拳 / revisable 允许改拳 |
| `[match]` | `initiative` | simultaneous 同时行动 / alternating 轮流行动 |
| `[players.red]`、`[players.blue]` | `adapter`、`model`、`base_url`、`api_key`、`thinking`、`name` | 双方适配器、模型与身份设置 |

内置默认值为真实模式、BO1、同时行动、锁定出拳、双方思考 `off`、60 秒超时、1 秒重试间隔和 20,000 token 输出上限。模板覆盖部分设置：BO9、双方思考 `high`、120 秒超时。模型与服务地址以你填写的配置为准。

省略 `max_games` 时，上限按**最终生效的 BO × 10** 计算；显式设置时不得小于目标胜局数。配置中的相对日志路径基于配置文件所在目录，命令行 `--log-dir` 则基于当前工作目录。

```powershell
# 使用另一份配置
.\.venv\Scripts\python.exe -m model_rps --config config.local.toml

# 真实 BO5，最多进行 20 局（包含平局）
.\.venv\Scripts\python.exe -m model_rps --bo 5 --max-games 20

# 临时调整请求时限与输出上限
.\.venv\Scripts\python.exe -m model_rps --timeout 180 --max-output-tokens 24000

# 查看全部参数；无需密钥或有效配置
.\.venv\Scripts\python.exe -m model_rps --help
```

`--config` 与 `--no-config` 互斥。显式指定的配置不存在、TOML 格式错误或包含非法参数时，程序会在请求前报错。运行模拟比赛建议使用 `--no-config --mode demo`，以免读入真实配置中的思考设置；模拟模式要求双方思考为 `off`。

未显式指定配置且当前目录没有 `config.toml` 时，程序使用内置默认值，不会生成文件。真实模式在没有 TOML 密钥时仍有旧版密钥文件回退逻辑；新用户若遇到 `local_secrets.py` 缺失提示，应按快速开始创建并填写 `config.toml`。

### 同模型对战与旧配置

将 Red 的 `adapter`、`model`、`base_url`、`api_key` 复制到 Blue，即可让同一个模型对战。双方可以共享密钥，仍会发起各自的请求。`thinking` 可相同或分别设置，`name` 可选；即使名称相同，Red / Blue 标签也能区分双方。

两席都可选择 `openai` 或 `deepseek`，适配器决定思考和输出长度参数，不根据模型名称猜测。省略适配器时，Red 默认为 `openai`，Blue 默认为 `deepseek`；省略模型或地址时，使用所选适配器的内置值。已经填写的模型、地址不会因切换适配器而自动改写。

命令行支持 `--red-adapter`、`--blue-adapter`，以及各席位的 `--red-model` / `--blue-model`、`--red-base-url` / `--blue-base-url`、`--red-thinking` / `--blue-thinking`、`--red-name` / `--blue-name`。密钥仍填写在本地配置中。

旧 `[players.gpt]`、`[players.deepseek]` 分别兼容映射到 Red、Blue；旧 `--openai-*`、`--deepseek-*` 参数也是对应席位的别名。迁移时将两个配置节一起改为 `[players.red]`、`[players.blue]`，并分别添加 `adapter = "openai"`、`adapter = "deepseek"`。不能在同一文件中混用新旧玩家配置节。使用旧密钥文件时，按所选适配器取对应密钥。

## 流式响应

在 `config.toml` 的 `[app]` 中设置 `stream = true`，或通过命令行临时启用：

```powershell
.\.venv\Scripts\python.exe -m model_rps --config config.toml --stream --bo 3
```

`--no-stream` 可临时恢复非流式。开关对双方的对局和复盘都生效，兼容同时行动、轮流行动及允许改拳模式；模拟玩家仍使用固定脚本，不产生流式网络请求。

流式接收期间，前台不打印逐阶段进度；开启 `--debug` 后在独立窗口显示等待、接收思考或接收正文的状态。接口思考逐步写入各自的 `thought.log`，正文片段只写入私有 `debug.log`。收到正常结束标记并组装完整 JSON 后，才进行动作或复盘校验；即使某个中间片段看起来已经能解析，也不会提前发言、出拳或公开分析。

| 时限 | TOML 字段 | CLI 参数 | 默认值 |
| --- | --- | --- | --- |
| 首个数据事件（包含连接时间） | `stream_first_timeout_seconds` | `--stream-first-timeout` | 60 秒 |
| 后续数据事件间隔 | `stream_idle_timeout_seconds` | `--stream-idle-timeout` | 30 秒 |
| 整次请求，包括持续输出 | `timeout_seconds` | `--timeout` | 内置 60 秒，模板 120 秒 |

以上字段均放在 `[app]`；总时限始终有效，实际以最先达到的时限为准。仅有注释心跳不会重置数据事件等待时间。思考较长时，可根据服务表现调整这些时限。

断流、缺失结束标记、截断或非法事件会使本次响应无效，沿用最多三次的重试额度。失败片段保留在私有日志，不进入后续模型历史，也不会与下一次请求拼接。服务不支持流式或相关参数时会报错，不自动改为非流式。

## 终端显示与调试模式

Red 的玩家标签显示为红色，Blue 为蓝色；交换先手时颜色不变。日志、重定向输出和不支持颜色的终端保留纯文本标签；设置 `NO_COLOR` 环境变量可关闭颜色。

**普通模式与 debug 模式的前台显示、交互一致。** Windows 下添加 `--debug` 会额外打开一个独立命令行窗口，显示 Tick、请求阶段、耗时、用量及错误和重试概况：

```powershell
.\.venv\Scripts\python.exe -m model_rps --stream --debug --bo 3
```

调试窗口只读取本场日志，不会重复请求模型，关闭它不影响比赛。比赛结束后窗口保留内容，按 Enter 关闭。前台继续显示聊天、提交与等待、截止提醒、简短重试或错误、比分和复盘，不显示 Tick 标题或请求阶段。完整输入、正文和思考保留在私有日志，不在调试窗口逐片刷屏。

无需开启调试模式即可保存全部五份日志。窗口启动失败时报告简短提示，比赛继续，诊断内容不会回退到前台。自动打开窗口目前仅支持 Windows；其他平台可自行在另一个终端运行以下只读查看器。`--debug` 仍只作为命令行参数，不加入 TOML。

```powershell
# 将路径替换为前台打印的本次运行目录；--once 仅读取现有记录
.\.venv\Scripts\python.exe -m model_rps.debug_console "logs/本次运行目录" --once
```

两种显示模式都将双方拳型与结果合并为一行：

```text
System：Red · gpt-5.6-sol → 石头；Blue · deepseek-flash → 布；Blue · deepseek-flash 获胜。
```

平局和弃权也使用单行结算，未提交者显示“未出拳”；技术异常中止时不揭晓拳型。

## 比赛规则

一场 BO 的目标胜局数为 `N // 2 + 1`，例如 BO3 先赢 2 局、BO5 先赢 3 局。平局不加分，继续下一局；达到实际总局数上限仍无人达标，比赛记为“未决”。同一 BO 内保留双方各自上下文，每局只重置出拳与行动计数。

每次响应必须恰好包含一个 `MOVE` 或 `WAIT`，可以附带多个 `SAY`。`SAY` 是公开发言，不等于实际出拳。对手可以知道是否已提交，结算前看不到真实拳型。

### 同时行动（默认）

一个 Tick 内同时请求双方，双方根据 Tick 开始时的状态决策，看不到对手本 Tick 的新响应。两份响应均有效后执行动作并判定结果。

| 出拳规则 | `locked`（默认） | `revisable`（对照模式） |
| --- | --- | --- |
| 有效拳型 | 第一次有效 MOVE | 最后一次有效 MOVE |
| 后续 MOVE | 忽略，保留原拳型和本轮 SAY | 允许跨 Tick 修改 |
| 双方均已提交 | 当前 Tick 全部动作处理后结算 | 再进行一个结算 Tick，仍可改拳 |
| 第 20 Tick 才提交齐全 | 当 Tick 结算 | 追加第 21 Tick 结算 |

常规提交截止为第 20 Tick；仍未出拳的一方弃权，双方都未出拳则平局。未结束且尚未安排结算 Tick 时，第 10、18、19 Tick 结束分别提醒剩余 10、2、1 Tick。

```powershell
# 使用真实配置，观察允许改拳的 BO3
.\.venv\Scripts\python.exe -m model_rps --bo 3 --initiative simultaneous --move-policy revisable
```

### 轮流行动（实验性）

`alternating` 每次只请求一方，通过校验后立即公开发言和提交状态，另一方下次行动即可看到。它按固定次序轮流行动，不根据 API 返回速度决定先后手。

- 第一局 Red 先手，之后逐局交换，平局也交换；席位和颜色不交换。
- 每方最多 20 次行动，全局最多 40 Tick；双方出拳齐全后，在当前完整响应处理后结算。
- 每位玩家完成自己的第 10、18、19 次行动后提醒剩余次数；重试不消耗行动机会。
- 仅支持 `locked`，配置为 `alternating + revisable` 会报错。

```powershell
# 离线演示
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --initiative alternating --bo 3

# 真实实验
.\.venv\Scripts\python.exe -m model_rps --initiative alternating --move-policy locked --bo 3
```

## 思考、分析与赛后复盘

双方独立设置思考级别，同一场 BO 的对局与复盘共用设置：

| 适配器 | 程序允许的 thinking 值 | 配置与 CLI |
| --- | --- | --- |
| `openai` | off、low、medium、high、xhigh、max | 任一席位的 `thinking`；`--red-thinking` / `--blue-thinking` |
| `deepseek` | off、low、high、max | 任一席位的 `thinking`；`--red-thinking` / `--blue-thinking` |

这些是本项目适配器接受的选项，不代表所有模型和兼容服务都支持。接口拒绝参数时明确报错，不自动降级。仅更换 `model` 不会改变该席位的请求参数方案，接入不兼容模型需要调整适配器。

模型必须提供非空的正文策略分析，字段名为 `decision_summary`；只有当前响应实际返回非空 `reasoning_content` 或 `reasoning` 时，才可省略正文分析。仅开启思考或返回思考 token 数不足以替代正文分析。无论是否提供思考文本，动作 JSON 都必须完整有效。

- **正文策略分析**：比赛期间仅保留在该玩家自己的历史与私有日志中，整场 BO 结束后公开。
- **接口思考文本**：只写入该玩家私有日志，不发送给对手、不回填后续请求，也不纳入公开复盘材料。

产生冠军或达到总局数上限后，程序公开有效动作与正文分析，并让双方分别给出整体评价、关键决策和改进建议。复盘不改变比分。技术异常或用户中止时不自动复盘；模拟模式使用明确标注的预设内容。

输出上限对对局和复盘均生效，不要求模型写满。思考可能占用生成额度，实际支持的上限取决于服务；长输出也可能触发超时。模型分析与复盘可能有误，实际动作和结果以裁判记录为准。

## 日志

每次运行创建独立目录，启动时打印路径：

```text
logs/日期时间_唯一编号/
├─ chat.log
├─ red/
│  ├─ thought.log
│  └─ debug.log
└─ blue/
   ├─ thought.log
   └─ debug.log
```

| 文件 | 内容 |
| --- | --- |
| `chat.log` | 公开发言、系统消息、结算、比分、赛后公开分析及双方复盘 |
| `thought.log` | 按局号、Tick 和尝试次数分段的中文阅读版：接口思考、正文分析、动作、复盘及失败状态 |
| `debug.log` | JSON Lines 格式的输入、响应、结构化事件、用量、耗时、错误与重试记录 |

流式模式增加 `stream_delta`（文本片段）和 `stream_end`（完整或未完成的接收记录）。`thought.log` 连续保留思考的段落，结束后记录状态与用量，不重复打印整段思考。跨分片的已知密钥前缀会暂缓写入以完成脱敏；请求结束时仍不完整的前缀会被遮盖。用量缺失记为 `null`，不视为零。

日志目录按席位固定，记录中仍包含玩家名称和实际请求的模型。更换模型、相同模型或相同昵称不会导致日志覆盖。旧版本日志保持原样，新的调试查看器读取上述 red / blue 目录结构。

“通过校验”不等于“已执行”。同时行动时，若对手在该 Tick 失败，双方暂存动作都不执行；轮流行动时保留此前已完成的行动。以公开裁判记录判断实际结果。锁定后被忽略的 MOVE 在私有记录与赛后报告中单独标记为 `ignored_actions`。

日志使用 UTF-8，逐条写入；写入失败会中止比赛。不记录鉴权头，并过滤本次配置的两把 API key。私有日志仍包含模型输入、输出和分析，分享前应检查内容。默认 `logs/` 已被 Git 忽略；自定义目录需自行配置忽略规则。

## 常见问题

| 情况 | 处理方式 |
| --- | --- |
| `No module named model_rps` | 在项目根目录，用同一个虚拟环境的 Python 执行 `-m pip install -e .` |
| 首次运行后没有 `config.toml` | 程序不会自动生成；复制 `config.example.toml` 为 `config.toml`，再填写双方密钥 |
| 找不到配置或密钥为空 | 确认文件已保存、名称不是 `config.toml.txt`，并在项目根目录运行，或用 `--config` 指定路径 |
| HTTP 401 | 核对密钥是否属于当前 `base_url` 对应的服务 |
| 模型或参数不支持 | 核对服务支持的模型标识、思考档位、JSON 输出和输出上限 |
| 请求超时或 JSON 被截断 | 查看私有 `debug.log`，结合服务限制调整超时、思考级别及输出上限 |
| 模拟模式提示思考设置不支持 | 使用 `--no-config --mode demo`，跳过真实配置 |
| 双方很快出拳，几乎不聊天 | 这是规则允许的模型选择；提示词不强制聊天或等待 |

每位玩家每次行动最多尝试 3 次，格式纠错和网络重试共用额度；HTTP 408、409、429 和 5xx 可重试，其他 HTTP 错误直接中止。重试仍使用该次行动开始时的状态，只重新请求出错的一方。

退出码：`0` 冠军产生且复盘完成；`1` 技术或日志异常；`2` 配置错误；`3` 比赛未决且复盘完成；`4` 比赛结束但复盘失败；`130` 用户中止。复盘失败不改变已确定的比赛结果。

## 开发与测试

```text
model-rps-protocol/
├─ README.md                     安装与使用说明
├─ model-rps-protocol-v0.2.md     详细协议
├─ config.example.toml           无密钥的配置模板
├─ pyproject.toml                Python 版本、依赖与入口
├─ src/model_rps/
│  ├─ __main__.py / cli.py       启动参数与比赛流程组装
│  ├─ config.py                  配置读取与校验
│  ├─ player_factory.py          按席位创建独立玩家、兼容旧密钥文件
│  ├─ console.py / terminal.py   前台展示、红蓝颜色
│  ├─ debug_console.py           独立只读调试窗口
│  ├─ game.py                    状态、动作与裁判规则
│  ├─ runner.py / match.py       行动调度、重试与 BO 比赛
│  ├─ players.py                 模型适配器与模拟玩家
│  ├─ streaming.py               SSE 事件解码与完整响应组装
│  ├─ prompts.py                 对局与复盘提示词
│  ├─ postmatch.py               赛后公开与复盘
│  ├─ logs.py / diagnostics.py   日志与诊断
│  └─ identities.py              玩家名称与日志目录
└─ tests/                        自动化测试
```

对局和复盘提示词集中在 [prompts.py](src/model_rps/prompts.py)，修改后重启程序生效。`SYSTEM_PROMPT_TEMPLATE` 使用普通字符串，保留 `__MOVE_RULES__` 等占位符；`game_prompt()` 按当前模式组合规则。修改提示词不会自动修改 [game.py](src/model_rps/game.py) 中的裁判逻辑。

以 `-e .` 安装后，修改源文件无需重新安装；依赖声明变化时再执行安装命令。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用标准库 `unittest` 和 HTTPX 模拟传输，不调用真实模型。覆盖裁判规则、同时与轮流行动、上下文隔离、配置、请求重试、日志、思考字段和赛后复盘，以及流式分片、断流、超时、取消和跨分片脱敏。

更多离线场景可以通过 `--scenario` 选择，例如：

```powershell
# 到期出拳
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario deadline

# 观察一局到期平局，退出码为 3（比赛未决）
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario draw --max-games 1

# 结算 Tick 改拳
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --move-policy revisable --scenario settlement
```
