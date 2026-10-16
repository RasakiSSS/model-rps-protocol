"""赛后公开记录与持续聊天；双方 EXIT 后结束，不修改比赛结果。"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter

from .diagnostics import LogError, TraceHandler, request_trace, trace
from .game import GameState, InvalidResponse
from .commands import PostmatchResponse, parse_postmatch
from .match import MatchConfig, MatchResult, MatchStatus
from .players import MatchPlayer, PlayerError
from .requests import MAX_ATTEMPTS, RequestPolicy


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
            {**{key: record[key] for key in ("game", "tick", "player", "attempt", "summary", "actions")},
             "ignored_actions": record.get("ignored_actions", [])}
            for record in self._decisions
            if record["game"] == number and record["tick"] <= state.completed_ticks
        ]
        self._games.append({
            "game": number, "completed_ticks": state.completed_ticks,
            "initiative": state.initiative.value,
            "first_player": state.first_player if state.initiative.value == "alternating" else None,
            "turns": {"A": state.turns[0], "B": state.turns[1]},
            "result": {"reason": state.result.reason.value, "winner": state.result.winner},
            "events": [event.to_dict() for event in state.events],
            "decisions": sorted(decisions, key=lambda r: (r["tick"], r["player"])),
        })
        self._decisions = [r for r in self._decisions if r["game"] != number]

    def report(self, config: MatchConfig, result: MatchResult) -> dict:
        if result.status == MatchStatus.TECHNICAL_ERROR:
            raise ValueError("技术异常中止的比赛不自动公开说明或开始赛后聊天。")
        return deepcopy({
            "best_of": config.best_of, "target_wins": config.target_wins, "max_games": config.max_games,
            "move_policy": config.move_policy.value,
            "initiative": config.initiative.value,
            "result": {"status": result.status.value, "winner": result.winner,
                       "wins": {"A": result.wins[0], "B": result.wins[1]},
                       "draws": result.draws, "games_played": result.games_played},
            "games": self._games,
        })


@dataclass(frozen=True)
class PostmatchOutcome:
    exited: dict[str, bool]
    failed_player: str | None = None
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None and all(self.exited.values())


class PostmatchFailed(Exception):
    """重试耗尽或不可重试；区别于玩家主动 EXIT。"""


async def _request(player: MatchPlayer, context: dict, policy: RequestPolicy,
                   on_request: TraceHandler | None) -> PostmatchResponse:
    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with request_trace(on_request, player=context["player"], tick=0, attempt=attempt,
                           phase="postmatch", request_id=context["request_id"]):
            started = perf_counter()
            trace("request_start", context=deepcopy(context), correction=feedback)
            retryable = True
            try:
                async with asyncio.timeout(policy.timeout):
                    raw = await player.postmatch(deepcopy(context), feedback)
                trace("raw_response", content=raw)
                response = parse_postmatch(raw)
                trace("postmatch_response", messages=response.messages, exit=response.exit)
                trace("request_end", status="valid", elapsed_seconds=perf_counter() - started)
                return response
            except LogError:
                raise
            except asyncio.CancelledError:
                trace("request_end", status="cancelled", elapsed_seconds=perf_counter() - started)
                raise
            except InvalidResponse as exc:
                summary = "赛后聊天格式无效"
                feedback = f"{exc} 请返回完整 JSON，使用 SAY 发言或 EXIT 退出。"
            except TimeoutError:
                summary = "赛后聊天请求超时"
                feedback = "上一请求超时，请根据同一份聊天状态返回完整 JSON。"
            except PlayerError as exc:
                summary = exc.safe_message
                retryable = exc.retryable
                feedback = f"{summary}，请根据同一份聊天状态返回完整 JSON。"
            except Exception:
                summary = "赛后聊天适配器发生内部错误"
                retryable = False
                feedback = None
            will_retry = retryable and attempt < MAX_ATTEMPTS
            trace("request_end", status="rejected", error=summary, correction=feedback,
                  will_retry=will_retry, elapsed_seconds=perf_counter() - started)
            if not will_retry:
                raise PostmatchFailed(f"{summary}；赛后聊天中止，比赛结果不受影响。")
        await asyncio.sleep(policy.retry_delay)
    raise AssertionError("赛后聊天重试循环不应到达此处。")


@dataclass
class PendingChat:
    request_id: int
    task: asyncio.Task
    checkpoint: object


async def run_postmatch(players: tuple[MatchPlayer, MatchPlayer], report: dict, *,
                        policy: RequestPolicy, on_request: TraceHandler | None = None,
                        on_event: TraceHandler | None = None) -> PostmatchOutcome:
    """首次邀请双方，之后由对手新消息/退出触发；忙碌时合并通知。"""
    seats = dict(zip(("A", "B"), players))
    exited = dict.fromkeys(seats, False)
    cursors = dict.fromkeys(seats, 0)
    invited = set()
    events = []
    pending: dict[str, PendingChat] = {}
    serial = 0

    def record(identity, request_id, kind, **data):
        if on_request is not None:
            on_request({"kind": kind, "player": identity, "phase": "postmatch",
                        "tick": 0, "request_id": request_id, **data})

    def publish(identity, kind, **data):
        event = {"sequence": len(events) + 1, "kind": kind, "player": identity, **data}
        events.append(event)
        if on_event is not None:
            on_event(deepcopy(event))

    def restore(identity, checkpoint):
        if checkpoint is not None:
            seats[identity].restore_history(checkpoint)

    try:
        while not all(exited.values()):
            for identity, player in seats.items():
                if exited[identity] or identity in pending:
                    continue
                updates = events[cursors[identity]:]
                peer_exited = exited["B" if identity == "A" else "A"]
                if identity in invited and not peer_exited and not any(e["player"] != identity for e in updates):
                    continue
                serial += 1
                context = {"player": identity, "request_id": serial, "events": deepcopy(updates),
                           "event_cursor": len(events), "exited": dict(exited)}
                if identity not in invited:
                    context["match"] = deepcopy(report)
                invited.add(identity)
                cursors[identity] = len(events)
                checkpoint = getattr(player, "checkpoint_history", lambda: None)()
                pending[identity] = PendingChat(serial, asyncio.create_task(
                    _request(player, context, policy, on_request)), checkpoint)
            # 非空 SAY 会唤醒对手；最后一位玩家始终可继续发言或 EXIT。
            if not pending:
                raise AssertionError("赛后聊天没有可运行的请求。")
            done, _ = await asyncio.wait([p.task for p in pending.values()], return_when=asyncio.FIRST_COMPLETED)
            failure = None
            for identity, item in sorted(list(pending.items()), key=lambda pair: pair[1].request_id):
                if item.task not in done:
                    continue
                try:
                    response = item.task.result()
                except PostmatchFailed as exc:
                    restore(identity, item.checkpoint)
                    pending.pop(identity)
                    if failure is None:
                        failure = PostmatchOutcome(dict(exited), identity, str(exc))
                    continue
                before = len(events)
                for message in response.messages:
                    publish(identity, "say", text=message)
                if response.exit:
                    exited[identity] = True
                    publish(identity, "exit")
                record(identity, item.request_id, "postmatch_applied", exit=response.exit,
                       event_sequences=list(range(before + 1, len(events) + 1)))
                pending.pop(identity)
            if failure is not None:
                return PostmatchOutcome(dict(exited), failure.failed_player, failure.error)
        return PostmatchOutcome(dict(exited))
    finally:
        for item in pending.values():
            item.task.cancel()
        await asyncio.gather(*(item.task for item in pending.values()), return_exceptions=True)
        for identity, item in pending.items():
            restore(identity, item.checkpoint)
            record(identity, item.request_id, "request_discarded", reason="赛后聊天中止，响应未公开")
