"""纯游戏规则：不调用模型、不打印内容；状态更新以完整 Tick 为单位。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

MAX_TICKS = 20
REMINDER_TICKS = (10, 18, 19)
PlayerId = Literal["A", "B"]


class Gesture(StrEnum):
    ROCK = "Rock"
    PAPER = "Paper"
    SCISSORS = "Scissors"


class ActionKind(StrEnum):
    SAY = "SAY"
    MOVE = "MOVE"
    WAIT = "WAIT"


class InvalidResponse(ValueError):
    """可安全反馈给模型的格式错误，不包含原始响应。"""


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    text: str | None = None
    move: Gesture | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            raise InvalidResponse("动作 type 必须是 SAY、MOVE 或 WAIT。")
        if self.kind == ActionKind.SAY:
            if not isinstance(self.text, str) or not self.text.strip() or self.move is not None:
                raise InvalidResponse("SAY 只允许包含 type 和非空字符串 text。")
        elif self.kind == ActionKind.MOVE:
            if not isinstance(self.move, Gesture) or self.text is not None:
                raise InvalidResponse("MOVE 只允许包含 type 和 move，拳型为 Rock、Paper、Scissors。")
        elif self.text is not None or self.move is not None:
            raise InvalidResponse("WAIT 只允许包含 type。")


def validate_actions(actions: tuple[Action, ...]) -> None:
    if not actions or any(not isinstance(action, Action) for action in actions):
        raise InvalidResponse("actions 必须是非空动作列表。")
    if len(actions) != 1 and any(action.kind == ActionKind.WAIT for action in actions):
        raise InvalidResponse("WAIT 必须是唯一动作，不能与其他动作混用。")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponse("JSON 对象不能包含重复字段。")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InvalidResponse("JSON 不能包含 NaN 或 Infinity。")


def parse_response(raw: str) -> tuple[Action, ...]:
    """严格解析整个 JSON 对象；任何一个动作无效则全部拒绝。"""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidResponse("响应不能为空，请返回包含 actions 的 JSON 对象。")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError):
        raise InvalidResponse("响应必须是完整 JSON，不要添加 Markdown 标记或解释文字。") from None
    if not isinstance(payload, dict) or set(payload) != {"actions"}:
        raise InvalidResponse("响应必须是只包含 actions 字段的 JSON 对象。")
    if not isinstance(payload["actions"], list) or not payload["actions"]:
        raise InvalidResponse("actions 必须是非空动作列表；主动等待请使用 WAIT。")
    actions = []
    expected = {ActionKind.SAY: {"type", "text"}, ActionKind.MOVE: {"type", "move"},
                ActionKind.WAIT: {"type"}}
    for item in payload["actions"]:
        if not isinstance(item, dict):
            raise InvalidResponse("每个动作必须是 JSON 对象。")
        try:
            kind = ActionKind(item.get("type"))
        except (ValueError, TypeError):
            raise InvalidResponse("动作 type 必须是 SAY、MOVE 或 WAIT。") from None
        if set(item) != expected[kind]:
            raise InvalidResponse(f"{kind.value} 字段不正确；请按规定的动作格式返回。")
        if kind == ActionKind.MOVE:
            try:
                move = Gesture(item["move"])
            except (ValueError, TypeError):
                raise InvalidResponse("move 必须是 Rock、Paper 或 Scissors。") from None
            actions.append(Action(kind, move=move))
        else:
            actions.append(Action(kind, text=item.get("text")))
    result = tuple(actions)
    validate_actions(result)
    return result


@dataclass(frozen=True)
class Event:
    tick: int
    kind: str
    player: PlayerId | None = None
    text: str = ""

    def to_dict(self) -> dict:
        return {"tick": self.tick, "kind": self.kind, "player": self.player, "text": self.text}


class EndReason(StrEnum):
    SHOWDOWN = "showdown"
    FORFEIT = "forfeit"
    NO_MOVES = "no_moves"
    TECHNICAL_ERROR = "technical_error"


@dataclass(frozen=True)
class Result:
    reason: EndReason
    winner: PlayerId | None = None


@dataclass(frozen=True)
class PlayerView:
    player: PlayerId
    tick: int
    own_move: Gesture | None
    opponent_committed: bool
    history: tuple[Event, ...]

    def to_dict(self) -> dict:
        return {
            "player": self.player,
            "tick": self.tick,
            "max_ticks": MAX_TICKS,
            "remaining_ticks_including_current": MAX_TICKS - self.tick + 1,
            "own_move": self.own_move.value if self.own_move is not None else None,
            "opponent_committed": self.opponent_committed,
            "history": [event.to_dict() for event in self.history],
        }


@dataclass(frozen=True)
class GameState:
    completed_ticks: int = 0
    moves: tuple[Gesture | None, Gesture | None] = (None, None)
    events: tuple[Event, ...] = ()
    result: Result | None = None

    def view_for(self, player: PlayerId) -> PlayerView:
        if self.result is not None:
            raise ValueError("已结束的对局不能继续请求动作。")
        if player not in ("A", "B"):
            raise ValueError("玩家必须是 A 或 B。")
        index = 0 if player == "A" else 1
        return PlayerView(player, self.completed_ticks + 1, self.moves[index],
                          self.moves[1 - index] is not None, self.events)


def judge(a: Gesture, b: Gesture) -> PlayerId | None:
    if a == b:
        return None
    winning_pairs = {(Gesture.ROCK, Gesture.SCISSORS), (Gesture.PAPER, Gesture.ROCK),
                     (Gesture.SCISSORS, Gesture.PAPER)}
    return "A" if (a, b) in winning_pairs else "B"


def advance(state: GameState, a: tuple[Action, ...], b: tuple[Action, ...]) -> GameState:
    """先校验双方，再产生新状态；结算前的公开事件不携带真实拳型。"""
    if state.result is not None or state.completed_ticks >= MAX_TICKS:
        raise ValueError("对局已经结束。")
    validate_actions(a)
    validate_actions(b)
    tick = state.completed_ticks + 1
    moves = list(state.moves)
    events = []
    for index, (player, actions) in enumerate((("A", a), ("B", b))):
        for action in actions:
            if action.kind == ActionKind.SAY:
                events.append(Event(tick, "say", player, action.text))
            elif action.kind == ActionKind.MOVE:
                kind = "commit" if moves[index] is None else "revise"
                moves[index] = action.move
                events.append(Event(tick, kind, player))
            else:
                events.append(Event(tick, "wait", player))

    result = None
    if all(move is not None for move in moves):
        result = Result(EndReason.SHOWDOWN, judge(moves[0], moves[1]))
    elif tick == MAX_TICKS:
        if any(move is not None for move in moves):
            result = Result(EndReason.FORFEIT, "A" if moves[0] is not None else "B")
        else:
            result = Result(EndReason.NO_MOVES)
    if result is not None:
        # 对局已经结束，才允许展示已提交的最终拳型。
        for player, move in zip(("A", "B"), moves):
            if move is not None:
                events.append(Event(tick, "reveal", player, move.value))
    elif tick in REMINDER_TICKS:
        events.append(Event(tick, "reminder", text=f"距离本局强制结束还剩 {MAX_TICKS - tick} Tick。"))
    return GameState(tick, tuple(moves), state.events + tuple(events), result)


def abort(state: GameState, player: PlayerId, message: str) -> GameState:
    """丢弃当前 Tick 的暂存动作，只保留此前完成的状态。"""
    event = Event(state.completed_ticks + 1, "error", player, message)
    return GameState(state.completed_ticks, state.moves, state.events + (event,),
                     Result(EndReason.TECHNICAL_ERROR))
