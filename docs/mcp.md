# Agent 通过 MCP 参加猜拳（实验）

让 DeepSeek Harness（DSH）、Codex 等宿主中的 Agent 直接作为玩家。Agent 调用 MCP 工具，服务负责比赛调度、裁判和日志，复用原有猜拳裁判与调度。

有两种接入方式：

| 方式 | 说明 | 适用 |
| --- | --- | --- |
| **共享房间**（推荐） | 宿主配置一次 `--shared-room`，每场由 Agent 调用 `rps_start` 带邀请加入。房间规则见 [共享房间](shared-room.md) | 两个 Agent 对战、人类对 Agent、需要连续开场 |
| **独立比赛** | 每个 MCP 进程自建一场 BO，对手是模拟或 API 模型 | 一个 Agent 对战模拟或 API 玩家 |

两个 Agent 可使用独立的 MCP 连接，也可在同一个 DSH 的两个对话中共用一个连接；每人使用自己的席位邀请和工具返回的 `player_id`。

## 安装

在项目根目录安装可选 MCP 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

这只安装游戏侧依赖；宿主还需配置一次 MCP 启动入口。普通 CLI 和双人终端无需安装 MCP。

## 宿主配置（共享房间）

固定的启动参数为：

```text
-X utf8 -m model_rps.mcp_agent.server --shared-room
```

房间地址和席位凭证通过 `rps_start` 传入，不写进宿主配置，也不要把项目模型密钥放在这里。每次开房后只需把该席位的邀请发给 Agent，无需改配置或重启 MCP。

### DeepSeek Harness

配置示例见 [examples/dsh-rps.cordis.yml](../examples/dsh-rps.cordis.yml)，按实际安装位置修改其中的 Python 路径和工作目录。DSH 使用 `@deepseek-ai/dsh-mcp-client` 插件，传输选择 `stdio`。

- 桌面版：将示例补丁合并到 `$DSH_HOME/profiles/desktop/cordis.patch.yml`，已有条目保留；未设置 `DSH_HOME` 时默认是用户目录下的 `.dsh`。保存后重启 DSH 使配置生效。
- Web 版：在项目目录执行 `dsh web --patch .\examples\dsh-rps.cordis.yml`，或修改 `profiles/web/cordis.patch.yml`。

同一实例只选一种加载方式，避免重复添加 `mcp-rps`。工具发现后可见 `mcp__rps__rps_start` 等工具；发现工具不会自动占席，调用加入工具才连接房间。房间 HTTP 地址通过 `rps_start` 传入，不应配置成 MCP 的 HTTP 服务地址。

配置格式已对照本机 DSH `0.2.0-rc.2` 及官方说明核对：[MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)、[配置补丁用法](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.md)。

### Codex

```toml
[mcp_servers.rps]
command = 'Z:\DSH_Home\model-rps-protocol\.venv\Scripts\python.exe'
args = ["-X", "utf8", "-m", "model_rps.mcp_agent.server", "--shared-room"]
cwd = 'Z:\DSH_Home\model-rps-protocol'
startup_timeout_sec = 30
tool_timeout_sec = 30
```

路径替换为自己的项目位置，不要覆盖已有配置。Codex 通过 `command`、`args`、`cwd` 启动 stdio 服务，见 [官方 MCP 文档](https://developers.openai.com/codex/mcp)。

### 从旧配置迁移

原有 `--room-url`、`--token` / `RPS_SEAT_TOKEN` 固定房间配置仍兼容，但只绑定最初加入的一场，且其 `rps_start` 不接受邀请参数。迁移时将启动参数替换为 `--shared-room`，移除原来的房间地址、token 参数及 `RPS_SEAT_TOKEN`，再重新加载一次 MCP。

## 加入共享房间

先按[共享房间](shared-room.md#启动与入口)启动房间，服务会为每个席位打印 Agent 邀请。把该席位的邀请发给 Agent，让它调用 `rps_start`：

```json
{
  "room_url": "http://127.0.0.1:8765",
  "token": "本次房间打印的本席位凭证",
  "name": "DeepSeek"
}
```

- `name` 可选，只控制显示名；`token` 决定 Red 或 Blue。凭证不是模型 API key。
- 只向每名玩家提供自己一方的邀请。
- 加入后保存返回的 `state.player_id`。之后的 `rps_state`、`rps_act`、`rps_wait` 都必须携带它，不能省略，也不向对手公开。
- 同一个 DSH 的两个对话可能共用 MCP 进程，服务按 `player_id` 路由，不会把第二方的加入视作第一方换席位。缺失或未知标识会报错，服务不自动选择已有玩家，也不列出其他玩家的连接。

可以在新的 Agent 会话中这样提示：

> 使用 rps MCP 工具参加一场完整比赛。先读取规则，仅按本席位可见信息参赛，不读取项目代码、配置或私有日志。完成比赛和赛后聊天后自行 EXIT，等到双方退出并确认 complete。

## 独立比赛

只想让一个 Agent 对战模拟或 API 玩家时使用。MCP 由宿主启动，不使用 `run.ps1` 或人类输入终端；直接在终端运行只会等待 MCP 客户端连接，不会出现输入框。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m model_rps.mcp_agent.server --no-config --mode demo --bo 3
```

安装后也可用 `model-rps-mcp` 入口。该方式沿用通用规则、对手席位和日志参数（见 [命令行参数](cli.md#启动参数)），不支持 `--session`、`--debug`。

| MCP 专用参数 | 用途 |
| --- | --- |
| `--agent-seat Red\|Blue` | 外部 Agent 席位，默认 Red |
| `--agent-name 名称` | 外部 Agent 的公开名称，默认“外部 Agent” |
| `--agent-analysis off\|optional\|required` | 外部 Agent 的分析要求，默认 required |

未配置运行模式时默认 demo；已有 `config.toml` 的运行模式仍会生效。外部 Agent 的模型、思考级别和登录由宿主管理，项目配置只控制另一席位。使用真实对手时指定 `--mode live`，并以 `--blue-profile 名称`（Agent 为 Red）或 `--red-profile 名称`（Agent 为 Blue）选择对手；配置字段见 [config.example.toml](../config.example.toml)。

Codex 配置示例（与共享房间条目二选一，不要重复定义同名服务）：

```toml
[mcp_servers.rps]
command = 'Z:\DSH_Home\model-rps-protocol\.venv\Scripts\python.exe'
args = [
  "-X", "utf8", "-m", "model_rps.mcp_agent.server",
  "--no-config", "--mode", "demo", "--bo", "3",
  "--agent-name", "Codex",
]
cwd = 'Z:\DSH_Home\model-rps-protocol'
startup_timeout_sec = 30
tool_timeout_sec = 30
```

这份配置使用离线对手。改用真实对手时移除 `--no-config`，将 `--mode` 改为 `live`，并按需选择对手 profile。

## 工具流程

| 工具 | 用途 |
| --- | --- |
| `rps_start` | 共享入口接收 `room_url`、`token` 和可选 `name`，返回 `state.player_id`；刷新只传 `player_id`。独立或固定房间入口省略参数 |
| `rps_state` | 读取自己的当前局面、权限与公共事件；共享入口需传 `player_id` |
| `rps_act` | 提交 SAY / ANALYZE / MOVE / ENDTURN；赛后使用 SAY / EXIT；共享入口需传 `player_id` |
| `rps_wait` | 等待状态或公共消息变化；不自动结束行动；共享入口需传 `player_id` |

- 每次读完返回结果后保存 `revision`，下一次工具调用用它作为 `after_revision`。
- 提交时使用最新的 `turn_token`，并为每次新提交分配独立 `action_id`。网络重试复用同一个 ID 与原指令，避免重复发言或出拳。权限或局面变化后旧令牌会拒绝，应读取新状态再决定。
- 共享入口未加入时工具会提示先提供邀请，不创建默认对手。加入后 `status=not_started` 表示对方未到齐，应调用 `rps_wait`。同一邀请重复调用返回相同 `player_id`，不重复占席。不能无参数调用来获取“当前玩家”，因为一个 MCP 进程可能服务多个对话。
- `can_submit` 表示可以提交输入；游戏阶段 `state.can_act` 表示可以执行本轮行动。仅聊天权限时可以 SAY 或空 `actions`；有行动权限时可以先聊天，也可以提交包含 ANALYZE、MOVE、ENDTURN 的完整行动。策略分析必须通过共同指令显式提交，`required` 时不能用宿主内部思考替代。
- MCP 只提供工具，不负责唤醒已结束响应的宿主 Agent。比赛期间应持续调用工具并等待新事件；一次 `rps_wait` 最多等待 20 秒，等待结束不等于行动结束，需要 ENDTURN 才消耗行动机会。
- 工具提交成功表示进入调度，未必已执行，比分、出拳公开与结算以返回的公共事件为准。整场结束进入 postmatch，双方退出后为 `complete`；读取完结果后宿主可关闭 stdio 服务，共享房间仍由操作者关闭。

## 信息、日志与会话

- 工具只返回本席位信息；对手拳型在正常结算时揭晓，策略分析在整场结束后公开。宿主若有读文件等其他能力，仍应遵守玩家信息边界，本服务没有隔离宿主的操作系统权限。
- 日志沿用每场的 `chat.log` 和 Red / Blue 私有日志，Agent 的 ANALYZE 会保存并在赛后公开；Codex / DSH 的内部思考、用量和原始上下文不由游戏服务读取。MCP 进程 stdout 专供通信，独立比赛的公开终端信息写入 stderr，共享比赛的日志和公开输出由房间服务统一生成。
- 同一 BO 内宿主保留上下文。新 BO 需要新的 Agent 会话和服务端新开的场次（或新的独立 MCP 进程），避免带入前场记忆；服务无法替宿主清空会话。席位凭证和 `player_id` 会出现在所属玩家的邀请或宿主工具记录中，接口不向对手返回；宿主若共享会话记录或文件权限，仍需遵守信息边界。

## 验证记录

| 日期 | 范围 | 结果 |
| --- | --- | --- |
| 2026-10-20 | Codex CLI 0.162.1（模型 gpt-6-luna）经 21 次 MCP 工具调用完成 BO3 和双方赛后退出 | 最终 `complete`。使用旧实验分支的 TextArena 环境，仅验证宿主工具流程，不代表当前裁判验证或模型水平；用临时运行目录，未改全局配置 |
| 2026-10-21 | 共享房间完整离线回归 278 项 | 全部通过：同一个真实 stdio MCP 连接的两个玩家沿用 token 连续完成两场、独立 MCP 连接、双人终端、人类 / MCP 混合对局（人类在 Red 或 Blue、聊天权限、分析要求、一方先退出后关闭连接）；安装后的 `model-rps-room` 入口：活动场拒绝 `/start`，`/stop` 后 `/start` 沿用邀请，`/quit` 正常退出并释放端口 |
| 2026-10-21 | 源码目录重构（`core` / `llm` / `players` / `engine` / `ui` 分层）后完整回归 284 项 | 全部通过 |
| 2026-10-21 | 目录重构并重启 DSH 后，经 `model_rps.mcp_agent.server --shared-room` 与 `model_rps.room.server` 的真实对局：两场 BO3，Red 均为 DSH 中的 Agent；第一场 Blue 由使用者另行接入（接入方式未记录），第二场 Blue 为独立会话的 subagent | 两场均 `complete`：第一场 5 局（2 胜 1 负 2 平），第二场 2 局（2:0）。揭晓前互不可见、赛后分析与发言公开、双方 `EXIT` 后收尾均符合预期，`rps_act` 无报错。每局只有一个 Tick，样本很小，不代表模型水平或长局稳定性 |

上述自动回归均不调用真实模型，也未修改宿主全局配置；旧实验分支的 TextArena 环境、配置选项、依赖和专属测试没有迁入此分支。自动测试另覆盖同时与轮流行动、锁定与改拳、隐私边界、无效指令整体拒绝、跨席位及过期令牌、重复提交、事件游标、失联取消、关闭服务唤醒等待、未决结果，以及连续开场的各种边界（等待与赛后停止、换场时的在途请求、旧心跳与断开隔离、比分与日志独立、管理指令不对玩家开放、发现不占席、无效邀请修正、加入重试、未知标识不泄露视图）。

**尚未完成**：两名真人手动终端试玩、不同模型之间的对局，以及更长的对局（上表两场每局都在第 1 个 Tick 结算）。DSH 中两个独立会话经共享房间完成 BO3 已在 2026-10-21 实测。
