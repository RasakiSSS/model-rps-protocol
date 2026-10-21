# 日志与展示

适用版本：v1.0.1 及 `codex/mcp-agent` 开发分支。

本文约定每场比赛写入的日志文件、字段含义、脱敏与写入失败策略，以及终端前台与调试窗口展示什么。操作层面的排错见 [命令行与终端操作](cli.md#日志与排错)。

## 日志

每场 BO 默认在 `logs/时间戳_唯一编号/` 下创建五份 UTF-8 日志。连续比赛的每场也使用新子目录，不覆盖旧记录。

| 文件 | 内容与可见性 |
| --- | --- |
| `chat.log` | 人类可读的 CLI 事件、系统消息、局号与 Tick、每局结果、累计比分、比赛结果、赛后公开记录及双方赛后聊天；结算前不展示实际拳型，整场比赛结束前不展示策略分析 |
| `red/thought.log` | Red 的有效动作与正文策略分析、通过校验的赛后响应，以及接口思考内容或缺失状态；未提交 ANALYZE 时内部 summary 为 null，失败或取消另记状态 |
| `red/debug.log` | Red 的输入视图、纠错反馈、HTTP 请求正文、HTTP 状态和响应正文、原始模型输出、可用的 token 用量、请求耗时、校验、重试、取消和比赛生命周期 |
| `blue/thought.log` | Blue 的策略分析、赛后聊天和思考记录 |
| `blue/debug.log` | Blue 的请求诊断，与 Red 日志规则相同 |

新日志的 player、winner、players 和 thinking 使用带 Red / Blue 的显示名称，wins 为该名称到胜局数的映射。目录固定为 red 和 blue，不从模型名或昵称生成路径。模型发言、策略分析和 HTTP 原文不改写。比赛开始记录模式、思考和分析设置；request_config 记录实际模型与请求参数。

debug.log 每行一个 JSON 对象，带时间戳和本场统一递增的 log_sequence；后者跨双方文件编号，用于还原同一毫秒内的写入顺序，不是公开事件编号。请求记录含局号、快照 Tick、玩家、阶段 phase（game/chat/postmatch），每次尝试带 attempt（1 至 3）。每次交互有 request_id，重试沿用编号。

request_start.state.request_reasons 记录赛中触发原因。interaction_applied 记录聊天发布或行动接收时的 Tick、聊天事件序号、发言数、是否结束行动或保持沉默；pending_action 表示行动仍在等待双方同步推进。action_resolved 记录裁判实际执行的 Tick、首次出拳/修改/等待/忽略结果及本方实际拳型，沿用原请求编号与作者。尚未推进便中止的行动另记 action_discarded；结算及取消时未应用的请求记 request_discarded。

赛后使用局号 0、Tick 0，postmatch_response 记录通过校验的响应，postmatch_applied 与 postmatch_event 记录实际发布的消息及退出，postmatch_end 记录双方退出状态与技术错误。模拟玩家不产生 HTTP 或用量事件；前台和 chat.log 仅展示实际公开的消息。

request_start 保存完整裁判快照，http_request 保存实际发给服务的增量消息及历史。赛后触发原因在 request_start.context.request_reasons 中；postmatch_response 与 postmatch_applied 的 silent 区分暂时沉默和退出。postmatch_idle / postmatch_resumed 记录待机与人工恢复，thought.log 明确显示暂时沉默。

chat_validated 保存通过校验的发言正文与数量；validation_error 保存校验失败原因及已取得的响应正文，尚未取得正文时为 null。错误后的 request_end 记录纠错反馈与是否重试；这些诊断不代表消息已公开。

原始 usage 按接口返回保存。每次真实请求结束或失败时另记 context_usage：input_tokens 为 prompt_tokens（兼容 input_tokens）；input_tokens_delta 为与相邻请求的变化；peak_input_tokens 为本场已知单次峰值；known_input_tokens_total 为已报告输入的累计；missing_usage_requests 记录用量缺失次数。仅接受非负整数，缺失记为 null；没有相邻完整数据时不计算变化。message_count 和 input_characters 分别统计实际发送的消息数与正文字符数，不当作 token 估算。

用量包含行动、聊天、重试、取消和赛后聊天；响应历史回退不撤销已消耗的用量，新 BO 清空统计。单次输入与累计用量分开，不以累计值判断上下文窗口是否已满，不估计剩余容量、不自动裁剪历史。流式中断可能没有用量尾包，具体协议见 [模型适配](model-adapters.md)。

thought.log 使用中文分段文本，按局号、Tick、响应编号、尝试次数和阶段区分接口思考、正文分析、动作、赛后响应与失败记录，聊天请求另加标记。正文保留换行、统一缩进并转义危险控制字符；缺失用量显示“未提供”。decision、reasoning、postmatch_response、request_end 等完整结构化事件存入 debug.log。通过校验的拟发送内容不等于实际发布，须以 chat.log 和应用事件为准。旧日志不改写，文件总数仍为五份。

阅读版的“通过校验”（debug 中 validated）不代表动作已经执行。simultaneous 中对手同 Tick 行动失败时，双方暂存的游戏行动丢弃，已公开聊天保留；alternating 后续失败不撤销此前行动。以 chat.log 和 game_end 为实际结果。decision.actions 排除锁定后忽略的 MOVE，后者单独存入 ignored_actions。纯聊天进入公共 events，不生成行动分析记录；无效响应文字仅留在私有日志。

不记录鉴权头、响应头或传输异常原文；对本场配置的 API key 做脱敏，包括服务端在响应正文中回显的情况。调试原文不向对手或公开日志传播；赛后只公开经过校验且实际执行的动作和说明，并在展示及发送给另一模型前脱敏。日志目录默认被 Git 忽略。

每条记录立即写入并 flush，退出时关闭文件。用户中止或意外中断时保留已产生的记录；日志创建或写入失败时停止比赛并返回错误，不在缺失日志的情况下继续请求。强制终止或断电时不保证文件系统缓存已经落盘。

match_start.profiles 按显示席位记录选中的 profile，未引用时为 null；player_streaming 记录各席位生效的流式设置。每次 request_config 记录实际发送的模型、思考及流式参数。stream_delta 记录接收片段，stream_end 标记完整、未完成或取消，记录组装内容、结束原因与可用用量。跨分片的已知密钥前缀暂缓写入，结束时仍不完整则遮盖。

局中接管不新建日志目录。公开事件、行动和请求诊断携带固定的 `actor` 标签；展示与序列化优先使用该标签，因此历史作者不受当前席位名称影响。`handoff` 诊断记录局号、Tick、原参与者、接管者及 profile 名称，以及交接的已完成局数、公共事件数、本席位分析与行动数、保留的拳型与锁定状态、行动权限和丢弃的草稿数量；接管模型的实际参数仍由 request_config 记录。所有已加载 profile 的密钥提前加入脱敏表，旧密钥文件中的接管密钥在首次请求前补入。

### 接口思考记录

Chat Completions 读取响应消息中非空字符串 `reasoning_content`，其次读取兼容字段 `reasoning`；Claude 原生 thinking 块转换为统一的私有思考字段，text 块转换为正文，签名与加密思考不作为可读分析。只保存服务实际返回的文本，不补写、不推测完整思考。普通 assistant 内容仍按原规则进入自己的对话历史；额外思考字段不进入后续请求、对手上下文或赛后公开报告。同场 BO 仍保留公开事件、自己的最终响应、策略分析和纠错记录。

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

开启思考且已取得有效响应消息时，即使没有文本也记录 reasoning 状态；关闭时若服务仍返回文本、异常字段或正的思考用量，也如实记录。超时等未取得响应消息的情况只记录请求失败，不伪造思考事件。截断或最终 JSON 无效时可能仍有思考记录，但该记录不表示动作通过校验或已经执行。脱敏与写入故障遵守本节日志规则。

## 终端与公开展示

前台展示公共聊天、提交与等待、截止提醒、必要的重试或错误概况、比分和赛后公开内容。Red / Blue 标签分别为红色和蓝色；不支持颜色时保留文本。颜色不写入日志或模型输入。

人类私有状态从本人的 Player View 生成，标记“仅自己”，不写入公共聊天。开发者诊断窗口和磁盘日志不属于玩家可见信息；本地运行者能访问这些文件，并不意味着实现了防作弊隔离。

出拳只显示提交或修改状态；正常结算时将双方拳型与结果合为一行，比分另行显示。平局、弃权和未出拳分别标明；技术异常不揭晓保密拳型。revisable 的结算刻提示最后一次行动机会。

普通模式与调试模式的前台显示、交互相同。调试窗口只观察本场日志，不重复请求模型、不参与调度；关闭窗口不影响比赛。比赛结束后停止读取并保留内容；启动失败只报告简短提示，不把诊断信息改放到前台。连续比赛每场使用对应的观察窗口。

调试窗口展示请求原因、权限、本方状态、新消息与实际参数，分段显示校验后的 ANALYZE、SAY、MOVE、ENDTURN 和赛后响应，并显示调度执行或丢弃结果。接口思考正文、原始成功响应、流式片段和完整 HTTP 请求仅保留在日志；窗口显示思考接收状态与可用用量。服务错误只提取错误字段，无效正文与长错误文本最多显示 4,000 字符并标明省略；不改动磁盘中的完整记录。双方记录按 log_sequence 合并，旧日志按时间戳尽力排序；换人前后名称都沿用固定席位颜色。

启动参数、玩家指令、大厅与赛后恢复操作统一见 [命令行与终端操作](cli.md)。
