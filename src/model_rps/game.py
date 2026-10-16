"""纯游戏规则：不调用模型、不打印内容；状态更新以完整 Tick 为单位。"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


class MovePolicy(StrEnum):
    LOCKED = "locked"
    REVISABLE = "revisable"


class Initiative(StrEnum):
    SIMULTANEOUS = "simultaneous"
    ALTERNATING = "alternating"


class InvalidResponse(ValueError):
    """可安全反馈给模型的格式或动作规则错误，不包含原始响应。"""


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    text: str | None = None
    move: Gesture | None = None

    def to_dict(self) -> dict:
        result = {"type": self.kind.value}
        if self.text is not None:
            result["text"] = self.text
        if self.move is not None:
            result["move"] = self.move.value
        return result

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
    if sum(action.kind in (ActionKind.MOVE, ActionKind.WAIT) for action in actions) != 1:
        raise InvalidResponse("每轮必须且只能有一个 MOVE 或 WAIT；SAY 可同时出现。")


def validate_turn(actions: tuple[Action, ...], own_move: Gesture | None, move_policy: MovePolicy) -> None:
    validate_actions(actions)


def ignored_move(action: Action, own_move: Gesture | None, move_policy: MovePolicy) -> bool:
    return action.kind == ActionKind.MOVE and own_move is not None and move_policy == MovePolicy.LOCKED


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
    settlement_tick: int | None = None
    move_policy: MovePolicy = MovePolicy.LOCKED
    initiative: Initiative = Initiative.SIMULTANEOUS
    first_player: PlayerId = "A"
    own_turn: int = 1

    @property
    def is_settlement_tick(self) -> bool:
        return self.tick == self.settlement_tick

    def to_dict(self) -> dict:
        alternating = self.initiative == Initiative.ALTERNATING
        turn_limit = MAX_TICKS if alternating else self.settlement_tick or MAX_TICKS
        return {
            "player": self.player,
            "initiative": self.initiative.value,
            "first_player": self.first_player if alternating else None,
            "own_turn": self.own_turn,
            "max_turns_per_player": turn_limit,
            "remaining_turns_including_current": turn_limit - self.own_turn + 1,
            "move_policy": self.move_policy.value,
            "own_move_locked": self.move_policy == MovePolicy.LOCKED and self.own_move is not None,
            "tick": self.tick,
            "max_ticks": MAX_TICKS * 2 if alternating else self.settlement_tick or MAX_TICKS,
            "submission_deadline_tick": MAX_TICKS * 2 if alternating else MAX_TICKS,
            "settlement_tick": self.settlement_tick,
            "is_settlement_tick": self.is_settlement_tick,
            "remaining_ticks_including_current": (MAX_TICKS * 2 if alternating else self.settlement_tick or MAX_TICKS) - self.tick + 1,
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
    settlement_tick: int | None = None
    move_policy: MovePolicy = MovePolicy.LOCKED
    initiative: Initiative = Initiative.SIMULTANEOUS
    first_player: PlayerId = "A"
    turns: tuple[int, int] = (0, 0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "move_policy", MovePolicy(self.move_policy))
        object.__setattr__(self, "initiative", Initiative(self.initiative))
        if self.first_player not in ("A", "B"):
            raise ValueError("先手必须是 A 或 B。")
        if self.initiative == Initiative.ALTERNATING and self.move_policy != MovePolicy.LOCKED:
            raise ValueError("实验性 alternating 模式仅支持 locked 出拳规则。")

    @property
    def current_player(self) -> PlayerId:
        if self.completed_ticks % 2 == 0:
            return self.first_player
        return "B" if self.first_player == "A" else "A"

    def view_for(self, player: PlayerId) -> PlayerView:
        if self.result is not None:
            raise ValueError("已结束的对局不能继续请求动作。")
        if player not in ("A", "B"):
            raise ValueError("玩家必须是 A 或 B。")
        index = 0 if player == "A" else 1
        return PlayerView(player, self.completed_ticks + 1, self.moves[index],
                          self.moves[1 - index] is not None, self.events, self.settlement_tick, self.move_policy,
                          self.initiative, self.first_player, self.turns[index] + 1 if self.initiative == Initiative.ALTERNATING
                          else self.completed_ticks + 1)


def judge(a: Gesture, b: Gesture) -> PlayerId | None:
    if a == b:
        return None
    winning_pairs = {(Gesture.ROCK, Gesture.SCISSORS), (Gesture.PAPER, Gesture.ROCK),
                     (Gesture.SCISSORS, Gesture.PAPER)}
    return "A" if (a, b) in winning_pairs else "B"


def advance(state: GameState, a: tuple[Action, ...], b: tuple[Action, ...]) -> GameState:
    """先校验双方，再产生新状态；结算前的公开事件不携带真实拳型。"""
    if state.initiative != Initiative.SIMULTANEOUS:
        raise ValueError("轮流行动模式请逐方推进。")
    if state.result is not None or state.completed_ticks >= (state.settlement_tick or MAX_TICKS):
        raise ValueError("对局已经结束。")
    validate_turn(a, state.moves[0], state.move_policy)
    validate_turn(b, state.moves[1], state.move_policy)
    tick = state.completed_ticks + 1
    moves = list(state.moves)
    events = []
    for index, (player, actions) in enumerate((("A", a), ("B", b))):
        for action in actions:
            if action.kind == ActionKind.SAY:
                events.append(Event(tick, "say", player, action.text))
            elif action.kind == ActionKind.MOVE:
                if ignored_move(action, moves[index], state.move_policy):
                    events.append(Event(tick, "move_ignored", player))
                    continue
                kind = "commit" if moves[index] is None else "revise"
                moves[index] = action.move
                events.append(Event(tick, kind, player))
            else:
                events.append(Event(tick, "wait", player))

    result = None
    settlement_tick = state.settlement_tick
    if tick == settlement_tick:
        result = Result(EndReason.SHOWDOWN, judge(moves[0], moves[1]))
    elif all(move is not None for move in moves):
        if state.move_policy == MovePolicy.LOCKED:
            result = Result(EndReason.SHOWDOWN, judge(moves[0], moves[1]))
        else:
            settlement_tick = tick + 1
            events.append(Event(tick, "settlement_pending", text=
                                f"双方均已提交出拳；下一刻（第 {settlement_tick} Tick）为结算刻，仍可改拳，全部动作处理后结算。"))
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
    elif settlement_tick is None and tick in REMINDER_TICKS:
        events.append(Event(tick, "reminder", text=f"距离本局提交截止还剩 {MAX_TICKS - tick} Tick。"))
    return replace(state, completed_ticks=tick, moves=tuple(moves), events=state.events + tuple(events),
                   result=result, settlement_tick=settlement_tick, turns=(tick, tick))


def advance_turn(state: GameState, player: PlayerId, actions: tuple[Action, ...]) -> GameState:
    """轮流行动：只执行当前玩家的完整响应，随后即时公开并判定。"""
    if state.initiative != Initiative.ALTERNATING or state.result is not None or state.completed_ticks >= MAX_TICKS * 2:
        raise ValueError("当前状态不能推进轮流行动。")
    if player != state.current_player:
        raise ValueError("尚未轮到该玩家。")
    index = 0 if player == "A" else 1
    validate_turn(actions, state.moves[index], state.move_policy)
    tick = state.completed_ticks + 1
    moves, turns, events = list(state.moves), list(state.turns), []
    for action in actions:
        if action.kind == ActionKind.SAY:
            events.append(Event(tick, "say", player, action.text))
        elif action.kind == ActionKind.WAIT:
            events.append(Event(tick, "wait", player))
        elif ignored_move(action, moves[index], state.move_policy):
            events.append(Event(tick, "move_ignored", player))
        else:
            moves[index] = action.move
            events.append(Event(tick, "commit", player))
    turns[index] += 1
    result = None
    if all(move is not None for move in moves):
        result = Result(EndReason.SHOWDOWN, judge(moves[0], moves[1]))
    elif all(turn >= MAX_TICKS for turn in turns):
        result = Result(EndReason.FORFEIT, "A" if moves[0] is not None else "B") if any(
            move is not None for move in moves) else Result(EndReason.NO_MOVES)
    if result:
        events.extend(Event(tick, "reveal", identity, move.value)
                      for identity, move in zip(("A", "B"), moves) if move is not None)
    elif turns[index] in REMINDER_TICKS:
        events.append(Event(tick, "turn_reminder", player,
                            f"本局已完成 {turns[index]} 次行动，剩余 {MAX_TICKS - turns[index]} 次行动机会。"))
    return replace(state, completed_ticks=tick, moves=tuple(moves), turns=tuple(turns),
                   events=state.events + tuple(events), result=result)


def abort(state: GameState, player: PlayerId, message: str) -> GameState:
    """丢弃当前 Tick 的暂存动作，只保留此前完成的状态。"""
    event = Event(state.completed_ticks + 1, "error", player, message)
    return replace(state, events=state.events + (event,), result=Result(EndReason.TECHNICAL_ERROR))
