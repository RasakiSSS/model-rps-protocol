from copy import deepcopy
from dataclasses import replace
import json
import unittest

import httpx

from fixtures import complete_public_report, postmatch_payload, turn_payload
from model_rps.core.game import Event, GameState
from model_rps.engine.match import MatchConfig, run_match
from model_rps.players.base import ChatCompletionPlayer, OPENAI, PlayerError
from model_rps.engine.postmatch import MatchArchive, run_postmatch
from model_rps.engine.requests import RequestPolicy


FAST = RequestPolicy(timeout=2, retry_delay=0)


def completion(content):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_events_retry_snapshot_and_checkpoint_restore(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return completion('invalid' if len(bodies) == 2 else '{"actions":[]}')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'test-key')
            player.start_game({'game': 1, 'last_game': None})
            view = replace(GameState().view_for('A'), can_act=False)
            first = Event(1, 'say', 'B', '同样的话')
            await player.respond(replace(view, history=(first,)))
            checkpoint = player.checkpoint_history()
            events = (first, Event(1, 'say', 'B', '同样的话'))
            await player.respond(replace(view, history=events))
            # 即使调用方传入更新的视图，纠错仍对应上次的快照和增量。
            await player.respond(replace(view, history=events + (Event(1, 'say', 'B', '稍后送达'),)), '修正格式')
            contexts = [json.loads(b['messages'][-1]['content']) for b in bodies]
            self.assertEqual([e['sequence'] for e in contexts[1]['state']['history']], [2])
            self.assertEqual(contexts[1]['state'], contexts[2]['state'])
            self.assertEqual(contexts[2]['state']['history_after'], 1)
            self.assertIn('invalid', [m['content'] for m in bodies[-1]['messages']])
            player.restore_history(checkpoint)
            await player.respond(replace(view, history=events))
            restored = json.loads(bodies[-1]['messages'][-1]['content'])
            self.assertEqual(restored['state'], contexts[1]['state'])
            self.assertNotIn('invalid', [m['content'] for m in bodies[-1]['messages']])
            await player.respond(replace(view, history=events))
            self.assertEqual(json.loads(bodies[-1]['messages'][-1]['content'])['state']['history'], [])

    async def test_network_failure_does_not_consume_event_and_new_match_resets_cursors(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                raise httpx.ConnectError('failed', request=request)
            return completion('{"actions":[]}')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'test-key')
            view = replace(GameState().view_for('A'), can_act=False, history=(Event(1, 'say', 'B', '你好'),))
            with self.assertRaises(PlayerError):
                await player.respond(view)
            self.assertEqual(len(player._messages), 1)
            await player.respond(view, '重试')
            self.assertEqual(len(bodies[-1]['messages']), 2)
            self.assertEqual(json.loads(bodies[-1]['messages'][-1]['content'])['state']['history_after'], 0)
            player.start_match()
            await player.respond(view)
            self.assertEqual(len(bodies[-1]['messages']), 2)
            self.assertEqual(json.loads(bodies[-1]['messages'][-1]['content'])['state']['history_after'], 0)

    async def test_discarded_previous_result_is_redelivered_and_postmatch_retry_is_frozen(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return completion('{"actions":[]}')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'test-key')
            previous = {'game': 1, 'events': [Event(1, 'reveal', 'A', 'Rock').to_dict()],
                        'result': {'winner': 'A'}}
            player.start_game({'game': 2, 'last_game': previous})
            checkpoint = player.checkpoint_history()
            await player.respond(GameState().view_for('A'))
            await player.respond(GameState().view_for('A'))
            self.assertNotIn('last_game', json.loads(bodies[-1]['messages'][-1]['content'])['match'])
            player.restore_history(checkpoint)
            await player.respond(GameState().view_for('A'))
            self.assertEqual(json.loads(bodies[-1]['messages'][-1]['content']),
                             json.loads(bodies[0]['messages'][-1]['content']))
            context = {'player': 'A', 'match': {'games': [{**previous, 'decisions': ['NEW-ANALYSIS']}]}}
            await player.postmatch(context)
            first_report = json.loads(bodies[-1]['messages'][-1]['content'])
            self.assertEqual(first_report['match']['games'][0]['events'], [])
            self.assertEqual(first_report['match']['games'][0]['decisions'], ['NEW-ANALYSIS'])
            await player.postmatch({'player': 'A', 'events': ['NOT-YET']}, '修正格式')
            self.assertEqual(json.loads(bodies[-1]['messages'][-1]['content'])['match'], first_report['match'])
            self.assertNotIn('NOT-YET', json.dumps(bodies[-1]))

    async def test_complete_bo_and_postmatch_material_reconstructs_archive_for_both_seats(self):
        for initiative, move_policy in (('simultaneous', 'locked'), ('simultaneous', 'revisable'), ('alternating', 'locked')):
            with self.subTest(initiative=initiative, move_policy=move_policy):
                archive, reports = MatchArchive(), {}

                def handler(request):
                    body = json.loads(request.content)
                    context = json.loads(body['messages'][-1]['content'])
                    if context['phase'] == 'postmatch':
                        reports[context['player']] = body['messages']
                        payload = postmatch_payload()
                    else:
                        seat = context['state']['player']
                        peer = 'B' if seat == 'A' else 'A'
                        self.assertNotIn(f'PRIVATE-{peer}', json.dumps(body))
                        payload = {'actions': []} if context['phase'] == 'chat' else turn_payload(
                            [{'type': 'SAY', 'text': '相同发言'},
                             {'type': 'MOVE', 'move': 'Paper' if seat == 'A' else 'Rock'}], f'PRIVATE-{seat}')
                    return completion(json.dumps(payload))

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    players = tuple(ChatCompletionPlayer(client, OPENAI, 'test-key') for _ in range(2))
                    config = MatchConfig(3, initiative=initiative, move_policy=move_policy)
                    result = await run_match(lambda: players, config=config, policy=FAST,
                        on_request=archive.request, on_end=lambda n, state, *_: archive.finish_game(n, state))
                    expected = archive.report(config, result)
                    outcome = await run_postmatch(players, expected, policy=FAST)
                self.assertTrue(outcome.complete)
                self.assertEqual(result.wins, (2, 0))
                for messages in reports.values():
                    self.assertEqual(complete_public_report(messages), expected)

    async def test_fixed_conversation_retains_all_events_with_smaller_request_text(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return completion('{"actions":[]}')

        old_messages = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'test-key')
            view = replace(GameState().view_for('A'), can_act=False)
            for index in range(30):
                view = replace(view, history=view.history + (Event(1, 'say', 'B', f'{index}:' + '固定公开聊天。' * 30),))
                await player.respond(view)
                current = deepcopy(bodies[-1]['messages'][-1])
                context = json.loads(current['content'])
                context['state'] = view.to_dict()  # 原实现每次追加整局历史。
                current['content'] = json.dumps(context, ensure_ascii=False)
                old_messages.extend((current, {'role': 'assistant', 'content': '{"actions":[]}'}))
        new_messages = bodies[-1]['messages']
        old_messages = [new_messages[0], *old_messages[:-1]]
        delivered = [e for m in new_messages if m['role'] == 'user'
                     for e in json.loads(m['content'])['state']['history']]
        self.assertEqual([e['sequence'] for e in delivered], list(range(1, 31)))
        encode = lambda value: len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        self.assertLess(encode(new_messages), encode(old_messages) / 2)


if __name__ == '__main__':
    unittest.main()
