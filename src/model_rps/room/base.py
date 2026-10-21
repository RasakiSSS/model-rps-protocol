"""一场 BO 的共享状态与生命周期；对外仅提供固定席位的视图和公共事件。"""

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import sys

from ..commands import AnalysisPolicy
from ..console import Console, display_text
from ..game import InvalidResponse
from ..identities import named_data
from ..logs import RunLogs
from ..match import MatchConfig, MatchStatus, run_match
from ..external_player import ExternalPlayer
from ..postmatch import MatchArchive, run_postmatch
from ..runner import RequestPolicy
from ..seats import SeatPlayer


class Room:
    def __init__(self, opponent_factory, *, config=None, policy=None, identity="A", name="外部 Agent",
                 opponent_name="模拟对手", analysis=AnalysisPolicy.REQUIRED, log_dir="logs", secrets=(),
                 external_players=None, output=None, source="mcp"):
        if identity not in ("A", "B"):
            raise ValueError("外部玩家席位必须是 A 或 B。")
        self.opponent_factory = opponent_factory  # async context manager，负责关闭模型 HTTP 客户端。
        self.config = config or MatchConfig()
        self.policy = policy or RequestPolicy()
        self.identity = identity
        self.names = {identity: f"{'Red' if identity == 'A' else 'Blue'} · {name}",
                      "B" if identity == "A" else "A": f"{'Blue' if identity == 'A' else 'Red'} · {opponent_name}"}
        self.inputs = external_players or {identity: ExternalPlayer(analysis=analysis)}
        self.player = self.inputs[identity]
        for player in self.inputs.values():
            player.on_update = self._changed
            player.on_boundary = self._changed
        self.output = output or (lambda text: print(text, file=sys.stderr, flush=True))
        self.source = source
        self.log_dir, self.secrets = Path(log_dir), secrets
        self.revision = 0
        self.changed = asyncio.Event()
        self.events = []
        self.receipts = {}
        self.task = None
        self.status = "not_started"
        self.result = None
        self.error = None
        self.report = None
        self.logs = None
        self.game = 0

    def _changed(self):
        self.revision += 1
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    def _publish(self, kind, **data):
        self._changed()
        self.events.append({"revision": self.revision, "kind": kind, **deepcopy(data)})

    def _check_cursor(self, after):
        if type(after) is not int or not 0 <= after <= self.revision:
            raise InvalidResponse("事件游标必须是 0 到当前 revision 之间的整数。")

    def snapshot(self, after=0, *, identity=None):
        self._check_cursor(after)
        identity = identity or self.identity
        player = self.inputs[identity]
        active = self.status in ("running", "postmatch")
        data = {"revision": self.revision, "status": self.status,
                "seat": "Red" if identity == "A" else "Blue", "player": self.names[identity],
                "phase": player.phase if active else self.status,
                "can_submit": active and player.can_submit,
                "turn_token": player.turn_token if active else None,
                "analysis": player.analysis_policy.value,
                "events": [e for e in self.events if e["revision"] > after]}
        if active and player.phase == "game" and player.view:
            view = player.view.to_dict()
            view["can_act"] = player.can_act
            # 公共事件按 revision 提供，避免在每次等待中重复发送历史。
            view.pop("history", None)
            data.update(state=view, match={k: v for k, v in player.match.items() if k != "last_game"})
        if self.status == "postmatch":
            data["exited"] = player.postmatch_context.get("exited", {})
        if self.result is not None:
            data["result"] = self.result
        if self.error:
            data["error"] = self.error
        safe = named_data(deepcopy(data), self.names)
        return self.logs.redact_value(safe) if self.logs else safe

    async def read(self, after=0):
        return self.snapshot(after)

    async def start(self):
        if self.task is None and self.status == "not_started":
            self.status = "running"
            self.task = asyncio.create_task(self._run())
            await asyncio.sleep(0)
        return self.snapshot()

    async def wait(self, after, timeout=20, *, identity=None):
        self._check_cursor(after)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 20:
            raise InvalidResponse("单次等待时长必须在 0 到 20 秒之间。")
        if self.revision == after and self.status in ("not_started", "running", "postmatch"):
            event = self.changed
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except TimeoutError:
                pass
        return self.snapshot(after, identity=identity)

    async def act(self, actions, turn_token, action_id, after_revision=0, *, identity=None):
        identity = identity or self.identity
        self._check_cursor(after_revision)
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 100:
            raise InvalidResponse("每次提交须提供 1 到 100 字符的独立 action_id；重试复用原 ID。")
        if not isinstance(actions, list) or not isinstance(turn_token, str):
            raise InvalidResponse("actions 必须是指令列表，turn_token 必须来自最新状态。")
        try:
            payload = json.dumps([turn_token, actions], sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise InvalidResponse("指令必须是有效 JSON。") from None
        receipt = (identity, action_id)
        if receipt in self.receipts:
            if self.receipts[receipt] != payload:
                raise InvalidResponse("相同 action_id 不能用于不同指令。")
            return {"accepted": True, "replayed": True, "action_id": action_id, "state": self.snapshot(after_revision, identity=identity)}
        if self.status not in ("running", "postmatch"):
            raise InvalidResponse("比赛尚未开始或已经结束。")
        self.inputs[identity].submit_actions(actions, turn_token)
        self.receipts[receipt] = payload
        await asyncio.sleep(0)
        return {"accepted": True, "replayed": False, "action_id": action_id, "state": self.snapshot(after_revision, identity=identity)}

    async def close(self):
        if self.task is not None and not self.task.done():
            # 清理输入 futures 也会唤醒长轮询；先关闭行动权限，避免返回仍在比赛的中间态。
            if self.status not in ("complete", "failed", "cancelled"):
                self.status = "cancelled"
                self._changed()
            if not self.task.cancelling():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        elif self.task is None:
            self.status = "cancelled"
            self._changed()

    async def _run(self):
        try:
            with RunLogs(self.log_dir, names=self.names, secrets=self.secrets) as logs:
                self.logs = logs
                async with self.opponent_factory() as opponent:
                    seats = {self.identity: SeatPlayer(self.player, self.names[self.identity]),
                             "B" if self.identity == "A" else "A": SeatPlayer(opponent, self.names["B" if self.identity == "A" else "A"])}
                    players = seats["A"], seats["B"]
                    console = Console(self.names, logs, color=False)
                    console.output = self.output
                    archive = MatchArchive()
                    logs.lifecycle("match_start", source=self.source, players=self.names,
                                   best_of=self.config.best_of, max_games=self.config.max_games,
                                   initiative=self.config.initiative, move_policy=self.config.move_policy)

                    def request(number, record):
                        record = {"actor": self.names[record["player"]], **record}
                        logs.request(number, record)
                        archive.request(number, record)

                    def start(number):
                        self.game = console.game = number
                        console.tick = 0
                        logs.lifecycle("game_start", game=number)
                        self._publish("game_start", game=number)

                    def event(number, item):
                        console.event(item)
                        self._publish("game_event", game=number, event=item.to_dict())

                    def end(number, state, wins, draws):
                        archive.finish_game(number, state)
                        result_text = console.result(state)
                        console.write(f"System：比分 {self.names['A']} {wins[0]} : {wins[1]} {self.names['B']}；平局 {draws}。")
                        public = {"game": number, "reason": state.result.reason.value,
                                  "winner": state.result.winner, "wins": {"A": wins[0], "B": wins[1]}, "draws": draws}
                        logs.lifecycle("game_end", **public)
                        self._publish("game_end", **public, text=result_text)

                    result = await run_match(lambda: players, config=self.config, policy=self.policy,
                                             on_start=start, on_event=event, on_request=request, on_end=end)
                    self.result = {"status": result.status.value, "winner": result.winner,
                                   "wins": {"A": result.wins[0], "B": result.wins[1]},
                                   "draws": result.draws, "games_played": result.games_played}
                    logs.lifecycle("match_end", **self.result)
                    if result.status == MatchStatus.WON:
                        text = f"System：{self.names[result.winner]} 赢得 BO{self.config.best_of}。"
                    elif result.status == MatchStatus.UNDECIDED:
                        text = "System：达到实际总局数上限，本场未决。"
                    else:
                        text = "System：比赛因技术异常中止。"
                    console.write(text)
                    self._publish("match_end", result=self.result, text=text)
                    if result.status == MatchStatus.TECHNICAL_ERROR:
                        self.status = "failed"
                        return
                    self.report = logs.redact_value(archive.report(self.config, result))
                    self.status = "postmatch"
                    self._publish("postmatch_start", report=self.report)
                    logs.lifecycle("postmatch_start")
                    for game in self.report["games"]:
                        for decision in game["decisions"]:
                            if decision['summary']:
                                console.write(f"System：第 {game['game']} 局 {self.names[decision['player']]} 分析：{display_text(decision['summary'])}")

                    def post_event(item):
                        logs.lifecycle("postmatch_event", event=item)
                        if item["kind"] == "say":
                            console.write(f"{self.names[item['player']]}：{display_text(item['text'])}")
                        else:
                            console.write(f"System：{self.names[item['player']]} 已退出赛后聊天。")
                        self._publish("postmatch_event", event=item)

                    outcome = await run_postmatch(players, self.report, policy=self.policy,
                                                  on_request=lambda r: request(0, r), on_event=post_event)
                    self.status = "complete" if outcome.complete else "failed"
                    logs.lifecycle("postmatch_end", complete=outcome.complete, exited=outcome.exited)
        except asyncio.CancelledError:
            self.status = "cancelled"
            raise
        except Exception:
            self.status = "failed"
            self.error = "比赛服务发生技术异常，已停止；不公开私有数据或原始异常。"
        finally:
            self._changed()
