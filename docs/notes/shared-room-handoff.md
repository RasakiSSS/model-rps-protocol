# 共享房间开发交接

状态：2026-10-21 08:08 Asia/Hong_Kong。本次服务端连续开场开发与文档同步已完成，278 项完整回归通过。无需定时续作，没有新建自动任务。分支保持 codex/mcp-agent；未 commit、merge、push 或发布。

## 本次用户要求与实现

- 服务端提供 `/start`、`/stop`、`/status`、`/quit`、`/help`；启动即开放第一场，双方 EXIT 后自动待机。
- 当前场仍在等待、比赛或赛后聊天时拒绝 /start 覆盖。/stop 中止当前场并保留结果和日志，不判弃权；已完成场再次 /stop 不改为取消。
- 同一进程内沿用地址和 Red / Blue token；/start 沿用启动规则，每场重建 room_id、玩家连接、比分、历史、草稿、行动令牌、提交记录和日志。仅重启服务端才更换 token。
- 人类重新执行原命令；Agent 用原邀请重新 rps_start，省略旧 player_id，保存新返回值。后续工具必须显式传本场 player_id。新 BO 用新 Agent 对话，服务无法清空宿主记忆。
- 新增 room/service.py 管理场次，room/terminal.py 管理异步终端。SharedRoom 仍负责单场，现有裁判与单终端 CLI 不变。
- HTTP 新增经过席位凭证认证的 /info，仅返回当前场次和阶段，不占席。其余请求绑定 room_id；旧请求不能读取、提交、刷新心跳或取消新场。在途长轮询只返回原场结果。
- RoomConnector 用 (url, token, room_id) 对加入去重，同场丢失加入响应可重试。旧 player_id 不自动升级。旧 --room-url 固定入口仍只绑定最初的场次，连续开场使用 --shared-room。
- 服务端指令只在本地管理窗口输入，不开放 HTTP 或 MCP 管理权限。交互输入由 prompt-toolkit 重绘；管道支持管理指令，EOF 保持服务运行。

## 验证结果

- .venv Python 完整回归：**278 tests / OK / 140.982 秒**。
- 完整记录：artifacts/shared_room_validation/room-lifecycle-full-regression.txt。
- 新增 6 项生命周期测试：同 token 两场完整比赛、一个真实 stdio MCP 连接中的两个玩家连续对局、旧请求及心跳隔离、发现与加入间换场、停止等待/比赛/赛后、服务端键盘编辑与管理指令。测试不调用真实模型。
- 原有人类双终端、人类/MCP 混合对局、双 stdio、游戏规则、CLI、模型适配和日志测试全部通过。
- 安装后的 model-rps-room.exe 管道冒烟：/status、活动场 /start 被拒、/stop、/start、/help、/quit；两次邀请一致，退出码 0，端口可重新绑定。记录：artifacts/shared_room_validation/room-admin-smoke.json（不含 token）。
- 首次定向测试中测试脚本多提交一次游戏行动进入赛后，已修正；room-lifecycle-tests.txt 的初次失败已被完整回归覆盖。
- Windows 沙箱会卡在 asyncio socketpair 创建；验证需在沙箱外运行本机端口/子进程测试。仅清理本次卡住的 unittest 进程，没有结束 DSH 或用户比赛。
- 文档已同步 README.md、MCP.md、PROTOCOL.md、ROADMAP.md、CHANGELOG.md、DSH 示例和工具提示。git diff --check 通过。

## 用户使用与待实测边界

启动：`.\.venv\Scripts\python.exe -X utf8 -m model_rps.room.server --bo 3`。
每方使用打印的本席位连接命令或 Agent 邀请加入。双方赛后 /exit 后，服务端 /start，然后各自用原邀请重新加入。

本次修改了 HTTP 协议，**需要关闭旧房间进程并重载 RPS MCP 一次**。只新建 DSH 对话不会更新已运行的 Python。普通连续开场不需要再重载。此次未自动重载宿主或干预用户运行中的服务。

DSH 真实双模型完整对局和两名真人手动试玩仍待用户验证；不能用离线 SDK 测试代替这项结论。尚无远程部署、局中接管或重连恢复。

## 历史宿主问题与环境

- 先前双对话加入冲突来自 DSH 运行旧 MCP 子进程。10 月 21 日 06:24 已定向重载，当时新进程链为 46000→2456→36816；这些 PID 仅为历史证据，再操作须重新核实。
- 实际配置位于 C:/Users/糕糕/.dsh/profiles/web/cordis.patch.yml：mcp-rps，stdio，项目 .venv Python，参数 -X utf8 -m model_rps.mcp_agent.server --shared-room，cwd 为项目目录。无需把 token 放进该配置。
- DSH Web 多个对话可共享一个 MCP 进程，因此每次工具请求必须带自己的 player_id。8765 是裁判 HTTP 地址，不是 Streamable HTTP MCP 地址。
- 保留原 TextArena 实验恢复点，但不再维护或重迁。原恢复 stash 为 9225dcfce000941b8105d05a5f8ea69c0a794cc3，资料在 artifacts/mcp_migration/。

## 备份、边界与后续

- 本次开始前源码备份：artifacts/shared_room_validation/before_room_lifecycle.zip。
- 本次完成快照：artifacts/shared_room_validation/completed_room_lifecycle_source.zip。
- 之前多玩家修复快照：completed_multi_player_source.zip；其他历史备份均在同一日志目录。
- 用户截图 dsh-still-old-error-20261021.png 含临时 token，不提交或公开输出。
- 所有未提交源码和测试均需保留，git diff 不显示未跟踪源码；检查时结合 git status。
- 私有 config.toml、密钥、.venv、日志和无关 tmp069rucnk/ 未修改。除非用户明确要求，不提交、合并、推送、发布或进行真实模型额度测试。
- 用户要求额度不足时做好交接。本轮额度足以完成；后续长任务先检查额度，保留可恢复进度，再按用户授权安排续作。
