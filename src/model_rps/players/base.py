"""玩家接口、模拟玩家及保留独立上下文的模型玩家；HTTP 协议由传输层处理。"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from ..core.game import MAX_TICKS, PlayerView
from ..core.commands import AnalysisPolicy
from .context_usage import ContextUsage
from .history import ConversationHistory, HistoryCheckpoint
from ..core.diagnostics import trace
from ..core.identities import named_data
from ..core.prompts import GROK_STREAM_FORMAT_PROMPT, POSTMATCH_PROMPT, game_prompt
from ..llm.adapters import (ModelSpec, MODEL_ADAPTERS, DEEPSEEK, OPENAI, configure_thinking,
                       DEEPSEEK_THINKING_MODES, OPENAI_THINKING_MODES)
from ..llm.transport import ModelTransport, PlayerError

MAX_OUTPUT_TOKENS = 20000


@dataclass(frozen=True)
class ModelResponse:
    """原始正文与本次接口是否返回思考的标记；不携带私有思考文本。"""

    content: str
    has_reasoning: bool = False


@dataclass(frozen=True)
class PlayerInput:
    """人类在确认输入时持有的视图；等待输入期间允许更新可见状态。"""

    content: str
    view: PlayerView


class Player(Protocol):
    analysis_policy: AnalysisPolicy

    def checkpoint_history(self):
        """可选的历史事务接口：有历史的适配器提供检查点，供取消时回退。"""
        ...

    def restore_history(self, checkpoint) -> None:
        """删除未应用请求产生的私有历史；不删除诊断日志或用量。"""
        ...

    async def respond(self, view: PlayerView, feedback: str | None = None) -> str | ModelResponse | PlayerInput:
        """返回待校验的 JSON；模型可附带思考标记，人类附带确认输入时的视图。"""
        ...


class MatchPlayer(Player, Protocol):
    def start_match(self) -> None:
        """新比赛开始时清空该玩家的上下文。"""
        ...

    async def postmatch(self, context: dict, feedback: str | None = None) -> str:
        """延续本场上下文，返回赛后发言或退出指令。"""
        ...

    def start_game(self, context: dict) -> None:
        """更新当前局及比赛的公开信息，保留同场比赛的上下文。"""
        ...


class ChatCompletionPlayer:
    def __init__(self, client: httpx.AsyncClient, spec: ModelSpec, api_key: str, *,
                 max_output_tokens: int = MAX_OUTPUT_TOKENS, names: dict[str, str] | None = None,
                 stream: bool = False, stream_first_timeout: float = 60,
                 stream_idle_timeout: float = 30,
                 analysis: AnalysisPolicy = AnalysisPolicy.REQUIRED) -> None:
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
        self.analysis_policy = AnalysisPolicy(analysis)
        if spec.protocol not in ("chat_completions", "anthropic_messages"):
            raise ValueError("不支持的模型接口协议。")
        self._transport = ModelTransport(client, spec, self._api_key, max_output_tokens=max_output_tokens,
            stream=stream, first_timeout=stream_first_timeout, idle_timeout=stream_idle_timeout)
        self.start_match()

    def _game_prompt(self, move_policy="locked", initiative="simultaneous") -> str:
        prompt = game_prompt(move_policy, initiative, self.analysis_policy)
        if self.stream and self.spec.thinking_api == "grok":
            prompt += "\n" + GROK_STREAM_FORMAT_PROMPT
        return prompt

    def start_match(self) -> None:
        self._history = ConversationHistory(self._game_prompt())
        self._messages = self._history.messages
        self._match_context = None
        self._context_usage = ContextUsage()
        self._usage = None

    def checkpoint_history(self) -> HistoryCheckpoint:
        return self._history.checkpoint()

    def restore_history(self, checkpoint: HistoryCheckpoint) -> None:
        self._history.restore(checkpoint)

    def start_game(self, context: dict) -> None:
        # 每个玩家持有独立副本，不能通过共享可变字典影响对手视图。
        self._match_context = json.loads(json.dumps(context, ensure_ascii=False))

    def receive_handoff(self, context):
        message = {"role": "user", "content": json.dumps(named_data(context, self.names), ensure_ascii=False)}
        self._history.receive_handoff(context, message)

    async def respond(self, view: PlayerView, feedback: str | None = None) -> ModelResponse:
        self._messages[0] = {"role": "system", "content": self._game_prompt(view.move_policy, view.initiative)}
        context = self._history.game_context(view, self._match_context, feedback)
        user_message = {"role": "user", "content": json.dumps(named_data(context, self.names), ensure_ascii=False)}
        response = await self._complete([*self._messages, user_message])
        # 只保留该玩家自己的响应；格式校验失败后的纠错也属于其私有上下文。
        # 网络失败没有完整响应，不追加未配对的用户消息。
        self._history.remember(context, user_message, response.content)
        return response

    async def postmatch(self, context: dict, feedback: str | None = None) -> str:
        # 公开材料相同；已经提供过的事件从保留历史中读取，首次公开的分析完整提供。
        context = self._history.postmatch_context(context, feedback)
        user_message = {"role": "user", "content": json.dumps(named_data(
            {**context, "phase": "postmatch", "request": POSTMATCH_PROMPT, "correction": feedback},
            self.names), ensure_ascii=False)}
        response = await self._complete([*self._messages, user_message])
        self._history.remember(context, user_message, response.content)
        return response.content

    async def _complete(self, messages: list[dict]) -> ModelResponse:
        self._usage = None
        try:
            return await self._send(messages)
        finally:
            trace("context_usage", **self._context_usage.record(self._usage, messages))

    async def _send(self, messages: list[dict]) -> ModelResponse:
        try:
            data = await self._transport.complete(messages)
        finally:
            self._usage = self._transport.usage
        return self._parse_completion(data)

    def _parse_completion(self, data: object) -> ModelResponse:
        try:
            usage = data.get("usage") if isinstance(data, dict) else None
            self._usage = usage
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
        # 只保存服务实际提供的字段，不从 ANALYZE 重构或推测思考文本。
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


WAIT_RESPONSE = '{"actions":[{"type":"ANALYZE","text":"继续等待。"},{"type":"ENDTURN"}]}'


class ScriptedPlayer:
    """按请求次数消耗预设响应；耗尽后结束行动并等待，适合演示和异常测试。"""

    def __init__(self, responses: list[str | Exception] | None = None, *, repeat_per_game: bool = False,
                 analysis: AnalysisPolicy = AnalysisPolicy.REQUIRED,
                 chat_responses: list[str | Exception] | None = None) -> None:
        self._initial_responses = tuple(responses or [])
        self._initial_chat_responses = tuple(chat_responses or [])
        self.repeat_per_game = repeat_per_game
        self.analysis_policy = AnalysisPolicy(analysis)
        self.start_match()

    def start_match(self) -> None:
        self.responses = deque(self._initial_responses)
        self.chat_responses = deque(self._initial_chat_responses)
        self.requests: list[tuple[PlayerView, str | None]] = []
        self.chat_requests: list[tuple[PlayerView, str | None]] = []
        self.game_contexts: list[dict] = []
        self.handoff = None

    def receive_handoff(self, context):
        self.handoff = json.loads(json.dumps(context, ensure_ascii=False))

    def start_game(self, context: dict) -> None:
        self.game_contexts.append(json.loads(json.dumps(context, ensure_ascii=False)))
        if self.repeat_per_game:
            self.responses = deque(self._initial_responses)
            self.chat_responses = deque(self._initial_chat_responses)

    async def respond(self, view: PlayerView, feedback: str | None = None) -> str:
        if view.can_act:
            self.requests.append((view, feedback))
            fallback = '{"actions":[{"type":"ENDTURN"}]}' if self.analysis_policy == AnalysisPolicy.OFF else WAIT_RESPONSE
            response = self.responses.popleft() if self.responses else fallback
        else:
            self.chat_requests.append((view, feedback))
            response = self.chat_responses.popleft() if self.chat_responses else '{"actions":[]}'
        if isinstance(response, Exception):
            raise response
        return response

    async def postmatch(self, context: dict, feedback: str | None = None) -> str:
        if "match" in context:
            result = context["match"]["result"]
            text = (f"本场完成 {result['games_played']} 局，比分 Red {result['wins']['A']} : "
                    f"{result['wins']['B']} Blue。这些出拳来自模拟脚本，你怎么看？")
            actions = [{"type": "SAY", "text": text}]
        else:
            actions = [{"type": "SAY", "text": "可以对照公开记录检查行动和结算时机。下次再聊！"},
                       {"type": "EXIT"}]
        return json.dumps({"actions": actions}, ensure_ascii=False)


def demo_players(scenario: str, *, analysis=(AnalysisPolicy.REQUIRED, AnalysisPolicy.REQUIRED)) -> tuple[ScriptedPlayer, ScriptedPlayer]:
    policies = iter(analysis)

    def scripted(responses=None, *, chat_responses=None):
        policy = AnalysisPolicy(next(policies))
        if policy == AnalysisPolicy.OFF and responses:
            # 演示脚本遵守配置，真实模型的违规分析仍由通用校验器拒绝。
            responses = [json.dumps({"actions": [c for c in json.loads(raw)["actions"]
                                                 if c["type"] != "ANALYZE"]}, ensure_ascii=False)
                         for raw in responses]
        return ScriptedPlayer(responses, repeat_per_game=True, analysis=policy, chat_responses=chat_responses)

    def response(*actions: dict) -> str:
        return json.dumps({"actions": [{"type": "ANALYZE", "text": "执行当前模拟场景的预设动作。"},
                                       *actions, {"type": "ENDTURN"}]}, ensure_ascii=False)

    rock = response({"type": "MOVE", "move": "Rock"})
    scissors = response({"type": "MOVE", "move": "Scissors"})
    if scenario == "chat":
        hello = json.dumps({"actions": [{"type": "SAY", "text": "先聊一句，这条消息不结束我的行动。"}]}, ensure_ascii=False)
        reply = json.dumps({"actions": [{"type": "SAY", "text": "现在不是我的行动机会，也可以先聊一句。"}]}, ensure_ascii=False)
        return scripted([hello, rock]), scripted([scissors], chat_responses=[reply])
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
        invalid = response({"type": "ENDTURN"}, {"type": "MOVE", "move": "Paper"})
        return scripted([invalid, rock]), scripted([scissors])
    raise ValueError("未知模拟场景。")
