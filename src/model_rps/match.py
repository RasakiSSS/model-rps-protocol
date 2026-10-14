"""BO 比赛调度：同场比赛复用玩家上下文，每局只重建游戏状态。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from .game import EndReason, Event, GameState, MovePolicy, PlayerId
from .players import MatchPlayer
from .runner import RequestPolicy, run_game


@dataclass(frozen=True)
class MatchConfig:
    best_of: int = 1
    max_games: int | None = None
    move_policy: MovePolicy = MovePolicy.LOCKED

    def __post_init__(self) -> None:
        object.__setattr__(self, "move_policy", MovePolicy(self.move_policy))
        if type(self.best_of) is not int or self.best_of < 1 or self.best_of % 2 != 1:
            raise ValueError("BO 必须是正奇数，例如 1、3、5。")
        if self.max_games is None:
            object.__setattr__(self, "max_games", self.best_of * 10)
        elif type(self.max_games) is not int or self.max_games < self.target_wins:
            raise ValueError("实际总局数上限必须是整数，且不能小于所需胜局数。")

    @property
    def target_wins(self) -> int:
        return self.best_of // 2 + 1


class MatchStatus(StrEnum):
    WON = "won"
    UNDECIDED = "undecided"
    TECHNICAL_ERROR = "technical_error"


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    wins: tuple[int, int]
    draws: int
    games_played: int
    winner: PlayerId | None = None


async def run_match(
    player_factory: Callable[[], tuple[MatchPlayer, MatchPlayer]], *,
    config: MatchConfig | None = None,
    policy: RequestPolicy | None = None,
    on_start: Callable[[int], None] | None = None,
    on_event: Callable[[int, Event], None] | None = None,
    on_request: Callable[[int, dict], None] | None = None,
    on_end: Callable[[int, GameState, tuple[int, int], int], None] | None = None,
) -> MatchResult:
    config = config or MatchConfig()
    wins = [0, 0]
    draws = 0
    a, b = player_factory()
    for player in (a, b):
        player.start_match()
    last_game = None
    for number in range(1, config.max_games + 1):
        context = {
            "game": number, "best_of": config.best_of, "target_wins": config.target_wins,
            "max_games": config.max_games, "wins": {"A": wins[0], "B": wins[1]},
            "draws": draws, "last_game": last_game,
            "move_policy": config.move_policy.value,
        }
        for player in (a, b):
            player.start_game(context)
        if on_start:
            on_start(number)
        state = await run_game(
            a, b, policy=policy, move_policy=config.move_policy,
            on_event=(lambda event: on_event(number, event)) if on_event else None,
            on_request=(lambda record: on_request(number, record)) if on_request else None,
        )
        if state.result.reason != EndReason.TECHNICAL_ERROR:
            if state.result.winner is None:
                draws += 1
            else:
                wins[0 if state.result.winner == "A" else 1] += 1
        scores = tuple(wins)
        if on_end:
            on_end(number, state, scores, draws)
        if state.result.reason == EndReason.TECHNICAL_ERROR:
            return MatchResult(MatchStatus.TECHNICAL_ERROR, scores, draws, number)
        if config.target_wins in wins:
            return MatchResult(MatchStatus.WON, scores, draws, number, state.result.winner)
        # 只传递已结算的公开事件，含最终拳型；双方私有响应不汇入此处。
        last_game = {
            "game": number, "events": [event.to_dict() for event in state.events],
            "result": {"reason": state.result.reason.value, "winner": state.result.winner},
        }
    return MatchResult(MatchStatus.UNDECIDED, tuple(wins), draws, config.max_games)
