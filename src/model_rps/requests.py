"""单次玩家交互的超时、校验与重试；不决定何时发布或推进游戏。"""

from __future__ import annotations

import asyncio
import math
from time import perf_counter
from dataclasses import dataclass
from typing import Callable

from .game import Event, InvalidResponse, PlayerView, ignored_move, validate_turn
from .commands import AnalysisPolicy, Interaction, parse_interaction
from .diagnostics import LogError, TraceHandler, request_trace, trace
from .players import ModelResponse, Player, PlayerError

MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 60.0
RETRY_DELAY_SECONDS = 1.0
EventHandler = Callable[[Event], None]


@dataclass(frozen=True)
class RequestPolicy:
    timeout: float = REQUEST_TIMEOUT_SECONDS
    retry_delay: float = RETRY_DELAY_SECONDS

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("请求超时必须是大于 0 的有限秒数。")
        if not math.isfinite(self.retry_delay) or self.retry_delay < 0:
            raise ValueError("重试间隔必须是非负有限秒数。")


class TurnFailed(Exception):
    def __init__(self, view: PlayerView, message: str) -> None:
        super().__init__(message)
        self.player = view.player


@dataclass(frozen=True)
class RequestResult:
    view: PlayerView
    interaction: Interaction


def validated_response(player, view, raw, *, has_reasoning=False) -> RequestResult:
    """人类与模型共用阶段、分析与行动校验，并产生相同的诊断记录。"""
    trace("raw_response", content=raw)
    interaction = parse_interaction(raw, can_act=view.can_act,
                                    analysis=getattr(player, "analysis_policy", AnalysisPolicy.REQUIRED))
    decision = interaction.decision
    if decision is not None:
        validate_turn(decision.actions, view.own_move, view.move_policy)
        trace("decision", summary=decision.summary, status="validated",
              analysis_source="analyze" if decision.summary is not None else "none",
              has_reasoning=has_reasoning,
              actions=[action.to_dict() for action in decision.actions
                       if not ignored_move(action, view.own_move, view.move_policy)],
              ignored_actions=[action.to_dict() for action in decision.actions
                               if ignored_move(action, view.own_move, view.move_policy)])
    else:
        trace("chat_validated", message_count=len(interaction.messages), messages=interaction.messages)
    return RequestResult(view, interaction)


async def request_interaction(player: Player, view: PlayerView, policy: RequestPolicy,
                           emit: EventHandler, on_request: TraceHandler | None) -> RequestResult:
    if getattr(player, "interactive", False):
        # 输入错误在终端逐条提示；等待用户不使用 API 超时或重试预算。
        submission = await player.respond(view)
        view = submission.view
        source = getattr(player, "input_source", "human")
        with request_trace(on_request, player=view.player, tick=view.tick, attempt=1,
                           phase="game" if view.can_act else "chat", request_id=view.request_id):
            trace("request_start", state=view.to_dict(), correction=None, source=source)
            result = validated_response(player, view, submission.content)
            trace("request_end", status="valid", source=source)
            return result
    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with request_trace(on_request, player=view.player, tick=view.tick, attempt=attempt,
                           phase="game" if view.can_act else "chat", request_id=view.request_id):
            started = perf_counter()
            retryable = True
            raw = None
            trace("request_start", state=view.to_dict(), correction=feedback)
            try:
                # 覆盖整个请求的时限，不只限制网络中两次数据之间的等待。
                async with asyncio.timeout(policy.timeout):
                    response = await player.respond(view, feedback)
                raw = response.content if isinstance(response, ModelResponse) else response
                has_reasoning = isinstance(response, ModelResponse) and response.has_reasoning
                result = validated_response(player, view, raw, has_reasoning=has_reasoning)
                trace("request_end", status="valid", elapsed_seconds=perf_counter() - started)
                return result
            except LogError:
                raise
            except asyncio.CancelledError:
                trace("request_end", status="cancelled", elapsed_seconds=perf_counter() - started)
                raise
            except InvalidResponse as exc:
                trace("validation_error", content=raw, error=str(exc))
                feedback = f"本次响应无效，整份动作均未执行。{exc} 请重新返回完整 JSON。"
                summary = "响应格式或动作无效"
            except TimeoutError:
                summary = "请求超时"
                feedback = "本次请求超时，未执行任何动作。请根据相同状态重新返回完整 JSON。"
            except PlayerError as exc:
                summary = exc.safe_message
                feedback = f"{summary}，未执行任何动作。请根据相同状态重新返回完整 JSON。"
                retryable = exc.retryable
            except Exception:
                # 不记录 SDK、HTTP 或自定义适配器的原始异常。
                summary = "玩家适配器发生内部错误"
                feedback = None
                retryable = False
            will_retry = retryable and attempt < MAX_ATTEMPTS
            trace("request_end", status="rejected", error=summary, correction=feedback,
                  elapsed_seconds=perf_counter() - started, will_retry=will_retry)
            if not retryable:
                raise TurnFailed(view, summary)
            if attempt == MAX_ATTEMPTS:
                raise TurnFailed(view, f"{summary}；已请求 {MAX_ATTEMPTS} 次。")
            emit(Event(view.tick, "retry", view.player,
                       f"{summary}，准备第 {attempt + 1}/{MAX_ATTEMPTS} 次请求。"))
        await asyncio.sleep(policy.retry_delay)
    raise AssertionError("请求循环不应到达此处。")
