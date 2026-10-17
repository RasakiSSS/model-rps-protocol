# 模型适配与配置

v0.8.0 将模型服务参数与比赛逻辑分开。`adapters.py` 定义参数、协议、地址和鉴权，`transport.py` 负责 HTTP 与流式生命周期，`anthropic.py` 转换原生 Messages 响应。模型玩家继续管理自己的历史，裁判、调度、指令校验和重试策略共用原有实现。

## 选择模型

在 `config.toml` 中保存模型条目，然后由席位引用：

```toml
[profiles.claude]
adapter = "anthropic"
model = "claude-sonnet-5-5"
base_url = "https://api.anthropic.com/v1"
api_key = "填写这个服务的密钥"
thinking = "high"

[players.red]
profile = "claude"

[players.blue]
profile = "claude"
thinking = "medium"
```

这会建立两个独立的 Claude 玩家，分别使用 high / medium。相同 profile 只共享配置值，不共享历史、状态、传输连接中的响应或日志。每个玩家仍然最多只有一个在途请求。

完整六模型及人类 profile 模板见 [config.example.toml](config.example.toml)。未选中的模型允许密钥为空，但所有配置表的字段和思考档位仍须合法。profile 名称自行定义；例如 `claude` 是配置名，`anthropic` 是固定适配器名，`claude-sonnet-5-5` 是服务模型标识，三者含义不同。

CLI `--red-profile` / `--blue-profile` 选择条目。玩家字段按 **CLI > 席位显式字段 > 选中条目 > 默认值** 合并。切换 profile 不继承之前条目的字段；席位上显式写下的覆盖项继续有效。如果希望更换模型时一起更换地址和密钥，应将这些字段放在 profiles 条目中。

`profile` 只属于席位，不支持 profile 嵌套继承。`type` 可放在 profile 或席位中，接受 llm / human，默认 llm。模型 profile 必须提供 adapter；人类 profile 只接受 type、name、analysis，无需 API key：

```toml
[profiles.human]
type = "human"
name = "小明"
analysis = "optional"
```

用 `--red-profile human` 或 `--blue-profile human` 选择人类，一个终端最多一位。席位显式 type 会覆盖 profile；希望随 profile 切换时应省略席位 type。已有的完整席位配置仍可使用；旧 `[models.名称]` 继续接受原有模型字段及流式设置，与 profiles 共用名称，禁止重名。新增适配器不读取旧 `local_secrets.py`，请在 TOML 中填写密钥。

## 参数方案

以下为本版本支持的方案。思考设置影响对局、独立聊天和赛后聊天；服务仍可能拒绝某些模型或参数，程序不会自动降级或换模型。

| adapter | 接口 | thinking | 请求映射 |
| --- | --- | --- | --- |
| openai | Chat Completions | off / low / medium / high / xhigh / max | reasoning_effort；off 映射 none；max_completion_tokens |
| deepseek | Chat Completions | off / low / high / max | thinking.type=enabled 与 reasoning_effort；off 使用 disabled 并省略 effort；max_tokens |
| glm | Chat Completions | low / high / max | thinking.type=enabled 与 reasoning_effort；max_tokens |
| gemini | 官方 OpenAI 兼容 Chat Completions | low / medium / high | reasoning_effort；extra_body.google.thinking_config.include_thoughts=true；max_tokens |
| grok | Chat Completions | low / medium / high / xhigh | reasoning_effort；max_tokens |
| anthropic | 原生 Messages | low / medium / high / xhigh / max | thinking.type=adaptive、display=summarized、output_config.effort；max_tokens |

新增方案分别面向 GLM-5.3、Gemini 4 Argon、Grok 4.7、Claude Sonnet 5.5，默认 high，不提供 off。不同代际的模型可能需要其他参数；仅修改 model 不会自动生成另一套适配方案。

参数依据（核对日期：2026-10-17）：

- [GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3)：支持 low / high / max，使用思考模式。
- [Gemini OpenAI 兼容接口](https://ai.google.dev/gemini-api/docs/openai)：使用 reasoning_effort，另请求可用的思考摘要；不同时设置 thinking_level 或 thinking_budget。
- [Grok reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning)：Grok 4.7 支持 low / medium / high / xhigh。
- [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)：Sonnet 5.5 使用 output_config.effort 与 adaptive thinking。
- [Claude OpenAI SDK 兼容限制](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)：兼容层不能等同于原生思考接口，因此本项目采用 Messages。

Chat Completions 使用 Bearer 鉴权，在 base_url 后追加 `/chat/completions`，请求 `response_format.type=json_object`。Claude 使用 `x-api-key` 与 `anthropic-version: 2023-06-01`；base_url 为服务根地址时追加 `/v1/messages`，已有 `/v1` 时追加 `/messages`。第三方服务必须支持对应端点和鉴权方式。不要把完整端点写进 base_url。

Claude 原生接口不发送 Chat Completions 的 response_format 或 stream_options；JSON 由共同提示词要求，再由共同指令层严格校验。不修复或执行不完整的模型 JSON。

## 响应、思考与流式

模型 profile 与席位均可单独设置 stream、stream_first_timeout_seconds、stream_idle_timeout_seconds，未填写的字段继承 app。优先级为 **席位 CLI > 全局 CLI > 席位配置 > profile > app > 内置值**。`--stream` / `--no-stream` 临时覆盖双方；`--red-stream` / `--no-red-stream`、`--blue-stream` / `--no-blue-stream` 只覆盖对应席位，等待时限也有 red / blue 参数。人类不使用这些时限，整次模型请求总时限仍由 app.timeout_seconds 控制。

同一场比赛可以混用流式与非流式；每席在对局、独立聊天和赛后聊天中沿用自己的设置。match_start.player_streaming 记录各席位生效值，request_config 记录每次实际请求的设置。共同规则仍然是完整接收、校验后执行；半截流、超时、协议错误和格式错误都进入原有重试机制，不产生有效游戏动作。

- Chat Completions 合并 content 和可用的 reasoning_content / reasoning，要求 finish_reason=stop 与 `[DONE]`。
- Claude 非流式将 text 块合并为正文，thinking 块合并为私有思考摘要；要求 stop_reason=end_turn。
- Claude 流式按 message_start、内容块与增量、message_delta、message_stop 组装；缺失结束事件、乱序、错误事件、工具调用和输出截断均失败。ping 不延长等待时限。协议依据：[Messages 流式文档](https://platform.claude.com/docs/en/build-with-claude/streaming)。
- Claude 的签名和 redacted_thinking 加密内容不作为可读分析展示；HTTP 原始响应仍只保留在私有调试记录中。

服务返回的思考文本只进入私有日志，不进入对手视图、不回填历史，也不代替必填的 ANALYZE。未返回思考文本记为 not_returned，不能据此判断模型是否执行了思考。Claude adaptive 可以根据任务选择思考量；不同网关也可能不返回摘要。本项目不从正文补造思考内容。

统一上下文用量记录保留服务的 usage。Claude 的输入统计将 input_tokens、cache_creation_input_tokens、cache_read_input_tokens 相加，包含缓存命中的上下文；不是计费金额估算。缺失或无效用量不当作零，未提供独立思考 token 数时保留 null。无工具调用时，历史只回填普通正文，符合 [Claude thinking 文档](https://platform.claude.com/docs/en/build-with-claude/thinking) 的对话处理方式。

## 本次实测边界

2026-10-17 使用本地已配置的第三方服务，以 high、20,000 token 上限测试了新模型的对局与赛后请求；密钥不在仓库中。结果只反映测试时这些服务的行为，不保证其他地址或未来响应相同。

| 模型 | 非流式对局及赛后 | 流式 | 观察 |
| --- | --- | --- | --- |
| GLM-5.3 | 通过 | 通过 | 返回正文和思考文本 |
| Gemini 4 Argon | 通过 | 取得有效对局响应 | 有首包超时及连接中断；成功响应未提供思考文本 |
| Grok 4.7 | 通过 | 当前第三方服务返回的正文 JSON 无效 | 原始 SSE content 直接拼接与适配器结果一致，多个动作之间缺少 JSON 字符 |
| Claude Sonnet 5.5 | 通过 | 通过 | 原生 Messages 可用；此次响应未提供思考文本 |

另以本地原有的 100,000 token 上限补测单次非流式对局：Claude、Grok 通过，GLM 连接中断，Gemini 返回 HTTP 429。后两项未能验证这一上限的兼容性；这些错误本身不能证明由 token 上限造成。

当前 Grok 服务建议在它的 profile 中设置 stream=false，或单独关闭对应席位流式：

```powershell
.\.venv\Scripts\python.exe -m model_rps --red-profile grok --blue-profile glm --no-red-stream --bo 3
```

Gemini 流式等待较长时，可在其 profile 设置 stream_first_timeout_seconds=90，必要时同时调整总时限；增加等待时间不保证服务成功。较晚补测的 HTTP 429 正文为 get_channel_failed / no untried channel capacity，指向第三方通道分配失败；不据此推断用户请求频率或 token 上限就是原因。请求参数被接受只能证明请求兼容，不能证明第三方服务实际采用了指定思考强度。

输出上限仍由 `[app].max_output_tokens` 统一设置，程序不会静默截小。已有配置采用较高上限时，应核对目标模型：Gemini 4 Argon 普通同步请求的官方上限为 65,536，本版没有接入 long decode。可以在切换时使用 `--max-output-tokens 20000`，与本次完整对局 / 赛后请求测试的设置一致。参见 [Gemini 4 Argon 模型限制](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon)。

自动化测试不调用真实服务，覆盖配置覆盖与校验、请求映射、原生流式和非流式、缓存用量、私有思考隔离、断流、取消及混合协议 BO。真实请求测试与模拟测试的结论分别记录，避免把 HTTP 200 当作完整游戏响应成功。
