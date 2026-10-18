# Model RPS

让人类与大语言模型，或两个独立的模型，在命令行中聊天、等待和出拳。Python 裁判负责规则、信息隔离和胜负判定；比赛结束后，双方沿用各自上下文继续聊天。

当前版本 **0.11.1**。支持任意正奇数 BO、Red / Blue 席位、同模型对战、人类玩家、独立聊天、同时或轮流行动、局中模型接管，以及比赛之间换人。已接入 OpenAI、DeepSeek、GLM、Gemini、Grok 和 Claude 的参数方案。项目用于观察博弈行为，不预设模型必须聊天，也不把单场胜负当作能力排名。

| 要找的内容 | 位置 |
| --- | --- |
| 配置字段、默认值、优先级与填写示例 | [config.example.toml](config.example.toml) 的注释 |
| 启动参数、终端操作与排错 | 本文 |
| 裁判规则、信息边界、请求与日志数据 | [PROTOCOL.md](PROTOCOL.md) |
| 接口适配、官方资料与实测限制 | [MODEL_ADAPTERS.md](MODEL_ADAPTERS.md) |
| 下一版计划与尚待确定的问题 | [ROADMAP.md](ROADMAP.md) |

## 安装与首次使用

需要 Python 3.12 或更高版本。下载源码后，在项目根目录打开终端；已有虚拟环境时跳过创建步骤。

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

也可以从 GitHub 的 Code → Download ZIP 下载。Python 启动器中的版本号按实际安装版本调整；以下示例直接调用虚拟环境，无需激活。

**首次运行不会自动创建 config.toml。** 使用真实模型前，复制 [配置模板](config.example.toml)，然后按其中注释填写。已有配置时直接编辑，不要覆盖：

```powershell
Copy-Item config.example.toml config.toml
```

macOS / Linux 使用 `cp config.example.toml config.toml`。离线演示不需要配置或密钥。完成安装后，按下一节启动。

## 启动参数

以下命令在项目根目录执行。macOS / Linux 将 `.\.venv\Scripts\python.exe` 替换为 `./.venv/bin/python`。安装后也可用 `model-rps` 入口，参数相同。

### 常用启动方式

```powershell
# 使用当前 config.toml 的设置
.\.venv\Scripts\python.exe -m model_rps

# 无需密钥的离线 BO3
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --bo 3

# 人类使用 Red，对战 gpt profile
.\.venv\Scripts\python.exe -m model_rps --red-profile human --blue-profile gpt --bo 3

# 同一模型占两个席位，各自历史独立
.\.venv\Scripts\python.exe -m model_rps --red-profile gpt --blue-profile gpt

# 连续比赛：赛后双方退出后进入下一场大厅
.\.venv\Scripts\python.exe -m model_rps --session

# 另开调试窗口，前台显示不变
.\.venv\Scripts\python.exe -m model_rps --debug

# 查看完整帮助；无需有效配置或密钥
.\.venv\Scripts\python.exe -m model_rps --help
```

真实模式会发起 API 请求。人类参与和连续比赛需要交互终端，不要重定向输入或输出。没有连续比赛参数时，一场 BO 及赛后聊天结束后退出程序。

### 通用参数

参数仅影响本次进程，不写回配置。配置字段的默认值、允许范围和覆盖顺序统一见 [模板注释](config.example.toml)。

| 参数 | 用途 |
| --- | --- |
| `-h`、`--help` | 显示帮助 |
| `--config 路径` | 指定 TOML 文件；文件不存在时报错 |
| `--no-config` | 跳过 TOML，与 `--config` 互斥 |
| `--mode live\|demo` | 真实模型或预设模拟玩家 |
| `--session` | 开启连续比赛，双方退出赛后聊天后进入大厅 |
| `--debug` | Windows 下额外打开只读调试窗口 |
| `--bo N` | 比赛的 BO 数 |
| `--max-games M` | 包含平局的实际总局数上限 |
| `--initiative simultaneous\|alternating` | 同时行动或实验性轮流行动 |
| `--move-policy locked\|revisable` | 锁定出拳或允许跨 Tick 改拳 |
| `--timeout 秒` | 每次模型请求总时限 |
| `--retry-delay 秒` | 请求重试间隔 |
| `--max-output-tokens N` | 单次输出上限 |
| `--log-dir 路径` | 日志根目录 |
| `--stream`、`--no-stream` | 覆盖双方流式开关 |
| `--stream-first-timeout 秒` | 覆盖双方流式首个数据事件的等待时限 |
| `--stream-idle-timeout 秒` | 覆盖双方流式后续数据事件间的等待时限 |
| `--scenario 名称` | 选择模拟脚本；不会自动切换到 demo |
| `--secrets-file 路径` | 兼容旧 Python 密钥文件，来源规则见模板末尾 |

### 席位参数

Red / Blue 使用对称参数。没有 API key 命令行参数，密钥按模板填写。

| Red 参数 | Blue 参数 | 用途 |
| --- | --- | --- |
| `--red-profile 名称` | `--blue-profile 名称` | 选择玩家条目 |
| `--red-type llm\|human` | `--blue-type llm\|human` | 临时覆盖玩家类型 |
| `--red-adapter 名称` | `--blue-adapter 名称` | 选择请求参数方案 |
| `--red-model 标识` | `--blue-model 标识` | 覆盖模型标识 |
| `--red-base-url 地址` | `--blue-base-url 地址` | 覆盖服务基础地址 |
| `--red-name 名称` | `--blue-name 名称` | 覆盖显示名 |
| `--red-thinking 档位` | `--blue-thinking 档位` | 覆盖思考级别 |
| `--red-analysis off\|optional\|required` | `--blue-analysis off\|optional\|required` | 覆盖策略分析要求 |
| `--red-stream`、`--no-red-stream` | `--blue-stream`、`--no-blue-stream` | 单席流式开关，优先于全局开关 |
| `--red-stream-first-timeout 秒` | `--blue-stream-first-timeout 秒` | 单席首包时限 |
| `--red-stream-idle-timeout 秒` | `--blue-stream-idle-timeout 秒` | 单席空闲时限 |

兼容旧别名：`--openai-model`、`--openai-base-url`、`--openai-name`、`--openai-thinking` 对应 Red 的四项参数；`--deepseek-model`、`--deepseek-base-url`、`--deepseek-name`、`--deepseek-thinking` 对应 Blue。别名只指定席位，不自动选择适配器。

```powershell
# 单独关闭 Grok 的流式，另一方保留原设置
.\.venv\Scripts\python.exe -m model_rps --red-profile grok --blue-profile glm --no-red-stream --bo 3

# 修改本次请求时限和输出上限
.\.venv\Scripts\python.exe -m model_rps --timeout 180 --max-output-tokens 24000

# 无密钥的人类练习
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --red-type human --red-name "小明"
```

### 模拟场景与调试查看器

模拟场景可选 `locked`、`change`、`settlement`、`draw`、`forfeit`、`deadline`、`retry`、`chat`。省略时随出拳规则选择 locked 或 change；change、settlement 要求 revisable。模拟脚本不能代表真实模型行为。

```powershell
# 独立聊天与轮流行动
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario chat --initiative alternating

# 观察到期平局；比赛未决，退出码为 3
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --scenario draw --max-games 1

# 结算 Tick 的最后一次改拳
.\.venv\Scripts\python.exe -m model_rps --no-config --mode demo --move-policy revisable --scenario settlement

# 单独查看已有日志；路径替换为前台打印的实际目录
.\.venv\Scripts\python.exe -m model_rps.debug_console "logs/本场目录" --once
```

查看器省略 `--once` 会持续观察日志。自动开调试窗口目前仅支持 Windows，其他平台可手动在另一个终端运行查看器。

## 终端操作

Red 标签为红色，Blue 为蓝色；交换先手不交换身份。终端不支持颜色或设置 `NO_COLOR` 时保留纯文本标签。一个终端最多一位人类玩家。

### 对局与赛后聊天

| 人类指令 | 效果 |
| --- | --- |
| `/say 文本` | 立即发言；等待模型或轮到对手时也能使用 |
| `/move Rock` | 暂存石头；也可选 Paper（布）、Scissors（剪刀），再次输入替换草稿 |
| `/analyze 文本` | 暂存私有分析，多次输入按顺序追加 |
| `/endturn` | 提交出拳与分析，结束本次行动；没有新 MOVE 就等待，已有拳型保留 |
| `/clear` | 清空未提交的行动草稿 |
| `/status` | 查看当前权限、自己的拳型、草稿与剩余机会 |
| `/help` | 查看指令 |
| `/handoff deepseek` | 比赛进行中把自己的席位交给指定模型；名称使用已有 profile |
| `/exit` | 仅在赛后退出聊天；双方退出后本场结束 |

每行一条指令。只有具备行动权限时才能暂存出拳、分析或结束行动；发言不会提交或清空草稿。对手发言不打断正在编辑的文字。新局或赛后会清空行动草稿和未发送的行动指令，保留未发送的聊天文字。输入错误只反馈本人，人类等待没有时限。

赛后公开有效动作与正文策略分析，双方保留本场历史继续聊天；接口思考不公开。沉默不等于退出，也不强制回复。

| 赛后状态 | 如何继续 |
| --- | --- |
| 人类仍在聊天 | 直接发言，或自行退出 |
| 显示 `赛后控制 >`，模型暂无请求 | 操作者输入 `/resume`，邀请仍在场的模型再响应一次 |
| 双方已退出 | 普通模式结束进程；连续比赛模式进入大厅 |

恢复命令不代替玩家发言，也不恢复已退出的席位。没有交互终端时，沉默待机不会自动轮询。任何阶段都可用 Ctrl+C 中止；关闭输入同样按中止处理，已有日志保留。

### 局中交接

人类可在有行动权限或仅能聊天时输入 `/handoff deepseek`。系统公开换人消息，模型接手原来的 Red / Blue 席位，你继续观看。接管者会继续参加本场后续各局及赛后聊天；本版不支持把席位交还人类，也不能在赛后发起交接。

比分、Tick、剩余行动机会和已锁定拳型保留。已确认提交的行动继续有效，模型不能重复行动；未提交的出拳与分析草稿丢弃。模型完整继承本场公共记录、本席位已提交的分析和行动，以及自己的当前拳型；不会获得对手未公开的信息或接口思考日志。

交接不等待对手生成完毕或整局结束。若处理时本局已结算，交接会被拒绝；还有下一局时可重新输入。配置无效时原玩家继续操作；成功接管后，请求失败沿用现有重试与技术错误规则。profile 的配置及覆盖方式见 [模板注释](config.example.toml)。demo 模式交给模拟玩家，不调用真实服务。

换人沿用本场日志目录，旧发言和分析保留原作者。交接仅对当前 BO 生效，不改下一场的大厅选人设置。

### 比赛大厅

只有开启连续比赛并正常结束本场赛后聊天，才会出现 `下一场 >`。比赛或赛后聊天技术失败时结束运行。

| 大厅指令 | 效果 |
| --- | --- |
| `/profiles` | 查看已加载的玩家条目，不显示密钥 |
| `/red human`、`/blue deepseek` | 选择下一场的玩家；名称按自己的条目填写 |
| `/swap` | 连同覆盖项整体交换两个席位 |
| `/status` | 查看下一场参与者与比赛规则 |
| `/start` | 校验后开赛；不换人时原班再战 |
| `/exit` | 结束程序 |
| `/help` | 查看大厅指令 |

更换 profile 会完整替换对应席位，具体覆盖规则见配置模板。BO 与游戏规则沿用启动设置。无效选择会留在大厅提示修改；大厅不发模型请求，其指令不进入玩家聊天。

每场新建玩家与日志，清空上一场的比分、草稿、记忆和用量统计。即使选择同一模型也不会继承上一场历史。

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

完整的动作格式、倒计时、重试与取消规则见 [协议](PROTOCOL.md)。

## 日志与排错

每场开赛时打印独立日志路径：

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
| `chat.log` | 公开聊天、系统消息、结算、比分、赛后公开分析与聊天 |
| `thought.log` | 按局号、Tick、请求与尝试次数排列的思考、分析、动作及失败说明 |
| `debug.log` | JSON Lines：请求、响应、校验、执行、取消、用量和耗时等诊断 |

普通模式也保存全部日志。调试窗口只读取日志，关闭它不影响比赛；正常结束后保留内容，按 Enter 关闭。窗口无法启动时比赛继续。前台始终只显示公开聊天、提交状态、提醒、结果和必要错误概况。

调试窗口显示每次请求的触发原因、行动权限、自己的出拳状态、新消息和实际请求设置；返回后分段展示策略分析（ANALYZE）与指令，并区分通过校验、等待同步、已执行、已忽略和已丢弃。服务错误、无效响应、纠错与重试、交接资料数量也在窗口中显示。分析在完整响应通过校验后显示，接口思考正文仅写入日志，窗口只显示接收状态和用量，不重复输出完整请求历史。

“通过校验”不等于“已执行”，以公开裁判记录和应用事件为准。流式片段、失败响应与接口思考不会直接变成公开聊天。窗口中的长错误正文最多显示 4,000 字符，完整内容仍保存在 debug.log。新日志按本场统一写入序号合并显示；旧日志仍可查看。日志字段及用量口径见 [协议](PROTOCOL.md)。

日志使用 UTF-8，逐条写入；写入失败会停止比赛。鉴权头不记录，已知密钥会脱敏；私有日志仍包含模型输入、输出和分析，分享前应检查内容。

| 情况 | 处理方式 |
| --- | --- |
| `No module named model_rps` | 在项目目录，用同一虚拟环境重新执行安装命令 |
| 找不到配置或密钥为空 | 按模板首段创建并填写配置，确认文件已保存、文件名正确 |
| HTTP 401 | 核对密钥是否属于所用服务地址 |
| 不支持模型或参数 | 对照配置模板及模型适配说明，检查服务实际支持范围 |
| 超时、断流或 JSON 无效 | 查看对应 debug.log；可调整等待时限或流式设置，不自动降级 |
| Grok 流式 JSON 缺字符 | 已自动加入缩进换行格式要求；若仍失败，可按模板单独关闭流式。适用范围与实测见模型适配说明 |
| 思考文本缺失或只有标题 | 记录服务实际返回内容；未返回不等于未执行思考，不补造内容 |
| 赛后没有继续输出 | 查看是否进入沉默待机；按“终端操作”继续或中止 |
| 人类模式提示终端不可用 | 在交互终端运行，避免重定向输入或输出 |

退出码：0 已产生冠军；3 达到局数上限但未决；1 比赛技术异常或日志故障；2 配置错误；4 赛后聊天技术异常；130 用户中止。大厅正常退出时返回最后一场的状态。

## 开发与测试

源码位于 `src/model_rps/`，分层与模块职责见 [协议](PROTOCOL.md)。提示词集中在 [prompts.py](src/model_rps/prompts.py)，裁判逻辑在 [game.py](src/model_rps/game.py)；修改提示词不会改变裁判规则。

以可编辑方式安装后，修改源文件无需重装；依赖变化时重新执行安装命令。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用 unittest、模拟 HTTP 和异步输入，不调用真实模型。覆盖规则、权限与信息隔离、调度与取消、配置、模型适配、流式接收、日志、BO 记忆，以及人类输入、局中交接、赛后待机和连续比赛。真实服务的测试日期与边界单独记录在模型适配文档中。
