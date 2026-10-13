"""同步 Tick 调度：快照先冻结，双方全部校验成功后再执行动作。"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Callable

from .game import Action, Event, GameState, InvalidResponse, PlayerView, abort, advance, parse_response
from .players import Player, PlayerError

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


async def _request_actions(player: Player, view: PlayerView, policy: RequestPolicy,
                           emit: EventHandler) -> tuple[Action, ...]:
    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retryable = True
        try:
            # 覆盖整个请求的时限，不只限制网络中两次数据之间的等待。
            async with asyncio.timeout(policy.timeout):
                raw = await player.respond(view, feedback)
            return parse_response(raw)
        except InvalidResponse as exc:
            feedback = f"本次响应无效，整份动作均未执行。{exc} 请重新返回完整 JSON。"
            summary = "响应格式无效"
        except TimeoutError:
            summary = "请求超时"
            feedback = "本次请求超时，未执行任何动作。请根据相同状态重新返回完整 JSON。"
        except PlayerError as exc:
            summary = exc.safe_message
            feedback = f"{summary}，未执行任何动作。请根据相同状态重新返回完整 JSON。"
            retryable = exc.retryable
        except Exception:
            # 不将 SDK、HTTP 或自定义适配器的原始异常（可能含密钥）写入日志。
            raise TurnFailed(view, "玩家适配器发生内部错误，本局中止。") from None
        if not retryable:
            raise TurnFailed(view, f"{summary}，本局中止。")
        if attempt == MAX_ATTEMPTS:
            raise TurnFailed(view, f"{summary}；已请求 {MAX_ATTEMPTS} 次，本局中止。")
        emit(Event(view.tick, "retry", view.player,
                   f"{summary}，准备第 {attempt + 1}/{MAX_ATTEMPTS} 次请求。"))
        await asyncio.sleep(policy.retry_delay)
    raise AssertionError("请求循环不应到达此处。")


async def run_game(a: Player, b: Player, *, policy: RequestPolicy | None = None,
                   on_event: EventHandler | None = None) -> GameState:
    policy = policy or RequestPolicy()
    emit = on_event or (lambda event: None)
    state = GameState()
    while state.result is None:
        views = (state.view_for("A"), state.view_for("B"))
        emit(Event(views[0].tick, "tick"))
        tasks = [asyncio.create_task(_request_actions(player, view, policy, emit))
                 for player, view in zip((a, b), views)]
        try:
            actions = await asyncio.gather(*tasks)
        except BaseException as exc:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not isinstance(exc, TurnFailed):
                raise
            new_state = abort(state, exc.player, str(exc))
        else:
            new_state = advance(state, actions[0], actions[1])
        for event in new_state.events[len(state.events):]:
            emit(event)
        state = new_state
    return state
