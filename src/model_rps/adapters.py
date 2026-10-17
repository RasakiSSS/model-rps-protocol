"""模型服务的参数方案；不参与比赛状态或玩家上下文管理。"""

from copy import deepcopy
from dataclasses import dataclass, field, replace

DEEPSEEK_THINKING_MODES = ("off", "low", "high", "max")
OPENAI_THINKING_MODES = ("off", "low", "medium", "high", "xhigh", "max")
THINKING_MODES = {
    "openai": OPENAI_THINKING_MODES,
    "deepseek": DEEPSEEK_THINKING_MODES,
    "glm": ("low", "high", "max"),
    "gemini": ("low", "medium", "high"),
    "grok": ("low", "medium", "high", "xhigh"),
    "anthropic": ("low", "medium", "high", "xhigh", "max"),
}


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model: str
    base_url: str
    token_parameter: str
    extra_body: dict = field(default_factory=dict)
    thinking_api: str | None = None
    thinking_mode: str = "off"
    protocol: str = "chat_completions"


DEEPSEEK = ModelSpec("DeepSeek V4.1 Flash", "deepseek-flash", "https://api.deepseek.com",
                    "max_tokens", {"thinking": {"type": "disabled"}}, thinking_api="deepseek")
OPENAI = ModelSpec("GPT-6 Luna", "gpt-6-luna", "https://ai.yuhuantin.icu/v1",
                  "max_completion_tokens", {"reasoning_effort": "none"}, thinking_api="openai")
MODEL_ADAPTERS = {
    "openai": OPENAI, "deepseek": DEEPSEEK,
    "glm": ModelSpec("GLM-5.3", "glm-5.3", "https://api.z.ai/api/paas/v4", "max_tokens",
                     thinking_api="glm", thinking_mode="high"),
    "gemini": ModelSpec("Gemini 4 Argon", "gemini-4-argon", "https://generativelanguage.googleapis.com/v1beta/openai",
                        "max_tokens", thinking_api="gemini", thinking_mode="high"),
    "grok": ModelSpec("Grok 4.7", "grok-4.7", "https://api.x.ai/v1", "max_tokens",
                      thinking_api="grok", thinking_mode="high"),
    "anthropic": ModelSpec("Claude Sonnet 5.5", "claude-sonnet-5-5", "https://api.anthropic.com/v1", "max_tokens",
                           thinking_api="anthropic", thinking_mode="high", protocol="anthropic_messages"),
}


def configure_thinking(spec: ModelSpec, mode: str) -> ModelSpec:
    """只发送所选方案对应的思考参数；不猜测模型后缀或静默降级。"""
    if mode not in THINKING_MODES.get(spec.thinking_api, ()):
        raise ValueError("该适配器不支持所选思考档位。")
    body = deepcopy(spec.extra_body)
    if spec.thinking_api in ("deepseek", "glm"):
        body["thinking"] = {"type": "disabled" if mode == "off" else "enabled"}
        body.pop("reasoning_effort", None)
        if mode != "off":
            body["reasoning_effort"] = mode
    elif spec.thinking_api == "anthropic":
        body["thinking"] = {"type": "adaptive", "display": "summarized"}
        body["output_config"] = {"effort": mode}
    else:
        body["reasoning_effort"] = "none" if mode == "off" else mode
        if spec.thinking_api == "gemini":
            body["extra_body"] = {"google": {"thinking_config": {"include_thoughts": True}}}
    return replace(spec, extra_body=body, thinking_mode=mode)


# 默认配置本身也可直接用于模型玩家，参数应与声明的默认思考档位一致。
MODEL_ADAPTERS = {name: configure_thinking(spec, spec.thinking_mode) for name, spec in MODEL_ADAPTERS.items()}


def request_payload(spec: ModelSpec, messages: list[dict], max_tokens: int, stream: bool) -> dict:
    payload = {**deepcopy(spec.extra_body), "model": spec.model, "messages": messages,
               spec.token_parameter: max_tokens, "stream": stream}
    if spec.protocol == "anthropic_messages":
        payload["system"] = "\n".join(m["content"] for m in messages if m["role"] == "system")
        payload["messages"] = [m for m in messages if m["role"] != "system"]
        # JSON 由共同提示词要求和共同指令层校验；不向 Messages 发送 response_format。
    else:
        payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream_options"] = {"include_usage": True}
    return payload


def endpoint(spec: ModelSpec) -> str:
    base = spec.base_url.rstrip("/")
    if spec.protocol == "anthropic_messages":
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    return base + "/chat/completions"


def auth_headers(spec: ModelSpec, api_key: str) -> dict:
    if spec.protocol == "anthropic_messages":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {api_key}"}
