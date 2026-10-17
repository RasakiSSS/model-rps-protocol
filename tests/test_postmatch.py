import asyncio
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.cli import main
from model_rps.diagnostics import LogError
from model_rps.game import MovePolicy, GameState, InvalidResponse
from model_rps.match import MatchConfig, MatchResult, MatchStatus
from model_rps.players import ChatCompletionPlayer, DEEPSEEK, OPENAI, PlayerError
from model_rps.commands import parse_postmatch
from model_rps.postmatch import MatchArchive, run_postmatch
from model_rps.runner import RequestPolicy

VALID = json.dumps(postmatch_payload("\n\n".join(["整体评价", "第一局结算刻改拳", "改善出拳分布"])))
REPORT = {"result": {"status": "won", "wins": {"A": 2, "B": 0}}, "games": []}
FAST = RequestPolicy(timeout=0.02, retry_delay=0)


class ChatParticipant:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def postmatch(self, context, feedback=None):
        self.requests.append((deepcopy(context), feedback))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PostmatchTests(unittest.TestCase):
    def test_strict_postmatch_format(self):
        self.assertEqual(parse_postmatch(VALID).messages, ("整体评价\n\n第一局结算刻改拳\n\n改善出拳分布",))
        self.assertTrue(parse_postmatch(VALID).exit)
        self.assertFalse(parse_postmatch(json.dumps(postmatch_payload('继续聊', exit=False))).exit)
        self.assertTrue(parse_postmatch(json.dumps(postmatch_payload())).exit)
        silent = parse_postmatch('{"actions":[]}')
        self.assertTrue(silent.silent)
        self.assertFalse(silent.exit)
        invalid = ["", "```json\n" + VALID + "\n```", "[]",
                   '{"actions":[{"type":"WAIT"}]}',
                   '{"assessment":"a","key_moments":"b","improvements":"c","actions":[]}',
                   '{"actions":[{"type":"SAY","text":"a","text":"b"}]}',
                   '{"actions":[{"type":"SAY","text":NaN}]}',
                   '{"actions":[{"type":"EXIT"},{"type":"SAY","text":"late"}]}',
                   '{"actions":[{"type":"EXIT"},{"type":"EXIT"}]}',
                   '{"actions":[{"type":"EXIT","text":"extra"}]}',
                   '{"actions":[{"type":"REVIEW","text":"obsolete"}]}']
        for value in (None, "", " ", 1, []):
            invalid.append(json.dumps({'actions': [{'type': 'SAY', 'text': value}]}))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(InvalidResponse):
                parse_postmatch(raw)

    def test_technical_match_cannot_be_published_as_normal_postmatch(self):
        with self.assertRaises(ValueError):
            MatchArchive().report(MatchConfig(3, move_policy=MovePolicy.REVISABLE), MatchResult(MatchStatus.TECHNICAL_ERROR, (1, 0), 0, 2))

    def test_cli_bo_discloses_only_after_match_and_both_postmatchs_receive_same_redacted_report(self):
        game_calls = []
        postmatch_calls = []
        postmatch_attempts = {"A": 0, "B": 0}

        def handler(request):
            payload = json.loads(request.content)
            player = "A" if payload["model"] == OPENAI.model else "B"
            context = json.loads(payload["messages"][-1]["content"])
            if json.loads(payload["messages"][-1]["content"]).get("phase") == "postmatch":
                self.assertEqual(len(game_calls), 8)
                postmatch_calls.append((player, context))
                postmatch_attempts[player] += 1
                self.assertEqual(payload.get("max_tokens", payload.get("max_completion_tokens")), 20000)
                self.assertEqual(context["player"], ("Red · " if player == "A" else "Blue · ") + payload["model"])
                report = context["match"]
                self.assertEqual(report["result"]["wins"], {"Red · " + OPENAI.model: 2, "Blue · " + DEEPSEEK.model: 0})
                self.assertEqual(len(report["games"]), 2)
                decisions = [d for g in report["games"] for d in g["decisions"]]
                self.assertEqual(len(decisions), 8)
                text = json.dumps(report)
                self.assertIn("PRIVATE-A", text)
                self.assertIn("PRIVATE-B", text)
                self.assertNotIn("test-key-a", text)
                self.assertNotIn("test-key-b", text)
                content = '{"actions":[{"type":"MOVE","move":"Rock"}]}' if player == "A" and postmatch_attempts[player] == 1 else VALID
            else:
                game_calls.append((player, context))
                opponent = "B" if player == "A" else "A"
                self.assertNotIn(f"PRIVATE-{opponent}", json.dumps(payload))
                move = "Paper" if player == "A" else "Rock"
                content = json.dumps(turn_payload([{"type": "MOVE", "move": move}], f"PRIVATE-{player} test-key-{player.lower()}"))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.py"
            path.write_text('DEEPSEEK_API_KEY="test-key-a"\nOPENAI_API_KEY="test-key-b"\n', encoding="utf-8")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            output = io.StringIO()
            with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(output):
                status = main(["--no-config", "--move-policy", "revisable", "--bo", "3", "--log-dir", str(Path(directory) / "logs"), "--secrets-file", str(path)])
            self.assertEqual(status, 0)
            self.assertEqual(postmatch_attempts, {"A": 2, "B": 1})
            self.assertTrue(all(c["match"] == postmatch_calls[0][1]["match"] for _, c in postmatch_calls))
            self.assertIsNotNone(next(c for p, c in postmatch_calls if p == "A" and c["correction"] is not None))
            stdout = output.getvalue()
            self.assertLess(stdout.index("赢得 BO3"), stdout.index("PRIVATE-A"))
            self.assertIn("双方已退出，赛后聊天结束", stdout)
            self.assertNotIn("test-key-a", stdout)
            root, = (Path(directory) / "logs").iterdir()
            chat = (root / "chat.log").read_text(encoding="utf-8")
            self.assertLess(chat.index("赢得 BO3"), chat.index("赛后公开"))
            for player in ("red", "blue"):
                thoughts = [json.loads(s) for s in (root / f"{player}/debug.log").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len([r for r in thoughts if r["kind"] == "decision"]), 4)
                postmatchs = [r for r in thoughts if r["kind"] == "postmatch_response"]
                self.assertEqual(len(postmatchs), 1)
                self.assertEqual(postmatchs[0]["phase"], "postmatch")
                self.assertEqual(postmatchs[0]["game"], 0)

    def test_cli_failed_postmatch_keeps_final_score_and_successful_peer(self):
        attempts = {"A": 0, "B": 0}

        def handler(request):
            payload = json.loads(request.content)
            player = "A" if payload["model"] == OPENAI.model else "B"
            if json.loads(payload["messages"][-1]["content"]).get("phase") == "postmatch":
                attempts[player] += 1
                if player == "A":
                    return httpx.Response(401, text="sensitive-server-body")
                content = VALID
            else:
                content = json.dumps(turn_payload([{"type": "MOVE", "move": "Paper" if player == "A" else "Rock"}], "简短决策"))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "keys.py"
            key_file.write_text('DEEPSEEK_API_KEY="test-a"\nOPENAI_API_KEY="test-b"\n', encoding="utf-8")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            output = io.StringIO()
            with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(output):
                status = main(["--no-config", "--move-policy", "revisable", "--max-games", "1", "--secrets-file", str(key_file), "--log-dir", str(Path(directory) / "logs")])
            self.assertEqual(status, 4)
            self.assertEqual(attempts, {"A": 1, "B": 1})
            self.assertIn("赢得 BO1", output.getvalue())
            self.assertIn("赛后聊天中止，比赛结果不受影响", output.getvalue())
            self.assertNotIn("双方已退出", output.getvalue())
            self.assertNotIn("sensitive-server-body", output.getvalue())
            root, = (Path(directory) / "logs").iterdir()
            rows = [json.loads(s) for s in (root / "red/debug.log").read_text(encoding="utf-8").splitlines()]
            result = next(r for r in rows if r["kind"] == "match_end")
            self.assertEqual(result["wins"], {"Red · " + OPENAI.model: 1, "Blue · " + DEEPSEEK.model: 0})
            self.assertEqual(result["winner"], "Red · " + OPENAI.model)
            ended = next(r for r in rows if r["kind"] == "postmatch_end")
            self.assertFalse(ended["complete"])
            self.assertEqual(ended["failed_player"], "Red · " + OPENAI.model)
            self.assertFalse(ended["exited"]["Red · " + OPENAI.model])


class AsyncPostmatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_silent_wait_without_polling_until_operator_resumes(self):
        silent = json.dumps(postmatch_payload(exit=False))
        players = tuple(ChatParticipant([silent, VALID]) for _ in range(2))
        events, records, idle_states = [], [], []
        idle_started, resume = asyncio.Event(), asyncio.Event()

        async def idle(exited):
            idle_states.append(exited)
            idle_started.set()
            await resume.wait()

        task = asyncio.create_task(run_postmatch(players, REPORT, policy=FAST, on_idle=idle,
            on_event=events.append, on_request=records.append))
        try:
            await asyncio.wait_for(idle_started.wait(), 1)
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertEqual([len(p.requests) for p in players], [1, 1])
            self.assertEqual(events, [])
            self.assertEqual(idle_states, [{'A': False, 'B': False}])
            resume.set()
            outcome = await asyncio.wait_for(task, 1)
            self.assertTrue(outcome.complete)
            for player in players:
                self.assertEqual(player.requests[1][0]['request_reasons'], ['operator_resume'])
                self.assertNotIn('match', player.requests[1][0])
            self.assertEqual(len([r for r in records if r['kind'] == 'postmatch_applied' and r['silent']]), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_exited_peer_not_polled_and_silence_after_exit_idles(self):
        silent = json.dumps(postmatch_payload(exit=False))
        # 同时首次沉默的玩家还能收到对手新产生的 EXIT；读完 EXIT 后不重复唤醒。
        a, b = ChatParticipant([silent, silent, VALID]), ChatParticipant([VALID])
        states = []

        async def idle(exited):
            states.append(exited)
            self.assertEqual((len(a.requests), len(b.requests)), (2, 1))
            await asyncio.sleep(.03)
            self.assertEqual((len(a.requests), len(b.requests)), (2, 1))

        outcome = await asyncio.wait_for(run_postmatch((a, b), REPORT, policy=FAST, on_idle=idle), 1)
        self.assertTrue(outcome.complete)
        self.assertEqual(states, [{'A': False, 'B': True}])
        self.assertEqual(a.requests[-1][0]['request_reasons'], ['operator_resume'])
        self.assertEqual(len(b.requests), 1)

    async def test_human_new_say_wakes_silent_model_without_operator_command(self):
        from model_rps.human import HumanPlayer
        from test_human import until

        human = HumanPlayer()
        model = ChatParticipant([json.dumps(postmatch_payload(exit=False)), VALID])
        events = []
        task = asyncio.create_task(run_postmatch((human, model), REPORT, policy=FAST, on_event=events.append))
        try:
            await until(lambda: human._available.is_set() and len(model.requests) == 1)
            await human.submit('/say 再聊一句')
            await until(lambda: any(e['kind'] == 'exit' and e['player'] == 'B' for e in events))
            self.assertEqual(model.requests[1][0]['request_reasons'], ['opponent_event'])
            self.assertEqual(model.requests[1][0]['events'][0]['text'], '再聊一句')
            await human.submit('/exit')
            self.assertTrue((await asyncio.wait_for(task, 1)).complete)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_idle_without_resume_handler_is_cancellable_and_does_not_exit(self):
        players = tuple(ChatParticipant(['{"actions":[]}']) for _ in range(2))
        events, applied = [], asyncio.Event()
        count = 0

        def record(row):
            nonlocal count
            if row['kind'] == 'postmatch_applied':
                count += 1
                if count == 2:
                    applied.set()

        task = asyncio.create_task(run_postmatch(players, REPORT, policy=FAST, on_event=events.append, on_request=record))
        try:
            await asyncio.wait_for(applied.wait(), 1)
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(events, [])
            self.assertEqual([len(p.requests) for p in players], [1, 1])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_both_speak_repeatedly_see_peer_and_exit_without_repeating_report(self):
        def messages(name):
            return [json.dumps(postmatch_payload(f'{name}-{i}', exit=i == 4)) for i in range(5)]

        a, b = ChatParticipant(messages('red')), ChatParticipant(messages('blue'))
        events, records = [], []
        outcome = await run_postmatch((a, b), REPORT, policy=FAST, on_event=events.append,
                                      on_request=records.append)
        self.assertTrue(outcome.complete)
        self.assertEqual((len(a.requests), len(b.requests)), (5, 5))
        self.assertEqual(a.requests[0][0]['match'], b.requests[0][0]['match'])
        for player, peer in ((a, 'B'), (b, 'A')):
            for index, (context, _) in enumerate(player.requests):
                self.assertEqual('match' in context, index == 0)
                if index:
                    self.assertTrue(any(e['player'] == peer and e['kind'] == 'say' for e in context['events']))
                self.assertEqual([e['sequence'] for e in context['events']],
                                 list(range(context['event_cursor'] - len(context['events']) + 1,
                                            context['event_cursor'] + 1)))
        self.assertEqual([e['sequence'] for e in events], list(range(1, 13)))
        for identity in ('A', 'B'):
            own = [e for e in events if e['player'] == identity]
            self.assertEqual([e['kind'] for e in own], ['say'] * 5 + ['exit'])
        self.assertEqual(len([r for r in records if r['kind'] == 'postmatch_applied']), 10)

    async def test_one_player_exits_other_can_keep_talking_until_own_exit(self):
        a = ChatParticipant([json.dumps(postmatch_payload('还有一句', exit=False)),
                      json.dumps(postmatch_payload('再补一句', exit=False)),
                      json.dumps(postmatch_payload())])
        b = ChatParticipant([json.dumps(postmatch_payload())])
        events = []
        result = await run_postmatch((a, b), REPORT, policy=FAST, on_event=events.append)
        self.assertTrue(result.complete)
        self.assertEqual((len(a.requests), len(b.requests)), (3, 1))
        self.assertTrue(a.requests[1][0]['exited']['B'])
        self.assertEqual([e['kind'] for e in a.requests[1][0]['events']], ['say', 'exit'])
        self.assertEqual(events[-1], {'sequence': 4, 'kind': 'exit', 'player': 'A'})

    async def test_busy_player_receives_merged_messages_without_overlapping_requests(self):
        release = asyncio.Event()
        a = ChatParticipant([json.dumps({'actions': [{'type': 'SAY', 'text': text} for text in ('one', 'two')]}), VALID])

        class SlowPeer(ChatParticipant):
            async def postmatch(self, context, feedback=None):
                if not self.requests:
                    await release.wait()
                    self.assert_no_extra_request()
                return await super().postmatch(context, feedback)

        b = SlowPeer([json.dumps(postmatch_payload('reply', exit=False)), VALID])
        b.assert_no_extra_request = lambda: self.assertEqual(len(a.requests), 1)

        def publish(event):
            if event.get('text') == 'two':
                release.set()

        result = await run_postmatch((a, b), REPORT, policy=FAST, on_event=publish)
        self.assertTrue(result.complete)
        received = b.requests[1][0]['events']
        self.assertEqual([e['text'] for e in received if e['player'] == 'A'], ['one', 'two'])

    async def test_invalid_output_is_atomic_and_retry_uses_same_snapshot(self):
        invalid = json.dumps({'actions': [{'type': 'SAY', 'text': 'DO-NOT-PUBLISH'},
                                          {'type': 'EXIT'}, {'type': 'MOVE', 'move': 'Rock'}]})
        a, b = ChatParticipant([invalid, VALID]), ChatParticipant([VALID])
        events = []
        result = await run_postmatch((a, b), REPORT, policy=FAST, on_event=events.append)
        self.assertTrue(result.complete)
        self.assertEqual(a.requests[0][0], a.requests[1][0])
        self.assertIsNotNone(a.requests[1][1])
        self.assertNotIn('DO-NOT-PUBLISH', repr(events))

    async def test_format_and_network_retries_share_budget_and_do_not_repeat_peer(self):
        a = ChatParticipant(["invalid", TimeoutError(), VALID])
        b = ChatParticipant([VALID])
        events = []
        before = deepcopy(REPORT)
        outcome = await run_postmatch((a, b), REPORT, policy=FAST, on_request=events.append)
        self.assertTrue(outcome.complete)
        self.assertEqual((len(a.requests), len(b.requests)), (3, 1))
        self.assertEqual(REPORT, before)
        self.assertTrue(all(c["match"] == before for c, _ in a.requests))
        self.assertTrue(all(r["phase"] == "postmatch" and r["tick"] == 0 for r in events))
        self.assertEqual([r["attempt"] for r in events if r["kind"] == "request_start" and r["player"] == "A"], [1, 2, 3])

    async def test_timeout_is_bounded_and_does_not_discard_published_messages(self):
        class Slow:
            cancelled = 0

            async def postmatch(self, context, feedback=None):
                try:
                    await asyncio.sleep(10)
                finally:
                    self.cancelled += 1

        a, b = Slow(), ChatParticipant([VALID])
        events = []
        outcome = await run_postmatch((a, b), REPORT, policy=FAST, on_event=events.append)
        self.assertEqual(a.cancelled, 3)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.exited, {'A': False, 'B': True})
        self.assertEqual([e['kind'] for e in events], ['say', 'exit'])
        self.assertEqual(len(b.requests), 1)

    async def test_cancelled_postmatch_cleans_up_both_requests(self):
        started = [asyncio.Event(), asyncio.Event()]
        cancelled = []

        class Pending:
            def __init__(self, index):
                self.index = index
                self.history = ['game']

            def checkpoint_history(self):
                return len(self.history)

            def restore_history(self, checkpoint):
                del self.history[checkpoint:]

            async def postmatch(self, context, feedback=None):
                self.history.append('unpublished')
                started[self.index].set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.append(self.index)

        players = (Pending(0), Pending(1))
        records = []
        task = asyncio.create_task(run_postmatch(players, REPORT, on_request=records.append,
                                              policy=RequestPolicy(timeout=20, retry_delay=0)))
        await asyncio.gather(*(e.wait() for e in started))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(cancelled), [0, 1])
        self.assertTrue(all(p.history == ['game'] for p in players))
        self.assertEqual(len([r for r in records if r['kind'] == 'request_discarded']), 2)

    async def test_unexpected_error_is_sanitized_and_log_error_propagates(self):
        result = await run_postmatch((ChatParticipant([RuntimeError("private-message")]), ChatParticipant([VALID])), REPORT, policy=FAST)
        self.assertNotIn("private-message", result.error)
        self.assertFalse(result.complete)
        with self.assertRaises(LogError):
            await run_postmatch((ChatParticipant([LogError("日志故障")]), ChatParticipant([VALID])), REPORT, policy=FAST)

    async def test_postmatch_preserves_own_context_until_next_match(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            content = VALID if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch" else json.dumps(turn_payload([], "游戏决策"))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, "test-key")
            await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("B"))
            await player.postmatch({"player": "B", "match": REPORT})
            await player.postmatch({"player": "B", "match": REPORT}, "重试反馈")
            player.start_match()
            await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("B"))
        self.assertEqual(len(bodies[1]["messages"]), 4)
        self.assertEqual(bodies[1]["messages"][:2], bodies[0]["messages"])
        self.assertEqual(json.loads(bodies[1]["messages"][2]["content"]), turn_payload([], "游戏决策"))
        self.assertEqual(len(bodies[2]["messages"]), 6)
        self.assertEqual(bodies[2]["messages"][4]["content"], VALID)
        self.assertEqual(len(bodies[3]["messages"]), 2)
        self.assertNotIn("改善出拳分布", json.dumps(bodies[3], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
