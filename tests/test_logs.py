import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import turn_payload
from model_rps.cli import Console, main
from model_rps.diagnostics import LogError
from model_rps.game import MovePolicy
from model_rps.logs import RunLogs
from model_rps.players import ChatCompletionPlayer, DEEPSEEK, OPENAI, ScriptedPlayer
from model_rps.runner import RequestPolicy, run_game

FAST = RequestPolicy(timeout=1, retry_delay=0)


def response(move="Rock", summary="PRIVATE-DECISION", text="中文公开消息"):
    return json.dumps(turn_payload([{"type": "SAY", "text": text}, {"type": "MOVE", "move": move}], summary), ensure_ascii=False)


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class LogTests(unittest.TestCase):
    def test_readable_thought_preserves_paragraphs_redacts_and_keeps_structured_debug(self):
        with tempfile.TemporaryDirectory() as directory, RunLogs(Path(directory), secrets=("secret-key",),
                                                                 names={"A": "模型甲", "B": "模型乙"}) as logs:
            base = {"player": "A", "tick": 3, "attempt": 2, "phase": "game"}
            logs.request(2, {**base, "kind": "reasoning", "status": "returned", "requested_thinking": "high",
                             "reasoning_tokens": 0, "finish_reason": "stop",
                             "content": "第一段 secret-key\n\n第二段\r\n伪标题\x1b[2J"})
            logs.request(2, {**base, "kind": "decision", "status": "validated", "analysis_source": "reasoning",
                             "summary": None, "actions": [{"type": "MOVE", "move": "Paper"}]})
            text = (logs.path / "red/thought.log").read_text(encoding="utf-8")
            self.assertIn("第 2 局 · Tick 3 · 第 2 次请求 · 接口思考", text)
            self.assertIn("模型：模型甲", text)
            self.assertIn("    第一段 [REDACTED]\n    \n    第二段\n    伪标题\\u001b[2J", text)
            self.assertNotIn("secret-key", text)
            self.assertNotIn("\x1b", text)
            self.assertIn("思考 token：0", text)
            self.assertIn("MOVE · 出拳：布（Paper）", text)
            self.assertIn("动作是否执行", text)
            self.assertIn("正文策略分析：未提交", text)
            debug = records(logs.path / "red/debug.log")
            self.assertEqual([r["kind"] for r in debug], ["reasoning", "decision"])
            self.assertEqual(debug[0]["content"], "第一段 [REDACTED]\n\n第二段\r\n伪标题\x1b[2J")
            self.assertIsNone(debug[1]["summary"])
            self.assertEqual((logs.path / "blue/thought.log").read_text(encoding="utf-8"), "")

    def test_readable_missing_reasoning_and_failed_review_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory, RunLogs(Path(directory)) as logs:
            base = {"player": "B", "tick": 0, "attempt": 3, "phase": "review"}
            logs.request(0, {**base, "kind": "reasoning", "status": "not_returned", "requested_thinking": "high",
                             "reasoning_tokens": None, "finish_reason": "length", "content": None})
            logs.request(0, {**base, "kind": "request_end", "status": "rejected", "error": "模型输出未完整结束",
                             "correction": "请重新返回完整复盘 JSON。", "will_retry": False})
            text = (logs.path / "blue/thought.log").read_text(encoding="utf-8")
            self.assertIn("赛后复盘 · 第 3 次请求", text)
            self.assertIn("接口未返回思考文本", text)
            self.assertIn("思考 token：未提供", text)
            self.assertIn("结束原因：length", text)
            self.assertIn("本次未取得有效复盘", text)
            self.assertIn("后续：不再重试", text)
            self.assertIn("模型输出未完整结束", text)

    def test_bo3_cli_writes_five_files_and_match_results(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()):
                status = main(["--no-config", "--move-policy", "revisable", "--mode", "demo", "--bo", "3", "--log-dir", directory])
            self.assertEqual(status, 0)
            root, = Path(directory).iterdir()
            self.assertEqual({str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*.log")},
                             {"chat.log", "red/thought.log", "red/debug.log",
                              "blue/thought.log", "blue/debug.log"})
            chat = (root / "chat.log").read_text(encoding="utf-8")
            self.assertIn("第 2 局开始", chat)
            self.assertNotIn("第 3 局开始", chat)
            self.assertIn("比分 Red · 模拟玩家1 0 : 2 Blue · 模拟玩家2", chat)
            self.assertIn("赢得 BO3", chat)
            self.assertNotIn("预设动作", chat.split("赛后公开")[0])
            self.assertIn("预设动作", chat.split("赛后公开")[1])
            self.assertIn("赛后复盘结束", chat)
            for player in ("red", "blue"):
                debug = records(root / f"{player}/debug.log")
                result = next(r for r in debug if r["kind"] == "match_end")
                self.assertEqual(result["wins"], {"Red · 模拟玩家1": 0, "Blue · 模拟玩家2": 2})
                self.assertEqual(result["games_played"], 2)
                thought = [r for r in debug if r["kind"] in ("decision", "review")]
                self.assertEqual({r["game"] for r in thought if r["kind"] == "decision"}, {1, 2})
                self.assertEqual(len([r for r in thought if r["kind"] == "review"]), 1)
                self.assertTrue(all(r["player"] == ("Red · 模拟玩家1" if player == "red" else "Blue · 模拟玩家2") for r in thought))
                readable = (root / f"{player}/thought.log").read_text(encoding="utf-8")
                self.assertIn("第 2 局 · Tick", readable)
                self.assertIn("赛后复盘 · 第 1 次请求 · 复盘结果", readable)
                self.assertIn("复盘：", readable)

    def test_unique_directories_flush_and_redact_both_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), secrets=("secret-a", "secret-b")) as first:
                first.chat("中文 secret-a secret-b\n另一行")
                first.request(1, {"kind": "http_response", "player": "A", "tick": 1, "attempt": 1,
                                  "status": 401, "body": "secret-a secret-b"})
                # 尚未关闭文件时已经能够从磁盘读取。
                self.assertIn("[REDACTED]", (first.path / "chat.log").read_text(encoding="utf-8"))
                self.assertEqual(records(first.path / "red/debug.log")[0]["body"],
                                 "[REDACTED] [REDACTED]")
            with RunLogs(Path(directory)) as second:
                self.assertNotEqual(first.path, second.path)
            all_logs = "".join(p.read_text(encoding="utf-8") for p in Path(directory).rglob("*.log"))
            self.assertNotIn("secret-a", all_logs)
            self.assertNotIn("secret-b", all_logs)

    def test_bad_log_directory_exits_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "occupied"
            root.write_text("keep", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                status = main(["--no-config", "--move-policy", "revisable", "--mode", "demo", "--log-dir", str(root)])
            self.assertEqual(status, 1)
            self.assertEqual(root.read_text(encoding="utf-8"), "keep")

    def test_redaction_keeps_json_valid_with_escaped_secrets(self):
        secret = 'key-"\\-中文'
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), secrets=(secret,)) as logs:
                logs.request(1, {"kind": "http_response", "player": "A", "tick": 1, "attempt": 1,
                                 "body": json.dumps({"echo": secret}), "plain": secret})
                record, = records(logs.path / "red/debug.log")
                self.assertEqual(record["plain"], "[REDACTED]")
                self.assertEqual(json.loads(record["body"])["echo"], "[REDACTED]")

    def test_undecided_and_invalid_cli_options(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["--no-config", "--move-policy", "revisable", "--mode", "demo", "--scenario", "draw", "--bo", "3", "--max-games", "2", "--log-dir", directory])
            self.assertEqual(status, 3)
            self.assertIn("比赛未决", output.getvalue())
            self.assertEqual(output.getvalue().count("本局提交截止还剩 10 Tick"), 2)
            for options in (["--bo", "2"], ["--bo", "3", "--max-games", "1"]):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--no-config", "--move-policy", "revisable", *options, "--log-dir", directory]), 2)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)


class AsyncLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_retry_usage_private_summary_and_secret_echo_redaction(self):
        counts = {DEEPSEEK.model: 0, OPENAI.model: 0}

        async def handler(request):
            payload = json.loads(request.content)
            model = payload["model"]
            counts[model] += 1
            await asyncio.sleep(0)
            if model == DEEPSEEK.model and counts[model] == 1:
                return httpx.Response(429, text="secret-a secret-b upstream error")
            if model == DEEPSEEK.model and counts[model] == 2:
                content = '{"actions":[{"type":"MOVE","move":"Rock"},{"type":"ENDTURN"}]}'
            else:
                content = response("Paper" if model == DEEPSEEK.model else "Rock")
            return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens": 100, "completion_tokens": 25}})

        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), secrets=("secret-a", "secret-b")) as logs:
                console = Console({"A": "A", "B": "B"}, logs)
                console.game = 1
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with contextlib.redirect_stdout(io.StringIO()):
                        state = await run_game(ChatCompletionPlayer(client, DEEPSEEK, "secret-a"),
                                               ChatCompletionPlayer(client, OPENAI, "secret-b"), policy=FAST,
                                               on_request=lambda r: logs.request(1, r), on_event=console.event, move_policy=MovePolicy.REVISABLE)
                self.assertEqual(state.result.winner, "A")
                a = records(logs.path / "red/debug.log")
                b = records(logs.path / "blue/debug.log")
                self.assertEqual([(r["tick"], r["attempt"]) for r in a if r["kind"] == "request_start"],
                                 [(1, 1), (1, 2), (1, 3), (2, 1)])
                self.assertEqual([(r["tick"], r["attempt"]) for r in b if r["kind"] == "request_start"], [(1, 1), (2, 1)])
                self.assertEqual([r["status"] for r in a if r["kind"] == "http_response"], [429, 200, 200, 200])
                self.assertTrue(all(r["game"] == 1 and r["tick"] in (1, 2) and r["player"] == "A" for r in a if r["kind"] != "tick"))
                self.assertTrue(all(r["elapsed_seconds"] >= 0 for r in a if r["kind"] == "request_end"))
                self.assertEqual([r["usage"] for r in b if r["kind"] == "usage"],
                                 [{"prompt_tokens": 100, "completion_tokens": 25}] * 2)
                self.assertIn("ANALYZE", next(r for r in a if r["kind"] == "request_start" and r["attempt"] == 3)["correction"])
                thoughts = (logs.path / "red/thought.log").read_text(encoding="utf-8")
                self.assertEqual(thoughts.count("· 请求未完成"), 2)
                self.assertEqual(thoughts.count("· 策略分析与行动"), 2)
                self.assertIn("PRIVATE-DECISION", thoughts)
                self.assertEqual([r for r in a if r["kind"] == "decision"][-1]["summary"], "PRIVATE-DECISION")
                chat = (logs.path / "chat.log").read_text(encoding="utf-8")
                self.assertNotIn("PRIVATE-DECISION", chat)
                self.assertNotIn("Paper", chat.split("→")[0])
                self.assertIn("中文公开消息", chat)
                all_logs = "".join(p.read_text(encoding="utf-8") for p in logs.path.rglob("*.log"))
                self.assertNotIn("secret-a", all_logs)
                self.assertNotIn("secret-b", all_logs)
                self.assertNotIn("Bearer", all_logs)

    async def test_cancelled_requests_and_interrupt_are_saved(self):
        started = [asyncio.Event(), asyncio.Event()]

        class Pending:
            def __init__(self, i):
                self.i = i

            async def respond(self, view, feedback=None):
                started[self.i].set()
                await asyncio.sleep(20)

        with tempfile.TemporaryDirectory() as directory:
            logs = RunLogs(Path(directory))

            async def play():
                with logs:
                    await run_game(Pending(0), Pending(1), policy=FAST,
                                   on_request=lambda r: logs.request(1, r), move_policy=MovePolicy.REVISABLE)

            task = asyncio.create_task(play())
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started)), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for player in ("red", "blue"):
                debug = records(logs.path / f"{player}/debug.log")
                self.assertEqual(next(r for r in debug if r["kind"] == "request_end")["status"], "cancelled")
                self.assertEqual(debug[-1]["status"], "interrupted")
            self.assertIn("运行中断", (logs.path / "chat.log").read_text(encoding="utf-8"))

    async def test_log_write_failure_stops_requests_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory)) as logs:
                a, b = ScriptedPlayer([response()]), ScriptedPlayer([response()])
                with patch.object(logs._files["A", "debug"], "write", side_effect=OSError("private failure")):
                    with self.assertRaises(LogError) as caught:
                        await run_game(a, b, policy=FAST, on_request=lambda r: logs.request(1, r), move_policy=MovePolicy.REVISABLE)
                self.assertNotIn("private failure", str(caught.exception))
                self.assertEqual(len(a.requests), 0)
                self.assertLessEqual(len(b.requests), 1)


if __name__ == "__main__":
    unittest.main()
