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
