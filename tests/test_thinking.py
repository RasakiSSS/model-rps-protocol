import asyncio
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.commands import parse_decision, AnalysisPolicy
from model_rps.cli import make_parser, play
from model_rps.diagnostics import LogError, request_trace
from model_rps.game import EndReason, GameState, InvalidResponse, MovePolicy
from model_rps.logs import RunLogs
from model_rps.players import (ChatCompletionPlayer, DEEPSEEK, OPENAI, DEEPSEEK_THINKING_MODES,
                               OPENAI_THINKING_MODES, PlayerError, ScriptedPlayer,
                               configure_thinking, game_prompt)
from model_rps.runner import RequestPolicy, run_game

FAST = RequestPolicy(timeout=1, retry_delay=0)
POSTMATCH = json.dumps(postmatch_payload("\n\n".join(["评价", "关键局", "改进"])))


def decision(move="Paper", summary="简短说明"):
    return json.dumps(turn_payload([{"type": "MOVE", "move": move}], summary))


def completion(content=None, *, finish="stop", usage=None, **message_fields):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": content, **message_fields}}],
        "usage": usage,
    })


class ThinkingConfigTests(unittest.TestCase):
    def test_all_modes_and_disabling_do_not_mutate_default_specs(self):
        for spec, modes in ((DEEPSEEK, DEEPSEEK_THINKING_MODES), (OPENAI, OPENAI_THINKING_MODES)):
            before = json.dumps(spec.extra_body)
            for mode in modes:
                result = configure_thinking(spec, mode)
                self.assertEqual(result.thinking_mode, mode)
                self.assertEqual(result.model, spec.model)
                if spec == DEEPSEEK:
                    self.assertEqual(result.extra_body["thinking"]["type"], "disabled" if mode == "off" else "enabled")
                    self.assertEqual(result.extra_body.get("reasoning_effort"), None if mode == "off" else mode)
                else:
                    self.assertEqual(result.extra_body["reasoning_effort"], "none" if mode == "off" else mode)
                self.assertEqual(configure_thinking(result, "off").extra_body, spec.extra_body)
            self.assertEqual(json.dumps(spec.extra_body), before)
        for spec, mode in ((DEEPSEEK, "medium"), (OPENAI, "ultra"), (replace(OPENAI, thinking_api=None), "high")):
            with self.assertRaises(ValueError):
                configure_thinking(spec, mode)
        args = make_parser().parse_args([])
        self.assertEqual((args.blue_thinking, args.red_thinking), ("off", "off"))


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parameters_prompts_and_token_cap_for_both_phases_and_rules(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            return completion(POSTMATCH if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch" else decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for spec, modes in ((DEEPSEEK, DEEPSEEK_THINKING_MODES), (OPENAI, OPENAI_THINKING_MODES)):
                for mode in modes:
                    for rule in MovePolicy:
                        player = ChatCompletionPlayer(client, configure_thinking(spec, mode), "test-key")
                        await player.respond(GameState(move_policy=rule).view_for("A"))
                        await player.postmatch({"player": "A", "match": {"move_policy": rule.value}})
                        self.assertEqual(bodies[-2]["messages"][0]["content"], game_prompt(rule))
                        for body in bodies[-2:]:
                            self.assertEqual(body[spec.token_parameter], 20000)
                            self.assertEqual(body["response_format"], {"type": "json_object"})
                            for key, value in configure_thinking(spec, mode).extra_body.items():
                                self.assertEqual(body[key], value)
                            self.assertNotIn("temperature", body)
                            self.assertNotIn("tools", body)

    async def test_cli_independent_switches_and_private_reasoning_across_bo_and_postmatch(self):
        for a_mode, b_mode in (("off", "off"), ("medium", "off"), ("off", "high"), ("medium", "high")):
            bodies = []

            def handler(request):
                body = json.loads(request.content)
                bodies.append(body)
                player = "A" if body["model"] == OPENAI.model else "B"
                mode = a_mode if player == "A" else b_mode
                ctx = json.loads(body["messages"][-1]["content"])
                spec = OPENAI if player == "A" else DEEPSEEK
                self.assertEqual(str(request.url), spec.base_url + "/chat/completions")
                self.assertEqual(request.headers["authorization"],
                                 "Bearer test-key-b" if player == "A" else "Bearer test-key-a")
                self.assertEqual(ctx.get("player", ctx.get("state", {}).get("player")), ("Red · " if player == "A" else "Blue · ") + spec.model)
                self.assertNotIn("NATIVE-", json.dumps(body))
                if json.loads(body["messages"][-1]["content"]).get("phase") == "postmatch":
                    self.assertEqual(len(bodies), 5 if player == "A" else 6)
                    self.assertEqual(ctx["match"]["result"]["wins"], {"Red · " + OPENAI.model: 2, "Blue · " + DEEPSEEK.model: 0})
                    content = POSTMATCH
                else:
                    if ctx["match"]["game"] == 2:
                        self.assertGreater(len(body["messages"]), 2)
                    content = decision("Paper" if player == "A" else "Rock", f"SUMMARY-{player}")
                self.assertEqual(body.get("reasoning_effort"),
                                 (None if mode == "off" else mode) if player == "B" else ("none" if mode == "off" else mode))
                extra = {"reasoning_content": f"NATIVE-{player} test-key-a test-key-b"} if mode != "off" else {}
                return completion(content, usage={"completion_tokens_details": {"reasoning_tokens": 20 if mode != "off" else 0}}, **extra)

            with self.subTest(a=a_mode, b=b_mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path / "keys.py").write_text('DEEPSEEK_API_KEY="test-key-a"\nOPENAI_API_KEY="test-key-b"\n', encoding="utf-8")
                args = make_parser().parse_args(["--bo", "3", "--deepseek-thinking", b_mode, "--openai-thinking", a_mode,
                                                 "--secrets-file", str(path / "keys.py"), "--log-dir", str(path / "logs")])
                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch("model_rps.cli.httpx.AsyncClient", return_value=client), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(await play(args), 0)
                self.assertNotIn("NATIVE-", output.getvalue())
                self.assertIn("SUMMARY-A", output.getvalue())
                self.assertIn("SUMMARY-B", output.getvalue())
                self.assertIn(f"参赛模型：Red · {OPENAI.model} vs Blue · {DEEPSEEK.model}", output.getvalue())
                root, = (path / "logs").iterdir()
                for directory, player, mode in (("red", OPENAI.model, a_mode), ("blue", DEEPSEEK.model, b_mode)):
                    rows = [json.loads(s) for s in (root / f"{directory}/debug.log").read_text(encoding="utf-8").splitlines()]
                    readable = (root / f"{directory}/thought.log").read_text(encoding="utf-8")
                    self.assertEqual("NATIVE-" in readable, mode != "off")
                    self.assertIn("赛后聊天", readable)
                    native = [r for r in rows if r["kind"] == "reasoning"]
                    decisions = [r for r in rows if r["kind"] == "decision"]
                    self.assertEqual(len(decisions), 2)
                    self.assertTrue(all(r["analysis_source"] == "analyze"
                                        for r in decisions))
                    self.assertTrue(all(r["summary"] is not None for r in decisions))
                    self.assertTrue(all(r["has_reasoning"] == (mode != "off") for r in decisions))
                    self.assertEqual(len(native), 0 if mode == "off" else 3)
                    if native:
                        self.assertEqual([r["phase"] for r in native], ["game", "game", "postmatch"])
                        self.assertTrue(all(r["status"] == "returned" and "[REDACTED]" in r["content"] for r in native))
                    debug = [json.loads(s) for s in (root / f"{directory}/debug.log").read_text(encoding="utf-8").splitlines()]
                    self.assertEqual(next(r for r in debug if r["kind"] == "match_start")["thinking"], {"Red · " + OPENAI.model: a_mode, "Blue · " + DEEPSEEK.model: b_mode})
                    self.assertEqual(next(r for r in debug if r["kind"] == "match_start")["players"],
                                     {"Red · " + OPENAI.model: "Red · " + OPENAI.model, "Blue · " + DEEPSEEK.model: "Blue · " + DEEPSEEK.model})
                    self.assertTrue(all(r["model"] == player
                                        for r in debug if r["kind"] == "request_config"))
                    self.assertEqual(len([r for r in debug if r["kind"] == "request_config"]), 3)
                for file in root.rglob("*.log"):
                    text = file.read_text(encoding="utf-8")
                    self.assertNotIn("test-key-a", text)
                    self.assertNotIn("test-key-b", text)
                self.assertNotIn("NATIVE-", (root / "chat.log").read_text(encoding="utf-8"))

    async def test_absent_reasoning_and_usage_are_not_fabricated(self):
        for usage, expected in ((None, None), ({}, None), ({"reasoning_tokens": 0}, 0),
                                ({"completion_tokens_details": {"reasoning_tokens": 36}}, 36),
                                ({"reasoning_tokens": True}, None)):
            events = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion(decision(), usage=usage))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(OPENAI, "medium"), "key")
                with request_trace(events.append, player="B", tick=1, attempt=1):
                    await player.respond(GameState().view_for("B"))
            record = next(r for r in events if r["kind"] == "reasoning")
            self.assertEqual(record["status"], "not_returned")
            self.assertIsNone(record["content"])
            self.assertEqual(record["reasoning_tokens"], expected)

    async def test_required_analysis_is_independent_of_native_reasoning_and_tokens(self):
        for extra in ({}, {"reasoning_content": " "}, {"reasoning_content": {"text": "invalid"}},
                      {"reasoning_content": "native reasoning cannot replace ANALYZE"}):
            bodies, events = [], []

            def handler(request):
                body = json.loads(request.content)
                bodies.append(body)
                content = '{"actions":[{"type":"MOVE","move":"Paper"},{"type":"ENDTURN"}]}' if len(bodies) == 1 else decision()
                return completion(content, usage={"reasoning_tokens": 100}, **extra)

            with self.subTest(extra=extra):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    player = ChatCompletionPlayer(client, configure_thinking(OPENAI, "high"), "key")
                    state = await run_game(player, ScriptedPlayer([decision("Rock")]), policy=FAST, on_request=events.append)
                self.assertEqual(state.result.winner, "A")
                self.assertEqual(len(bodies), 2)
                correction = json.loads(bodies[1]["messages"][-1]["content"])["correction"]
                self.assertIn("ANALYZE", correction)
                self.assertEqual(len([r for r in events if r["kind"] == "decision" and r["player"] == "A"]), 1)

    async def test_reasoning_cannot_bypass_command_or_analysis_validation(self):
        bodies, events = [], []

        def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return completion('{"actions":[{"type":"WAIT"},{"type":"MOVE","move":"Paper"}]}',
                                  reasoning_content="PRIVATE-ONLY-FIRST-ATTEMPT")
            if len(bodies) == 2:
                return completion('{"actions":[{"type":"MOVE","move":"Paper"},{"type":"ENDTURN"}]}')
            return completion(decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, configure_thinking(DEEPSEEK, "high"), "key")
            state = await run_game(player, ScriptedPlayer([decision("Rock")]), policy=FAST, on_request=events.append)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(len(bodies), 3)
        self.assertIn("指令 type", json.loads(bodies[1]["messages"][-1]["content"])["correction"])
        self.assertIn("ANALYZE", json.loads(bodies[2]["messages"][-1]["content"])["correction"])
        self.assertNotIn("PRIVATE-ONLY-FIRST-ATTEMPT", json.dumps(bodies))
        self.assertEqual(len([r for r in events if r["kind"] == "decision" and r["player"] == "A"]), 1)

    async def test_reasoning_does_not_relax_json_or_provided_analysis_types(self):
        accepted = parse_decision('{"actions":[{"type":"ENDTURN"}]}', analysis=AnalysisPolicy.OPTIONAL)
        self.assertIsNone(accepted.summary)
        for raw in ('{"actions":[{"type":"ENDTURN"}],"actions":[{"type":"ENDTURN"}]}',
                    '{"actions":[{"type":"ENDTURN"}],"has_reasoning":true}',
                    *[json.dumps(turn_payload([], value))
                      for value in (None, "", " ", 1, [])]):
            with self.subTest(raw=raw), self.assertRaises(InvalidResponse):
                parse_decision(raw, analysis=AnalysisPolicy.OPTIONAL)

    async def test_alternate_or_invalid_optional_reasoning_fields_do_not_become_actions(self):
        for field, value, status in (("reasoning", "provider text", "returned"),
                                     ("reasoning_content", {"actions": []}, "invalid_field"),
                                     ("reasoning_content", " ", "not_returned")):
            events = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion(decision(), **{field: value}))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(OPENAI, "medium"), "key")
                with request_trace(events.append, player="B", tick=1, attempt=1):
                    raw = await player.respond(GameState().view_for("B"))
            self.assertEqual(raw.content, decision())
            self.assertEqual(raw.has_reasoning, status == "returned")
            record = next(r for r in events if r["kind"] == "reasoning")
            self.assertEqual(record["status"], status)
            self.assertEqual(record["content"], value if status == "returned" else None)

    async def test_reasoning_only_truncated_output_is_logged_but_never_executes(self):
        events, bodies = [], []

        def handler(request):
            bodies.append(json.loads(request.content))
            return completion(None, finish="length", reasoning_content="unfinished provider text")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, configure_thinking(DEEPSEEK, "high"), "key")
            state = await run_game(player, ScriptedPlayer([decision("Rock")]), policy=FAST, on_request=events.append)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(state.moves, (None, None))
        self.assertEqual(len(bodies), 3)
        self.assertTrue(all(len(b["messages"]) == 2 for b in bodies))
        self.assertEqual(len([r for r in events if r["kind"] == "reasoning"]), 3)
        self.assertFalse(any(r["kind"] == "decision" and r["player"] == "A" for r in events))

    async def test_invalid_final_json_keeps_reasoning_separate_through_retry(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return completion("invalid" if len(bodies) == 1 else decision(), reasoning_content="NATIVE-PRIVATE")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, configure_thinking(DEEPSEEK, "low"), "key")
            state = await run_game(player, ScriptedPlayer([decision("Rock")]), policy=FAST)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(len(bodies), 2)
        self.assertEqual(len(bodies[1]["messages"]), 4)
        self.assertNotIn("NATIVE-PRIVATE", json.dumps(bodies))
        self.assertIsNotNone(json.loads(bodies[1]["messages"][-1]["content"])["correction"])

    async def test_unsupported_mode_http_error_does_not_silently_disable_thinking(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(400, text="private error")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, configure_thinking(OPENAI, "medium"), "key")
            state = await run_game(player, ScriptedPlayer([decision()]), policy=FAST)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["reasoning_effort"], "medium")
        self.assertNotIn("private error", repr(state))

    async def test_reasoning_log_failure_stops_before_actions(self):
        with tempfile.TemporaryDirectory() as directory, RunLogs(Path(directory)) as logs:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion(decision(), reasoning_content="text"))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(DEEPSEEK, "high"), "key")
                with patch.object(logs._files["A", "thought"], "write", side_effect=OSError("failed")):
                    with self.assertRaises(LogError):
                        await run_game(player, ScriptedPlayer([decision()]), policy=FAST,
                                       on_request=lambda record: logs.request(1, record))

    async def test_thinking_timeout_uses_existing_budget_and_cancels_requests(self):
        cancelled = []

        async def handler(request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        events = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, configure_thinking(DEEPSEEK, "high"), "key")
            state = await run_game(player, ScriptedPlayer([decision()]), policy=RequestPolicy(timeout=0.02, retry_delay=0), on_request=events.append)
        self.assertEqual(state.result.reason, EndReason.TECHNICAL_ERROR)
        self.assertEqual(len(cancelled), 3)
        self.assertFalse(any(r["kind"] == "reasoning" for r in events))

    async def test_demo_cannot_claim_real_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            args = make_parser().parse_args(["--mode", "demo", "--deepseek-thinking", "high", "--log-dir", directory])
            with self.assertRaises(ValueError):
                await play(args)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
