# Model RPS

让人类与大语言模型，或两个模型，在命令行中聊天、等待、出拳，比赛结束后继续自由交流，直到双方退出。

玩家负责交流与决策，Python 裁判负责规则、信息隔离和胜负判定。项目用于观察模型在不同规则和提示词下的博弈行为，不预设模型必须聊天，也不把单场胜负当作模型能力排名。

当前代码版本为 **0.8.0**，支持 OpenAI、DeepSeek、GLM、Gemini、Grok 与 Claude 六种适配方案，可在配置中保存多个模型条目。Red、Blue 两个席位可选择人类或模型，一个终端最多一位人类；模型可以相同，各自上下文独立。提供独立聊天、上下文增量传递和赛后沉默待机。默认同时行动、首次出拳锁定，另有允许改拳的对照模式和实验性轮流行动模式。支持可选流式响应，默认仍为非流式。

## 功能

- **统一指令**：`SAY` 发言、`ANALYZE` 分析、`MOVE` 出拳，以 `ENDTURN` 结束行动；未出拳则等待。
- **人类玩家**：用斜杠指令参与任一席位，聊天即时发送，出拳与分析在结束行动时提交；对手发言不打断正在编辑的输入。
- **独立聊天**：可单独发言，不消耗行动机会；开局和行动权限变化时，非行动方也有主动发言机会。消息排队合并，同一玩家始终只有一个请求。
- **BO 比赛**：支持任意正奇数 BO，先达到目标胜局数即结束；平局继续，设有实际总局数上限。
- **独立上下文**：同一场 BO 内保留各自历史，新比赛清空；结算前隐藏拳型。
- **TOML 配置**：保存人类与模型 profile，按模型设置密钥、地址、思考与流式参数，再指定 Red / Blue 使用的条目。
- **Red / Blue 席位**：红蓝玩家标签，支持同模型、同地址、同密钥对战，各自上下文与日志独立。
- **独立调试窗口**：Windows 下 `--debug` 另开只读命令行窗口，前台显示与普通模式一致。
- **日志与赛后聊天**：分析要求可按席位配置；赛后公开分析，双方沿用本场上下文继续 `SAY` 交流，双方 `EXIT` 后结束。
- **离线演示**：提供预设模拟玩家，无需 API key。

完整规则与数据约定见 [项目协议 v0.2](model-rps-protocol-v0.2.md)。

后续计划支持比赛之间换对手，再处理局中模型接管，见 [开发方向与设计讨论](ROADMAP.md)。这些计划尚未实现，当前使用规则以本文为准。

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

这会运行无需密钥、不会调用模型的 BO3，展示聊天、出拳、比分和预设赛后聊天，并打印本次日志目录。模拟玩家使用固定脚本，不能代表真实模型表现。

### 3. 手动创建 config.toml 并配置真实模型

**程序首次运行不会自动创建 `config.toml`。仓库只提供 `config.example.toml` 模板；首次使用真实模型前，需要在项目根目录复制模板，新建自己的 `config.toml`。** 上一步的模拟比赛不需要配置文件。

如果已经有 `config.toml`，直接编辑现有文件，不要再次复制覆盖。首次创建时执行：

```powershell
Copy-Item config.example.toml config.toml
```

macOS / Linux 使用 `cp config.example.toml config.toml`。也可以在文件管理器中复制模板并重命名为 `config.toml`；确认文件名不是 `config.toml.txt`。

用文本编辑器打开新建的 `config.toml`，在 `[profiles.gpt]`、`[profiles.deepseek]` 等模型条目中填写密钥、地址和模型标识，再通过 `[players.red]`、`[players.blue]` 的 `profile` 选择它们。模板默认选择 gpt / deepseek，未选中的模型可以暂不填写密钥。仅复制模板仍无法运行真实比赛，因为模板中的密钥为空。人类 profile 不需要密钥。

| 字段 | 填写内容 |
| --- | --- |
| `type` | profile 或席位使用：`llm`（默认）或 `human`；一个终端最多一位人类 |
| `profile` | 仅席位使用：引用 `[profiles.名称]`，例如 `profile = "claude"` 或 `"human"` |
| `adapter` | `openai`、`deepseek`、`glm`、`gemini`、`grok`、`anthropic`；模型 profile 必填，人类不填 |
| `api_key` | 对应服务提供的密钥，仅模型席位需要填写 |
| `base_url` | API 基础地址，不包含末尾的 `/chat/completions` 或 `/messages` |
| `model` | 该服务实际支持的模型标识 |
| `thinking` | 当前适配器允许、且模型服务支持的思考级别 |
| `analysis` | 策略分析要求：`off` 关闭、`optional` 可选、`required` 必填；省略时模型必填、人类可选 |
| `name` | 可选名称，显示为 `Red · 名称` 或 `Blue · 名称`；省略时模型使用模型标识，人类显示“玩家” |

除 `profile` 外，上述字段可放在模型 profile 中，或在席位上覆盖。人类 profile 只使用 `type`、`name`、`analysis`。仍支持完整席位配置；旧 `[models.名称]` 兼容为模型条目，与 profiles 共享名称，不能重名。

模板中的 GPT 地址是第三方兼容服务，其他地址是相应官方接口的示例，不要求使用相同服务。更换服务时，应同时核对地址、模型标识与密钥。`anthropic` 使用原生 Messages 接口，其他适配器使用 Chat Completions；具体参数和兼容性见 [模型适配说明](MODEL_ADAPTERS.md)。

`config.toml` 已被 Git 忽略。共享配置时使用不含密钥的 [config.example.toml](config.example.toml)。

### 4. 运行真实 BO3

```powershell
.\.venv\Scripts\python.exe -m model_rps --config config.toml --mode live --bo 3
```

此命令明确读取刚创建的配置并运行真实 BO3，会发起 API 请求，费用由所用服务计收。显式指定配置可以在文件缺失时直接报错。之后在项目根目录不加参数运行，会使用 `config.toml` 中的比赛设置。正常结束后自动请求双方赛后聊天；按 `Ctrl+C` 可以中止，已经写入的日志会保留。

## 人类参与比赛

模板已提供 `[profiles.human]`。将其中的 `name` 改为自己的用户名，然后选择这个 profile，与 Blue 的模型对战：

```powershell
.\.venv\Scripts\python.exe -m model_rps --red-profile human --bo 3
```

改用 Blue 时使用 `--blue-profile human`；另一席位继续使用配置中的模型与密钥。`--initiative alternating` 启用轮流行动，第一局 Red 先手，之后每局交换。

也可以在配置中选择人类 profile：

```toml
[profiles.human]
type = "human"
name = "小明"
analysis = "optional"

[players.red]
profile = "human"
```

修改已有配置节，不要重复添加同名节。席位不写 `type` 时跟随 profile；如果席位显式写了 `type = "llm"`，它会覆盖人类 profile，应删掉该覆盖项或使用 `--red-type human`。`--red-name`、`--red-analysis` 仍可临时覆盖昵称和分析要求。

旧的 `--red-type human --red-name "小明"` 和直接在席位配置 type/name 的方式继续支持。人类无需密钥，输入等待不受流式时限影响。省略 `analysis` 时，人类默认为 `optional`，模型默认为 `required`；显式填写的分析要求不随类型自动变化。

无需密钥的离线练习：

```powershell
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --red-type human --red-name "小明" --bo 3
```

在正常终端窗口运行，不要重定向标准输入或输出。人类模式使用 `prompt-toolkit` 处理异步输入；从旧版本升级后，先执行一次 `-m pip install -e .` 安装新增依赖。

| 输入 | 效果 |
| --- | --- |
| `/say 你好` | 立即公开发言，不结束行动；等待模型或轮到对手时也可发送 |
| `/move Rock` | 暂存石头，也可选 `Paper`（布）、`Scissors`（剪刀）；再次输入替换草稿 |
| `/analyze 我的策略` | 暂存私有分析，多次输入按顺序追加；有效分析在整场 BO 结束后公开 |
| `/endturn` | 提交暂存的出拳与分析并结束行动；没有 MOVE 就等待，已有拳型保留 |
| `/clear` | 清空尚未提交的出拳与分析 |
| `/status` | 查看当前行动权限、自己的拳型、草稿与剩余机会 |
| `/help` | 查看输入说明 |
| `/exit` | 仅在赛后退出聊天；双方退出后程序结束 |

每行一条指令。`/move` 与 `/analyze` 只在有行动权限时接受，`/say` 不会提交或清空它们。`/endturn` 后不能再修改本次行动；locked 下已锁定拳型不接受后续修改，revisable 允许下一 Tick 再提交新拳型。

底部显示当前局、Tick 和行动权限。对手发言时，正在编辑的输入会保留；进入新局或赛后时清空行动草稿及未发送的行动指令，未发送的聊天文字可继续编辑。输入错误只反馈本人，不占模型重试额度；人类等待输入不受 API 超时限制。`Ctrl+C` 或关闭输入会中止整场运行并保留已有日志。

双方共用裁判和可见状态，人类只能看到自己的未公开拳型与分析。带“仅自己”的草稿反馈不会写入 `chat.log` 或送给对手；提交后的分析进入本人私有日志，正常赛后才公开。开发者调试窗口和私有日志不属于玩家公开信息。

## 配置与常用命令

程序默认读取**当前工作目录**中的 `config.toml`。玩家字段的优先级为 **命令行参数 > TOML 席位字段 > 选中的 profile > 内置默认值**；流式字段还可继承 app 默认值，详见下文。其余字段为命令行 > TOML > 内置默认值。配置文件使用 UTF-8 编码。

| 配置节 | 字段 | 用途 |
| --- | --- | --- |
| `[app]` | `mode`、`log_dir` | live / demo，以及日志根目录 |
| `[app]` | `timeout_seconds`、`retry_delay_seconds` | 每次模型请求总时限、重试间隔；不限制人类输入等待 |
| `[app]` | `max_output_tokens` | 对局和赛后聊天共用的单次输出上限 |
| `[app]` | `stream` | 双方流式开关的默认值，内置 `false` |
| `[app]` | `stream_first_timeout_seconds`、`stream_idle_timeout_seconds` | 首包、空闲等待的默认值 |
| `[match]` | `best_of`、`max_games` | BO 正奇数、包含平局的实际总局数上限 |
| `[match]` | `move_policy` | locked 锁定出拳 / revisable 允许改拳 |
| `[match]` | `initiative` | simultaneous 同时行动 / alternating 轮流行动 |
| `[profiles.名称]`，type=llm 或省略 | `type`、`adapter`、`model`、`base_url`、`api_key`、`thinking`、`analysis`、`name`，以及三个流式字段 | 可重复引用的模型配置 |
| `[profiles.名称]`，type=human | `type`、`name`、`analysis` | 可重复选择的人类身份，无需密钥 |
| `[players.red]`、`[players.blue]` | `profile`，以及模型 profile 中的所有字段 | 选择玩家，按席位覆盖设置 |

内置默认值为真实模式、BO1、同时行动、锁定出拳、OpenAI / DeepSeek 思考 `off`、60 秒超时、1 秒重试间隔和 20,000 token 输出上限。新增的四种适配器默认思考为 `high`。模板覆盖部分设置：BO9、所列模型思考 `high`、120 秒超时。模型与服务地址以你填写的配置为准。

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

两席引用同一个 `profile` 即可让相同模型对战。双方可以共享密钥，仍会发起各自的请求。`thinking` 可相同或分别设置，`name` 可选；即使名称相同，Red / Blue 标签也能区分双方。

```powershell
# 同模型对战
.\.venv\Scripts\python.exe -m model_rps --red-profile glm --blue-profile glm --bo 3

# 选择不同模型，单独覆盖 Blue 的思考级别
.\.venv\Scripts\python.exe -m model_rps --red-profile claude --blue-profile gemini --blue-thinking medium --max-output-tokens 20000 --bo 3
```

`--red-profile`、`--blue-profile` 选择当前配置文件中的玩家条目；切换时重新读取该条目的类型、模型及相关设置，席位上显式填写的覆盖字段仍然生效。新建条目时建议使用 `glm` 这样的简单名称，含点号的 TOML 名称需要加引号，例如 `[profiles."glm-5.3"]`。

两席可选任意适配器。适配器决定协议、鉴权、思考和输出长度参数，不根据模型名称猜测。未选择 profile 且省略适配器时，Red 默认为 `openai`，Blue 默认为 `deepseek`；省略模型或地址时，使用所选适配器的内置值。已经填写的模型、地址不会因单独切换适配器而自动改写。

命令行支持 `--red-adapter`、`--blue-adapter`，以及各席位的 `--red-model` / `--blue-model`、`--red-base-url` / `--blue-base-url`、`--red-thinking` / `--blue-thinking`、`--red-name` / `--blue-name`。密钥仍填写在本地配置中。

旧 `[players.gpt]`、`[players.deepseek]` 分别兼容映射到 Red、Blue；旧 `--openai-*`、`--deepseek-*` 参数也是对应席位的别名。迁移时将两个配置节一起改为 `[players.red]`、`[players.blue]`，并分别添加 `adapter = "openai"`、`adapter = "deepseek"`。不能在同一文件中混用新旧玩家配置节。旧密钥文件仅支持 OpenAI / DeepSeek；新增适配器的密钥应填在 TOML 中。

## 流式响应

在 `[app]` 中设置流式默认值；每个模型 profile 或席位都可单独覆盖 `stream`、`stream_first_timeout_seconds`、`stream_idle_timeout_seconds`。例如在已有模型条目中添加：

```toml
[profiles.grok]
# 保留原有 adapter、model、base_url、api_key 等字段
stream = false

[profiles.gemini]
# 保留原有模型字段
stream = true
stream_first_timeout_seconds = 90
stream_idle_timeout_seconds = 40
```

这样可让 Grok 非流式、Gemini 流式，其他模型继承 `[app]`。同一模型占两席时，也可在 `[players.red]` / `[players.blue]` 分别覆盖。

流式字段的优先级为 **席位 CLI > 全局 CLI > 席位 TOML > profile > app > 内置默认值**，逐字段合并。显式 `false` 会覆盖 `true`。临时要求双方流式：

```powershell
.\.venv\Scripts\python.exe -m model_rps --config config.toml --stream --bo 3
```

`--no-stream` 临时关闭双方流式；`--red-stream` / `--no-red-stream` 和 `--blue-stream` / `--no-blue-stream` 只覆盖指定席位，优先于全局参数。同样提供 `--red-stream-first-timeout`、`--red-stream-idle-timeout` 及对应 blue 参数。各席位设置用于自己的对局、独立聊天和赛后聊天；人类与模拟玩家不发起流式网络请求。

流式接收期间，前台不打印逐阶段进度；开启 `--debug` 后在独立窗口显示等待、接收思考或接收正文的状态。接口思考逐步写入各自的 `thought.log`，正文片段只写入私有 `debug.log`。收到正常结束标记并组装完整 JSON 后，才进行动作或赛后聊天校验；即使某个中间片段看起来已经能解析，也不会提前发言、出拳或公开分析。

| 时限 | TOML 字段 | CLI 参数 | 默认值 |
| --- | --- | --- | --- |
| 首个数据事件（包含连接时间） | `stream_first_timeout_seconds` | `--stream-first-timeout` | 60 秒 |
| 后续数据事件间隔 | `stream_idle_timeout_seconds` | `--stream-idle-timeout` | 30 秒 |
| 整次请求，包括持续输出 | `timeout_seconds` | `--timeout` | 内置 60 秒，模板 120 秒 |

首包和空闲字段可写在 `[app]`、模型 profile 或席位中；整次请求的 `timeout_seconds` 仍仅放在 `[app]`，实际以最先达到的时限为准。心跳不会重置数据事件等待时间。思考较长时，可根据服务表现调整这些时限。

断流、缺失结束标记、截断或非法事件会使本次响应无效，沿用最多三次的重试额度。失败片段保留在私有日志，不进入后续模型历史，也不会与下一次请求拼接。服务不支持流式或相关参数时会报错，不自动改为非流式。

## 终端显示与调试模式

Red 的玩家标签显示为红色，Blue 为蓝色；交换先手时颜色不变。日志、重定向输出和不支持颜色的终端保留纯文本标签；设置 `NO_COLOR` 环境变量可关闭颜色。

**普通模式与 debug 模式的前台显示、交互一致。** Windows 下添加 `--debug` 会额外打开一个独立命令行窗口，显示 Tick、请求阶段、耗时、用量及错误和重试概况：

```powershell
.\.venv\Scripts\python.exe -m model_rps --stream --debug --bo 3
```

调试窗口只读取本场日志，不会重复请求模型，关闭它不影响比赛。比赛结束后窗口保留内容，按 Enter 关闭。前台继续显示聊天、提交与等待、截止提醒、简短重试或错误、比分和赛后聊天，不显示 Tick 标题或请求阶段。完整输入、正文和思考保留在私有日志，不在调试窗口逐片刷屏。

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

每次行动响应最多包含一个 `MOVE`，以唯一的 `ENDTURN` 结束，可以附带多个 `SAY` 和 `ANALYZE`。没有 `MOVE` 时，结束行动表示本次等待，保留已有拳型。`SAY` 是公开发言，不等于实际出拳，也不会自动结束行动。对手可以知道是否已提交，结算前看不到真实拳型。

模型统一返回 `{"actions":[指令,...]}`，例如：

```json
{"actions":[{"type":"ANALYZE","text":"先提交选择，观察后续交流。"},{"type":"MOVE","move":"Rock"},{"type":"SAY","text":"我已经决定了。"},{"type":"ENDTURN"}]}
```

旧 `decision_summary` 字段和显式 `WAIT` 指令已移除。出拳、分析随完整行动提交，以 `ENDTURN` 结束。模型一次返回完整 JSON；人类逐条输入 `/move`、`/analyze` 暂存，再以 `/endturn` 提交，两者共用指令校验。

### 独立聊天

双方可以单独返回一个或多个 `SAY`。完整响应校验通过后立即公开，不需要分析或 `ENDTURN`，不消耗行动次数，也不要求等到自己的行动机会：

```json
{"actions":[{"type":"SAY","text":"先聊一句，你这么快就决定了吗？"}]}
```

模型收到的 `state.can_act` 表示本次权限：为 `true` 时可以聊天，也可提交完整行动；为 `false` 时可以主动发言或回复，选择沉默就返回 `{"actions":[]}`。沉默不会产生新消息或再次唤醒对方。有行动权限时不能用空数组代替行动；明确 `ENDTURN` 且没有 `MOVE` 才表示等待。

聊天机会由以下事件触发，不依赖对手先开口：

- 每局开始同时请求双方；轮流模式下先手可行动，后手可聊天或沉默。同时模式下双方都可行动。
- 轮流模式每次轮换时通知双方：新行动方可行动，另一方可聊天或沉默。
- 同时模式提前结束本 Tick 行动的一方，在等待对手期间获得一次聊天机会；不提前向对手公开其提交状态。
- 对手的有效 `SAY` 会提供回复机会。

模型忙碌时，消息和状态通知先排队，下一次请求合并提供；同类状态通知合并一次。同一玩家始终只有一个请求，下一次有行动机会时优先以行动权限处理全部通知，不抢占已经进行中的聊天请求。每次请求都有固定快照，生成期间不插入新消息，权限也不升级。`state.event_cursor` 和 `state.notifications` 标记消息边界与新发言序号，`state.request_reasons` 标记触发原因。

没有新事件时，不会为已经沉默的模型不断发起聊天请求。获得机会也不强制发言；如果对局已经结束，尚未派发的聊天通知直接失效。

聊天不会阻止已经满足条件的结算。该局结束时取消未完成的请求，尚未应用的响应不公开、不执行，也不留在后续上下文中；已经公开的聊天会保留。流式聊天同样等完整响应结束后才校验，不展示半截 JSON。

本版默认启用独立聊天，无需新增配置。暂不增加聊天额度或自动裁剪历史；如果双方持续只聊天，行动计数不会推进。

### 同时行动（默认）

每个 Tick 同时给予双方行动机会。两方都提交完整行动后，才统一执行出拳、公开提交状态并判定结果；提前结束行动的一方仍可聊天，但不能重新提交本 Tick 的行动。

聊天即时公开，后续请求可以看到本 Tick 的新发言；已经开始生成的请求保持原快照。因此双方后续聊天所依据的信息可能不同，但聊天请求不会暴露对手尚未公开的提交状态或拳型。自己已经暂存的拳型仍对自己可见。

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

`alternating` 每次只有一方拥有行动权限，完整行动通过校验后立即公开提交状态并轮换。开局及每次轮换都会提供双方的响应机会，非行动方可以主动聊天或沉默；请求可能并行，但行动顺序固定，不根据 API 返回速度决定先后手。

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

## 思考、分析与赛后聊天

双方独立设置思考级别，同一场 BO 的对局与赛后聊天共用设置：

| 适配器 | 程序允许的 thinking 值 | 配置与 CLI |
| --- | --- | --- |
| `openai` | off、low、medium、high、xhigh、max | 任一席位的 `thinking`；`--red-thinking` / `--blue-thinking` |
| `deepseek` | off、low、high、max | 任一席位的 `thinking`；`--red-thinking` / `--blue-thinking` |
| `glm` | low、high、max | 同上，面向 GLM-5.3 |
| `gemini` | low、medium、high | 同上，面向 Gemini 4 Argon |
| `grok` | low、medium、high、xhigh | 同上，面向 Grok 4.7 |
| `anthropic` | low、medium、high、xhigh、max | 同上，面向 Claude Sonnet 5.5 |

这些是本项目适配器接受的选项，不代表所有模型和兼容服务都支持。新增方案按表中模型的文档配置，不提供 `off`。接口拒绝参数时明确报错，不自动降级。仅更换 `model` 不会改变该席位的请求参数方案，接入不兼容模型需要调整适配器。参数映射、文档来源和实测限制见 [模型适配说明](MODEL_ADAPTERS.md)。

`analysis` 与 `thinking` 分别控制玩家提交的分析、服务的思考参数。每个席位都可独立设置：

| `analysis` | 行为 |
| --- | --- |
| `required` | 每次行动必须通过 `ANALYZE` 提交非空策略分析 |
| `optional` | 可提交，也可省略 |
| `off` | 不接受 `ANALYZE`，提示词也要求不提交 |

在已有的 `[players.red]` 或 `[players.blue]` 配置节中添加 `analysis = "optional"` 即可修改。也可临时使用 `--red-analysis optional --blue-analysis off`。配置同时影响提示词与程序校验；接口返回思考文本不能替代必填的 `ANALYZE`。同一行动多段分析按顺序保留，每次行动重新检查要求；纯聊天不要求分析。省略该字段时，模型使用 `required`，人类使用 `optional`。

- **正文策略分析**：比赛期间仅保留在该玩家自己的历史与私有日志中，整场 BO 结束后公开。
- **接口思考文本**：只写入该玩家私有日志，不发送给对手、不回填后续请求，也不纳入赛后公开材料。

产生冠军或达到总局数上限后，程序先保存比分、公开有效动作与正文分析，再邀请双方赛后聊天。各自保留本场上下文，首次请求补齐尚未收到的公开事件，并提供最终结果与公开分析，结合已保留历史可还原相同的公开比赛记录。后续只追加新的聊天事件和退出状态。双方私有历史不会合并，接口思考不进入公开报告。

赛后只接受 `SAY` 和 `EXIT`，不要求分析或结束行动，也不强制讨论赛况。可以继续发言，也可以先告别再退出：

```json
{"actions":[{"type":"SAY","text":"刚才那局你为什么选布？"}]}
```

```json
{"actions":[{"type":"SAY","text":"下次再玩！"},{"type":"EXIT"}]}
```

`EXIT` 无参数、最多一次且必须放在最后；只想退出可返回 `{"actions":[{"type":"EXIT"}]}`。退出的一方不再被请求，另一方仍可继续发言，直到双方都退出。暂时不发言可返回 `{"actions":[]}`，仍留在聊天中；沉默不发布空消息，也不触发立即重试。人类使用 `/say 文本`、`/exit`，等待输入没有时限，也不需要等模型回复后才能继续发言或退出。

初次同时邀请双方，之后由对手的新发言或退出触发请求；生成期间通知合并，每个玩家最多一个在途请求。最后留在聊天中的模型发言后仍可继续发言、沉默或退出；一旦沉默便停止请求，不因对手已经退出而反复唤醒。不设固定聊天轮数，`analysis=off` 不关闭赛后聊天，聊天也不修改比分。新 BO 清空上一场历史。

双方沉默，或一方退出而另一方沉默时，程序继续等待，不自动替玩家退出。人类仍在场时直接 `/say 文本` 即可继续交流；没有玩家输入或模型请求在途时，终端显示 `赛后控制 >`，输入 `/resume` 邀请仍在场的模型再响应一次。这是操作者控制命令，不发送玩家发言、不恢复已退出席位。模型再次沉默便重新待机；`Ctrl+C` 可中止并保留日志。没有交互终端时保持待机，不自动轮询模型。

每次模型响应最多尝试三次，重试使用同一快照；最终失败时中止赛后聊天、保留已公开消息和比赛结果，不将失败视为玩家主动退出。比赛本身技术异常或用户中止时不开始赛后聊天；模拟模式中的非人类席位使用预设交流脚本。

输出上限对对局和赛后聊天均生效，不要求模型写满。思考可能占用生成额度，实际支持的上限取决于服务；长输出也可能触发超时。模型分析与赛后聊天可能有误，实际动作和结果以裁判记录为准。

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
| `chat.log` | 公开发言、系统消息、结算、比分、赛后公开分析及双方赛后聊天 |
| `thought.log` | 按局号、Tick、响应编号和尝试次数分段的中文阅读版：接口思考、正文分析、动作、赛后聊天及失败状态；聊天请求单独标记 |
| `debug.log` | JSON Lines 格式的输入、响应、结构化事件、用量、耗时、错误与重试记录 |

流式模式增加 `stream_delta`（文本片段）和 `stream_end`（完整或未完成的接收记录）。`thought.log` 连续保留思考的段落，结束后记录状态与用量，不重复打印整段思考。跨分片的已知密钥前缀会暂缓写入以完成脱敏；请求结束时仍不完整的前缀会被遮盖。用量缺失记为 `null`，不视为零。

日志目录按席位固定，记录中仍包含玩家名称和实际请求的模型。更换模型、相同模型或相同昵称不会导致日志覆盖。旧版本日志保持原样，新的调试查看器读取上述 red / blue 目录结构。

“通过校验”不等于“已执行”。同时行动时，若对手在该 Tick 失败，双方暂存的出拳都不执行，已经公开的聊天保留；轮流行动时也保留此前已完成的行动。以公开裁判记录判断实际结果。锁定后被忽略的 MOVE 在私有记录与赛后报告中单独标记为 `ignored_actions`。

同一 Tick 可有多次有效聊天响应，`debug.log` 用 `request_id` 区分，重试沿用同一编号；`phase` 区分 `game`、`chat`、`postmatch`。`request_start.state.request_reasons` 记录赛中开局、轮换、行动结束或对手发言等触发原因。`interaction_applied` 记录赛中实际应用的 Tick、发言事件序号和 `silent`（是否保持沉默），`request_discarded` 标记结算或取消时丢弃的请求。赛后使用局号 0、Tick 0，`postmatch_response` 表示通过校验，`postmatch_applied` 与 `postmatch_event` 记录实际发布的发言和退出，`postmatch_end` 记录是否双方退出或技术中止。

赛后请求的 `request_start.context.request_reasons` 记录首次邀请、新消息、终端恢复或对手离开后继续交流。`postmatch_response` 与 `postmatch_applied` 的 `silent` 标记暂时沉默，`postmatch_idle` / `postmatch_resumed` 记录待机及恢复；`thought.log` 也会明确注明沉默。

每次真实接口请求还记录 `context_usage`，调试窗口显示其概况：

| 字段 | 含义 |
| --- | --- |
| `input_tokens` / `input_tokens_delta` | 本次输入 token 与相邻请求的变化；缺少任一用量时无法计算变化 |
| `peak_input_tokens` | 本场已知的单次最大输入 |
| `known_input_tokens_total` | 本场所有已报告输入的累计值，包含重试、取消及赛后聊天 |
| `missing_usage_requests` | 本场未取得有效输入用量的请求次数 |
| `message_count` / `input_characters` | 本次发送的消息数与正文字符数；不是 token 估算 |

单次输入反映该请求的上下文大小，累计输入反映反复发送历史产生的总用量，不能用累计值判断窗口是否已满。程序只记录服务实际返回的数据，缺失记为 `null`，新 BO 重置统计；没有自动摘要、截断或剩余容量估算。流式中断可能收不到用量尾包，见 [OpenAI 流式事件文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

模型每次新增的状态消息只附带尚未提供的公开事件；上一局结果首次提供后不重复追加，赛后再补齐最终事件与公开分析。同场原始聊天、自己的响应与纠错仍全部保留；每次 HTTP 请求仍携带完整消息历史。因此可以减少重复资料，上下文仍会随交流增长。重试使用固定快照；取消未应用请求时，已提供事件的进度与该次历史一起回退。诊断中的 `request_start` 保留完整裁判视图，实际增量请求见 `http_request`。

日志使用 UTF-8，逐条写入；写入失败会中止比赛。不记录鉴权头，并过滤本次配置的两把 API key。私有日志仍包含模型输入、输出和分析，分享前应检查内容。默认 `logs/` 已被 Git 忽略；自定义目录需自行配置忽略规则。

## 常见问题

| 情况 | 处理方式 |
| --- | --- |
| `No module named model_rps` | 在项目根目录，用同一个虚拟环境的 Python 执行 `-m pip install -e .` |
| 首次运行后没有 `config.toml` | 程序不会自动生成；复制 `config.example.toml` 为 `config.toml`，再填写模型席位的密钥 |
| 找不到配置或密钥为空 | 确认文件已保存、名称不是 `config.toml.txt`，并在项目根目录运行，或用 `--config` 指定路径 |
| HTTP 401 | 核对密钥是否属于当前 `base_url` 对应的服务 |
| 模型或参数不支持 | 核对服务支持的模型标识、思考档位、JSON 输出和输出上限 |
| 请求超时或 JSON 被截断 | 查看私有 `debug.log`，结合服务限制调整超时、思考级别及输出上限 |
| 模拟模式提示思考设置不支持 | 使用 `--no-config --mode demo`，跳过真实配置 |
每次模型交互最多尝试 3 次，格式纠错和网络重试共用额度；HTTP 408、409、429 和 5xx 可重试，其他 HTTP 错误不重试。重试保持该次请求的快照，只请求出错的一方。有行动权限的请求最终失败会中止比赛；仅聊天请求失败则跳过，不扣机会、不判输，之后等新消息、状态通知或行动机会才再次请求。成功的纯聊天可以接着提交行动，不占后续请求的重试额度。人类输入错误只在本地提示，可直接改正后继续。

退出码：`0` 冠军产生且双方退出赛后聊天；`1` 技术或日志异常；`2` 配置错误；`3` 比赛未决且双方退出赛后聊天；`4` 比赛结束但赛后聊天技术中止；`130` 用户中止。赛后聊天失败不改变已确定的比赛结果。

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
│  ├─ commands.py                统一指令、JSON / 斜杠输入解析与阶段校验
│  ├─ runner.py / match.py       单局入口与 BO 比赛
│  ├─ scheduler.py              聊天发布、收件箱、行动权限与请求取消
│  ├─ requests.py               单次交互的校验、超时与重试
│  ├─ players.py                 保留独立上下文的模型玩家与模拟玩家
│  ├─ adapters.py                厂商参数、协议、地址与鉴权方案
│  ├─ transport.py               HTTP 请求、流式生命周期与安全错误
│  ├─ anthropic.py               原生 Messages 响应、思考与 SSE 转换
│  ├─ history.py                 增量公开事件、同场历史与取消回退
│  ├─ human.py                   人类玩家、私有行动草稿与确认提交
│  ├─ human_terminal.py          异步命令输入、输入保护与中断清理
│  ├─ streaming.py               SSE 事件解码与完整响应组装
│  ├─ prompts.py                 对局与赛后聊天提示词
│  ├─ postmatch.py               赛后公开与赛后聊天
│  ├─ logs.py / diagnostics.py   日志与诊断
│  ├─ context_usage.py           同场输入用量及上下文增长记录
│  └─ identities.py              玩家名称与日志目录
└─ tests/                        自动化测试
```

对局和赛后聊天提示词集中在 [prompts.py](src/model_rps/prompts.py)，修改后重启程序生效。`SYSTEM_PROMPT_TEMPLATE` 使用普通字符串，保留 `__MOVE_RULES__` 等占位符；`game_prompt()` 按当前模式组合规则。修改提示词不会自动修改 [game.py](src/model_rps/game.py) 中的裁判逻辑。

以 `-e .` 安装后，修改源文件无需重新安装；依赖声明变化时再执行安装命令。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用标准库 `unittest` 和 HTTPX 模拟传输，不调用真实模型。覆盖裁判规则、同时与轮流行动、独立聊天、通知合并、权限隔离、旧局请求取消、上下文用量、配置、请求重试、日志、思考字段和赛后聊天，以及流式分片、断流、超时和跨分片脱敏。人类玩家测试覆盖任一席位、三种合法规则组合、BO 跨局、草稿与分析隔离、输入期间对手发言、赛后连续聊天与退出，以及 EOF / Ctrl+C 清理。

增量历史测试从双方实际请求还原整场公开报告，检查重试与取消不漏事件，并比较固定对话的新旧请求文本体积；赛后测试覆盖沉默、新消息唤醒、待机恢复及人类退出后的终端交接。

模型适配测试覆盖 profile 选择与覆盖优先级、两席独立配置、各服务思考参数、Claude 原生非流式 / SSE、缓存输入用量、思考隔离、断流与取消，以及 Claude / GLM 混合协议的完整 BO 与赛后聊天。

玩家 profile 测试覆盖人类任一席位、昵称与分析要求、密钥豁免、旧 models 兼容、同名冲突及非法配置；混合流式测试从 CLI 完成 BO 与赛后聊天，核对实际请求和日志中的独立开关及等待时限。

更多离线场景可以通过 `--scenario` 选择，例如：

```powershell
# 独立聊天与轮流行动
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario chat --initiative alternating --bo 3

# 到期出拳
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario deadline

# 观察一局到期平局，退出码为 3（比赛未决）
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario draw --max-games 1

# 结算 Tick 改拳
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --move-policy revisable --scenario settlement
```
