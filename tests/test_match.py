import json
import unittest

import httpx

from model_rps.game import MovePolicy, EndReason
from model_rps.match import MatchConfig, MatchStatus, run_match
from model_rps.players import ChatCompletionPlayer, DEEPSEEK, OPENAI, PlayerError, ScriptedPlayer, WAIT_RESPONSE
from model_rps.runner import RequestPolicy

FAST = RequestPolicy(timeout=1, retry_delay=0)


def move(gesture, *, summary="本局决策", say=None):
    actions = [{"type": "MOVE", "move": gesture}]
    if say:
        actions.insert(0, {"type": "SAY", "text": say})
    return json.dumps({"decision_summary": summary, "actions": actions})


class MatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_bo_targets_early_stop_and_same_players_with_fresh_game_state(self):
        for bo in (1, 3, 5, 101):
            with self.subTest(bo=bo):
                players = []

                def factory():
                    pair = (ScriptedPlayer([move("Paper"), WAIT_RESPONSE] * (bo // 2 + 1)),
                            ScriptedPlayer([move("Rock"), WAIT_RESPONSE] * (bo // 2 + 1)))
                    players.append(pair)
                    return pair

                result = await run_match(factory, config=MatchConfig(bo, move_policy=MovePolicy.REVISABLE), policy=FAST)
                self.assertEqual(result.winner, "A")
                self.assertEqual(result.wins, (bo // 2 + 1, 0))
                self.assertEqual(len(players), 1)
                for player in players[0]:
                    self.assertEqual(len(player.requests), 2 * (bo // 2 + 1))
                    self.assertTrue(all(view.tick == 1 and view.own_move is None and not view.history
                                        for view, feedback in player.requests[::2]))
                    self.assertTrue(all(view.is_settlement_tick for view, _ in player.requests[1::2]))
                    self.assertEqual(player.game_contexts[-1]["wins"], {"A": bo // 2, "B": 0})

    async def test_draws_do_not_score_and_last_allowed_game_can_win(self):
        outcomes = [("Rock", "Rock"), ("Rock", "Paper"), ("Paper", "Rock"), ("Rock", "Paper")]

        def factory():
            return (ScriptedPlayer([response for a, b in outcomes for response in (move(a), WAIT_RESPONSE)]),
                    ScriptedPlayer([response for a, b in outcomes for response in (move(b), WAIT_RESPONSE)]))

        result = await run_match(factory, config=MatchConfig(3, 4, move_policy=MovePolicy.REVISABLE), policy=FAST)
        self.assertEqual(result.status, MatchStatus.WON)
        self.assertEqual(result.winner, "B")
        self.assertEqual(result.wins, (1, 2))
        self.assertEqual((result.games_played, result.draws), (4, 1))

    async def test_draw_cap_and_leading_score_are_undecided(self):
        count = 0

        def factory():
            nonlocal count
            count += 1
            return (ScriptedPlayer([move("Paper"), WAIT_RESPONSE] + [move("Rock"), WAIT_RESPONSE] * 2),
                    ScriptedPlayer([move("Rock"), WAIT_RESPONSE] * 3))

        result = await run_match(factory, config=MatchConfig(3, 3, move_policy=MovePolicy.REVISABLE), policy=FAST)
        self.assertEqual(result.status, MatchStatus.UNDECIDED)
        self.assertIsNone(result.winner)
        self.assertEqual(result.wins, (1, 0))
        self.assertEqual(result.draws, 2)
        self.assertEqual(count, 1)

    async def test_forfeit_scores_but_technical_failure_stops_and_preserves_score(self):
        count = 0
        reasons = []

        def factory():
            nonlocal count
            count += 1
            return (ScriptedPlayer([move("Rock")] + [WAIT_RESPONSE] * 19 +
                                   [PlayerError("HTTP 401", retryable=False)]),
                    ScriptedPlayer([WAIT_RESPONSE] * 20 + [move("Paper")]))

        result = await run_match(factory, config=MatchConfig(3, move_policy=MovePolicy.REVISABLE), policy=FAST,
                                 on_end=lambda n, state, wins, draws: reasons.append(state.result.reason))
        self.assertEqual(reasons, [EndReason.FORFEIT, EndReason.TECHNICAL_ERROR])
        self.assertEqual(result.status, MatchStatus.TECHNICAL_ERROR)
        self.assertEqual(result.wins, (1, 0))
        self.assertEqual(result.draws, 0)
        self.assertEqual(count, 1)

    async def test_http_context_persists_within_match_but_is_private_and_resets_next_match(self):
        requests = []
        counts = {DEEPSEEK.model: 0, OPENAI.model: 0}

        def handler(request):
            payload = json.loads(request.content)
            context = json.loads(payload["messages"][-1]["content"])
            requests.append((payload, context))
            model = payload["model"]
            counts[model] += 1
            # 第一局 A 先出现格式错误：历史保留，但新局的当前 correction 为空。
            if model == DEEPSEEK.model and counts[model] == 1:
                content = "bad-response"
            else:
                content = move("Paper" if model == DEEPSEEK.model else "Rock",
                               summary=f"PRIVATE-{model}", say=f"PUBLIC-{model}")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            players = ChatCompletionPlayer(client, DEEPSEEK, "key-a"), ChatCompletionPlayer(client, OPENAI, "key-b")

            def factory():
                return players

            result = await run_match(factory, config=MatchConfig(5, move_policy=MovePolicy.REVISABLE), policy=FAST)
            self.assertEqual(result.wins, (3, 0))
            self.assertEqual(counts, {DEEPSEEK.model: 7, OPENAI.model: 6})
            for payload, context in requests:
                if context["state"]["tick"] == 1:
                    self.assertEqual(context["state"]["history"], [])
                    self.assertIsNone(context["state"]["own_move"])
                    self.assertFalse(context["state"]["opponent_committed"])
                else:
                    self.assertEqual(context["state"]["tick"], 2)
                    self.assertTrue(context["state"]["is_settlement_tick"])
                    self.assertTrue(context["state"]["opponent_committed"])
                opponent = OPENAI.model if payload["model"] == DEEPSEEK.model else DEEPSEEK.model
                self.assertNotIn(f"PRIVATE-{opponent}", json.dumps(payload))
                if context["match"]["game"] == 1:
                    self.assertIsNone(context["match"]["last_game"])
                    if context["state"]["tick"] == 1:
                        self.assertNotIn(f"PUBLIC-{opponent}", json.dumps(payload))
                else:
                    self.assertGreater(len(payload["messages"]), 2)
                    self.assertIn(f"PRIVATE-{payload['model']}", json.dumps(payload))
                    self.assertIn(f"PUBLIC-{opponent}", json.dumps(payload))
                    self.assertIsNone(context["correction"])
                    self.assertEqual(context["match"]["wins"], {"A": context["match"]["game"] - 1, "B": 0})
                    self.assertEqual(context["match"]["last_game"]["result"]["winner"], "A")
                    revealed = [e["text"] for e in context["match"]["last_game"]["events"] if e["kind"] == "reveal"]
                    self.assertEqual(revealed, ["Paper", "Rock"])
                    if context["match"]["game"] == 3:
                        previous_user_games = [json.loads(m["content"])["match"]["game"]
                                               for m in payload["messages"][:-1] if m["role"] == "user"]
                        self.assertIn(1, previous_user_games)
                        self.assertIn(2, previous_user_games)

            # 即使调用方复用同一组玩家对象，新比赛仍必须显式清空记忆。
            requests.clear()
            await run_match(factory, config=MatchConfig(1, move_policy=MovePolicy.REVISABLE), policy=FAST)
            self.assertEqual(len(requests), 4)
            for payload, context in requests:
                if context["state"]["tick"] != 1:
                    continue
                self.assertEqual(len(payload["messages"]), 2)
                self.assertNotIn("PRIVATE-", json.dumps(payload))
                self.assertNotIn("PUBLIC-", json.dumps(payload))
                self.assertNotIn("bad-response", json.dumps(payload))
                self.assertEqual(context["match"]["wins"], {"A": 0, "B": 0})
                self.assertIsNone(context["match"]["last_game"])

    def test_invalid_config_and_default_limits(self):
        for bo in (0, -1, 2, 4, True, 3.0, "3"):
            with self.subTest(bo=bo), self.assertRaises(ValueError):
                MatchConfig(bo, move_policy=MovePolicy.REVISABLE)
        for limit in (0, -1, 1, True, 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                MatchConfig(3, limit, move_policy=MovePolicy.REVISABLE)
        self.assertEqual(MatchConfig(3, move_policy=MovePolicy.REVISABLE).max_games, 30)
        self.assertEqual(MatchConfig(1, move_policy=MovePolicy.REVISABLE).max_games, 10)


if __name__ == "__main__":
    unittest.main()
