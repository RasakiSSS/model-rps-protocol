# Model RPS

使用 Python 和 CLI（命令行界面），让两个独立的大语言模型通过交流和自主出拳完成一局猜拳博弈。

当前版本 v0.2 提供 DeepSeek 和 GPT 兼容接口适配、TOML 配置、BO 比赛、运行日志和赛后复盘。模型负责策略、交流与复盘，Python 程序负责规则、状态、信息可见性和比赛判定。

详细规则见 [项目协议 v0.2](model-rps-protocol-v0.2.md)。

## 当前进度

裁判规则、Tick 并发调度、响应校验与重试、模拟玩家、模型适配器和 CLI 均已实现。支持五份运行日志、BO 赛制、同场上下文保留、赛后公开复盘和双方独立的思考设置。默认同时行动、首次提交后锁定出拳；新增实验性 alternating 轮流行动模式，原来的允许改拳模式仍可用于同时行动。本地 113 项自动化测试已通过。

两种服务均已验证接受 20,000 token 输出上限。模型决策说明与复盘可能包含事实或判断错误，实际动作与结果以裁判记录为准。输出上限和双方思考级别现在可在配置文件中调整。

2026-10-15 已完成锁定模式真实验证：双方第 1 Tick 提交，GPT 的布胜 DeepSeek 的石头，当 Tick 结算，无额外结算请求；随后双方复盘成功。该场仍快速提交，不能据此推断锁定模式会增加交流或等待。

## 安装与首次运行

在项目根目录打开 PowerShell，使用现有 `.venv`（Python 3.12.6）。首次配置时复制 `config.example.toml` 为 `config.toml`，填写双方 `api_key`。当前工作区已从原密钥文件迁入本地配置，无需再次复制或填写。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m model_rps
```

当前工作区已完成安装。`-e .` 表示以可编辑方式安装，修改源文件后无需重复安装；依赖声明变化时再执行安装命令。

当前本地配置启动真实模型 BO9，使用 `gpt-5.6-sol` 与官方 `deepseek-flash`，双方思考 `high`、请求时限 120 秒、单次输出上限 20,000 token、锁定出拳规则。每次运行创建独立日志目录。无 API 请求的演示使用 `--no-config --mode demo`。

## TOML 配置

程序自动读取**当前工作目录**的 `config.toml`。模板见 [config.example.toml](config.example.toml)，实际配置被 Git 忽略，模板不含密钥。配置用 UTF-8 保存，可以写注释，也可以只保留需要覆盖的字段。

优先级为：**命令行参数 > TOML > 内置默认值**。内置值保持兼容：真实模式、BO1、locked、双方思考 off、60 秒超时、1 秒重试间隔和 20,000 token；模板采用上述当前实验设置。

| 配置节 | 字段 | 含义 |
| --- | --- | --- |
| `[app]` | `mode`、`log_dir` | live/demo 与日志根目录 |
| `[app]` | `timeout_seconds`、`retry_delay_seconds`、`max_output_tokens` | 单次时限、重试间隔、对局与复盘共用输出上限 |
| `[match]` | `move_policy`、`best_of`、`max_games` | 出拳规则、BO 正奇数、含平局的总局数上限 |
| `[match]` | `initiative` | `simultaneous` 同时行动；`alternating` 实验性轮流行动，仅支持 locked |
| `[players.gpt]`、`[players.deepseek]` | `model`、`base_url`、`api_key`、`thinking` | 各自的模型、服务、密钥和思考级别 |
| 同上 | `name`（可选） | 玩家显示名；省略时随模型标识变化 |

省略 `max_games` 时按**最终生效的 BO × 10** 计算；例如模板 BO9 用 `--bo 3` 覆盖后，上限为 30 局。若显式填写了 `max_games`，它保持填写值，并且不得低于目标胜局数。

```powershell
# 临时覆盖 BO 和输出上限，不改配置文件
.\.venv\Scripts\python.exe -m model_rps --bo 3 --max-output-tokens 24000

# 使用另一份本地配置
.\.venv\Scripts\python.exe -m model_rps --config config.local.toml

# 跳过配置进行模拟，不调用模型
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --bo 3
```

`--config` 与 `--no-config` 互斥。默认配置不存在时使用内置值；显式指定的配置不存在则报错。文件存在但语法错误、字段拼错、类型或数值非法时，在请求前退出，不悄悄忽略。`--help` 无需读取配置。

配置中的相对 `log_dir` 相对于**配置文件所在目录**；CLI 的 `--log-dir` 相对于**当前工作目录**。当前仍固定每局 20 Tick、每次最多 3 次尝试及赛后复盘流程，不开放额外规则配置。

## BO 比赛

通过 `--move-policy` 选择整场比赛的规则；一场 BO 中所有局采用同一模式。

| 规则 | `locked`（默认） | `revisable`（对照模式） |
| --- | --- | --- |
| 出拳 | 首次有效 MOVE 锁定 | 最后一次有效 MOVE 生效 |
| 已提交后再次 MOVE | 忽略该 MOVE，保留原拳型和本轮 SAY，不重试 | 允许跨 Tick 修改，以最后一次为准 |
| 每轮行动 | 恰好一个 MOVE 或 WAIT，可附带 SAY | 恰好一个 MOVE 或 WAIT，可附带 SAY |
| 双方提交齐全 | 当前 Tick 全部动作处理后直接结算 | 下一 Tick 为最后行动机会，之后结算 |
| 第 20 Tick 才提交齐全 | 当 Tick 结算，无第 21 Tick | 追加第 21 Tick 结算 |

上表的 Tick 时机适用于同时行动：第 20 Tick 判定尚未出拳者弃权、双方均未出拳则平局；已结束的对局不再请求动作。所有模式都要求每轮恰好一个 MOVE 或 WAIT；只有 SAY、MOVE 与 WAIT 同时出现、多个 MOVE 或多个 WAIT，均使整份响应无效并重试。SAY 可与 WAIT 或 MOVE 同时出现。违规重试耗尽按技术异常处理，不记弃权。

```powershell
# 模拟 BO3，先赢 2 局结束
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --bo 3

# 真实 BO5，先赢 3 局；实际最多进行 50 局
.\.venv\Scripts\python.exe -m model_rps --mode live --bo 5

# 保留原来的改拳规则作对照
.\.venv\Scripts\python.exe -m model_rps --bo 3 --move-policy revisable

# 自定义实际总局数上限
.\.venv\Scripts\python.exe -m model_rps --mode live --bo 3 --max-games 10
```

- `--bo N` 接受任意正奇数，覆盖配置的 `best_of`；目标胜局数为 `N // 2 + 1`。
- 平局不加分，继续下一局；弃权正常计分。
- `--max-games` 包含平局，默认 `10 × N`，不能小于目标胜局数。达到上限无人赢得足够胜局时记为“未决”，保留比分。
- 技术故障立即中止整场比赛，失败局不算胜负或平局。
- **同一场 BO 内保留上下文**：模型记得此前的公开交流、结算拳型、结果、比分，以及自己的响应和决策说明。每局只重置 Tick、出拳及当前局状态。
- 比赛期间双方上下文独立，对手的决策说明和纠错信息不可见；开始新的比赛时清空双方上下文。

## 实验性轮流行动

`initiative = "alternating"` 是行动有先后的实验模式，一次只请求一方；默认仍为 `simultaneous`，不会按 API 返回速度抢先行动。

- 第一局 GPT 席位先手，第二局 DeepSeek 席位先手，此后逐局交换；平局也交换。新比赛重新从第一席位先手开始。
- 一方完整响应通过校验后，立即处理并公开发言和提交状态；另一方下次请求即可看到。拳型仍在结算前保密。
- 每方最多 20 次行动，全局最多 40 个行动 Tick。双方出拳齐全时，在当前方全部动作处理后立即结算，不再请求对手。
- 双方都用完机会后，仅一方未出拳则弃权，双方未出拳则平局。第 39 个 Tick 不提前剥夺后手的第 20 次机会。
- 每位玩家完成自己的第 10、18、19 次行动后，分别提醒剩余 10、2、1 次；重试不占额外行动机会。
- 锁定后的 MOVE 被忽略，仍消耗本次行动机会，SAY 正常公开。日志和复盘以 `ignored_actions` 单独标记，不算实际改拳或 WAIT。
- 只支持 `locked`，与 `revisable` 同时配置会在请求前报错。技术失败中止比赛，保留此前已经公开的行动；不执行失败响应，不继续请求下一方。

```powershell
# 无 API 请求的轮流行动演示
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --initiative alternating --bo 3

# 使用现有模型配置进行真实实验
.\.venv\Scripts\python.exe -m model_rps --initiative alternating --move-policy locked --bo 3
```

同一 BO 的上下文仍保留。日志、输入状态和复盘记录均标注行动模式、每局先手及行动次数。当前完成模拟与自动化验证，尚未进行该模式的真实模型实验。

## 赛后公开与复盘

整场 BO 产生冠军或达到实际总局数上限后，程序自动进行以下步骤：

1. 展示最终比分和比赛结果。
2. 按局号、Tick、玩家顺序公开双方实际执行的动作及其决策说明，改拳模式下包括每次有效改拳。
3. 向两位模型提供相同的完整赛后记录，各自从自己的视角分析双方策略，给出**整体评价、关键决策和改进建议**。
4. 在 CLI 和 `chat.log` 展示双方复盘。

公开的是模型在对局时生成的正文策略分析（沿用字段名 `decision_summary`），不包含接口思考文本。仅返回接口思考时，正文分析记为 null，赛后显示“未提供正文分析”，复盘仍可依据动作和公开事件进行。整个 BO 结束前不会向对手公开正文分析。失败响应、纠错信息和原始 HTTP 数据不公开；成功行动中锁定后被忽略的 MOVE 单独列出，不混入实际执行动作。

复盘使用独立提示和完整比赛记录，双方互相看不到尚未完成的复盘。复盘不执行游戏动作，也不改变已确定的比分。真实模式通常额外请求两次，每位模型失败时最多请求三次，沿用超时设置和配置的单次输出上限。某一方复盘失败仍保留另一方成功的内容，并返回退出码 `4`。

技术异常或用户中止的比赛不自动进入复盘。模拟模式展示明确标注的预设复盘，不发起 API 请求。

## 提示词与自由选择

双方使用相同的规则提示，明确拳型克制关系、系统比分、动作效果和信息可见性。每轮必须选择 MOVE 或 WAIT，可以同时 SAY；一方提交不会独自触发结算，仍未提交的一方可以聊天并等待，直到提交截止。提示按本场出拳模式和行动顺序生成。

提示不要求模型尽快出拳，也不要求为了展示功能而聊天、等待或改拳。分析要求仅为“提供你的策略分析过程，并给出本 Tick 的行动”，不规定步骤、句数或逐项解释。格式示例覆盖 SAY+WAIT、单独 WAIT 和三种 MOVE，不指定默认拳型。复盘仍依据实际记录进行，缺失的分析不补写。提示词不能保证模型完全正确，也不能保证出现更多交流；具体行为需从多场记录中观察。

单次输出上限由 `app.max_output_tokens` 或 `--max-output-tokens` 设置，内置值与模板均为 20,000，对局和复盘共用。它是允许的最大输出量，不要求模型写满，也不代表开启思考模式；实际可用上限取决于所选模型与服务。

## 思考模式

两个开关互相独立，一场 BO 内设置固定，对局与赛后复盘均适用；改变思考设置不会改变游戏规则或提示词。

| 玩家 | 参数 | 可选值 | 内置值 / 模板值 |
| --- | --- | --- | --- |
| GPT | `--openai-thinking` | `off`、`low`、`medium`、`high`、`xhigh`、`max` | `off` / `high` |
| DeepSeek | `--deepseek-thinking` | `off`、`low`、`high`、`max` | `off` / `high` |

```powershell
# 双方关闭，作为对照
.\.venv\Scripts\python.exe -m model_rps --bo 3 --openai-thinking off --deepseek-thinking off

# 只开启 DeepSeek
.\.venv\Scripts\python.exe -m model_rps --bo 3 --deepseek-thinking high --openai-thinking off

# 只开启 GPT
.\.venv\Scripts\python.exe -m model_rps --bo 3 --openai-thinking medium --deepseek-thinking off

# 双方开启，给每次请求最多 120 秒
.\.venv\Scripts\python.exe -m model_rps --bo 3 --deepseek-thinking high --openai-thinking medium --timeout 120
```

DeepSeek 开启时发送 `thinking.type="enabled"` 和所选 `reasoning_effort`；关闭时发送 `thinking.type="disabled"`。GPT 将 `off` 映射为 `reasoning_effort="none"`，其余值直接发送。参数依据 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/) 和 [GPT-6 Luna 模型文档](https://developers.openai.com/api/docs/models/gpt-6-luna)。兼容服务或自定义模型未必支持全部选项；接口拒绝参数时明确报错，不自动关闭思考重试。模拟模式不支持开启思考。

思考会占用生成额度，输出上限不是单独预留给最终 JSON 的额度。长思考也可能触发请求超时；内置值为 60 秒，模板为 120 秒，可手动调整。截断、空内容或无效 JSON 沿用原有重试和技术异常处理。加载当前配置再运行 demo 时，须把双方思考都覆盖为 off，或直接使用 `--no-config --mode demo`。

`decision_summary` 现在承载正文策略分析，不再限制一至三句。当前响应实际返回了非空 `reasoning_content` 或 `reasoning` 文本时，可以省略正文分析，无需重复生成；只有开启思考或返回思考 token 数不足以满足这个条件。没有思考文本且缺少正文分析时，沿用原有纠错重试额度。接口思考只保存到该玩家的私有日志，不进入后续请求、对手上下文或赛后公开报告；同场 BO 的公开历史、自己的最终响应和正文分析仍正常保留。

2026-10-15 已以 DeepSeek `high`、GPT `medium` 完成一次真实对局和双方复盘，日志目录为 `logs/20261015_010235_022230_45bc151d/`。双方首 Tick 出布，达到单局验证上限后比赛未决，双方复盘成功。DeepSeek 两次均返回思考文本；GPT 对局请求未返回思考文本及思考 token 数，复盘请求则返回了相关字段。缺失字段不表示模型没有思考。这次验证确认接口可用，不用于判断策略优劣。

## 运行日志

启动后会打印本次日志目录。默认目录结构：

```text
logs/20261014_153000_唯一编号/
├─ chat.log
├─ gpt-5.6-sol/
│  ├─ thought.log
│  └─ debug.log
└─ deepseek-flash/
   ├─ thought.log
   └─ debug.log
```

`chat.log` 是人类可读的公开过程，包含系统消息、聊天、结算揭晓、比分、比赛结果，以及赛后公开的动作、决策说明和双方复盘。结算前不显示实际拳型，整场比赛结束前不公开决策说明。

`thought.log` 改为直接阅读的中文分段文本，按“第几局 / Tick / 第几次请求”标识，分别展示接口思考、正文策略分析、拟执行动作、复盘结果以及失败或取消状态。保留分析的换行和段落，正文缩进显示，危险控制字符转义。仅使用接口思考时明确标注未提供正文分析；缺少思考 token 数显示“未提供”，实际 0 则显示 0。复盘分为整体评价、关键决策和改进建议。

“通过校验”不代表动作已经执行；同 Tick 对手失败时双方动作都不会执行，以 `chat.log` 的裁判记录为准。正文分析在比赛期间仅进入自己的历史，赛后才公开；接口思考始终不进入公开记录。失败记录显示原因、纠错反馈和是否重试，包括复盘失败。记录立即写入，不等整局或整场结束。

结构化的 `decision`、`reasoning`、`review` 事件现在统一保存在 `debug.log`，无需解析阅读版文本。`decision.summary` 省略正文分析时为 null，`analysis_source` 标明来源。仍然每次运行五份日志。此格式只影响新运行，旧日志不改写；旧 `thought.log` 仍可能是 JSON 行格式。

`kind=reasoning` 单独记录接口返回的思考内容，同时写入该玩家的 `debug.log`。字段包含请求的思考级别、来源字段、文本、接口提供的思考 token 数和结束原因。`status` 为 `returned`（取得文本）、`not_returned`（未返回文本）或 `invalid_field`（返回字段类型不支持）；缺失用量为 null，不能当作 0。思考记录不代表最终动作有效，截断或无效响应也可能已有思考文本。开启思考后取得有效的响应消息时，即使没有思考文本也会记录状态；超时等未取得响应消息的情况由失败事件记录。

`debug.log` 记录该玩家的输入、请求正文、HTTP 状态和响应正文、原始模型输出、接口提供的 token 用量、耗时、错误、重试与取消，包含赛后复盘请求。比赛开始记录 `move_policy` 和双方思考设置；每次 `request_config` 记录模型、请求的思考级别与输出上限。游戏快照包含模式及自己的锁定状态，复盘记录也包含比赛模式。该文件每行一个 JSON 对象，用局号、Tick、请求次数区分；请求记录的 `phase` 为 `game` 或 `review`。复盘请求的局号和 Tick 均为 `0`，不增加比赛局数或 Tick。用量缺失记录为 null，模拟模式不产生 HTTP 事件。

日志使用 UTF-8，每条记录立即 flush，异常退出时保留已经产生的记录。创建或写入失败会停止比赛。不保存鉴权头，并过滤本次运行的两个 API key。默认 `logs/` 目录不纳入 Git；自定义日志目录时需自行设置对应忽略规则。

使用 `--log-dir 路径` 更改日志根目录，每次运行仍创建新子目录。

## 模拟场景

```powershell
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario deadline
```

| `--scenario` | 展示内容 |
| --- | --- |
| `locked` | 锁定模式默认场景：模拟玩家1先提交，下一 Tick 继续发言，模拟玩家2提交后当刻结算 |
| `change` | 改拳模式默认场景：出拳后修改，按最终拳型判定；需 `--move-policy revisable` |
| `settlement` | 第 1 Tick 双方出石头；第 2 Tick 结算刻模拟玩家1改为布并获胜；需 `--move-policy revisable` |
| `draw` | 双方一直等待，每局 20 Tick 后平局；反复开局，直至比赛达到实际总局数上限 |
| `forfeit` | 仅模拟玩家1出拳，模拟玩家2到期弃权 |
| `deadline` | 第 20 Tick 双方出拳：锁定模式当刻结算，改拳模式第 21 Tick 结算 |
| `retry` | WAIT 与 MOVE 同时出现被拒绝，在同一 Tick 纠正后完成对局 |

未指定 `--scenario` 时随模式选择默认场景；锁定模式指定 `change` 或 `settlement` 会在运行前报配置错误。模拟场景在每局重新开始，会重复相同结果。只想观察一局平局时，可运行 `--no-config --mode demo --scenario draw --max-games 1`，最终比赛状态为未决。`--scenario` 只用于模拟模式，不会自动切换运行模式。

## 接入真实模型

优先在 `config.toml` 的两个玩家节中填写各自的 `api_key`，并让密钥、模型标识与服务地址对应。配置不纳入 Git。程序不会输出密钥，也不把完整配置写入日志。

兼容旧版的 `local_secrets.py`，格式仍为两个字符串常量：

```python
DEEPSEEK_API_KEY = "你的 DeepSeek API key"
OPENAI_API_KEY = "你的 OpenAI API key"
```

只有 TOML 中双方都未填写 `api_key` 字段时，才回退读取该文件；写了空字符串或只填一方会报错，不混用两个来源。显式 `--secrets-file 路径` 可以用指定旧文件的两把密钥覆盖 TOML 密钥，其他配置照常生效。旧文件同样被 Git 忽略；程序只读取这两个常量，不执行其中其他 Python 语句。

```powershell
.\.venv\Scripts\python.exe -m model_rps --mode live
```

`live` 是默认模式，会向两个服务发起实际 API 请求，因此上面的 `--mode live` 可以省略。密钥缺失、为空或格式不正确时，程序会在发起请求前退出。

| 玩家 | 模板 API 基础地址 | 模板模型标识 |
| --- | --- | --- |
| GPT | `https://ai.yuhuantin.icu/v1` | `gpt-5.6-sol` |
| DeepSeek V4.1 Flash | `https://api.deepseek.com` | `deepseek-flash` |

玩家身份使用配置的 `name`，省略时使用实际模型标识，例如 `gpt-5.6-sol` 与 `deepseek-flash`。模型输入的 `player`、历史事件、胜者、比分键、赛后报告，以及终端和新日志统一使用这些名称。例如比分显示为 `gpt-5.6-sol 2 : 1 deepseek-flash`。旧日志保留原样，阅读旧记录时以该场 `match_start.players` 为准。未配置 GPT 模型时仍使用内置的 `gpt-6-luna`。

未指定显示名时，自定义模型标识会同步更新身份和日志目录；也可以用 `--openai-name`、`--deepseek-name` 覆盖显示名。含路径分隔符等不适用于文件名的字符时，仅目录名作安全转换。双方显示名相同则附加“玩家1/玩家2”区分；模拟模式使用“模拟玩家1/模拟玩家2”。玩家自己生成的发言、决策说明与 HTTP 原文不作名称替换。

DeepSeek 使用官方服务，GPT 使用用户指定的兼容服务；每个玩家分别使用对应服务的密钥。现有适配器已完成真实对局验证，本次配置改动通过模拟 HTTP 验证，没有额外发起付费请求。

此前第三方 DeepSeek 在真实联调中出现超时和缺少 JSON 结尾的响应，因此改用官方服务。原服务使用的 DeepSeek 标识为 `deepseek-v4.1-flash`；切换地址时必须同时匹配服务的模型标识和密钥，不能仅替换地址。当前本地密钥文件保留原密钥为 `DEEPSEEK_GATEWAY_API_KEY`，程序不读取该备份字段。

程序自动在基础地址后追加 `/chat/completions`。可以通过 `--deepseek-base-url`、`--openai-base-url`、`--deepseek-model`、`--openai-model` 覆盖地址或模型标识。覆盖模型名称仍使用该玩家的参数方案；适配不兼容的模型时，需要在 `players.py` 中调整 `ModelSpec` 或新增实现 `MatchPlayer` 的适配器，包含对局响应、比赛上下文管理与赛后复盘。

双方按配置发送思考参数；具体映射见上方“思考模式”。两者都请求 JSON 输出，对局和复盘共用 `max_output_tokens`。响应正文必须包含非空 `actions` 列表；未返回接口思考文本时还必须包含非空 `decision_summary`。如提供该字段，仍须是非空字符串。思考文本不能替代动作 JSON，不改变动作校验、截断处理或重试次数。调用方式依据 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 和 [OpenAI Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

## 超时、重试与退出状态

- 每次请求总时限由 `app.timeout_seconds` 设置，内置 60 秒、模板 120 秒，可用 `--timeout` 覆盖。
- 每位玩家每个 Tick 最多请求 3 次，网络错误和格式纠错共用额度。
- 真实模式重试间隔由 `app.retry_delay_seconds` 设置，内置 1 秒，可用 `--retry-delay` 覆盖；模拟模式不增加等待时间。
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
├─ config.example.toml          可提交的配置模板，不含密钥
├─ config.toml                  本地运行配置与 API key，不纳入 Git
├─ local_secrets.py             旧版密钥文件，兼容保留且不纳入 Git
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
│     ├─ identities.py          模型身份显示与安全日志目录
│     ├─ config.py              TOML 读取、合并与启动前校验
│     ├─ players.py             模型接入与响应处理
│     └─ cli.py                 命令行入口与事件展示
└─ tests/
   ├─ test_game.py              裁判、动作校验、可见性和到期判定
   ├─ test_runner.py            并发、重试、超时与取消
   ├─ test_players.py           模拟 HTTP 请求与模型响应处理
   ├─ test_cli.py               命令行场景、密钥读取和展示
   ├─ test_config.py            配置优先级、校验、密钥来源与请求集成
   ├─ test_match.py             BO 判定、跨局记忆和新比赛重置
   ├─ test_logs.py              日志内容、脱敏、取消与写入故障
   ├─ test_postmatch.py         赛后公开、复盘输入、重试和故障隔离
   ├─ test_locked.py            锁定规则、违规纠错、即时结算与模式切换
   ├─ test_alternating.py       轮流行动、先手轮换、每方上限、失败与复盘
   ├─ test_thinking.py          思考参数、日志隔离、缺失字段与失败处理
   └─ test_identities.py        模型名、跨局历史、复盘与日志一致性
```

## 运行测试

测试使用 Python 标准库 `unittest`；模型请求通过 HTTPX 的模拟传输验证，不需要真实密钥。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

核心覆盖：两种出拳模式的九种拳型组合、锁定后忽略 MOVE 但保留 SAY、每轮恰好一个 MOVE/WAIT、聊天并等待、当 Tick 结算、模式切换与日志，跨 Tick 与结算刻改拳、结算前保密、10/18/19 提醒、到期弃权或平局、第 20/21 Tick 边界、整份响应拒绝、同时行动的信息隔离、结算刻重试与故障、HTTP 错误、请求取消、BO 比赛、同场记忆、新比赛清空、日志脱敏，以及赛后公开与复盘失败隔离。

轮流行动测试覆盖双方先手的九种结果、后手即时可见信息、每方 20 次机会与 40 Tick 边界、平局后交换先手、重试不增加行动次数、故障保留此前行动、超时与取消不启动下一方、TOML/CLI 配置，以及真实模式模拟 HTTP 的 BO 历史、忽略动作、日志与复盘链路。

思考测试覆盖双方关闭、仅一方开启、双方开启，所有级别的请求参数，两种出拳规则及复盘，私有思考不进入后续上下文，缺失字段与用量、截断、无效 JSON、超时、参数拒绝和日志写入失败。

分析测试覆盖原生思考省略正文分析、正文分析回退、仅有思考 token 数或无效思考字段、重试之间不得沿用思考可用性，以及缺少正文分析时的完整 BO、日志和赛后复盘。

配置测试覆盖读取与跳过配置、显式文件缺失、CLI 覆盖、相对路径、BO 派生上限、非法字段与类型、空或缺失密钥、旧密钥文件覆盖、模型显示名，以及 BO 历史、复盘、请求参数和日志的完整链路。测试隔离工作区配置，不调用真实模型。
