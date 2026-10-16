import json
import unittest

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.context_usage import ContextUsage
from model_rps.diagnostics import request_trace
from model_rps.game import GameState
from model_rps.players import ChatCompletionPlayer, OPENAI, PlayerError


class ContextUsageTests(unittest.TestCase):
    def test_reported_input_is_distinct_from_cumulative_and_unknown_is_not_zero(self):
        tracker = ContextUsage()
        messages = [{'role': 'user', 'content': '中文🙂'}]
        rows = [tracker.record(usage, messages) for usage in (
            {'prompt_tokens': 100}, {'input_tokens': 140}, None,
            {'prompt_tokens': 0}, {'prompt_tokens': 20},
        )]
        self.assertEqual([r['input_tokens'] for r in rows], [100, 140, None, 0, 20])
        self.assertEqual([r['input_tokens_delta'] for r in rows], [None, 40, None, None, 20])
        self.assertEqual(rows[-1]['peak_input_tokens'], 140)
        self.assertEqual(rows[-1]['known_input_tokens_total'], 260)
        self.assertEqual(rows[-1]['missing_usage_requests'], 1)
        self.assertEqual((rows[-1]['message_count'], rows[-1]['input_characters']), (1, 3))
        self.assertEqual(messages, [{'role': 'user', 'content': '中文🙂'}])

    def test_malformed_usage_is_unknown(self):
        for value in (True, False, -1, 1.5, '12', [], {}):
            with self.subTest(value=value):
                record = ContextUsage().record({'prompt_tokens': value}, [])
                self.assertIsNone(record['input_tokens'])
                self.assertIsNone(record['peak_input_tokens'])
                self.assertEqual(record['known_input_tokens_total'], 0)
                self.assertEqual(record['missing_usage_requests'], 1)


class ContextUsageAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_postmatch_failure_history_rollback_and_new_match_accounting(self):
        bodies, records = [], []
        values = iter((100, 160, 'error', 200, 90))

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            value = next(values)
            if value == 'error':
                return httpx.Response(503)
            ctx = json.loads(body['messages'][-1]['content'])
            result = (postmatch_payload('复盘') if ctx['phase'] == 'postmatch'
                      else turn_payload([{'type': 'MOVE', 'move': 'Rock'}], '分析'))
            return httpx.Response(200, json={
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}],
                'usage': {'prompt_tokens': value},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            player = ChatCompletionPlayer(client, OPENAI, 'test-key')
            with request_trace(records.append, player='A', tick=1, attempt=1):
                checkpoint = player.checkpoint_history()
                await player.respond(GameState().view_for('A'))
                player.restore_history(checkpoint)  # 撤回未应用响应，不撤回已消耗的用量。
                await player.respond(GameState().view_for('A'))
                with self.assertRaises(PlayerError):
                    await player.respond(GameState().view_for('A'))
            with request_trace(records.append, player='A', tick=0, attempt=1, phase='postmatch'):
                await player.postmatch({'player': 'A', 'match': {}})
            player.start_match()
            with request_trace(records.append, player='A', tick=1, attempt=1):
                await player.respond(GameState().view_for('A'))

        rows = [r for r in records if r['kind'] == 'context_usage']
        self.assertEqual([r['input_tokens'] for r in rows], [100, 160, None, 200, 90])
        self.assertEqual([r['input_tokens_delta'] for r in rows], [None, 60, None, None, None])
        self.assertEqual([r['message_count'] for r in rows], [2, 2, 4, 4, 2])
        self.assertEqual(rows[3]['known_input_tokens_total'], 460)
        self.assertEqual(rows[3]['phase'], 'postmatch')
        self.assertEqual(rows[3]['missing_usage_requests'], 1)
        self.assertEqual(rows[-1]['known_input_tokens_total'], 90)
        self.assertEqual(rows[-1]['missing_usage_requests'], 0)
        self.assertEqual(rows[-1]['peak_input_tokens'], 90)
        self.assertEqual(len(player._messages), 3)


if __name__ == '__main__':
    unittest.main()
