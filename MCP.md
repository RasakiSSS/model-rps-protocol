# Agent 通过 MCP 参加猜拳（实验）

目标是让 DeepSeek Harness 等宿主中的 Agent 直接作为玩家。Agent 调用 MCP 工具，服务负责比赛调度、裁判和日志。当前 `codex/mcp-agent` 从 main 创建，直接复用原有猜拳裁判与调度。

支持两种方式：`--shared-room` 提供可长期复用的共享房间入口，由 Agent 调用工具加入；独立比赛入口每个 MCP 进程自建一场 BO，对战模拟或 API 玩家。两个 Agent 可以使用独立 MCP 连接，也可以在同一个 DSH 的两个对话中共用 MCP 连接；每人使用自己的席位邀请和工具返回的 `player_id`。

## 安装

在项目根目录安装可选 MCP 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

这只安装游戏侧依赖；宿主还需配置一次 MCP 启动入口。普通 CLI 和双人终端无需安装 MCP。

## 共享房间：人类或 Agent 对战

先在项目目录启动房间，保持窗口运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m model_rps.room.server --bo 3
```

服务会打印 Red 和 Blue 各自的人类连接命令和 Agent 邀请。两个人类分别在两个终端执行对应命令即可加入；双方到齐后自动开赛。每个终端支持 `/say`、`/move`、`/analyze`、`/endturn`、`/clear`、`/status`、`/help` 和赛后 `/exit`。草稿只保存在自己的终端，按 `/endturn` 才提交行动。

Agent 使用下文的固定 MCP 配置。每次开房后，只把该席位的 Agent 邀请发给它，让它调用 `rps_start`；无需修改宿主配置或重启 MCP。例如：

```json
{
  "room_url": "http://127.0.0.1:8765",
  "token": "本次房间打印的本席位凭证",
  "name": "DeepSeek"
}
```

`name` 可选，只控制显示名；token 决定 Red 或 Blue。凭证不是模型 API key，同一服务端进程连续开场时保持不变，重启进程才更换。同一场的同一席位只允许一个客户端。只向每名玩家提供自己一方的邀请；两个对话分别加入后保存各自返回的 `state.player_id`。后续 `rps_state`、`rps_act`、`rps_wait` 必须携带自己的标识，不能省略，也不向对手公开。

同一个 DSH 的两个对话可能共用 MCP 进程；服务按 `player_id` 路由，不会把第二方的加入视作第一方换席位。缺失或未知标识会报错，服务不自动选择已有玩家，也不列出其他玩家的连接。`player_id` 是本玩家的访问凭证，不是 Red / Blue 的公开名字。

### DeepSeek Harness 固定配置

配置文件见 [examples/dsh-rps.cordis.yml](examples/dsh-rps.cordis.yml)。按实际安装位置修改其中的 Python 路径和工作目录，启动参数固定为：

```text
-X utf8 -m model_rps.mcp_agent.server --shared-room
```

DSH 使用 `@deepseek-ai/dsh-mcp-client` 插件，传输选择 `stdio`。桌面版将示例补丁合并到 `$DSH_HOME/profiles/desktop/cordis.patch.yml`，已有条目保留；未设置 `DSH_HOME` 时，默认是用户目录下的 `.dsh`。保存后重启 DSH 使配置生效。Web 版可在项目目录使用：

```powershell
dsh web --patch .\examples\dsh-rps.cordis.yml
```

同一实例选择一种加载方式，避免重复添加 `mcp-rps`。工具发现后可见 `mcp__rps__rps_start` 等工具；发现工具不会自动占席，调用加入工具才连接房间。房间 HTTP 地址通过 `rps_start` 传入，不应配置成 MCP 的 HTTP 服务地址。

配置格式已对照本机 DSH `0.2.0-rc.2` 及官方说明核对：[MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)、[配置补丁用法](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.md)。尚未修改用户的 DSH 配置或完成真实宿主对局。

### Codex 固定配置

将以下条目加入宿主 MCP 配置；每次开房时无需改动：

```toml
[mcp_servers.rps]
command = 'Z:\DSH_Home\model-rps-protocol\.venv\Scripts\python.exe'
args = ["-X", "utf8", "-m", "model_rps.mcp_agent.server", "--shared-room"]
cwd = 'Z:\DSH_Home\model-rps-protocol'
startup_timeout_sec = 30
tool_timeout_sec = 30
```

原有 `--room-url`、`--token` / `RPS_SEAT_TOKEN` 固定房间配置仍兼容，此方式的 `rps_start` 不接受邀请参数。迁移到新方式时，将启动参数替换为 `--shared-room`，移除原来的房间地址、token 参数及 `RPS_SEAT_TOKEN` 配置，再重新加载一次。

### 房间生命周期

桥接模式不读取 `config.toml`，不请求模型 API；Agent 由自己的宿主运行。BO、行动模式和分析要求在房间启动时确定，参数见 [README](README.md#共享房间入口)。

房间只监听本机回环地址，当前用于同一台电脑的两个终端或宿主。客户端每 10 秒自动发送心跳；退出连接或超过 90 秒失联会中止未完成比赛，不计作弃权，不会因为玩家思考或沉默而超时。已在赛后 EXIT 的玩家可关闭连接，另一方仍可继续退出。双方 EXIT 后客户端完成，服务端自动待机，保留结果直到下一场。

服务端窗口用 `/start` 开下一场，`/stop` 中止当前场，`/status` 查看状态，`/quit` 或 Ctrl+C 关闭服务；完整语义见 [README 管理指令](README.md#共享房间入口)。这些指令不开放给玩家。服务端启动时自动开放第一场，后续 `/start` 沿用地址、token 与比赛规则。人类重新运行原连接命令；Agent 用原邀请再次调用 `rps_start`，省略旧 `player_id`，保存新返回的标识。同场重复加入仍幂等。

每场拥有独立 `room_id`、玩家连接、事件游标、行动令牌、提交记录和日志；新比赛不继承比分、草稿或历史。每个 `player_id` 固定绑定原场次和席位，旧标识在换场后报错，不会自动连接新场；旧心跳、断开、等待和提交也不能作用于新场。宿主可复用同一个 MCP 进程，但新 BO 应使用新的 Agent 会话，避免带入前场记忆。

关闭共享 MCP 进程会断开它承载的所有玩家连接；只关闭一个宿主对话并不一定关闭 MCP 连接，赛后应调用 EXIT。MCP 重启后旧 `player_id` 不可用，本版不恢复已占席连接；在服务端 `/stop` 后 `/start`，用原邀请重新加入即可。项目升级后须重载 RPS MCP 进程加载新协议，单纯新建对话不会更新运行中的 Python。

目前不支持共享房间的 `/handoff`、赛后 `/resume`、重连恢复或远程部署。原有单终端 CLI 功能保持原样。无需手动在终端启动 MCP 桥接器，宿主会按配置启动；房间只需在调用加入工具前启动，MCP 可提前加载。旧 `--room-url` 固定入口仍只绑定最初加入的一场，连续开场请使用推荐的 `--shared-room` 入口。

## 独立比赛配置

只想让一个 Agent 对战模拟或 API 玩家时，将以下条目添加到 Codex 的 MCP 配置；与上面的共享房间条目选择一种，不要重复定义同名服务。路径替换为自己的实际项目位置，不要覆盖已有配置，也不要把项目模型密钥放到这里。Codex 支持通过 `command`、`args`、`cwd` 启动 stdio 服务，见 [Codex 官方 MCP 文档](https://developers.openai.com/codex/mcp)。

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

这份配置使用离线对手。加载后可在新的 Agent 会话中说；共享房间方式还需附上本席位邀请：

> 使用 rps MCP 工具参加一场完整比赛。先读取规则，仅按本席位可见信息参赛，不读取项目代码、配置或私有日志。完成比赛和赛后聊天后自行 EXIT，等到双方退出并确认 complete。

MCP 只提供工具，不负责唤醒已结束响应的宿主 Agent。比赛期间应持续调用工具并等待新事件；一次 `rps_wait` 最多等待 20 秒。等待结束不等于游戏行动结束，需要 ENDTURN 才消耗行动机会。

改用真实对手时，移除示例中的 `--no-config`，将 `--mode` 改为 `live`，并按需在 `args` 中选择对手 profile。项目配置字段全部见 [config.example.toml](config.example.toml)；启动参数集中在 [README](README.md#启动参数)。外部 Agent 自己的模型、思考级别和鉴权由 Codex / DSH 管理，项目配置只控制另一方。

## 工具流程

| 工具 | 用途 |
| --- | --- |
| `rps_start` | 共享入口接收 `room_url`、`token` 和可选 `name`，返回 `state.player_id`；刷新只传 `player_id`。独立或固定房间入口省略参数 |
| `rps_state` | 读取自己的当前局面、权限与公共事件；共享入口需传 `player_id` |
| `rps_act` | 提交 SAY / ANALYZE / MOVE / ENDTURN；赛后使用 SAY / EXIT；共享入口需传 `player_id` |
| `rps_wait` | 等待状态或公共消息变化；不自动结束行动；共享入口需传 `player_id` |

每次读完返回结果后保存 `revision`，下一次工具调用用它作为 `after_revision`。提交时使用最新的 `turn_token`，并为每次新提交分配独立 `action_id`。网络重试复用同一个 ID 与原指令，避免重复发言或出拳。权限或局面变化后旧令牌会拒绝，读取新状态再决定。

共享入口未加入时，工具会提示先提供邀请，不创建默认对手。加入后 status=not_started 表示尚未到齐，应调用 rps_wait 等待。同一邀请重复调用返回相同 `player_id`，不重复占席；也可仅传原 `player_id` 刷新 `rps_start`。不能无参数调用来获取“当前玩家”，因为一个 MCP 进程可能服务多个对话。无效邀请可以修正后重试。

`can_submit` 表示可以提交输入；游戏阶段 `state.can_act` 表示可以执行本轮行动。仅聊天权限时可以 SAY 或空 actions；有行动权限时，可以先聊天，也可以提交包含 ANALYZE、MOVE、ENDTURN 的完整行动。策略分析通过共同指令显式提交，`required` 时不能用宿主内部思考替代。

工具提交成功表示进入调度，未必已经执行。比分、出拳公开与结算以返回的公共事件为准。整场结束进入 postmatch，双方退出后为 complete；读取完结果后宿主可关闭 stdio 服务，共享房间仍由操作者关闭。中途断开会取消当前比赛，已在赛后 EXIT 的席位除外；暂无重连恢复。

## 信息、日志与验证范围

工具只返回本席位信息；对手拳型在正常结算时揭晓，策略分析在整场结束后公开。宿主若有读文件等其他能力，仍应遵守玩家信息边界；本服务没有隔离宿主的操作系统权限。

日志沿用每场的 chat.log 和 Red / Blue 私有日志。Agent 的 ANALYZE 会保存并在赛后公开；Codex / DSH 的内部思考、用量和原始上下文不由游戏服务读取或重建。MCP 进程 stdout 专供通信；独立比赛的公开终端信息写入 stderr，共享比赛的日志和公开输出由房间服务统一生成，桥接器不另建日志。私有诊断始终留在日志。

同一 BO 内宿主保留上下文。新 BO 需要新的 Agent 会话和服务端新开的场次（或独立 MCP 服务进程），避免带入前场记忆；服务无法替宿主清空会话。共享入口可沿用邀请，固定宿主配置不变。席位凭证和 `player_id` 会出现在所属玩家的邀请或宿主工具记录中，游戏接口不向对手返回这些凭证。宿主本身若共享会话记录或文件权限，仍需遵守信息边界。

前期原型验证：2026-10-20，Codex CLI 0.162.1 使用当时默认模型 gpt-6-luna，通过 21 次 MCP 工具调用完成 BO3 和双方赛后退出，最终 complete。该次使用旧实验分支的 TextArena 环境，结果仅用于验证宿主工具流程，不代表当前分支的裁判验证或模型水平评测。测试使用临时 Codex 运行目录，未修改全局配置。

共享房间验证（2026-10-21）：完整离线回归 **278 项全部通过**，包括同一个真实 stdio MCP 连接的两个玩家沿用 token 连续完成两场、原有独立 MCP 连接、双人终端及人类 / MCP 混合对局。终端测试使用生产组件和模拟键盘；混合测试覆盖人类处于 Red 或 Blue、聊天权限、分析要求和一方先退出后关闭连接。另验证了安装后的 `model-rps-room` 入口：活动场拒绝 /start，/stop 后 /start 沿用邀请，/quit 正常退出并释放端口。本次没有调用真实模型或修改宿主全局配置。此前的 TextArena 环境、配置选项、依赖和专属测试没有迁入此分支。

自动测试另覆盖同时与轮流行动、锁定与改拳、隐私边界、无效指令整体拒绝、跨席位及过期令牌、重复提交、事件游标、失联取消、关闭服务唤醒等待和未决结果。连续开场覆盖等待、比赛和赛后停止、换场时的在途请求、旧心跳与断开隔离、比分及日志独立、管理指令不对玩家开放；动态邀请覆盖发现不占席、无效邀请修正、加入重试及未知标识不泄露视图。尚未进行两名真人手动终端试玩，也未完成 DSH 两个真实模型对话的完整共享房间对局；DSH 已确认可调用加入工具，更新后的完整实测待进行。
