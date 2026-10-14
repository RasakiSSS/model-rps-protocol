"""赛后公开记录与独立复盘；不参与游戏裁判，也不修改比赛结果。"""

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from time import perf_counter

from .diagnostics import LogError, TraceHandler, request_trace, trace
from .game import GameState, InvalidResponse, parse_json_object
from .match import MatchConfig, MatchResult, MatchStatus
from .players import MatchPlayer, PlayerError
from .runner import MAX_ATTEMPTS, RequestPolicy


class MatchArchive:
    """只收集公开事件和通过校验的说明；出错 Tick 的暂存动作不公开。"""

    def __init__(self) -> None:
        self._decisions: list[dict] = []
        self._games: list[dict] = []

    def request(self, game: int, record: dict) -> None:
        if record["kind"] == "decision":
            self._decisions.append({"game": game, **deepcopy(record)})

    def finish_game(self, number: int, state: GameState) -> None:
        decisions = [
            {key: record[key] for key in ("game", "tick", "player", "attempt", "summary", "actions")}
            for record in self._decisions
            if record["game"] == number and record["tick"] <= state.completed_ticks
        ]
        self._games.append({
            "game": number, "completed_ticks": state.completed_ticks,
            "result": {"reason": state.result.reason.value, "winner": state.result.winner},
            "events": [event.to_dict() for event in state.events],
            "decisions": sorted(decisions, key=lambda r: (r["tick"], r["player"])),
        })
        self._decisions = [r for r in self._decisions if r["game"] != number]

    def report(self, config: MatchConfig, result: MatchResult) -> dict:
        if result.status == MatchStatus.TECHNICAL_ERROR:
            raise ValueError("技术异常中止的比赛不自动公开说明或请求复盘。")
        return deepcopy({
            "best_of": config.best_of, "target_wins": config.target_wins, "max_games": config.max_games,
            "move_policy": config.move_policy.value,
            "result": {"status": result.status.value, "winner": result.winner,
                       "wins": {"A": result.wins[0], "B": result.wins[1]},
                       "draws": result.draws, "games_played": result.games_played},
            "games": self._games,
        })


@dataclass(frozen=True)
class Review:
    assessment: str
    key_moments: str
    improvements: str


@dataclass(frozen=True)
class ReviewOutcome:
    player: str
    review: Review | None = None
    error: str | None = None


def parse_review(raw: str) -> Review:
    data = parse_json_object(raw)
    if set(data) != {"assessment", "key_moments", "improvements"}:
        raise InvalidResponse("复盘必须只包含 assessment、key_moments、improvements 三个字段。")
    if any(not isinstance(value, str) or not value.strip() for value in data.values()):
        raise InvalidResponse("复盘的三个字段都必须是非空字符串。")
    return Review(**data)


async def _review(player: MatchPlayer, identity: str, report: dict, policy: RequestPolicy,
                  on_request: TraceHandler | None) -> ReviewOutcome:
    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with request_trace(on_request, player=identity, tick=0, attempt=attempt, phase="review"):
            started = perf_counter()
            context = {"player": identity, "match": deepcopy(report)}
            trace("request_start", context=deepcopy(context), correction=feedback)
            retryable = True
            try:
                async with asyncio.timeout(policy.timeout):
                    raw = await player.review(context, feedback)
                trace("raw_response", content=raw)
                review = parse_review(raw)
                trace("review", review=asdict(review))
                trace("request_end", status="valid", elapsed_seconds=perf_counter() - started)
                return ReviewOutcome(identity, review=review)
            except LogError:
                raise
            except asyncio.CancelledError:
                trace("request_end", status="cancelled", elapsed_seconds=perf_counter() - started)
                raise
            except InvalidResponse as exc:
                summary = "复盘格式无效"
                feedback = f"{exc} 请重新返回完整复盘 JSON，不要提交游戏动作。"
            except TimeoutError:
                summary = "复盘请求超时"
                feedback = "上一请求超时，请根据同一份赛后记录返回完整复盘 JSON。"
            except PlayerError as exc:
                summary = exc.safe_message
                retryable = exc.retryable
                feedback = f"{summary}，请根据同一份赛后记录返回完整复盘 JSON。"
            except Exception:
                summary = "复盘适配器发生内部错误"
                retryable = False
                feedback = None
            will_retry = retryable and attempt < MAX_ATTEMPTS
            trace("request_end", status="rejected", error=summary, correction=feedback,
                  will_retry=will_retry, elapsed_seconds=perf_counter() - started)
            if not will_retry:
                return ReviewOutcome(identity, error=f"{summary}；复盘未完成，比赛结果不受影响。")
        await asyncio.sleep(policy.retry_delay)
    raise AssertionError("复盘重试循环不应到达此处。")


async def run_reviews(players: tuple[MatchPlayer, MatchPlayer], report: dict, *,
                      policy: RequestPolicy, on_request: TraceHandler | None = None) -> tuple[ReviewOutcome, ...]:
    tasks = [asyncio.create_task(_review(player, identity, report, policy, on_request))
             for player, identity in zip(players, ("A", "B"))]
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
