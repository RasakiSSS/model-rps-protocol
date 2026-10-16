# Model RPS — 项目协议 v0.2

当前程序版本：v0.4.0。支持 Red / Blue 通用席位、同模型对战、独立调试窗口，以及默认同时行动与实验性轮流行动、锁定出拳与可选改拳模式、BO 比赛、同场上下文保留、日志、赛后公开复盘、双方独立思考设置、TOML 配置与可选流式响应。

最后更新：2026-10-16。

## 1. 项目目标与本版范围

使用 Python 和 CLI（命令行界面），让两个独立的 LLM（大语言模型）通过交流和自主出拳完成一局猜拳博弈。

本版支持本地启动、两个模型接入、同时与轮流行动、动作校验、出拳保密、运行日志、BO 比赛、赛后复盘和可选思考模式。回放工具、图形界面留待后续版本。

| 参与者 | 当前选择 | 职责 |
| --- | --- | --- |
| Red | 模板为 GPT-5.6 Sol，可配置为任一受支持模型 | 根据可见信息交流、等待、出拳；仅改拳模式允许修改 |
| Blue | 模板为 DeepSeek Flash，也可与 Red 使用相同模型 | 根据可见信息交流、等待、出拳；仅改拳模式允许修改 |
| System | Python 裁判与调度器 | 管理规则、请求、状态、信息可见性和结果判定 |

模型负责策略与交流，System 负责规则和判定。双方使用各自独立的上下文。

玩家对外身份固定带有 Red / Blue 标签，后接配置的 name，省略时使用实际模型标识。模型输入、历史事件、比分、胜者、复盘与新日志均使用该名称；模型名和昵称相同也不会混淆。每个席位独立选择 adapter、密钥、服务地址、模型及思考设置，不按席位推断厂商。历史日志不改写，以各场 `match_start.players` 为准。模拟模式显示为 Red · 模拟玩家1、Blue · 模拟玩家2。

## 2. 出拳模式与 Tick 流程

Tick 从 1 开始。默认 `initiative=simultaneous` 时，一个 Tick 是双方同时行动的周期；实验性 `initiative=alternating` 时，一个 Tick 是当前一方的一次行动。`move_policy=locked` 为默认出拳规则；revisable 允许跨 Tick 改拳，只适用于 simultaneous。一场 BO 内这两项设置固定。

simultaneous 每个 Tick 的流程：

1. 根据开始状态冻结双方各自可见的快照，同时请求双方。
2. 解析完整响应，校验 JSON、动作格式和当前模式下的合法性；只重试出错的一方。
3. 双方均有效后，按各自返回顺序执行所有动作，再判定是否结束。
4. 公开允许展示的事件；结束时才揭晓拳型。未结束则进入下一 Tick。

同 Tick 的新输出不能提前提供给另一方。若某一方失败，整刻暂存动作均不执行；重试仍用同一份开始状态。

alternating 每次只请求当前一方，完整响应通过校验后立即处理并公开，再请求另一方。后手可看到先手已公开的发言与提交状态，仍看不到拳型。双方出拳齐全时，在当前完整响应处理后立即结算，不额外请求对手。第一局 Red 先手，之后逐局交换先手，包括平局；新 BO 从第一席位先手重新开始。重试保持同一快照且不消耗额外行动次数。技术失败中止比赛，保留之前已经完成的行动。

以下出拳与结算表中的第 20/21 Tick 指 simultaneous；alternating 的机会与截止规则见第 4 节。

| 行为 | locked（默认） | revisable |
| --- | --- | --- |
| 首次 MOVE | 锁定本局拳型 | 建立当前拳型，之后可改 |
| 已提交后再次 MOVE | 忽略，保留原拳型与本轮 SAY，不重试 | 跨 Tick 允许，以最后一次为准 |
| 单响应多个 MOVE | 整份拒绝，不执行第一个 MOVE | 整份拒绝，不执行第一个 MOVE |
| 双方均已提交 | 当前 Tick 所有动作处理完后结算 | 预定下一 Tick 为结算刻 |
| 独立结算 Tick | 无 | 可以最后一次 SAY、ANALYZE、MOVE，并以 ENDTURN 结束；之后必定结算 |
| 第 20 Tick 才提交齐全 | 当刻结算，不追加 Tick | 追加第 21 Tick 结算 |

## 3. 统一玩家指令

模型 JSON 与未来的人类斜杠输入均转换为 `commands.py` 中的 Command，再经过相同的阶段与分析校验。当前提供斜杠解析能力，尚未接入人类终端或自由聊天调度。

| 意图 | JSON 指令 | 斜杠输入 | 语义 |
| --- | --- | --- | --- |
| 发言 | `{"type":"SAY","text":"内容"}` | `/say 内容` | 公开聊天，不代表出拳或结束行动 |
| 分析 | `{"type":"ANALYZE","text":"内容"}` | `/analyze 内容` | 私有策略分析，整场 BO 后公开 |
| 出拳 | `{"type":"MOVE","move":"Rock"}` | `/move Rock` | 提交拳型；JSON 大小写固定，斜杠参数不区分大小写 |
| 结束行动 | `{"type":"ENDTURN"}` | `/endturn` | 本次无 MOVE 时视为等待，保留已有拳型 |
| 复盘 | `{"type":"REVIEW","text":"内容"}` | `/review 内容` | 仅用于赛后，接受自由文本 |

文本必须是非空字符串；move 只能为 Rock、Paper、Scissors。拒绝额外字段、未知指令、重复 JSON 键和非标准常量。旧 decision_summary 字段与显式 WAIT 指令不再接受。

### 3.1 完整行动

当前 Tick 调度器要求每次完整响应以唯一的 ENDTURN 结束，最多一个 MOVE；可以有多条 SAY、ANALYZE。ENDTURN 后不能再有指令。未返回 MOVE 且明确 ENDTURN 才算等待；纯聊天指令本身合法，但不是完整行动响应，现阶段会要求补全并重试，不能隐式扣除行动机会。空响应、超时、断流和非法输出也不是等待。

完整 JSON 通过校验后，SAY 与 MOVE 按返回顺序执行；ANALYZE 仅记录，不进入公开事件。整场比赛结束后才公开已完成行动的分析。同一行动的多条分析按顺序以空行分隔保存，每次重新检查要求。指令层将无 MOVE 的 ENDTURN 转为裁判内部的 WAIT 动作；这是内部等待结果，不是玩家输入指令。

### 3.2 提交与锁定

MOVE 执行时即提交，locked 下首次有效 MOVE 锁定；ENDTURN 仅表示行动结束。当前完整响应校验与 Tick 执行边界保持不变，不会提前执行流式片段或等待中的部分响应。未来即时输入需要在后续事件调度中接入。

locked 下后续行动中的 MOVE（含相同拳型）只被忽略，保留原拳型、同行动的发言和分析，不重试，不伪记为等待；结束该行动仍消耗一次机会。单响应多个 MOVE 是格式错误，整份拒绝，不先执行第一个 MOVE。

revisable 允许跨 Tick 改拳，以最后一次有效 MOVE 为准；双方提交齐全后的下一 Tick 仍可改拳，结束后结算，不因修改延期。alternating 仅支持 locked。

### 3.3 分析配置

`players.red.analysis`、`players.blue.analysis` 支持 `off`、`optional`、`required`（默认）。off 不接受 ANALYZE；optional 允许省略；required 每次完整行动至少一条非空 ANALYZE。CLI 对应 `--red-analysis`、`--blue-analysis`。

提示词与校验使用同一席位配置。接口思考文本独立存入私有日志，不能替代 ANALYZE；分析配置不控制 thinking，也不约束赛后 REVIEW。缺少必填分析或关闭时仍提交分析，均反馈重试，不执行部分指令。

## 4. 倒计时与结果判定

simultaneous 常规提交截止为第 20 Tick。仍缺少任一方出拳时，第 10、18、19 Tick 结束分别提醒剩余 10、2、1 Tick；提示进入 CLI 及下一 Tick 双方快照。revisable 预定结算后不再发布常规提醒。

locked 双方提交齐全的 Tick 结束时直接比较已锁定拳型；revisable 在预定结算 Tick 全部动作处理完后比较最终拳型。必须先处理本 Tick 的完整响应，不能在某个 MOVE 中途提前结束；simultaneous 处理双方响应，alternating 处理当前方响应。

simultaneous 第 20 Tick 仍是完整行动机会，先校验和执行动作再判定：

- 双方出拳齐全：locked 当刻结算；revisable 若尚未进入结算刻则追加第 21 Tick，否则当刻结算。
- 仅一方未出拳：该方弃权，已出拳者胜。
- 双方均未出拳：平局，原因为双方到期未出拳。

alternating 每方最多 20 次行动，全局最多 40 个行动 Tick。每位玩家完成自己的第 10、18、19 次行动后分别提醒剩余 10、2、1 次，提醒带对应玩家身份；已结束不提醒。双方提交齐全则提前结算，否则双方用完机会后才判定弃权或平局。第 39 个 Tick 不提前结束，保证后手也拥有完整的第 20 次行动机会。重试不增加次数。

结束后公布已提交的拳型并停止游戏动作请求。技术异常不算弃权或平局，不揭晓暂存或此前保密的拳型；已完成 Tick 的状态保留。

## 5. 信息可见性

System 完整状态、Player View（模型视图）和 CLI View（人类观察视图）分别处理。

| 信息 | System | Player View | CLI View |
| --- | --- | --- | --- |
| 当前 Tick、上限和系统提醒 | 可见 | 双方可见 | 可见 |
| 已公开的 SAY 消息 | 可见 | 双方可见 | 可见 |
| 双方是否已有有效出拳 | 可见 | 双方可见 | 可见 |
| 结算前的具体拳型 | 双方拳型均可见 | 仅自己的当前拳型可见 | 不显示 |
| 正常结算时的最终拳型 | 可见 | 结算时公开 | 公开 |
| 模型的响应纠错信息 | 可见 | 仅提供给出错的一方 | 可显示错误概况，不泄露拳型 |
| 策略分析 | 可见 | 比赛期间仅自己的可见；整场比赛结束后公开已执行动作的说明 | 整场比赛结束后公开 |
| 接口额外返回的思考文本 | 保存到对应玩家私有日志 | 不追加到后续请求，不提供给对手 | 不显示 |

提交状态可见性固定在代码中，不提供配置开关。新提交状态只在当前 Tick 完整响应处理后公开：simultaneous 等双方完成，alternating 当前方完成后即可公开。锁定后忽略的 MOVE 只公开忽略状态，不公开尝试的拳型。

System 不会在结算前公开真实拳型；模型自行通过 SAY 表达的拳型属于玩家发言，System 不提前确认其真假。

## 6. CLI 展示原则

- CLI 负责展示游戏事件，不决定程序内部请求和动作的执行顺序。
- 前台展示玩家发言、提交状态、倒计时、结算刻提示、错误概况及最终结果。revisable 的结算刻单独标记“最后一次行动机会”，包含可能追加的第 21 Tick；locked 无此独立阶段。启动时展示本场模式。
- 出拳或改拳时只显示状态事件；具体拳型留到结算时公开。
- 允许同一个模型在一个 Tick 中连续产生多条发言。
- 分别展示正常胜负、相同拳型平局、到期弃权、双方到期未出拳平局和技术异常中止。
- 整场比赛正常结束后，按局号、Tick、玩家公开实际执行的动作与策略分析，再展示双方赛后复盘。

## 7. 响应校验、超时与重试

- 完整校验响应后才执行动作。解析失败、指令字段无效、分析不符合配置或未按规定结束行动时，整份响应作废。格式合法的锁定后 MOVE 只被忽略，不重试，SAY 正常执行。
- 格式或动作规则错误反馈给出错的一方，让其重新生成；请求超时或可重试的 API 失败也采用有限重试。
- 每位玩家每个 Tick 最多请求 **3 次**：首次请求加最多 2 次重试。超时、API 失败和响应纠错共用这个额度。
- 重试发生在当前 Tick 内，不增加 Tick 编号，不消耗新的游戏 Tick。
- 只重试出错的一方，成功一方的响应暂存，不重复请求。
- 重试使用同一份 Tick 开始状态；可以补充该玩家自己的纠错信息，但不能加入对手本 Tick 的新输出。
- 重试额度耗尽时，中止本局并记录技术错误。simultaneous 丢弃本 Tick 双方暂存动作；alternating 不执行当前失败响应，也不再请求下一方。两者都保留已完成 Tick 的状态。

单次请求总时限由 `app.timeout_seconds` 或 CLI `--timeout` 设置，内置值 60 秒、配置模板 120 秒；真实模式重试间隔由 `app.retry_delay_seconds` 或 `--retry-delay` 设置，内置值 1 秒，模拟模式固定 0 秒。HTTP 408、409、429 和 5xx 可重试，其他 HTTP 错误立即中止本局。HTTP 客户端不额外进行自动重试，避免超过协议规定的请求额度。

## 8. 运行环境与模型接入

- 使用工作区现有 `.venv`，已验证 Python 版本为 3.12.6。
- 游戏规则与模型接入分开，使以后添加模型时能够复用裁判和 Tick 流程。
- API key 保存在本地 `config.toml`，通过 `.gitignore` 排除；保留 `local_secrets.py` 兼容读取，来源规则见第 17 节。
- `.venv`、本地密钥和 Python 生成文件不纳入 Git 版本管理。

| 玩家 | 模板模型标识 | 未配置时的内置标识 |
| --- | --- | --- |
| DeepSeek | `deepseek-flash` | `deepseek-flash` |
| GPT | `gpt-5.6-sol` | `gpt-6-luna` |

模板中 DeepSeek 使用官方服务 `https://api.deepseek.com` 和官方密钥；GPT 使用用户指定的兼容服务 `https://ai.yuhuantin.icu/v1` 和对应密钥。此前第三方 DeepSeek 出现超时和 JSON 内容截断，已按用户授权切换官方服务。TOML 与 CLI 支持设置各自的基础地址和模型标识，切换服务时必须使用对应密钥，并确认服务支持相应请求参数；具体方法见 [README](README.md)。

## 9. 项目结构

以下模块已经实现。游戏规则、调度、模型适配和 CLI 分别维护。

```text
model-rps-protocol/
├─ README.md                    项目介绍、安装与启动说明
├─ model-rps-protocol-v0.2.md    本协议
├─ pyproject.toml               Python 项目信息与依赖
├─ .gitignore                   Git 忽略规则
├─ config.example.toml          不含密钥的配置模板
├─ config.toml                  本地运行配置与密钥，不纳入 Git
├─ local_secrets.py             旧版密钥文件，兼容保留且不纳入 Git
├─ .venv/                       本地 Python 环境，不纳入 Git
├─ logs/                        每次运行的五份日志，不纳入 Git
├─ src/
│  └─ model_rps/
│     ├─ __init__.py            Python 包标记与说明
│     ├─ __main__.py            python -m model_rps 入口
│     ├─ commands.py            指令、输入解析与阶段校验
│     ├─ game.py                状态、动作执行与结果判定
│     ├─ runner.py              Tick 推进、并发请求与重试协调
│     ├─ match.py               BO 比赛与同场上下文管理
│     ├─ postmatch.py           赛后记录与沿用上下文的复盘
│     ├─ diagnostics.py         并发请求的诊断事件
│     ├─ logs.py                日志保存、脱敏与资源管理
│     ├─ identities.py          红蓝显示身份与固定席位目录
│     ├─ player_factory.py      模型配置到独立玩家实例
│     ├─ console.py / terminal.py 前台展示与终端颜色
│     ├─ debug_console.py       独立只读调试窗口
│     ├─ config.py              TOML 读取、合并与严格校验
│     ├─ players.py             模型接入与响应处理
│     ├─ prompts.py             统一维护对局与复盘提示词
│     └─ cli.py                 命令行入口与比赛组装
└─ tests/
   ├─ test_game.py              裁判规则与响应校验
   ├─ test_runner.py            Tick 调度、重试和取消
   ├─ test_players.py           模型接口与模拟 HTTP 验证
   ├─ test_cli.py               CLI 运行与展示
   ├─ test_config.py            配置优先级、密钥来源与请求集成
   ├─ test_match.py             BO 结果、跨局记忆与新比赛重置
   ├─ test_logs.py              日志、脱敏与异常处理
   ├─ test_postmatch.py         赛后公开、复盘与故障隔离
   ├─ test_locked.py            锁定规则、即时结算、纠错与模式切换
   ├─ test_alternating.py       轮流行动、先手交换、截止与完整 CLI 链路
   ├─ test_thinking.py          思考参数、日志隔离与异常处理
   └─ test_identities.py        模型身份、历史、复盘与日志对应
```

## 10. 单局验收标准

- 两个模型能够在本地完成一局，CLI 能展示交流和结束原因。
- simultaneous 同一 Tick 双方互相看不到新输出；alternating 当前行动完成后立即公开给下一方。重试都不推进状态。
- revisable 双方提交后至少再进行一个结算 Tick；结算刻的有效改拳覆盖原出拳，全部动作处理完后才判定结果，不再次推迟。
- 结算前不会向对手或 CLI 暴露 System 保存的真实拳型。
- 正确判定九种拳型组合，以及单方弃权和双方到期未出拳的情况。
- 对尚未预定结算刻的对局，准确在第 10、18、19 Tick 结束时提醒。
- 第 20 Tick 有效动作先执行；locked 提交齐全当刻结算，revisable 尚非结算刻时可追加第 21 Tick；仍缺出拳时判定弃权或平局。
- locked 首次提交后再次 MOVE 被忽略但保留 SAY；同响应多个 MOVE 全部拒绝。双方齐全的 Tick 全部动作处理完后立即结算。
- 每次最多一个 MOVE，以唯一 ENDTURN 结束，可同时 SAY、ANALYZE；不合法响应触发纠错，不产生部分动作或消耗额外行动机会。
- alternating 每局交换先手，每方最多 20 次机会；验证最后一次机会、公平计数、错误中止与既有行动保留。
- 请求失败受重试上限约束，耗尽后以技术异常中止，不无限等待。

## 11. 模型输入与输出约定

### 11.1 输入状态

每场比赛以规则提示开始；每次请求保留本席位之前的 user/assistant 历史，再追加当前 JSON 状态。对局消息含 `phase="game"`、`state`、`correction`，BO 调度时另有 `match`。提示词在 `prompts.py`，`game_prompt()` 按出拳规则、行动模式与分析配置组合；`REVIEW_PROMPT` 仅为一句邀请，不另起对话。

提示词只包含比赛目标、信息边界、模式规则与统一输出约束，不规定分析步骤或策略。修改提示词不能改变 `commands.py` 的指令校验或 `game.py` 的裁判规则。

| `state` 字段 | 含义 |
| --- | --- |
| `player` | 自己的席位与名称，如 Red · gpt-6-luna 或 Blue · gpt-6-luna |
| `move_policy` | 本场模式 locked 或 revisable |
| `own_move_locked` | locked 模式已提交时为 true，其余为 false |
| `initiative` | simultaneous 同时行动，alternating 轮流行动 |
| `first_player` | alternating 本局先手的席位与名称；simultaneous 为 null |
| `own_turn` | 当前请求是自己的第几次行动；重试保持不变 |
| `max_turns_per_player` | 每方行动上限；alternating 为 20，同时改拳模式预定结算后随结算 Tick 更新 |
| `remaining_turns_including_current` | 自己含本次的剩余行动机会 |
| `tick` | 全局 Tick；同时行动常规 1 至 20、特殊结算刻可为 21；轮流行动 1 至 40 |
| `max_ticks` | 当前已知的终止 Tick；同时行动为 20 或 `settlement_tick`，轮流行动为 40 |
| `submission_deadline_tick` | 同时行动为 20，轮流行动为 40；后者每方均有 20 次机会 |
| `settlement_tick` | revisable 预定的结算 Tick，尚未预定或 locked 时为 null |
| `is_settlement_tick` | 是否为 revisable 独立结算刻；locked 始终 false，不代表本 Tick 不会结束 |
| `remaining_ticks_including_current` | 包含当前 Tick、到当前已知终止 Tick 的剩余次数；结算刻为 1 |
| `own_move` | 自己的当前拳型，未提交时为 `null` |
| `opponent_committed` | 对手是否已在此前 Tick 提交出拳 |
| `history` | 此前完成 Tick 的公开事件，包括发言、提交或修改状态、WAIT、倒计时和结算刻提示 |

每条历史事件包含 `tick`、`kind`、`player` 和 `text`。结算前的提交或改拳事件不包含拳型。系统消息的 `player` 为 `null`。

`correction` 在每个 Tick 的首次请求时为 `null`；重试时为仅提供给该玩家的错误反馈。历史中保留该玩家已收到的反馈和自己返回的文本，包括随后被判为格式无效的响应；无效动作仍不执行。网络失败且未取得完整文本时，不向对话追加未配对的用户消息。不会把对手的原始响应、私有策略分析或错误反馈混入上下文。

`match` 包含当前局号 `game`、`best_of`、`target_wins`、`max_games`、出拳模式 `move_policy`、行动模式 `initiative`、本局先手 `first_player`、累计胜局 `wins`（以席位与名称为键）、平局数 `draws`，以及上一局的公开记录 `last_game`。第一局的 `last_game` 为 null，此后包含上一局局号、公开事件（包括结算揭晓的拳型）、结束原因和胜者。更早各局的信息保留在对话历史中。当前局的 `state.history` 仍只包含当前局已完成 Tick 的公开事件。

### 11.2 输出格式

顶层仅允许 actions 非空数组，对局示例：

```json
{"actions":[{"type":"ANALYZE","text":"先提交选择，同时观察交流。"},{"type":"MOVE","move":"Rock"},{"type":"SAY","text":"我决定好了。"},{"type":"ENDTURN"}]}
```

等待示例（分析 required）：

```json
{"actions":[{"type":"ANALYZE","text":"等待对手进一步表态。"},{"type":"ENDTURN"}]}
```

analysis 为 off 或 optional 时，单独 `{"actions":[{"type":"ENDTURN"}]}` 也是有效行动。phase 为 review 时只允许一条 REVIEW，见第 15 节。

### 11.3 接口参数

仍使用 Chat Completions 的 JSON 对象输出。两席独立设置 adapter、model、base_url、api_key、thinking；不根据名称猜测参数。max_output_tokens 内置与模板均为 20,000，覆盖对局与复盘，实际参数名由适配器决定。服务不支持参数时明确报错，不自动降级。

## 12. BO 比赛与上下文生命周期

一次启动运行一场比赛，一场比赛包含多局，每局沿用第 2 至 7 节的规则。

通过 `python -m model_rps` 或安装后的 `model-rps` 命令启动时，自动读取工作目录的 `config.toml`。内置和模板均采用真实模型与 locked 规则；可用 `--move-policy revisable` 启用改拳对照模式。配置 `app.mode="demo"` 或 CLI `--mode demo` 使用模拟玩家，并要求双方思考为 off；`--no-config --mode demo` 可直接演示。`--scenario` 仅选择模拟场景，不改变运行模式。

- `--bo N`：N 必须是正奇数，覆盖 `match.best_of`；内置值 1、模板值 9。先获得 `N // 2 + 1` 胜的一方赢得比赛，立即停止对局，不再请求游戏动作，进入赛后公开与复盘。
- 平局计入实际局数，不增加双方胜局数，继续下一局。
- 弃权产生的胜局正常计分；技术异常不记胜负、不算平局，立即中止整场比赛并保留此前比分。
- `--max-games M`：包含平局的实际总局数上限，默认 `10 × N`，不得小于所需胜局数。最后一局先正常判定；若达到目标胜局数，正常产生冠军，否则记为比赛“未决”，即使某方暂时领先也不能判其赢得比赛。
- 每场比赛创建两位玩家，并明确清空其对话历史；同场比赛的所有局复用这两位玩家。
- 每局创建全新的 GameState，继承本场固定的 move_policy。Tick 从 1 开始，出拳清空，当前局的 history 为空，首次请求的当前纠错信息为空。
- **同一场 BO 内保留上下文**：模型能够记住之前各局的公开交流、最终拳型、结果、比分，以及自己的响应、策略分析和纠错历史，并据此调整策略。
- 下一局开始时向双方提供上一局的公开结果；当前 Tick 的对手新输出仍须等双方处理完后才能公开。比赛期间对手的策略分析不可见，错误反馈始终不向对手提供。
- **新比赛清空上下文**，即使程序内部复用玩家对象，也必须清空上一场比赛的历史。日志只供人类查看，不读取日志来构建上下文。
- 当前保留完整对话历史，不自动截断或摘要；若达到服务的上下文限制，按技术异常处理，不静默清除记忆。`max_output_tokens` 是单次输出上限，不是整场上下文上限。
- 默认模拟场景每局重新开始，因此确定性场景会重复相同结果。

## 13. 日志

每次启动默认在 `logs/时间戳_唯一编号/` 下创建五份 UTF-8 日志。可通过 `--log-dir` 指定日志根目录，每次运行都使用新子目录，不覆盖旧记录。

| 文件 | 内容与可见性 |
| --- | --- |
| `chat.log` | 人类可读的 CLI 事件、系统消息、局号与 Tick、每局结果、累计比分、比赛结果、赛后公开记录及双方复盘；结算前不展示实际拳型，整场比赛结束前不展示策略分析 |
| `red/thought.log` | Red 的有效动作与正文策略分析、成功复盘，以及接口思考内容或缺失状态；未提交 ANALYZE 时内部 summary 为 null，失败或取消另记状态 |
| `red/debug.log` | Red 的输入视图、纠错反馈、HTTP 请求正文、HTTP 状态和响应正文、原始模型输出、可用的 token 用量、请求耗时、校验、重试、取消和比赛生命周期 |
| `blue/thought.log` | Blue 的策略分析、复盘和思考记录 |
| `blue/debug.log` | Blue 的请求诊断，与 Red 日志规则相同 |

新日志的 player、winner、players 和 thinking 使用带 Red / Blue 的显示名称，wins 为该名称到胜局数的映射。目录固定为 red 和 blue，不从模型名或昵称生成路径。模型发言、策略分析和 HTTP 原文不改写。比赛开始事件包含 move_policy 和双方思考设置；每次 request_config 记录模型、请求的思考级别与输出上限。游戏快照记录模式与 own_move_locked，赛后报告也携带模式。结构化日志每行一个 JSON 对象，包含时间戳；请求事件带局号、Tick、请求次数（1 至 3）、玩家标识和阶段 `phase`（`game` 或 `review`）。复盘请求使用局号 `0`、Tick `0`，不增加实际局数或 Tick。`usage` 由接口提供，缺失时记录 null，不推算或伪造数值。模拟玩家不产生 HTTP 事件。复盘请求及失败状态写入各自的 debug 日志，成功复盘同时写入 thought 日志和公开的 chat 日志。

新 `thought.log` 使用中文分段文本，按局号、Tick、请求次数及阶段区分接口思考、正文分析、动作、复盘与失败记录。正文保留换行、统一缩进并转义危险控制字符。思考 token 缺失显示“未提供”，不记为 0。失败及取消立即记录，复盘失败也显示原因和是否重试。对应的完整 decision、reasoning、review、request_end 结构化事件统一存入 debug.log；thought.log 不再作为 JSON 解析。旧日志不改写，文件总数仍为五份。

阅读版的“通过校验”（debug 中 validated）表示响应已通过格式与当前动作规则校验，不代表动作已经执行。simultaneous 中对手同 Tick 失败时，双方暂存动作全部丢弃；alternating 只执行当前方，后续失败不撤销此前行动。以 `chat.log` 和 `game_end` 为实际结果。decision.actions 排除锁定后忽略的 MOVE，后者单独存入 ignored_actions；thought.log 也单独标记。无效响应中的文字保留在 debug 日志，不作为有效策略分析。

不记录鉴权头、响应头或传输异常原文；对本次运行的两个 API key 做脱敏，包括服务端在响应正文中回显的情况。调试原文不向对手或公开日志传播；赛后只公开经过校验且实际执行的动作和说明，并在展示及发送给另一模型前脱敏。日志目录默认被 Git 忽略。

每条记录立即写入并 flush，退出时关闭文件。用户中止或意外中断时保留已产生的记录；日志创建或写入失败时停止比赛并返回错误，不在缺失日志的情况下继续请求。强制终止或断电时不保证文件系统缓存已经落盘。

## 14. 第二版验收标准

- BO1、BO3、较大 BO 的目标胜局数正确；提前结束后不再请求游戏动作，只进行赛后复盘。
- 平局继续、弃权计分、最后允许的一局获胜、达到上限未决、技术错误停止均正确。
- 检查实际请求：同一场比赛下一局能看到此前交流、自己的响应、公开结果和比分；不能看到对手私有说明。新比赛首个请求不含前一场历史。每局 Tick、出拳和当前局历史仍重新开始。
- 两个模型均请求所配置的输出上限，内置值为 20,000 token；缺少正文分析且无接口思考，或提供了无效正文分析时，整份拒绝并重试。
- 每次运行生成五份日志，目录不重复；并发请求及重试的玩家、局号、Tick、次数不混淆。
- 日志记录中文、响应错误、耗时、用量、取消和比赛结果，且立即可读；比赛期间不向 chat 或对手请求泄露私有说明或未揭晓拳型。
- 接口响应回显已知 API key 时仍完成脱敏，鉴权头不进入日志。
- 日志故障能停止比赛，已写入的记录保留。
- 整场 BO 结束前不公开策略分析；赛后按行动顺序公开已执行动作与分析，锁定后忽略的 MOVE 单独标记，失败响应不公开。
- 双方独立收到相同比赛记录，复盘不执行动作、不改变比分；一方重试或失败不重复请求成功的另一方。
- 技术异常中止不自动公开说明或请求复盘；复盘取消或日志故障能够清理并发请求。

## 15. 赛后公开与复盘

整场 BO 产生冠军或达到总局数上限后，先保存结果，再公开所有已完成 Tick 的有效动作、正文策略分析、公开事件与结果；单局结束不提前公开分析。技术异常或用户中止不自动公开或复盘。

公开材料来自内存中的 MatchArchive，不通过读取日志恢复。锁定后被忽略的改拳以 ignored_actions 单列；失败响应、未完成 Tick 暂存动作、私有纠错和接口思考不进入公共报告。未提交 ANALYZE 时 summary 为 null，不补写分析。公共材料先过滤本次密钥，CLI 与双方模型获得同一份公开报告。

### 15.1 延续本场历史

保留每位玩家自己的全部本场 user/assistant 消息与规则提示，追加 `phase="review"`、玩家身份、最终公共报告、correction 和一句简短复盘邀请。双方的私有历史不合并，接口思考不回填。对话历史管理使用 Chat Completions 的消息列表方式，见 [OpenAI 会话状态文档](https://developers.openai.com/api/docs/guides/conversation-state)。

赛后只要求一条 REVIEW，文本自由组织，不要求固定栏目、ANALYZE、MOVE 或 ENDTURN：

```json
{"actions":[{"type":"REVIEW","text":"本场复盘，可按需要分段。"}]}
```

复盘不增加行动、不修改比分；analysis=off 不关闭复盘。正常每位模型只请求一次，不根据对手复盘自动追问。新 BO 的 start_match 清空上一场对局和复盘历史。

### 15.2 失败处理与展示

每方独立最多三次请求，格式错误、超时与可重试接口错误共用额度；重试保留自己的响应与反馈，使用同一公共报告。永久错误直接结束该方复盘；成功的另一方不重复请求。网络失败和失败流片段不回填历史。

成功内容保留段落写入 thought.log，并在 CLI 与 chat.log 展示；请求详情和失败状态写入 debug.log。任一方失败，保留另一方成功结果，返回 4，已确定的比分不变；双方成功时冠军比赛返回 0、未决比赛返回 3。日志故障返回 1，用户中止返回 130。

## 16. 可选思考模式

### 16.1 配置与请求

`--red-thinking`、`--blue-thinking` 分别覆盖该席位的 thinking。合法档位由所选 adapter 决定：deepseek 为 off/low/high/max，openai 为 off/low/medium/high/xhigh/max。thinking 两席内置值均为 off，模板均为 high。两席开关互相独立；同场 BO 的对局和复盘共用配置，不修改裁判规则。旧 --openai-thinking、--deepseek-thinking 分别兼容为 Red、Blue 的参数别名。

DeepSeek 开启时发送 `thinking.type="enabled"` 和对应的 `reasoning_effort`；关闭时发送 disabled 并省略 effort。GPT 将 off 映射为 `reasoning_effort="none"`，其余值直接发送。参照 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/) 和 [GPT-6 Luna 模型文档](https://developers.openai.com/api/docs/models/gpt-6-luna)。当前采用无工具调用的文本接口。

服务不支持所选参数时明确失败，不自动降低级别、关闭思考或切换模型。更换服务或模型标识仍须自行确认参数兼容性。模拟模式指定非 off 思考设置时，在建立日志和发起请求前报配置错误。

思考开启后仍沿用配置的输出上限、单次时限和统一重试额度。用户可通过 `--timeout` 调整时间；程序不因思考模式自动放宽时限。只有完整、合法的最终 `message.content` 可以产生动作或复盘；思考文本不能替代最终响应。

### 16.2 返回内容与隔离

适配器读取响应消息中非空字符串 `reasoning_content`，其次读取兼容字段 `reasoning`。只保存服务实际返回的文本，不补写、不推测完整思考。普通 assistant 内容仍按原规则进入自己的对话历史；额外思考字段不进入后续请求、对手上下文或赛后公开报告。同场 BO 仍保留公开事件、自己的最终响应、策略分析和纠错记录。

接口思考是否返回不改变 ANALYZE 校验，策略分析只按席位 analysis 配置处理。成功行动的 analysis_source 为 analyze 或 none，has_reasoning 单独标记本次是否返回思考文本；summary 是 ANALYZE 文本或 null。两类内容分别记录，不额外请求摘要。

`kind=reasoning` 同时写入对应玩家的 thought/debug 日志，带阶段、局号、Tick 和请求次数，以及：

| 字段 | 含义 |
| --- | --- |
| `requested_thinking` | 本次请求的思考设置，不是服务实际执行情况的证明 |
| `status` | returned：取得非空文本；not_returned：未提供文本；invalid_field：没有可用文本且返回字段类型不支持 |
| `source` / `content` | 实际文本来源字段与内容，缺失为 null |
| `reasoning_tokens` | 接口提供的非负整数用量，缺失为 null；明确的 0 保留为 0 |
| `finish_reason` | 服务返回的结束原因 |

思考用量优先读取 `usage.completion_tokens_details.reasoning_tokens`，其次读取兼容字段 `usage.reasoning_tokens`。未返回文本或用量不表示未执行思考，尤其不能把 null 当作 0。原始 usage 同时保留在 debug 日志。

开启思考且已取得有效响应消息时，即使没有文本也记录 reasoning 状态；关闭时若服务仍返回文本、异常字段或正的思考用量，也如实记录。超时等未取得响应消息的情况只记录请求失败，不伪造思考事件。截断或最终 JSON 无效时可能仍有思考记录，但该记录不表示动作通过校验或已经执行。脱敏与日志写入故障处理沿用第 13 节。

### 16.3 验证范围

自动化测试覆盖四种开关组合、各档参数、两种出拳规则及复盘、BO 上下文保留、思考文本隔离、缺失字段与用量、截断、无效 JSON、超时、服务拒绝参数和日志故障。


## 17. TOML 配置与启动校验

使用 Python 标准库 `tomllib` 读取 UTF-8 TOML，无新增依赖。默认读取当前工作目录的 `config.toml`；`--config 路径` 指定其他配置，`--no-config` 跳过配置，两者互斥。默认文件不存在时沿用内置值；显式指定的文件不存在、现有文件无法读取或语法错误时返回配置错误。`--help` 不依赖配置有效性。

配置优先级为 **CLI > TOML > 内置默认值**。配置文件本身须合法，字段不明、类型不符等错误不会因为 CLI 覆盖而被忽略。文件中的相对日志路径基于文件所在目录；CLI 的相对日志路径基于工作目录。

| 节 | 允许字段 |
| --- | --- |
| `[app]` | mode、log_dir、timeout_seconds、retry_delay_seconds、max_output_tokens、stream、stream_first_timeout_seconds、stream_idle_timeout_seconds |
| `[match]` | move_policy、best_of、max_games、initiative |
| `[players.red]` | adapter、name（可选）、model、base_url、api_key、thinking、analysis |
| `[players.blue]` | adapter、name（可选）、model、base_url、api_key、thinking、analysis |

adapter 支持 openai 和 deepseek，两席可相同；Red 默认 openai，Blue 默认 deepseek。省略模型与地址时按适配器选取默认值，显式填写的值不自动改写。旧 players.gpt/deepseek 分别兼容映射到 red/blue，禁止同一文件混用新旧配置节。其余省略的字段使用内置值。未指定 max_games 时，以最终合并后的 best_of × 10 计算。配置模板采用 GPT-5.6 Sol / DeepSeek Flash、双方 high、BO9、locked、120 秒超时、1 秒重试间隔、20,000 token。内置值保持旧版兼容：GPT-6 Luna / DeepSeek Flash、双方 off、BO1、60 秒超时，其余相同。initiative 默认为 simultaneous，可选实验性 alternating；alternating 与 revisable 的组合在请求及创建日志前被拒绝。每方常规 20 次机会、重试次数、提交可见性和赛后流程仍为固定规则。

真实模式需双方有效密钥。TOML 两个 api_key 字段均省略时，兼容读取工作目录的 local_secrets.py；任何一方写了 api_key 就要求两方均填写非空值，不混合读取。显式 --secrets-file 使用指定旧文件覆盖 TOML 密钥；按各席位实际 adapter 选取 OPENAI_API_KEY 或 DEEPSEEK_API_KEY。两席使用同一适配器时可使用同一把密钥。模板中的空字符串只作为待填写位置，不能启动真实请求。demo 可以不填密钥，但双方 thinking 必须为 off。

实际 config.toml、config.local.toml、secrets.toml 和旧 local_secrets.py 均被 Git 忽略。config.example.toml 可提交，不含真实密钥。程序不记录完整配置或鉴权头；解析错误不回显原始内容。比赛开始记录实际 BO、总局数上限、模式、思考与分析设置、超时、重试间隔、initiative 和输出上限，便于核对实验条件。

校验在请求前完成：严格检查未知节与字段、字符串、数值类型与范围、正奇数 BO、胜局与总局数关系、思考档位及 HTTP(S) 地址。地址不得夹带鉴权信息、查询参数或片段。显示名不允许使用裁判内部保留的 A/B。请求参数的远程兼容性仍由所选服务决定；不会自动降级思考、切换模型或改写输出上限。

自动化测试以模拟 HTTP 覆盖文件加载、跳过配置、CLI 优先级、非法配置、路径解析、密钥来源与脱敏，以及从真实模式入口经过 BO、跨局记忆、双方复盘到日志的完整链路。v0.4.0 通过离线模拟 HTTP 验证新协议，未额外调用真实模型。

## 18. 可选流式响应

`app.stream` 为布尔值，默认 false；CLI `--stream` / `--no-stream` 覆盖配置。双方对局和复盘共用开关，模拟玩家不发起流式请求。非流式路径保留，服务拒绝流式参数时不自动回退。流式请求发送 `stream=true` 与 `stream_options.include_usage=true`，按 [Chat Completions 流式事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events) 读取 SSE。

接收器按事件边界解析 data，支持传输分片、UTF-8 跨字节分片、CRLF、多行 data、注释心跳、空 role 片段及 choices 为空的用量尾包。只接受 index=0 的单条文本响应；合并 content、reasoning_content 和 reasoning。思考字段不进入后续对话。缺失 usage 保留 null，不根据片段数量推算 token。

必须取得 finish_reason=stop 和 `[DONE]`，再对完整正文执行现有 JSON、动作或复盘校验。中间内容永不执行；长度截断、拒绝、非法事件、EOF 或连接中断都使当前请求失败。重试使用原快照与原额度，成功的对手不重复请求；失败流的正文与思考片段不回填模型历史，各次尝试独立组装。取消会关闭响应连接；日志故障直接停止，不重试。

首个有效 SSE 数据事件的等待由 `stream_first_timeout_seconds` 限制，默认 60 秒并包含连接和响应头；后续事件间的等待由 `stream_idle_timeout_seconds` 限制，默认 30 秒。CLI 对应 `--stream-first-timeout`、`--stream-idle-timeout`。注释心跳不重置等待时限。原有 timeout_seconds 始终限制整次请求，即使持续收到事件也不能无限延长。

前台始终隐藏流式进度，开启 --debug 后在独立窗口报告等待、思考接收和正文接收阶段，不公开片段或影响模型可见状态。接口思考逐步写入私有 thought.log，正文与思考分片以 stream_delta 写入对应 debug.log；stream_end 记录完成、未完成或取消、组装内容、结束原因及可用用量。接收完整不代表通过校验。原 reasoning 结构化记录继续保留，阅读版以状态和用量结束，不重复正文。已知密钥跨分片匹配后脱敏，未完成的密钥前缀暂缓写入，结束时遮盖。


## 19. 终端调试模式与合并结算

CLI 参数 --debug 默认关闭，不加入 TOML 配置。普通与 debug 模式的前台显示、交互一致，保留公开聊天、提交/修改/等待、到期提醒、简短重试与错误、比分和复盘。Windows 下 --debug 额外打开只读命令行窗口，读取本场 red/debug.log 和 blue/debug.log，展示 Tick、请求与复盘阶段、耗时、用量和错误概况。它不重复调用模型、不参与调度，关闭窗口不影响比赛。正常结束后停止读取，窗口保留内容，按 Enter 关闭；父进程异常退出时也停止等待。

调试窗口启动失败时简短报错，比赛继续，不将诊断内容回退到前台。自动开窗暂仅支持 Windows；其他平台可在单独终端执行 `python -m model_rps.debug_console 日志目录`，添加 --once 仅读取已有记录。两种模式都保存全部五份日志；Tick 标题继续写入 chat.log，并作为结构化事件写入 debug.log。完整请求与响应片段保留在私有日志，调试窗口只展示诊断概况，沿用密钥脱敏与控制字符转义。

前台 Red 标签为红色、Blue 为蓝色；交换先手不交换身份与颜色。无颜色支持、输出重定向或设置 NO_COLOR 时使用纯文本。颜色不写入日志或模型输入。Windows 颜色通过 [控制台 VT 支持](https://learn.microsoft.com/en-us/windows/console/console-virtual-terminal-sequences) 启用，独立窗口使用 Python 的 [CREATE_NEW_CONSOLE](https://docs.python.org/3/library/subprocess.html#subprocess.CREATE_NEW_CONSOLE)。

裁判内部保留 reveal 事件，供模型历史与赛后记录使用；CLI 不逐条打印 reveal，而在该局 result 中合并双方拳型与胜负，终端和 chat.log 各得到一条结算记录。平局与弃权同样合并，未出拳者标明“未出拳”。技术异常只报告中止，不展示此前已提交但尚未公开的拳型。比分另行显示。
