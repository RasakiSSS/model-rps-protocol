# 模型服务适配

本文记录现有适配实现及带日期的验证结果。`adapters.py` 定义参数、协议、地址和鉴权，`transport.py` 负责 HTTP 与流式生命周期，`anthropic.py` 转换原生 Messages 响应。模型玩家继续管理自己的历史，裁判、调度、指令校验和重试策略共用原有实现。

玩家条目、字段、档位、流式参数与覆盖顺序统一在 [config.example.toml](config.example.toml) 中说明；启动参数见 [README](README.md)。本文只描述接口转换，不重复填写教程。

## 参数方案

以下为本版本支持的方案。思考设置影响对局、独立聊天和赛后聊天；服务仍可能拒绝某些模型或参数，程序不会自动降级或换模型。

| adapter | 接口 | 请求映射 |
| --- | --- | --- |
| openai | Chat Completions | reasoning_effort；off 映射 none；max_completion_tokens |
| deepseek | Chat Completions | thinking.type=enabled 与 reasoning_effort；off 使用 disabled 并省略 effort；max_tokens |
| glm | Chat Completions | thinking.type=enabled 与 reasoning_effort；max_tokens |
| gemini | OpenAI 兼容 Chat Completions | reasoning_effort；extra_body.google.thinking_config.include_thoughts=true；max_tokens |
| grok | Chat Completions | reasoning_effort；max_tokens |
| anthropic | 原生 Messages | thinking.type=adaptive、display=summarized、output_config.effort；max_tokens |

这些方案分别按对应模型代际实现；不能从修改模型标识推断跨代兼容。

参数依据（核对日期：2026-10-17）：

- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) 与 [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)：既有两种 Chat Completions 方案的参数来源。
- [GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3)：支持 low / high / max，使用思考模式。
- [Gemini OpenAI 兼容接口](https://ai.google.dev/gemini-api/docs/openai)：使用 reasoning_effort，另请求可用的思考摘要；不同时设置 thinking_level 或 thinking_budget。
- [Grok reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning)：Grok 4.7 支持 low / medium / high / xhigh。
- [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)：Sonnet 5.5 使用 output_config.effort 与 adaptive thinking。
- [Claude OpenAI SDK 兼容限制](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)：兼容层不能等同于原生思考接口，因此本项目采用 Messages。

Chat Completions 使用 Bearer 鉴权，在 base_url 后追加 `/chat/completions`，请求 `response_format.type=json_object`。Claude 使用 `x-api-key` 与 `anthropic-version: 2023-06-01`；base_url 为服务根地址时追加 `/v1/messages`，已有 `/v1` 时追加 `/messages`。第三方服务必须支持对应端点和鉴权方式。

Claude 原生接口不发送 Chat Completions 的 response_format 或 stream_options；JSON 由共同提示词要求，再由共同指令层严格校验。不修复或执行不完整的模型 JSON。

## 响应、思考与流式

流式和非流式都通过同一玩家、指令校验和重试流程。首包、数据间隔与总时限共同约束接收过程，数值及继承规则见配置模板。

同一场比赛可以混用流式与非流式；每席在对局、独立聊天和赛后聊天中沿用自己的设置。match_start.player_streaming 记录各席位生效值，request_config 记录每次实际请求的设置。共同规则仍然是完整接收、校验后执行；半截流、超时、协议错误和格式错误都进入原有重试机制，不产生有效游戏动作。

v0.11.1 对开启流式的 Grok 适配器自动追加一条系统格式约束：JSON 使用缩进换行，每个对象的左花括号后换行，字段名放到下一行。约束在 prompts.py 中维护，覆盖对局、独立聊天、重试、接管、新 BO 和赛后；依据 adapter 判断，不绑定模型名或服务域名。Grok 非流式和其他适配器不增加这条要求。配置仍使用原有流式开关，不新增字段。

该措施绕过下文已复现的部分服务缺字符问题，并不修改服务端。响应仍须完整结束且通过原有严格校验；不自动补符号，不以提示词约束替代校验，也不自动改为非流式。

- Chat Completions 合并 content 和可用的 reasoning_content / reasoning，要求 finish_reason=stop 与 `[DONE]`。
- Claude 非流式将 text 块合并为正文，thinking 块合并为私有思考摘要；要求 stop_reason=end_turn。
- Claude 流式按 message_start、内容块与增量、message_delta、message_stop 组装；缺失结束事件、乱序、错误事件、工具调用和输出截断均失败。ping 不延长等待时限。协议依据：[Messages 流式文档](https://platform.claude.com/docs/en/build-with-claude/streaming)。
- Claude 的签名和 redacted_thinking 加密内容不作为可读分析展示；HTTP 原始响应仍只保留在私有调试记录中。

服务返回的思考文本只进入私有日志，不进入对手视图、不回填历史，也不代替必填的 ANALYZE。未返回思考文本记为 not_returned，不能据此判断模型是否执行了思考。Claude adaptive 可以根据任务选择思考量；不同网关也可能不返回摘要。本项目不从正文补造思考内容。

统一上下文用量记录保留服务的 usage。Claude 的输入统计将 input_tokens、cache_creation_input_tokens、cache_read_input_tokens 相加，包含缓存命中的上下文；不是计费金额估算。缺失或无效用量不当作零，未提供独立思考 token 数时保留 null。无工具调用时，历史只回填普通正文，符合 [Claude thinking 文档](https://platform.claude.com/docs/en/build-with-claude/thinking) 的对话处理方式。

## 实测记录：2026-10-17

2026-10-17 使用本地已配置的第三方服务，以 high、20,000 token 上限测试了新模型的对局与赛后请求；密钥不在仓库中。结果只反映测试时这些服务的行为，不保证其他地址或未来响应相同。

| 模型 | 非流式对局及赛后 | 流式 | 观察 |
| --- | --- | --- | --- |
| GLM-5.3 | 通过 | 通过 | 返回正文和思考文本 |
| Gemini 4 Argon | 通过 | 取得有效对局响应 | 有首包超时及连接中断；成功响应未提供思考文本 |
| Grok 4.7 | 通过 | 当前第三方服务返回的正文 JSON 无效 | 原始 SSE content 直接拼接与适配器结果一致，多个动作之间缺少 JSON 字符 |
| Claude Sonnet 5.5 | 通过 | 通过 | 原生 Messages 可用；此次响应未提供思考文本 |

另以本地原有的 100,000 token 上限补测单次非流式对局：Claude、Grok 通过，GLM 连接中断，Gemini 返回 HTTP 429。后两项未能验证这一上限的兼容性；这些错误本身不能证明由 token 上限造成。

Gemini 较晚补测的 HTTP 429 正文为 get_channel_failed / no untried channel capacity，指向第三方通道分配失败；不据此推断用户请求频率或 token 上限就是原因。请求参数被接受只能证明请求兼容，不能证明第三方服务实际采用了指定思考强度。

当次核对的 [Gemini 4 Argon 模型文档](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon) 列出的普通同步输出上限为 65,536；本版没有接入 long decode。该记录不代表其他服务或未来版本的上限，程序不会据此自动改写输出额度。

自动化测试不调用真实服务，覆盖配置覆盖与校验、请求映射、原生流式和非流式、缓存用量、私有思考隔离、断流、取消及混合协议 BO。真实请求测试与模拟测试的结论分别记录，避免把 HTTP 200 当作完整游戏响应成功。

## Grok 流式问题复测：2026-10-18

使用本地已配置的第三方服务，对 grok-4.7 发送相同的开局消息，采用 high、20,000 token 输出上限；每个方案各测试一次，不进行自动重试，不改写运行配置。

| 方案 | 结果 |
| --- | --- |
| 非流式，json_object | JSON 与游戏指令校验通过 |
| 流式，json_object | JSON 无效 |
| 流式，严格 json_schema | JSON 无效 |
| 流式，省略 response_format | JSON 无效 |
| 流式，省略 stream_options | JSON 无效 |

四次流式响应均为 HTTP 200，收到 finish_reason=stop 和 `[DONE]`，应用的 SSE 解码通过。但动作对象之间出现 `},type":`，缺少下一对象开头的 `{"`。直接解析原始 SSE 并拼接 delta.content，与应用结果逐字相同；原始事件中也没有缺失的字符。这排除了这些样本中由本地拼接造成缺字，错误发生在收到的数据中，尚不能区分第三方网关与其上游。

本轮没有验证官方直连；不能据此判断 Grok 官方接口普遍存在此问题。[官方结构化输出文档](https://docs.x.ai/developers/model-capabilities/text/structured-outputs)说明支持流式结构化输出，但当前服务的实测没有因更换格式参数而改善。Grok 可单独设为非流式；下文的换行格式实验提供了另一个待继续验证的绕过方向。继续严格校验完整指令，不猜测并补写缺失内容。

上述五次请求只说明本轮样本的结果，不是稳定性统计。脱敏的原始响应、逐片正文、请求参数和结果摘要保存在本地 `logs/_grok_validation/`，受 Git 忽略规则保护。

### 同日最小复现与换行格式实验

移除游戏提示词，只要求逐字返回固定 JSON，共测试 12 次请求，仍使用同一服务、high 和 20,000 token 上限。

| 固定内容 | 流式结果 | 非流式对照 |
| --- | --- | --- |
| 数组含单个对象，紧凑格式 | 1/1 正确 | 1/1 正确 |
| 数组含三个对象，紧凑格式 | 0/3 正确；后两个对象都缺少 `{"` | 1/1 正确 |
| 同样的三个对象，缩进换行 | 3/3 正确 | 1/1 正确 |
| 同样的三个对象，在括号与字段之间加空格 | 1/1 正确 | 未测 |
| 两个嵌套对象，紧凑格式 | 0/1 正确；两个内层对象都缺少 `{"` | 未测 |

例如要求 `{"items":[{"x":1},{"x":2},{"x":3}]}`，三次流式实际均为 `{"items":[{"x":1},x":2},x":3}]}`。所有流式样本均收到 stop 与 `[DONE]`，独立提取原始片段和项目解析结果相同。成功样本不仅是合法 JSON，也与要求逐字一致。这说明问题与输出形式有关，并非游戏指令特有；仍无法确定上游内部的具体原因。

随后使用真实对局提示词，仅在临时请求中追加“输出 JSON 使用缩进换行格式，每个对象的左花括号后先换行，字段名放到下一行；不要使用紧凑单行 JSON。只改变空白，不改变指令字段或规则。”，三次流式开局请求全部通过 JSON 与完整行动校验。

这一阶段仅在临时请求中验证了三个开局响应；正式兼容实现与后续回归属于 v0.11.1。实验没有补写损坏响应，不能作为服务端问题已根治的结论。实验原始记录仍保存在本地 `logs/_grok_validation/`。

### v0.11.1 接入后的验证

同日使用正式 Grok 流式玩家与固定脚本对手，完成 alternating / locked 的 BO3：三局结束，比分 1:2，无平局，双方正常退出赛后聊天。共发起七次真实请求，三次行动、一次独立聊天、一次赛后响应有效；另两次聊天在结算时正常取消。五份完整响应均通过原有 JSON 和指令校验，没有格式重试，所有请求都只包含一份格式约束。测试临时启用流式，未修改本地 config.toml。

246 项离线测试通过，覆盖 simultaneous / alternating、locked / revisable、BO 跨局、纠错重试、独立聊天、赛后、新比赛和交接资料导入；也验证其他适配器及 Grok 非流式不追加该约束，损坏 JSON 仍被拒绝。真实 BO 的原始日志和摘要位于本地 `logs/_grok_validation/v0.11.1/20261018_193817_525188_08777fe5/`。这是一场真实服务验证，不代表所有服务及长期运行都已无此问题。
