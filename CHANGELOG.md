# 版本记录

## 未发布 · 共享房间与 MCP 外部玩家

- 源码重构：平铺的核心模块分入 `core`、`llm`、`players`、`engine`、`ui` 五个子包，连同新增的 `room`（共享房间）和 `mcp_agent`（MCP 桥接）共七层，依赖只能向下；不保留旧模块路径。命令入口更名为 `model_rps.mcp_agent.server`、`model_rps.room.server`、`model_rps.room.client`，调试查看器为 `model_rps.ui.debug_console`，**已有 MCP 宿主配置需同步更新并重载**。删除 `mcp_agent` 中的 `MCPPlayer` / `MCPRoom` 别名模块。
- 目录整理：验证与实验脚本移到 `validation/`、`experiments/`，其输出写入 Git 忽略的 `artifacts/`；`logs/` 只存放对局日志。新增 `tests/test_architecture.py` 检查分层方向、包根模块和文档、示例中的点分模块路径引用；`pyproject.toml` 增加 pytest、ruff 配置与 `dev` 可选依赖。
- 文档重组：`PROTOCOL.md`（约 50 KB）拆为 `docs/` 下的 `rules.md`、`model-io.md`、`logs-and-display.md`，并新增 `architecture.md`、`cli.md`、`shared-room.md`；`MCP.md`、`MODEL_ADAPTERS.md`、`ROADMAP.md` 移入 `docs/` 并更名。README 精简为介绍、安装与快速开始，命令行参数与排错移到 `docs/cli.md`。
- 新增服务端管理指令 `/start`、`/stop`、`/status`、`/quit`、`/help`；双方 EXIT 后自动待机，可在同一进程连续开场。地址和 Red / Blue token 沿用，每场独立状态及日志，旧请求不能进入新场；Agent 使用原邀请获取新的 player_id，无需重配或重启 MCP。
- 新增本机共享房间：两个独立人类终端、两个 MCP 桥接器或人类与 Agent 的组合加入同一场 BO；凭证绑定 Red / Blue，双方到齐才开赛。人类和 Agent 共用玩家接口，保留聊天、草稿与赛后退出；失联中止，不判弃权，赛后已 EXIT 的席位可先断开。
- 新增 `--shared-room` 固定 MCP 入口，房间地址、席位凭证和玩家名称由 `rps_start` 接收；房间终端打印可直接转交的 Agent 邀请。新房间无需重配宿主。每位玩家获得独立 `player_id`，支持同一 DSH 两个对话共用 MCP 进程；后续工具显式指定玩家，缺失标识不默认读取他人状态，已有标识不能改绑。提供 DSH YAML 示例，保留旧固定房间参数。
- 新增可选 stdio MCP 服务：外部 Agent 占据 Red 或 Blue，对战模拟或已配置的 API 玩家；每个服务进程运行一场 BO。
- 提供开始、查看、提交、等待四个工具；复用现有规则、信息权限、日志和赛后聊天。提交校验行动令牌并按 action_id 去重，公共事件按游标增量返回。
- 等待新消息不消耗行动次数；未退出的宿主断开时取消比赛和待处理请求。宿主的内部思考、登录和会话记忆不由本项目管理。
- 从 main 新建独立 MCP 分支，复用原有裁判与调度；可选依赖仅为 MCP SDK，保留其原始许可声明。
- Codex 工具流程已在前期原型验证；当前分支重新验证原有裁判下的完整 stdio 对局。DeepSeek Harness 尚待接入验证。

## v1.0.1 · 2026-10-19

- 项目自身采用 MIT License，版权署名为 RasakiSSS。
- 新增第三方依赖清单及原始版权、许可声明副本，说明 certifi 的 MPL-2.0 适用范围。
- README 增加 MIT 徽章与许可证说明。
- 打包元数据声明 MIT，并将项目许可证和第三方声明纳入源码分发包与 wheel；构建工具最低版本调整为 setuptools 77.0.3，以支持标准许可证字段。
- 此次为许可证与发布信息补充，运行依赖、配置和游戏规则沿用 v1.0.0。

## v1.0.0 · 2026-10-18

首个正式版本，整合此前开发阶段的 CLI 博弈功能。

### 玩法与玩家

- 模型对战模型、人类对战模型；Red / Blue 席位独立，同一模型也可同时参赛。
- 任意正奇数 BO，同时行动或实验性轮流行动，锁定出拳或同时行动下的改拳模式。
- 人类和模型使用共同指令与信息权限；聊天即时公开，出拳和策略分析按规则揭晓。
- 同一场 BO 保留上下文，赛后继续聊天；连续比赛支持大厅选人和交换席位，新 BO 清空历史。
- 人类可在比赛中交给已配置的模型，继承本席位已提交的分析、行动和公共记录。

### 模型、配置与诊断

- OpenAI、DeepSeek、GLM、Gemini、Grok 和 Claude 六种适配方案。
- TOML 玩家 profile、思考级别、分析要求，以及每模型流式开关和等待时限。
- 公开聊天日志、双方私有思考与调试日志，以及独立 Windows 调试窗口。
- Grok 流式追加缩进换行 JSON 要求，缓解已测第三方服务的缺字符问题；响应继续严格校验。

### 本次发布整理

- 新增 `run.ps1`：定位项目虚拟环境、转发启动参数并保留退出码。
- README 增加项目徽章、Release 下载入口和 Windows 启动说明。
- 统一版本号和现行文档。

### 安装与现有限制

- 下载 Release 的 Source code (zip)，解压后按 README 安装 Python 3.12+ 和依赖。
- 首次使用真实模型前，手动将 `config.example.toml` 复制为 `config.toml` 并填写；现有配置无需因本次升级而修改。
- 一个终端最多一位人类玩家；本版局中接管仅支持人类交给模型。
- 轮流行动仍为实验性模式；尚无上下文自动摘要、截断、聊天额度或自动行动期限。
- 第三方服务的模型和参数支持以实际响应为准，已测范围见 `docs/model-adapters.md`。

此前各阶段的功能摘要见 [开发计划](docs/roadmap.md)。
