import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import unittest

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.adapters import MODEL_ADAPTERS
from model_rps.game import GameState
from model_rps.match import MatchConfig, run_match
from model_rps.players import ChatCompletionPlayer, ScriptedPlayer
from model_rps.postmatch import MatchArchive, _request, run_postmatch
from model_rps.prompts import GROK_STREAM_FORMAT_PROMPT, game_prompt
from model_rps.requests import RequestPolicy, TurnFailed, request_interaction
from model_rps.seats import Replacement, SeatPlayer
from test_adapters import BytesStream, native_events, native_response
from test_streaming import complete, response


FAST = RequestPolicy(timeout=2, retry_delay=0)
BROKEN = '{"actions":[{"type":"ANALYZE","text":"未执行分析"},type":"MOVE","move":"Rock"},type":"ENDTURN"}]}'


class GrokStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_grok_streaming_adds_format_rule_in_actual_request(self):
        defaults = deepcopy(MODEL_ADAPTERS)
        for adapter, original in MODEL_ADAPTERS.items():
            for stream in (False, True):
                with self.subTest(adapter=adapter, stream=stream):
                    bodies = []
                    # 以适配器判定，不依赖模型名或特定服务域名。
                    spec = replace(original, model='custom-alias', base_url='https://service.invalid/v1')

                    def handler(request):
                        bodies.append(json.loads(request.content))
                        text = '{"actions":[]}'
                        if adapter == 'anthropic':
                            if stream:
                                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                                      stream=BytesStream(native_events(text)))
                            return httpx.Response(200, json=native_response(text))
                        if stream:
                            return response(complete(text))
                        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                                    'message': {'content': text}}]})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        player = ChatCompletionPlayer(client, spec, 'test-key', stream=stream)
                        await player.respond(replace(GameState().view_for('A'), can_act=False))
                    system = bodies[0]['system'] if adapter == 'anthropic' else bodies[0]['messages'][0]['content']
                    expected = game_prompt('locked')
                    if adapter == 'grok' and stream:
                        expected += '\n' + GROK_STREAM_FORMAT_PROMPT
                    self.assertEqual(system, expected)
        self.assertEqual(MODEL_ADAPTERS, defaults)

    async def test_bo_retry_chat_and_postmatch_keep_single_format_rule_and_raw_history(self):
        for initiative, move_policy in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
            with self.subTest(initiative=initiative, move_policy=move_policy):
                bodies, records = [], []
                rejected = False
                chat_seen = asyncio.Event()

                class WaitingOpponent(ScriptedPlayer):
                    def start_game(self, context):
                        super().start_game(context)
                        chat_seen.clear()

                    async def respond(self, view, feedback=None):
                        if view.can_act and view.initiative == 'simultaneous':
                            # 保持同步边界打开，实际触发已提交方的独立聊天请求。
                            await chat_seen.wait()
                        return await super().respond(view, feedback)

                def handler(request):
                    nonlocal rejected
                    body = json.loads(request.content)
                    bodies.append(body)
                    context = json.loads(body['messages'][-1]['content'])
                    if context['phase'] == 'postmatch':
                        payload = postmatch_payload('交流结束。')
                    elif not context['state']['can_act']:
                        chat_seen.set()
                        payload = {'actions': [{'type': 'SAY', 'text': '聊天内容'}]}
                    elif not rejected:
                        rejected = True
                        return response(complete(BROKEN))
                    else:
                        payload = turn_payload([{'type': 'MOVE', 'move': 'Rock'}])
                    return response(complete(json.dumps(payload, ensure_ascii=False, indent=2), 'PRIVATE-REASONING'))

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    player = ChatCompletionPlayer(client, MODEL_ADAPTERS['grok'], 'test-key', stream=True)
                    opponent = WaitingOpponent([json.dumps(turn_payload([{'type': 'MOVE', 'move': 'Scissors'}]))],
                                              repeat_per_game=True)
                    config = MatchConfig(best_of=3, max_games=3, initiative=initiative, move_policy=move_policy)
                    archive = MatchArchive()

                    def record(number, row):
                        records.append(row)
                        archive.request(number, row)

                    result = await run_match(lambda: (player, opponent), config=config, policy=FAST,
                        on_request=record, on_end=lambda number, state, *_: archive.finish_game(number, state))
                    self.assertEqual((result.wins, result.games_played), ((2, 0), 2))
                    outcome = await run_postmatch((player, opponent), archive.report(config, result), policy=FAST)
                    self.assertTrue(outcome.complete)
                self.assertTrue(rejected)
                self.assertEqual(len([r for r in records if r['kind'] == 'validation_error']), 1)
                for body in bodies:
                    self.assertEqual(body['messages'][0]['content'].count(GROK_STREAM_FORMAT_PROMPT), 1)
                    self.assertNotIn('PRIVATE-REASONING', json.dumps(body))
                contexts = [json.loads(b['messages'][-1]['content']) for b in bodies]
                self.assertEqual({c['phase'] for c in contexts}, {'game', 'chat', 'postmatch'})
                self.assertEqual({c['match']['game'] for c in contexts if c['phase'] != 'postmatch'}, {1, 2})
                self.assertTrue(any(c.get('correction') for c in contexts))
                self.assertIn(BROKEN, [m['content'] for m in bodies[-1]['messages']])

    async def test_handoff_and_fresh_postmatch_keep_format_rule_without_duplicates(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            context = json.loads(body['messages'][-1]['content'])
            payload = postmatch_payload(exit=True) if context['phase'] == 'postmatch' else turn_payload([])
            return response(complete(json.dumps(payload, indent=2)))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, MODEL_ADAPTERS['grok'], 'test-key', stream=True)
            seat = SeatPlayer(ScriptedPlayer(), 'Red · 原玩家')
            seat.start_match()
            seat.start_game({'game': 2})
            view = GameState(initiative='alternating').view_for('A')
            replacement = Replacement(player, 'Red · Grok', 'grok')
            handoff = seat.handoff_context(view, replacement)
            player.start_match()
            player.start_game(seat.context)
            player.receive_handoff(handoff)
            checkpoint = player.checkpoint_history()
            await player.respond(view)
            player.restore_history(checkpoint)
            await player.respond(view)
            self.assertEqual(bodies[0]['messages'], bodies[1]['messages'])
            self.assertEqual(json.loads(bodies[0]['messages'][1]['content'])['phase'], 'handoff')
            player.start_match()
            result = await _request(player, {'player': 'A', 'request_id': 1}, FAST, None)
            self.assertTrue(result.exit)
        self.assertEqual(len(bodies[-1]['messages']), 2)
        for body in bodies:
            self.assertEqual(body['messages'][0]['content'].count(GROK_STREAM_FORMAT_PROMPT), 1)

    async def test_format_hint_does_not_repair_or_execute_damaged_json(self):
        records = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(complete(BROKEN)))) as client:
            player = ChatCompletionPlayer(client, MODEL_ADAPTERS['grok'], 'test-key', stream=True)
            with self.assertRaises(TurnFailed):
                await request_interaction(player, GameState().view_for('A'), FAST, lambda _: None, records.append)
        errors = [r for r in records if r['kind'] == 'validation_error']
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(r['content'] == BROKEN for r in errors))
        self.assertFalse(any(r['kind'] == 'decision' for r in records))


if __name__ == '__main__':
    unittest.main()
