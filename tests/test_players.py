import json
import unittest
from dataclasses import replace

import httpx

from model_rps.core.commands import parse_response
from model_rps.core.game import MovePolicy, EndReason, GameState, advance
from model_rps.players.base import ChatCompletionPlayer, DEEPSEEK, OPENAI, PlayerError, WAIT_RESPONSE
from model_rps.engine.runner import RequestPolicy, run_game

ROCK = '{"actions":[{"type":"ANALYZE","text":"测试决策说明"},{"type":"MOVE","move":"Rock"},{"type":"ENDTURN"}]}'
SCISSORS = '{"actions":[{"type":"ANALYZE","text":"测试决策说明"},{"type":"MOVE","move":"Scissors"},{"type":"ENDTURN"}]}'


def completion(content=ROCK, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_payloads_auth_and_private_views(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())

        state = advance(GameState(move_policy=MovePolicy.REVISABLE), parse_response(ROCK), parse_response(WAIT_RESPONSE))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            deepseek = ChatCompletionPlayer(client, DEEPSEEK, "test-deepseek-key")
            openai = ChatCompletionPlayer(client, OPENAI, "test-openai-key")
            await deepseek.respond(state.view_for("A"), "仅 A 的纠错信息")
            await openai.respond(state.view_for("B"))
        self.assertEqual(str(requests[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(str(requests[1].url), "https://ai.yuhuantin.icu/v1/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-deepseek-key")
        self.assertEqual(requests[1].headers["authorization"], "Bearer test-openai-key")
        a, b = [json.loads(request.content) for request in requests]
        self.assertEqual(a["model"], "deepseek-flash")
        self.assertEqual(a["thinking"], {"type": "disabled"})
        self.assertEqual(a["max_tokens"], 20000)
        self.assertNotIn("max_completion_tokens", a)
        self.assertEqual(b["model"], "gpt-6-luna")
        self.assertEqual(b["reasoning_effort"], "none")
        self.assertEqual(b["max_completion_tokens"], 20000)
        self.assertNotIn("max_tokens", b)
        for payload in (a, b):
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["response_format"], {"type": "json_object"})
        a_context = json.loads(a["messages"][1]["content"])
        b_context = json.loads(b["messages"][1]["content"])
        self.assertEqual(a_context["state"]["own_move"], "Rock")
        self.assertEqual(a_context["correction"], "仅 A 的纠错信息")
        self.assertIsNone(b_context["correction"])
        self.assertNotIn("Rock", json.dumps(b_context))
        self.assertTrue(b_context["state"]["opponent_committed"])

    async def test_http_errors_are_classified_without_echoing_response_body(self):
        for status, retryable in ((400, False), (401, False), (403, False), (404, False),
                                  (408, True), (409, True), (429, True), (500, True), (503, True)):
            with self.subTest(status=status):
                def handler(request):
                    return httpx.Response(status, text="private-server-data test-key")

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    player = ChatCompletionPlayer(client, OPENAI, "test-key")
                    with self.assertRaises(PlayerError) as caught:
                        await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("A"))
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn("private-server-data", str(caught.exception))
                self.assertNotIn("test-key", str(caught.exception))

    async def test_malformed_or_incomplete_responses_are_rejected(self):
        responses = [
            {}, {"choices": []}, {"choices": [None]}, {"choices": [{}]},
            completion(None), completion(""), completion("  "), completion([]),
            completion(ROCK, "length"), completion(ROCK, "content_filter"),
        ]
        for data in responses:
            with self.subTest(data=data):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=data)
                )) as client:
                    player = ChatCompletionPlayer(client, OPENAI, "test-key")
                    with self.assertRaises(PlayerError):
                        await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("A"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="not JSON")
        )) as client:
            with self.assertRaises(PlayerError):
                await ChatCompletionPlayer(client, OPENAI, "test-key").respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("A"))

    async def test_transport_timeout_and_connection_error_are_sanitized(self):
        for kind, expected in ((httpx.ReadTimeout, TimeoutError), (httpx.ConnectError, PlayerError)):
            with self.subTest(kind=kind):
                def handler(request):
                    raise kind("sensitive transport message", request=request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    player = ChatCompletionPlayer(client, OPENAI, "test-key")
                    with self.assertRaises(expected) as caught:
                        await player.respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("A"))
                self.assertNotIn("sensitive", str(caught.exception))

    async def test_http_and_format_retries_share_budget_in_complete_game(self):
        counts = {"deepseek-flash": 0, "gpt-6-luna": 0}
        a_contexts = []

        def handler(request):
            body = json.loads(request.content)
            model = body["model"]
            counts[model] += 1
            if model == "deepseek-flash":
                a_contexts.append(json.loads(body["messages"][-1]["content"]))
                if counts[model] == 1:
                    return httpx.Response(429)
                if counts[model] == 2:
                    return httpx.Response(200, json=completion('{"actions":[]}'))
                return httpx.Response(200, json=completion(ROCK))
            return httpx.Response(200, json=completion(SCISSORS))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            state = await run_game(
                ChatCompletionPlayer(client, DEEPSEEK, "test-a"),
                ChatCompletionPlayer(client, OPENAI, "test-b"),
                policy=RequestPolicy(timeout=1, retry_delay=0),
            move_policy=MovePolicy.REVISABLE)
        self.assertEqual(state.result.reason, EndReason.SHOWDOWN)
        self.assertEqual(state.result.winner, "A")
        self.assertEqual(counts, {"deepseek-flash": 4, "gpt-6-luna": 2})
        self.assertTrue(all(context["state"] == a_contexts[0]["state"] for context in a_contexts[:3]))
        self.assertTrue(a_contexts[-1]["state"]["is_settlement_tick"])
        self.assertIn("HTTP 429", a_contexts[1]["correction"])
        self.assertIn("actions", a_contexts[2]["correction"])

    async def test_compatible_service_can_override_model_and_base_url(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())

        spec = replace(OPENAI, model="custom-model", base_url="https://example.test/v1/")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await ChatCompletionPlayer(client, spec, "test-key").respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("A"))
        self.assertEqual(str(requests[0].url), "https://example.test/v1/chat/completions")
        self.assertEqual(json.loads(requests[0].content)["model"], "custom-model")

    async def test_shared_gateway_still_uses_each_players_own_key(self):
        headers = []

        def handler(request):
            headers.append(request.headers["authorization"])
            return httpx.Response(200, json=completion())

        spec = replace(DEEPSEEK, base_url=OPENAI.base_url, model="deepseek-v4.1-flash")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await ChatCompletionPlayer(client, spec, "gateway-key-a").respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("A"))
            await ChatCompletionPlayer(client, OPENAI, "gateway-key-b").respond(GameState(move_policy=MovePolicy.REVISABLE).view_for("B"))
        self.assertEqual(headers, ["Bearer gateway-key-a", "Bearer gateway-key-b"])

    async def test_invalid_adapter_settings_fail_before_request(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: self.fail("Invalid settings must not make a request")
        )) as client:
            for key in ("", "  ", "test\nkey"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    ChatCompletionPlayer(client, OPENAI, key)
            for url in ("relative", "file:///tmp/model", "https://user:pass@example.test",
                        "https://example.test/?key=test", "https://example.test/#fragment"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    ChatCompletionPlayer(client, replace(OPENAI, base_url=url), "test-key")


if __name__ == "__main__":
    unittest.main()
