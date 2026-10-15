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

from model_rps.cli import main
from model_rps.diagnostics import LogError
from model_rps.game import MovePolicy, GameState, InvalidResponse
from model_rps.match import MatchConfig, MatchResult, MatchStatus
from model_rps.players import ChatCompletionPlayer, DEEPSEEK, OPENAI, PlayerError, REVIEW_PROMPT
from model_rps.postmatch import MatchArchive, parse_review, run_reviews
from model_rps.runner import RequestPolicy

VALID = json.dumps({"assessment": "整体评价", "key_moments": "第一局结算刻改拳", "improvements": "改善出拳分布"})
REPORT = {"result": {"status": "won", "wins": {"A": 2, "B": 0}}, "games": []}
FAST = RequestPolicy(timeout=0.02, retry_delay=0)


class Reviewer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def review(self, context, feedback=None):
        self.requests.append((deepcopy(context), feedback))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PostmatchTests(unittest.TestCase):
    def test_strict_review_format(self):
        self.assertEqual(parse_review(VALID).assessment, "整体评价")
        invalid = ["", "```json\n" + VALID + "\n```", "[]",
                   '{"actions":[{"type":"WAIT"}]}',
                   '{"assessment":"a","key_moments":"b","improvements":"c","actions":[]}',
                   '{"assessment":"a","assessment":"b","key_moments":"b","improvements":"c"}',
                   '{"assessment":"a","key_moments":NaN,"improvements":"c"}']
        for value in (None, "", " ", 1, []):
            invalid.append(json.dumps({"assessment": value, "key_moments": "b", "improvements": "c"}))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(InvalidResponse):
                parse_review(raw)

    def test_technical_match_cannot_be_published_as_normal_postmatch(self):
        with self.assertRaises(ValueError):
            MatchArchive().report(MatchConfig(3, move_policy=MovePolicy.REVISABLE), MatchResult(MatchStatus.TECHNICAL_ERROR, (1, 0), 0, 2))

    def test_cli_bo_discloses_only_after_match_and_both_reviews_receive_same_redacted_report(self):
        game_calls = []
        review_calls = []
        review_attempts = {"A": 0, "B": 0}

        def handler(request):
            payload = json.loads(request.content)
            player = "A" if payload["model"] == OPENAI.model else "B"
            context = json.loads(payload["messages"][-1]["content"])
            if payload["messages"][0]["content"] == REVIEW_PROMPT:
                self.assertEqual(len(game_calls), 8)
                review_calls.append((player, context))
                review_attempts[player] += 1
                self.assertEqual(payload.get("max_tokens", payload.get("max_completion_tokens")), 20000)
                self.assertEqual(context["player"], payload["model"])
                report = context["match"]
                self.assertEqual(report["result"]["wins"], {OPENAI.model: 2, DEEPSEEK.model: 0})
                self.assertEqual(len(report["games"]), 2)
                decisions = [d for g in report["games"] for d in g["decisions"]]
                self.assertEqual(len(decisions), 8)
                text = json.dumps(report)
                self.assertIn("PRIVATE-A", text)
                self.assertIn("PRIVATE-B", text)
                self.assertNotIn("test-key-a", text)
                self.assertNotIn("test-key-b", text)
                content = '{"actions":[{"type":"MOVE","move":"Rock"}]}' if player == "A" and review_attempts[player] == 1 else VALID
            else:
                game_calls.append((player, context))
                opponent = "B" if player == "A" else "A"
                self.assertNotIn(f"PRIVATE-{opponent}", json.dumps(payload))
                move = "Paper" if player == "A" else "Rock"
                content = json.dumps({"decision_summary": f"PRIVATE-{player} test-key-{player.lower()}",
                                      "actions": [{"type": "MOVE", "move": move}]})
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.py"
            path.write_text('DEEPSEEK_API_KEY="test-key-a"\nOPENAI_API_KEY="test-key-b"\n', encoding="utf-8")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            output = io.StringIO()
            with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(output):
                status = main(["--no-config", "--move-policy", "revisable", "--bo", "3", "--log-dir", str(Path(directory) / "logs"), "--secrets-file", str(path)])
            self.assertEqual(status, 0)
            self.assertEqual(review_attempts, {"A": 2, "B": 1})
            self.assertTrue(all(c["match"] == review_calls[0][1]["match"] for _, c in review_calls))
            self.assertIsNotNone(next(c for p, c in review_calls if p == "A" and c["correction"] is not None))
            stdout = output.getvalue()
            self.assertLess(stdout.index("赢得 BO3"), stdout.index("PRIVATE-A"))
            self.assertIn("复盘·整体评价", stdout)
            self.assertNotIn("test-key-a", stdout)
            root, = (Path(directory) / "logs").iterdir()
            chat = (root / "chat.log").read_text(encoding="utf-8")
            self.assertLess(chat.index("赢得 BO3"), chat.index("赛后公开"))
            for player in (OPENAI.model, DEEPSEEK.model):
                thoughts = [json.loads(s) for s in (root / f"{player}/debug.log").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len([r for r in thoughts if r["kind"] == "decision"]), 4)
                reviews = [r for r in thoughts if r["kind"] == "review"]
                self.assertEqual(len(reviews), 1)
                self.assertEqual(reviews[0]["phase"], "review")
                self.assertEqual(reviews[0]["game"], 0)

    def test_cli_failed_review_keeps_final_score_and_successful_peer(self):
        attempts = {"A": 0, "B": 0}

        def handler(request):
            payload = json.loads(request.content)
            player = "A" if payload["model"] == OPENAI.model else "B"
            if payload["messages"][0]["content"] == REVIEW_PROMPT:
                attempts[player] += 1
                if player == "A":
                    return httpx.Response(401, text="sensitive-server-body")
                content = VALID
            else:
                content = json.dumps({"decision_summary": "简短决策", "actions": [
                    {"type": "MOVE", "move": "Paper" if player == "A" else "Rock"}]})
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
            self.assertIn("deepseek-flash 复盘·整体评价", output.getvalue())
            self.assertNotIn("sensitive-server-body", output.getvalue())
            root, = (Path(directory) / "logs").iterdir()
            rows = [json.loads(s) for s in (root / "gpt-6-luna/debug.log").read_text(encoding="utf-8").splitlines()]
            result = next(r for r in rows if r["kind"] == "match_end")
            self.assertEqual(result["wins"], {OPENAI.model: 1, DEEPSEEK.model: 0})
            self.assertEqual(result["winner"], OPENAI.model)
            self.assertFalse(next(r for r in rows if r["kind"] == "review_end")["complete"])


class AsyncPostmatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_format_and_network_retries_share_budget_and_do_not_repeat_peer(self):
        a = Reviewer(["invalid", TimeoutError(), VALID])
        b = Reviewer([VALID])
        events = []
        before = deepcopy(REPORT)
        outcomes = await run_reviews((a, b), REPORT, policy=FAST, on_request=events.append)
        self.assertTrue(all(r.review for r in outcomes))
        self.assertEqual((len(a.requests), len(b.requests)), (3, 1))
        self.assertEqual(REPORT, before)
        self.assertTrue(all(c["match"] == before for c, _ in a.requests))
        self.assertTrue(all(r["phase"] == "review" and r["tick"] == 0 for r in events))
        self.assertEqual([r["attempt"] for r in events if r["kind"] == "request_start" and r["player"] == "A"], [1, 2, 3])

    async def test_timeout_is_bounded_and_does_not_discard_successful_review(self):
        class Slow:
            cancelled = 0

            async def review(self, context, feedback=None):
                try:
                    await asyncio.sleep(10)
                finally:
                    self.cancelled += 1

        a, b = Slow(), Reviewer([VALID])
        outcomes = await run_reviews((a, b), REPORT, policy=FAST)
        self.assertEqual(a.cancelled, 3)
        self.assertIsNone(outcomes[0].review)
        self.assertIsNotNone(outcomes[1].review)
        self.assertEqual(len(b.requests), 1)

    async def test_cancelled_postmatch_cleans_up_both_requests(self):
        started = [asyncio.Event(), asyncio.Event()]
        cancelled = []

        class Pending:
            def __init__(self, index):
                self.index = index

            async def review(self, context, feedback=None):
                started[self.index].set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.append(self.index)

        task = asyncio.create_task(run_reviews((Pending(0), Pending(1)), REPORT,
                                              policy=RequestPolicy(timeout=20, retry_delay=0)))
        await asyncio.gather(*(e.wait() for e in started))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(cancelled), [0, 1])

    async def test_unexpected_error_is_sanitized_and_log_error_propagates(self):
        result = await run_reviews((Reviewer([RuntimeError("private-message")]), Reviewer([VALID])), REPORT, policy=FAST)
        self.assertNotIn("private-message", result[0].error)
        self.assertIsNotNone(result[1].review)
        with self.assertRaises(LogError):
            await run_reviews((Reviewer([LogError("日志故障")]), Reviewer([VALID])), REPORT, policy=FAST)

    async def test_review_uses_independent_prompt_and_does_not_change_playing_context(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            content = VALID if body["messages"][0]["content"] == REVIEW_PROMPT else json.dumps({
                "decision_summary": "游戏决策", "actions": [{"type": "WAIT"}]})
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, "test-key")
            await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("B"))
            await player.review({"player": "B", "match": REPORT})
            await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("B"))
        self.assertEqual(len(bodies[1]["messages"]), 2)
        self.assertEqual(len(bodies[2]["messages"]), 4)
        self.assertNotIn("改善出拳分布", json.dumps(bodies[2], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
