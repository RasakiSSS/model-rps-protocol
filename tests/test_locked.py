import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from model_rps.cli import main, make_parser
from model_rps.game import Action, ActionKind, EndReason, GameState, Gesture, InvalidResponse, MovePolicy, advance, judge, parse_response
from model_rps.match import MatchConfig, run_match
from model_rps.players import ChatCompletionPlayer, DEEPSEEK, OPENAI, REVIEW_PROMPT, ScriptedPlayer, WAIT_RESPONSE, game_prompt
from model_rps.runner import RequestPolicy, run_game

FAST = RequestPolicy(timeout=1, retry_delay=0)
WAIT = parse_response(WAIT_RESPONSE)


def response(*actions):
    return json.dumps({"decision_summary": "测试锁定决策", "actions": actions})


def move(value):
    return response({"type": "MOVE", "move": value})


class LockedRulesTests(unittest.TestCase):
    def test_default_and_all_nine_results_settle_after_entire_tick(self):
        self.assertEqual(make_parser().parse_args([]).move_policy, "locked")
        self.assertEqual(MatchConfig().move_policy, MovePolicy.LOCKED)
        for a in Gesture:
            for b in Gesture:
                with self.subTest(a=a, b=b):
                    actions = parse_response(response({"type": "MOVE", "move": a}, {"type": "SAY", "text": "提交后的发言"}))
                    state = advance(GameState(), actions, parse_response(move(b)))
                    self.assertEqual(state.completed_ticks, 1)
                    self.assertEqual(state.result.winner, judge(a, b))
                    self.assertEqual(state.result.reason, EndReason.SHOWDOWN)
                    self.assertIsNone(state.settlement_tick)
                    self.assertEqual([e.kind for e in state.events], ["commit", "say", "commit", "reveal", "reveal"])

    def test_locked_moves_ignored_but_multiple_moves_rejected(self):
        original = advance(GameState(), parse_response(move("Rock")), WAIT)
        for gesture in Gesture:
            actions = parse_response(response({"type": "SAY", "text": "仍可公开"}, {"type": "MOVE", "move": gesture}))
            with self.subTest(gesture=gesture):
                state = advance(original, actions, parse_response(move("Paper")))
                self.assertEqual(state.moves, (Gesture.ROCK, Gesture.PAPER))
                self.assertEqual(state.result.winner, "B")
                self.assertIn("仍可公开", [e.text for e in state.events])
                self.assertTrue(any(e.kind == "move_ignored" for e in state.events))
            self.assertEqual(original.moves, (Gesture.ROCK, None))
        for gesture in Gesture:
            with self.assertRaises(InvalidResponse):
                parse_response(response({"type": "MOVE", "move": "Rock"}, {"type": "MOVE", "move": gesture}))

    def test_locked_player_can_chat_and_wait_without_revealing_move(self):
        state = advance(GameState(), parse_response(move("Paper")), WAIT)
        state = advance(state, (Action(ActionKind.SAY, text="继续交流"), *WAIT), WAIT)
        state = advance(state, WAIT, WAIT)
        a, b = state.view_for("A").to_dict(), state.view_for("B").to_dict()
        self.assertTrue(a["own_move_locked"])
        self.assertFalse(b["own_move_locked"])
        self.assertEqual(b["move_policy"], "locked")
        self.assertTrue(b["opponent_committed"])
        self.assertNotIn("Paper", json.dumps(b))
        state = advance(state, WAIT, parse_response(move("Rock")))
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(state.completed_ticks, 4)

    def test_deadline_settles_at_twenty_without_extra_tick(self):
        for a, b, reason in (("Rock", "Scissors", EndReason.SHOWDOWN), ("Rock", None, EndReason.FORFEIT), (None, None, EndReason.NO_MOVES)):
            state = GameState()
            for _ in range(19):
                state = advance(state, WAIT, WAIT)
            self.assertEqual([e.tick for e in state.events if e.kind == "reminder"], [10, 18, 19])
            state = advance(state, parse_response(move(a)) if a else WAIT, parse_response(move(b)) if b else WAIT)
            self.assertEqual(state.result.reason, reason)
            self.assertEqual(state.completed_ticks, 20)
            self.assertIsNone(state.settlement_tick)
            with self.assertRaises(ValueError):
                state.view_for("A")

    def test_cli_modes_and_incompatible_scenarios(self):
        for options, expected_tick in (([], 2), (["--move-policy", "revisable"], 3)):
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
                status = main(["--no-config", "--mode", "demo", "--max-games", "1", "--log-dir", directory, *options])
                self.assertEqual(status, 0)
                root, = Path(directory).iterdir()
                rows = [json.loads(s) for s in (root / "red/debug.log").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(next(r for r in rows if r["kind"] == "game_end")["completed_ticks"], expected_tick)
                self.assertTrue(next(r for r in rows if r["kind"] == "review_end")["complete"])
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--no-config", "--mode", "demo", "--scenario", "change", "--log-dir", directory]), 2)
            self.assertEqual(list(Path(directory).iterdir()), [])


class LockedFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_revision_ignored_without_retry_and_say_is_published(self):
        invalid = response({"type": "SAY", "text": "继续交流"}, {"type": "MOVE", "move": "Paper"})
        a = ScriptedPlayer([move("Rock"), invalid, WAIT_RESPONSE])
        b = ScriptedPlayer([WAIT_RESPONSE, move("Scissors")])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual((len(a.requests), len(b.requests)), (2, 2))
        self.assertEqual(state.moves, (Gesture.ROCK, Gesture.SCISSORS))
        self.assertNotIn("Scissors", json.dumps(a.requests[1][0].to_dict()))
        self.assertIn("继续交流", repr(state.events))

    async def test_exhausted_invalid_actions_abort_without_executing_peer_or_revealing(self):
        invalid = response({"type": "MOVE", "move": "Rock"}, {"type": "WAIT"})
        a = ScriptedPlayer([move("Rock")] + [invalid] * 3)
        b = ScriptedPlayer([WAIT_RESPONSE, move("Paper")])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(state.completed_ticks, 1)
        self.assertEqual(state.moves, (Gesture.ROCK, None))
        self.assertEqual(state.move_policy, MovePolicy.LOCKED)
        self.assertFalse(any(e.kind == "reveal" for e in state.events))

    async def test_multiple_initial_moves_retried_before_any_action_executes(self):
        a = ScriptedPlayer([response({"type": "MOVE", "move": "Rock"}, {"type": "MOVE", "move": "Paper"}), move("Scissors")])
        b = ScriptedPlayer([move("Paper")])
        state = await run_game(a, b, policy=FAST)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(state.moves, (Gesture.SCISSORS, Gesture.PAPER))
        self.assertEqual((len(a.requests), len(b.requests)), (2, 1))
        self.assertIsNone(a.requests[1][0].own_move)

    async def test_locked_bo_preserves_context_and_switching_match_resets_rules(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            content = move("Paper" if body["model"] == DEEPSEEK.model else "Rock")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            players = ChatCompletionPlayer(client, DEEPSEEK, "a"), ChatCompletionPlayer(client, OPENAI, "b")
            result = await run_match(lambda: players, config=MatchConfig(3), policy=FAST)
            self.assertEqual(result.wins, (2, 0))
            self.assertEqual(len(bodies), 4)
            for body in bodies:
                ctx = json.loads(body["messages"][-1]["content"])
                self.assertEqual(ctx["state"]["move_policy"], "locked")
                self.assertEqual(body["messages"][0]["content"], game_prompt(MovePolicy.LOCKED))
                self.assertEqual(ctx["state"]["tick"], 1)
                self.assertFalse(ctx["state"]["own_move_locked"])
                if ctx["match"]["game"] == 2:
                    self.assertEqual(ctx["match"]["wins"], {"A": 1, "B": 0})
                    self.assertEqual(len(body["messages"]), 4)
            bodies.clear()
            await run_match(lambda: players, config=MatchConfig(move_policy=MovePolicy.REVISABLE), policy=FAST)
            self.assertEqual(len(bodies), 4)
            self.assertEqual(len(bodies[0]["messages"]), 2)
            self.assertEqual(bodies[0]["messages"][0]["content"], game_prompt(MovePolicy.REVISABLE))

    async def test_live_cli_routes_locked_rules_to_play_reviews_and_logs(self):
        reviews = []

        def handler(request):
            body = json.loads(request.content)
            ctx = json.loads(body["messages"][-1]["content"])
            if body["messages"][0]["content"] == REVIEW_PROMPT:
                reviews.append(ctx)
                self.assertEqual(ctx["match"]["move_policy"], "locked")
                self.assertEqual(len(ctx["match"]["games"][0]["decisions"]), 2)
                content = json.dumps({"assessment": "完成", "key_moments": "第1局Tick1", "improvements": "观察更多场次"})
            else:
                self.assertEqual(body["messages"][0]["content"], game_prompt(MovePolicy.LOCKED))
                content = move("Paper" if body["model"] == DEEPSEEK.model else "Rock")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "keys.py").write_text('DEEPSEEK_API_KEY="test-a"\nOPENAI_API_KEY="test-b"\n', encoding="utf-8")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            from model_rps.cli import play
            args = make_parser().parse_args(["--secrets-file", str(path / "keys.py"), "--log-dir", str(path / "logs")])
            with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(await play(args), 0)
            self.assertEqual(len(reviews), 2)
            self.assertNotIn("结算 Tick", output.getvalue())
            root, = (path / "logs").iterdir()
            rows = [json.loads(s) for s in (root / "red/debug.log").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(next(r for r in rows if r["kind"] == "match_start")["move_policy"], "locked")


if __name__ == "__main__":
    unittest.main()
