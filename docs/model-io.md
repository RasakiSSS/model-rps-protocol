# 模型交互：校验、输入输出与赛后聊天

适用版本：v1.0.1 及 `codex/mcp-agent` 开发分支。

本文约定一次玩家请求从发出到生效的全过程：响应校验与重试、模型收到的状态与应返回的 JSON、整场结束后的公开与赛后聊天。规则本身见 [游戏规则](rules.md)；厂商接口差异见 [模型适配](model-adapters.md)。

## 响应校验、超时与重试

完整指令校验对人类与模型一致；以下请求时限和重试额度仅适用于模型，人类本地输入规则见 [规则：人类终端输入](rules.md#人类终端输入)。

- 完整校验响应后才执行动作。解析失败、指令字段无效、分析不符合配置或未按规定结束行动时，整份响应作废。格式合法的锁定后 MOVE 只被忽略，不重试，SAY 正常执行。
- 格式或动作规则错误反馈给出错的一方，让其重新生成；请求超时或可重试的 API 失败也采用有限重试。
- 每次交互最多请求 **3 次**：首次请求加最多 2 次重试。超时、API 失败和响应纠错共用额度；成功的纯聊天与下一次行动属于不同交互。
- 重试发生在当前 Tick 内，不增加 Tick 编号，不消耗新的游戏 Tick。
- 只重试出错的请求，成功的行动不重复请求；该玩家仍可另行收到聊天通知。
- 重试使用同一份请求快照和权限，仅补充该玩家的纠错信息；新消息等后续交互再提供。
- 有行动权限的请求最终失败，中止本局并记录技术错误。simultaneous 丢弃本 Tick 暂存游戏行动；alternating 不执行当前失败响应，也不再请求下一方。两者都保留已完成状态及公开聊天。
- 仅聊天请求最终失败则跳过，不扣行动、不判输；之后等新消息、状态通知或行动机会才再次请求。合法沉默同样不触发重复请求。

请求时限和间隔的填写方法见 [配置模板](../config.example.toml)。HTTP 408、409、429 和 5xx 可重试，其他 HTTP 错误直接结束本次交互，再按上述权限决定中止或跳过。HTTP 客户端不额外自动重试。

## 模型输入与输出约定

### 输入状态

每场比赛以规则提示开始；每次请求保留本席位之前的 user/assistant 历史，再追加当前 JSON 状态。对局消息含 `phase`、`state`、`correction`，BO 调度时另有 `match`；phase=game 表示有行动权限，phase=chat 表示仅聊天。提示词在 `core/prompts.py`，按模式与分析配置组合；`POSTMATCH_PROMPT` 仅为一句邀请，不另起对话。

提示词只包含比赛目标、信息边界、模式规则与统一输出约束，不规定分析步骤或策略。修改提示词不能改变 `core/commands.py` 的指令校验或 `core/game.py` 的裁判规则。

| `state` 字段 | 含义 |
| --- | --- |
| `player` | 自己的席位与名称，如 Red · gpt-6-luna 或 Blue · gpt-6-luna |
| `can_act` | 本次请求能否提交完整行动，生成期间不会升级；false 仅允许聊天或沉默 |
| `request_id` | 本局内从 1 递增的交互编号，双方共用序列；重试保持不变 |
| `request_reasons` | 合并后的请求触发原因，详见下表；权限始终以 can_act 为准 |
| `event_cursor` | 本次快照中最后一条公开事件的序号；没有事件时为 0 |
| `history_after` | 本席位此前已提供并保留的本局事件边界；只存在于模型增量请求中 |
| `notifications` | 自上次请求后合并通知的对手 SAY 序号；生成期间的新消息下次再提供 |
| `move_policy` | 本场模式 locked 或 revisable |
| `own_move_locked` | locked 模式已提交时为 true，其余为 false |
| `initiative` | simultaneous 同时行动，alternating 轮流行动 |
| `first_player` | alternating 本局先手的席位与名称；simultaneous 为 null |
| `own_turn` | 当前或下一次自己的行动编号；仅聊天请求不会消费该机会 |
| `max_turns_per_player` | 每方行动上限；alternating 为 20，同时改拳模式预定结算后随结算 Tick 更新 |
| `remaining_turns_including_current` | 自己含本次的剩余行动机会 |
| `tick` | 全局 Tick；同时行动常规 1 至 20、特殊结算刻可为 21；轮流行动 1 至 40 |
| `max_ticks` | 当前已知的终止 Tick；同时行动为 20 或 `settlement_tick`，轮流行动为 40 |
| `submission_deadline_tick` | 同时行动为 20，轮流行动为 40；后者每方均有 20 次机会 |
| `settlement_tick` | revisable 预定的结算 Tick，尚未预定或 locked 时为 null |
| `is_settlement_tick` | 是否为 revisable 独立结算刻；locked 始终 false，不代表本 Tick 不会结束 |
| `remaining_ticks_including_current` | 包含当前 Tick、到当前已知终止 Tick 的剩余次数；结算刻为 1 |
| `own_move` | 自己的当前拳型，包含同时模式中已暂存的拳型；未提交时为 `null` |
| `opponent_committed` | 对手是否已在此前 Tick 提交出拳 |
| `history` | 本局 history_after 之后、event_cursor 以内的新公开事件；较早事件保留在此前消息中，可能包含聊天、提交/修改状态、WAIT、提醒等 |

state.history 每条事件包含 `sequence`、`tick`、`kind`、`player` 和 `text`，sequence 在本局内从 1 开始，等于公共事件的顺序位置。结算前的提交或改拳事件不包含拳型。系统消息的 player 为 null。模型请求中，上一局与赛后报告的每局记录也带 history_after、event_cursor，其 events 带 sequence 并仅包含未读事件；局号和 sequence 共同标识事件，文本相同的两次发言不会合并。

增量只作用于模型消息的构建，裁判视图、存档和人类可见的公开记录保留完整事件。空 history / events 只说明没有新增事件，不表示本局没有发生事件。双方因请求时序而收到不同增量，但结合各自保留的历史能够还原相同的公开记录。

| `request_reasons` 值 | 含义 |
| --- | --- |
| `game_start` | 本局开局通知，双方均收到 |
| `turn_changed` | alternating 行动轮换通知，双方均收到 |
| `tick_started` | simultaneous 进入新 Tick，双方均收到 |
| `action_ended` | simultaneous 自己先结束行动、等待对手，只通知本人 |
| `action_available` | 本次具有行动权限，包含纯聊天之后仍待完成的行动 |
| `opponent_say` | 合并收到对手发言，序号见 notifications |

触发原因在派发请求时消费，重试保持不变。忙碌期间到来的通知留到下一次请求；新请求使用最新视图，通知不额外占用行动次数，也不保证聊天一定在结算前完成。

`correction` 在每次交互的首次请求时为 null；重试时为仅提供给该玩家的错误反馈。历史保留自己已收到的反馈和完整响应，包括随后判为格式无效的响应；无效动作不执行。网络失败不追加未配对的用户消息，也不推进已提供事件边界。重试固定使用原状态和原增量，避免消费生成期间的新消息。被结算或取消丢弃的整次交互一并回退历史、各局事件边界及结果已提供标记，不把对手原始响应、私有分析或错误反馈混入上下文。

`match` 包含当前局号 `game`、`best_of`、`target_wins`、`max_games`、出拳模式 `move_policy`、行动模式 `initiative`、本局先手 `first_player`、累计胜局 `wins`（以席位与名称为键）和平局数 `draws`。第一局的 `last_game` 为 null；此后首次提供上一局结果时附带 last_game，包含局号、尚未提供的公开事件（含揭晓拳型）、结束原因和胜者。该次交互后的新请求省略 last_game，重试仍保留原快照。更早记录保留在对话历史中，首次赛后请求补齐最后一局的事件和结果。

### 输出格式

顶层仅允许 actions 数组，仅聊天权限下可为空表示沉默。完整行动示例：

```json
{"actions":[{"type":"ANALYZE","text":"先提交选择，同时观察交流。"},{"type":"MOVE","move":"Rock"},{"type":"SAY","text":"我决定好了。"},{"type":"ENDTURN"}]}
```

等待示例（分析 required）：

```json
{"actions":[{"type":"ANALYZE","text":"等待对手进一步表态。"},{"type":"ENDTURN"}]}
```

analysis 为 off 或 optional 时，单独 `{"actions":[{"type":"ENDTURN"}]}` 也是有效行动。phase 为 postmatch 时只允许 SAY 和 EXIT，见下文「赛后公开与赛后聊天」。

独立聊天示例（不要求分析或 ENDTURN）：

```json
{"actions":[{"type":"SAY","text":"先聊一句。"}]}
```

### 模型服务边界

模型正文统一要求 JSON 指令；Chat Completions 请求 JSON 对象输出，Claude 原生 Messages 使用共同提示词与相同解析校验。参数映射、鉴权、响应组装与流式结束条件见 [模型适配说明](model-adapters.md)。配置只选择服务与参数，不改变裁判规则。

## 赛后公开与赛后聊天

整场 BO 产生冠军或达到总局数上限后，先保存结果，再公开所有已完成 Tick 的有效动作、正文策略分析、公开事件与结果；单局结束不提前公开分析。技术异常或用户中止不自动公开分析或开始赛后聊天。

公开材料来自内存中的 MatchArchive，不通过读取日志恢复。锁定后被忽略的改拳以 ignored_actions 单列；失败响应、未完成 Tick 暂存动作、私有纠错和接口思考不进入公共报告。未提交 ANALYZE 时 summary 为 null，不补写分析。公共材料先过滤本次密钥，再向 CLI 展示、向模型增量提供，双方能够获得的公共资料一致。

### 延续本场历史

保留每位玩家自己的全部本场 user/assistant 消息与规则提示。首次请求追加最终结果、各局有效动作与公开分析，公开事件按各席位已保留的边界补齐，并附简短聊天邀请；双方的私有历史不合并，接口思考不回填。对话历史统一管理为消息列表，由适配器转换为对应协议；Claude 将规则放入顶层 system，普通对话保持原顺序。

每次请求带 phase=postmatch、player、request_id、event_cursor、events、exited、request_reasons、correction；只有首次邀请包含 match 报告。events 是自上次请求以来的新赛后公开事件，sequence 从 1 递增，kind 为 say 或 exit，发言另带 text。exited 按玩家记录退出状态。快照在同一请求的重试间不变，生成期间的新消息下一次提供。

初次同时邀请双方；之后由对手新发言或退出触发请求，忙碌时合并通知，每位玩家最多一个在途请求。最后留在聊天中的模型发言后可继续响应；返回沉默便停止请求，不因对手已经退出而空转。不设固定聊天轮数，不要求讨论赛况，也不要求 ANALYZE 或 ENDTURN。

request_reasons 的值为 postmatch_start（首次邀请）、opponent_event（对手新发言或退出）、operator_resume（终端恢复）、continue_after_exit（对手退出后继续发言）；一次请求可以合并多个原因。

接受一个或多个 SAY、EXIT 或空 actions；EXIT 无参数、最多一次且必须在最后，可先 SAY 再 EXIT：

```json
{"actions":[{"type":"SAY","text":"刚才那局很有意思，下次再玩！"},{"type":"EXIT"}]}
```

只想离开可返回 `{"actions":[{"type":"EXIT"}]}`；`{"actions":[]}` 表示暂时沉默，仍留在聊天中，不发布空消息、不立即重试或唤醒对手。整份校验通过后按顺序公开发言与退出；一方退出后不再请求它，双方 EXIT 后才正常结束。人类通过 /say 与 /exit 持续输入，不受 API 超时限制，也不需要等待对手回复才能再次发言或退出。

人类在场时保持输入接收，新发言可唤醒沉默模型。没有任何在途请求、但仍有玩家未退出时，调度器等待 on_idle 外部恢复；CLI 显示赛后控制提示，操作者输入 /resume 邀请所有仍在场的模型响应一次，不产生玩家发言、不恢复已退出席位。再次沉默则继续待机。没有交互终端或调用方未提供恢复回调时，等待外部取消，不自动请求模型或伪造结束。Ctrl+C 中止并保留日志。

赛后聊天不增加行动、不修改比分；关闭策略分析不会关闭赛后聊天。新 BO 的 start_match 清空上一场对局和赛后聊天历史。

### 失败处理与展示

每次模型响应独立最多三次尝试，格式错误、超时与可重试接口错误共用额度；重试保留自己的响应与反馈，使用同一快照。最终失败时中止赛后聊天，取消其他未应用请求并回退其私有历史；诊断和实际用量保留。失败不伪造 EXIT，网络失败和失败流片段不回填历史。人类输入错误只本地提示。

发言按段落在 CLI 和 chat.log 中展示，退出显示系统消息。thought.log 记录接口思考、校验后的拟发送内容与失败状态；debug.log 区分响应校验、实际发布和丢弃。技术中止返回 4，已公开消息与比分保留；双方 EXIT 后冠军比赛状态为 0、未决比赛为 3。单场模式直接退出，连续比赛模式进入大厅，大厅 /exit 返回最后一场的状态。日志故障返回 1，用户中止返回 130。
