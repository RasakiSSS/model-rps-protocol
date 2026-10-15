"""玩家接口、模拟玩家和兼容 Chat Completions 的模型适配器。"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .game import MAX_TICKS, PlayerView
from .diagnostics import trace
from .identities import named_data
from .prompts import REVIEW_PROMPT, SYSTEM_PROMPT, game_prompt
from .streaming import CompletionStream, StreamError, sse_data

MAX_OUTPUT_TOKENS = 20000
DEEPSEEK_THINKING_MODES = ("off", "low", "high", "max")
OPENAI_THINKING_MODES = ("off", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ModelResponse:
    """原始正文与本次接口是否返回思考的标记；不携带私有思考文本。"""

    content: str
    has_reasoning: bool = False


class Player(Protocol):
    async def respond(self, view: PlayerView, feedback: str | None = None) -> str | ModelResponse:
        """返回待校验的 JSON 正文，可附带适配器确认的思考可用性。"""
        ...


class MatchPlayer(Player, Protocol):
    def start_match(self) -> None:
        """新比赛开始时清空该玩家的上下文。"""
        ...

    async def review(self, context: dict, feedback: str | None = None) -> str:
        """整场比赛结束后，根据公开的完整记录生成复盘。"""
        ...

    def start_game(self, context: dict) -> None:
        """更新当前局及比赛的公开信息，保留同场比赛的上下文。"""
        ...


class PlayerError(Exception):
    """safe_message 仅接受适配器生成的错误概况，不应包含服务器原文或密钥。"""

    def __init__(self, safe_message: str, *, retryable: bool = True) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retryable = retryable


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model: str
    base_url: str
    token_parameter: str
    extra_body: dict = field(default_factory=dict)
    thinking_api: str | None = None
    thinking_mode: str = "off"


DEEPSEEK = ModelSpec("DeepSeek V4.1 Flash", "deepseek-flash", "https://api.deepseek.com",
                     "max_tokens", {"thinking": {"type": "disabled"}}, thinking_api="deepseek")
OPENAI = ModelSpec("GPT-6 Luna", "gpt-6-luna", "https://ai.yuhuantin.icu/v1",
                   "max_completion_tokens", {"reasoning_effort": "none"}, thinking_api="openai")


def configure_thinking(spec: ModelSpec, mode: str) -> ModelSpec:
    """显式选择服务参数；不改模型标识、提示词或共享的默认配置。"""
    modes = {"deepseek": DEEPSEEK_THINKING_MODES, "openai": OPENAI_THINKING_MODES}
    if spec.thinking_api not in modes or mode not in modes[spec.thinking_api]:
        raise ValueError("该适配器不支持所选思考档位。")
    body = dict(spec.extra_body)
    if spec.thinking_api == "deepseek":
        body["thinking"] = {"type": "disabled" if mode == "off" else "enabled"}
        body.pop("reasoning_effort", None)
        if mode != "off":
            body["reasoning_effort"] = mode
    else:
        body["reasoning_effort"] = "none" if mode == "off" else mode
    return replace(spec, extra_body=body, thinking_mode=mode)


class ChatCompletionPlayer:
    def __init__(self, client: httpx.AsyncClient, spec: ModelSpec, api_key: str, *,
                 max_output_tokens: int = MAX_OUTPUT_TOKENS, names: dict[str, str] | None = None,
                 stream: bool = False, stream_first_timeout: float = 60,
                 stream_idle_timeout: float = 30) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("API key 未填写。")
        if any(ord(char) < 32 for char in api_key):
            raise ValueError("API key 包含无效字符。")
        url = urlsplit(spec.base_url)
        if (url.scheme not in ("https", "http") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("模型服务地址必须是完整的 HTTP(S) 基础地址。")
        if spec.token_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("不支持的输出长度参数。")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("输出 token 上限必须是正整数。")
        if type(stream) is not bool:
            raise ValueError("stream 必须为布尔值。")
        for limit in (stream_first_timeout, stream_idle_timeout):
            if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
                raise ValueError("流式等待时限必须为正有限秒数。")
        self.client = client
        self.spec = spec
        self._api_key = api_key.strip()
        self.max_output_tokens = max_output_tokens
        self.stream = stream
        self.stream_first_timeout = stream_first_timeout
        self.stream_idle_timeout = stream_idle_timeout
        self.names = dict(names) if names is not None else {"A": "A", "B": "B"}
        self.start_match()

    def start_match(self) -> None:
        self._messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._match_context = None

    def start_game(self, context: dict) -> None:
        # 每个玩家持有独立副本，不能通过共享可变字典影响对手视图。
        self._match_context = json.loads(json.dumps(context, ensure_ascii=False))

    async def respond(self, view: PlayerView, feedback: str | None = None) -> ModelResponse:
        self._messages[0] = {"role": "system", "content": game_prompt(view.move_policy, view.initiative)}
        context = {"state": view.to_dict(), "correction": feedback}
        if self._match_context is not None:
            context["match"] = self._match_context
        user_message = {"role": "user", "content": json.dumps(named_data(context, self.names), ensure_ascii=False)}
        response = await self._complete([*self._messages, user_message])
        # 只保留该玩家自己的响应；格式校验失败后的纠错也属于其私有上下文。
        # 网络失败没有完整响应，不追加未配对的用户消息。
        self._messages.extend([user_message, {"role": "assistant", "content": response.content}])
        return response

    async def review(self, context: dict, feedback: str | None = None) -> str:
        # 复盘用独立提示词和完整赛后记录，不修改比赛对话，也不执行动作。
        response = await self._complete([
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": json.dumps(named_data({**context, "correction": feedback}, self.names), ensure_ascii=False)},
        ])
        return response.content

    async def _complete(self, messages: list[dict]) -> ModelResponse:
        payload = {
            **self.spec.extra_body,
            "model": self.spec.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "stream": self.stream,
            self.spec.token_parameter: self.max_output_tokens,
        }
        trace("request_config", model=self.spec.model, thinking_api=self.spec.thinking_api,
              requested_thinking=self.spec.thinking_mode, max_output_tokens=self.max_output_tokens,
              stream=self.stream, stream_first_timeout=self.stream_first_timeout,
              stream_idle_timeout=self.stream_idle_timeout)
        if self.stream:
            payload['stream_options'] = {'include_usage': True}
        trace("http_request", payload=payload)
        if self.stream:
            return self._parse_completion(await self._stream_complete(payload))
        try:
            response = await self.client.post(
                self.spec.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException:
            raise TimeoutError from None
        except httpx.RequestError:
            raise PlayerError("模型服务连接失败") from None
        # 不记录请求头、响应头和传输异常原文。日志层会过滤本次运行的密钥。
        trace("http_response", status=response.status_code, body=response.text)
        if not response.is_success:
            retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
            raise PlayerError(f"模型服务返回 HTTP {response.status_code}", retryable=retryable)
        try:
            data = response.json()
        except (ValueError, RecursionError):
            raise PlayerError("模型服务返回的响应结构无效") from None
        return self._parse_completion(data)

    async def _stream_complete(self, payload: dict) -> dict:
        assembled = CompletionStream()
        status = 'incomplete'
        trace('stream_status', status='waiting')
        try:
            # 首包时限覆盖连接、响应头和第一个数据事件；心跳不能无限续命。
            async with asyncio.timeout(self.stream_first_timeout) as deadline:
                async with self.client.stream('POST', self.spec.base_url.rstrip('/') + '/chat/completions',
                                              headers={'Authorization': f'Bearer {self._api_key}'},
                                              json=payload, timeout=None) as response:
                    trace('http_response', status=response.status_code, stream=True)
                    if not response.is_success:
                        await response.aread()
                        trace('http_error_body', body=response.text)
                        retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
                        raise PlayerError(f'模型服务返回 HTTP {response.status_code}', retryable=retryable)
                    if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                        raise StreamError('模型服务未返回 SSE 流式响应')
                    async for data in sse_data(response):
                        assembled.feed(data)
                        if assembled.done:
                            break
                        deadline.reschedule(asyncio.get_running_loop().time() + self.stream_idle_timeout)
                    result = assembled.result()
            status = 'complete'
            return result
        except StreamError as exc:
            raise PlayerError(str(exc)) from None
        except TimeoutError:
            which = '首包' if not assembled.chunks else '空闲'
            raise PlayerError(f'流式{which}等待超时') from None
        except httpx.TimeoutException:
            raise TimeoutError from None
        except httpx.RequestError:
            raise PlayerError('模型流式连接中断') from None
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        finally:
            trace('stream_end', status=status, chunks=assembled.chunks, message=assembled.message,
                  usage=assembled.usage, finish_reason=assembled.finish_reason, done=assembled.done)

    def _parse_completion(self, data: object) -> ModelResponse:
        try:
            usage = data.get("usage") if isinstance(data, dict) else None
            trace("usage", usage=usage)
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise PlayerError("模型服务返回的响应结构无效")
            has_reasoning = self._record_reasoning(message, usage, choice.get("finish_reason"))
            if choice.get("finish_reason") != "stop":
                raise PlayerError("模型输出未完整结束或未返回文本内容")
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise PlayerError("模型返回空内容或非文本内容")
            return ModelResponse(content, has_reasoning)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
            raise PlayerError("模型服务返回的响应结构无效") from None

    def _record_reasoning(self, message: dict, usage: object, finish_reason: object) -> bool:
        # 只保存服务实际提供的字段，不从 decision_summary 重构或推测思考文本。
        fields = ("reasoning_content", "reasoning")
        source = next((key for key in fields if isinstance(message.get(key), str) and message[key].strip()), None)
        invalid = any(message.get(key) is not None and not isinstance(message[key], str) for key in fields)
        tokens = None
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details")
            candidate = details.get("reasoning_tokens") if isinstance(details, dict) else None
            if candidate is None:
                candidate = usage.get("reasoning_tokens")
            if type(candidate) is int and candidate >= 0:
                tokens = candidate
        if self.spec.thinking_mode != "off" or source or invalid or (tokens is not None and tokens > 0):
            trace("reasoning", requested_thinking=self.spec.thinking_mode,
                  status="returned" if source else "invalid_field" if invalid else "not_returned",
                  source=source, content=message[source] if source else None,
                  reasoning_tokens=tokens, finish_reason=finish_reason, streamed=self.stream)
        return source is not None


WAIT_RESPONSE = '{"decision_summary":"继续等待。","actions":[{"type":"WAIT"}]}'


class ScriptedPlayer:
    """按请求次数消耗预设响应；耗尽后主动 WAIT，适合演示和异常测试。"""

    def __init__(self, responses: list[str | Exception] | None = None, *, repeat_per_game: bool = False) -> None:
        self._initial_responses = tuple(responses or [])
        self.repeat_per_game = repeat_per_game
        self.start_match()

    def start_match(self) -> None:
        self.responses = deque(self._initial_responses)
        self.requests: list[tuple[PlayerView, str | None]] = []
        self.game_contexts: list[dict] = []

    def start_game(self, context: dict) -> None:
        self.game_contexts.append(json.loads(json.dumps(context, ensure_ascii=False)))
        if self.repeat_per_game:
            self.responses = deque(self._initial_responses)

    async def respond(self, view: PlayerView, feedback: str | None = None) -> str:
        self.requests.append((view, feedback))
        response = self.responses.popleft() if self.responses else WAIT_RESPONSE
        if isinstance(response, Exception):
            raise response
        return response

    async def review(self, context: dict, feedback: str | None = None) -> str:
        report = context["match"]
        result = report["result"]
        last = report["games"][-1]
        return json.dumps({
            "assessment": f"模拟复盘：本场完成 {result['games_played']} 局，比分 模拟玩家1 {result['wins']['A']} : {result['wins']['B']} 模拟玩家2，平局 {result['draws']} 局。",
            "key_moments": f"第 {last['game']} 局在第 {last['completed_ticks']} Tick 结束，原因是 {last['result']['reason']}；本场出拳规则为 {report['move_policy']}。",
            "improvements": "这些动作来自预设模拟脚本；可对照已公开的动作与说明，检查等待、改拳和结算时机。",
        }, ensure_ascii=False)


def demo_players(scenario: str) -> tuple[ScriptedPlayer, ScriptedPlayer]:
    def scripted(responses=None):
        return ScriptedPlayer(responses, repeat_per_game=True)

    def response(*actions: dict) -> str:
        if not any(action["type"] in ("MOVE", "WAIT") for action in actions):
            actions = (*actions, {"type": "WAIT"})
        return json.dumps({"decision_summary": "执行当前模拟场景的预设动作。", "actions": actions}, ensure_ascii=False)

    rock = response({"type": "MOVE", "move": "Rock"})
    scissors = response({"type": "MOVE", "move": "Scissors"})
    if scenario == "locked":
        return (scripted([rock, response({"type": "SAY", "text": "我已经提交，继续等你决定。"})]),
                scripted([response({"type": "SAY", "text": "我先考虑一下。"}), scissors]))
    if scenario == "change":
        return (
            scripted([
                response({"type": "SAY", "text": "我先做个决定。"}, {"type": "MOVE", "move": "Rock"}),
                response({"type": "MOVE", "move": "Paper"}, {"type": "SAY", "text": "我又改了主意。"}),
            ]),
            scripted([response({"type": "SAY", "text": "我再想一想。"}), scissors]),
        )
    if scenario == "draw":
        return scripted(), scripted()
    if scenario == "settlement":
        return (scripted([rock, response({"type": "MOVE", "move": "Paper"},
                                        {"type": "SAY", "text": "最后一次机会，我改为布。"})]),
                scripted([rock, WAIT_RESPONSE]))
    if scenario == "forfeit":
        return scripted([rock]), scripted()
    if scenario == "deadline":
        waiting = [WAIT_RESPONSE] * (MAX_TICKS - 1)
        return scripted(waiting + [rock]), scripted(waiting + [scissors])
    if scenario == "retry":
        invalid = response({"type": "WAIT"}, {"type": "MOVE", "move": "Paper"})
        return scripted([invalid, rock]), scripted([scissors])
    raise ValueError("未知模拟场景。")
