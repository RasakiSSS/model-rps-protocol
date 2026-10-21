import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.core.commands import parse_response
from model_rps.cli import main, make_parser, play
from model_rps.core.game import (EndReason, GameState, Gesture, Initiative, InvalidResponse, MovePolicy,
                            advance_turn, judge)
from model_rps.engine.match import MatchConfig, run_match
from model_rps.players.base import OPENAI, ScriptedPlayer, WAIT_RESPONSE, PlayerError
from model_rps.engine.runner import RequestPolicy, run_game

FAST = RequestPolicy(timeout=1, retry_delay=0)


def response(move=None, say=None):
    actions = [{"type": "MOVE", "move": move}] if move else []
    if say:
        actions.insert(0, {"type": "SAY", "text": say})
    return json.dumps(turn_payload(actions, "策略分析"))


class AlternatingRulesTests(unittest.TestCase):
    def test_all_results_both_seats_visibility_and_immediate_settlement(self):
        for first in ("A", "B"):
            for a in Gesture:
                for b in Gesture:
                    with self.subTest(first=first, a=a, b=b):
                        state = GameState(initiative="alternating", first_player=first)
                        other = "B" if first == "A" else "A"
                        moves = {"A": a, "B": b}
                        state = advance_turn(state, first, parse_response(response(moves[first], "即时公开")))
                        view = state.view_for(other).to_dict()
                        self.assertEqual((view["tick"], view["own_turn"], view["max_ticks"]), (2, 1, 40))
                        self.assertTrue(view["opponent_committed"])
                        self.assertIsNone(view["own_move"])
                        self.assertIn("即时公开", json.dumps(view, ensure_ascii=False))
                        self.assertNotIn(moves[first].value, json.dumps(view))
                        self.assertIsNone(state.result)
                        state = advance_turn(state, other, parse_response(response(moves[other], "最后发言")))
                        self.assertEqual(state.result.winner, judge(a, b))
                        self.assertEqual(state.completed_ticks, 2)
                        self.assertEqual(state.turns, (1, 1))
                        self.assertLess(next(i for i, e in enumerate(state.events) if e.text == "最后发言"),
                                        next(i for i, e in enumerate(state.events) if e.kind == "reveal"))
                        with self.assertRaises(ValueError):
                            advance_turn(state, first, parse_response(WAIT_RESPONSE))

    def test_deadline_and_reminders_use_twenty_opportunities_per_player(self):
        for first in ("A", "B"):
            for mode in ("draw", "forfeit-a", "forfeit-b", "last-showdown"):
                state = GameState(initiative="alternating", first_player=first)
                for tick in range(1, 41):
                    player = state.current_player
                    move = None
                    if mode == "forfeit-a" and player == "B":
                        move = "Rock"
                    elif mode == "forfeit-b" and player == "A":
                        move = "Paper"
                    elif mode == "last-showdown" and tick >= 39:
                        move = "Paper" if player == "A" else "Rock"
                    state = advance_turn(state, player, parse_response(response(move)))
                    if tick < 40:
                        self.assertIsNone(state.result)
                self.assertEqual(state.turns, (20, 20))
                self.assertEqual(state.result.reason, EndReason.NO_MOVES if mode == "draw" else
                                 EndReason.SHOWDOWN if mode == "last-showdown" else EndReason.FORFEIT)
                for player in ("A", "B"):
                    reminders = [e for e in state.events if e.kind == "turn_reminder" and e.player == player]
                    self.assertEqual(len(reminders), 3)
                    self.assertEqual([int(e.text.split("剩余 ")[1].split(" 次")[0]) for e in reminders], [10, 2, 1])
                if mode != "draw":
                    self.assertEqual(state.result.winner, "B" if mode == "forfeit-a" else "A")

    def test_invalid_order_and_rule_combinations(self):
        with self.assertRaises(ValueError):
            MatchConfig(initiative="alternating", move_policy="revisable")
        with self.assertRaises(ValueError):
            GameState(initiative="alternating", move_policy="revisable")
        with self.assertRaises(ValueError):
            advance_turn(GameState(initiative="alternating"), "B", parse_response(WAIT_RESPONSE))
        for actions in ([{"type": "ENDTURN"}, {"type": "SAY", "text": "结束后说话"}],
                        [{"type": "WAIT"}, {"type": "MOVE", "move": "Rock"}],
                        [{"type": "WAIT"}, {"type": "WAIT"}]):
            with self.assertRaises(InvalidResponse):
                parse_response(json.dumps(turn_payload(actions, "分析")))
        self.assertEqual(len(parse_response(response(say="聊天并等待"))), 2)


class AlternatingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_same_snapshot_no_extra_turn_and_immediate_peer_context(self):
        invalid = '{"actions":[{"type":"ANALYZE","text":"分析"},{"type":"SAY","text":"不能公开"}]}'
        a = ScriptedPlayer([invalid, response("Rock", "已公开的发言")])
        b = ScriptedPlayer([response("Paper")])
        state = await run_game(a, b, initiative="alternating", policy=FAST)
        self.assertEqual(state.result.winner, "B")
        self.assertEqual(state.turns, (1, 1))
        self.assertIs(a.requests[0][0], a.requests[1][0])
        self.assertIn("ENDTURN", a.requests[1][1])
        self.assertEqual(len(b.requests), 1)
        self.assertTrue(b.requests[0][0].opponent_committed)
        self.assertIn("已公开的发言", repr(b.requests[0][0].history))
        self.assertNotIn("不能公开", repr(state.events))

    async def test_locked_move_ignored_keeps_say_and_consumes_turn(self):
        a = ScriptedPlayer([response("Rock"), response("Paper", "仍然可说话")])
        b = ScriptedPlayer([response(say="我先等等"), response("Scissors")])
        records = []
        state = await run_game(a, b, initiative="alternating", policy=FAST, on_request=records.append)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(state.turns, (2, 2))
        self.assertEqual((len(a.requests), len(b.requests)), (2, 2))
        self.assertEqual(state.moves, (Gesture.ROCK, Gesture.SCISSORS))
        self.assertIn("仍然可说话", repr(b.requests[1][0].history))
        self.assertNotIn("Paper", json.dumps(b.requests[1][0].to_dict()))
        record = next(r for r in records if r["kind"] == "decision" and r["tick"] == 3)
        self.assertEqual(record["actions"], [{"type": "SAY", "text": "仍然可说话"}])
        self.assertEqual(record["ignored_actions"], [{"type": "MOVE", "move": "Paper"}])

    async def test_failure_keeps_prior_public_turn_and_never_requests_future_turn(self):
        for fail_first in (False, True):
            failed = ScriptedPlayer([PlayerError("服务不可用", retryable=False)])
            good = ScriptedPlayer([response("Rock", "之前的发言")])
            a, b = (failed, good) if fail_first else (good, failed)
            state = await run_game(a, b, initiative="alternating", policy=FAST)
            self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
            self.assertEqual(state.completed_ticks, 0 if fail_first else 1)
            self.assertEqual(len(good.requests), 0 if fail_first else 1)
            self.assertEqual(state.moves, (None, None) if fail_first else (Gesture.ROCK, None))
            self.assertFalse(any(e.kind == "reveal" for e in state.events))

    async def test_timeout_and_cancel_do_not_start_other_player(self):
        class Pending:
            def __init__(self):
                self.count = 0
                self.started = asyncio.Event()
                self.cancelled = 0

            async def respond(self, view, feedback=None):
                self.count += 1
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled += 1

        a, b = Pending(), ScriptedPlayer([response("Rock")])
        state = await run_game(a, b, initiative="alternating", policy=RequestPolicy(timeout=.02, retry_delay=0))
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual((a.count, a.cancelled, len(b.requests)), (3, 3, 0))
        a = Pending()
        task = asyncio.create_task(run_game(a, b, initiative="alternating", policy=FAST))
        await a.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((a.cancelled, len(b.requests)), (1, 0))

    async def test_bo_switches_first_player_after_draw_and_resets_next_match(self):
        a = ScriptedPlayer([response("Rock"), response("Paper"), response("Paper")])
        b = ScriptedPlayer([response("Rock")] * 3)
        starts = []
        config = MatchConfig(3, initiative="alternating")
        result = await run_match(lambda: (a, b), config=config, policy=FAST,
                                 on_request=lambda game, r: starts.append((game, r["player"]))
                                 if r["kind"] == "request_start" and r["phase"] == "game" else None)
        self.assertEqual((result.wins, result.draws, result.games_played), ((2, 0), 1, 3))
        self.assertEqual(starts, [(1, "A"), (1, "B"), (2, "B"), (2, "A"), (3, "A"), (3, "B")])
        self.assertEqual([c["first_player"] for c in a.game_contexts], ["A", "B", "A"])
        self.assertIsNotNone(a.game_contexts[1]["last_game"])
        await run_match(lambda: (a, b), config=config, policy=FAST)
        self.assertEqual(len(a.game_contexts), 3)
        self.assertIsNone(a.game_contexts[0]["last_game"])

    async def test_live_cli_config_history_postmatch_and_ignored_actions(self):
        requests, postmatchs = [], []

        def handler(request):
            body = json.loads(request.content)
            ctx = json.loads(body["messages"][-1]["content"])
            identity = "A" if body["model"] == OPENAI.model else "B"
            if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch":
                postmatchs.append(ctx)
                self.assertEqual(ctx["match"]["initiative"], "alternating")
                self.assertEqual(ctx["match"]["games"][0]["first_player"], "Red · GPT 测试")
                self.assertEqual(ctx["match"]["games"][1]["first_player"], "Blue · DeepSeek 测试")
                self.assertEqual(ctx["match"]["games"][0]["decisions"][2]["ignored_actions"],
                                 [{"type": "MOVE", "move": "Paper"}])
                content = json.dumps(postmatch_payload("\n\n".join(["测试复盘", "有效动作与忽略动作分别评价", "继续观察"])))
            elif ctx['phase'] == 'chat':
                content = '{"actions":[]}'
            else:
                state = ctx["state"]
                requests.append((ctx["match"]["game"], state["tick"], identity))
                self.assertEqual(state["initiative"], "alternating")
                self.assertIn("alternating：双方轮流行动", body["messages"][0]["content"])
                self.assertNotIn("simultaneous：", body["messages"][0]["content"])
                if ctx["match"]["game"] == 2:
                    self.assertGreater(len(body["messages"]), 2)
                content = response("Rock" if state["own_turn"] == 1 else "Paper", "公开消息") if identity == "A" else response(
                    "Scissors" if state["own_turn"] == 2 else None, "我先等等")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keys.py").write_text('OPENAI_API_KEY="test-a"\nDEEPSEEK_API_KEY="test-b"', encoding="utf-8")
            args = make_parser().parse_args(["--initiative", "alternating", "--bo", "3", "--openai-name", "GPT 测试",
                                            "--deepseek-name", "DeepSeek 测试", "--secrets-file", str(root / "keys.py"),
                                            "--log-dir", str(root / "logs")])
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(await play(args), 0)
            self.assertEqual(requests, [(1, 1, "A"), (1, 2, "B"), (1, 3, "A"), (1, 4, "B"),
                                        (2, 1, "B"), (2, 2, "A"), (2, 3, "B")])
            self.assertEqual(len(postmatchs), 2)
            self.assertIn("本次 MOVE 无效", output.getvalue())
            run, = (root / "logs").iterdir()
            thought = (run / "red/thought.log").read_text(encoding="utf-8")
            self.assertIn("忽略的 MOVE：布", thought)
            debug = [json.loads(s) for s in (run / "red/debug.log").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["first_player"] for r in debug if r["kind"] == "game_start"], ["Red · GPT 测试", "Blue · DeepSeek 测试"])

    def test_toml_alternating_and_incompatible_rules_fail_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text('[app]\nmode="demo"\nlog_dir="runs"\n[match]\ninitiative="alternating"\nbest_of=1', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), patch("model_rps.cli.httpx.AsyncClient") as client:
                self.assertEqual(main(["--config", str(path)]), 0)
            client.assert_not_called()
            with contextlib.redirect_stderr(io.StringIO()), patch("model_rps.cli.httpx.AsyncClient") as client:
                self.assertEqual(main(["--config", str(path), "--move-policy", "revisable"]), 2)
            client.assert_not_called()
            self.assertEqual(len(list((root / "runs").iterdir())), 1)
