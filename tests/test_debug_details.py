import asyncio
import contextlib
import io
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import turn_payload
from model_rps.debug_console import format_record, watch
from model_rps.game import GameState, MovePolicy
from model_rps.identities import player_names
from model_rps.logs import RunLogs
from model_rps.players import ChatCompletionPlayer, OPENAI, ScriptedPlayer, PlayerError
from model_rps.postmatch import _request, PostmatchFailed
from model_rps.requests import RequestPolicy, request_interaction
from model_rps.scheduler import GameSession
from model_rps.seats import SeatPlayer
from test_human import until


FAST = RequestPolicy(timeout=2, retry_delay=0)


def response(move=None, analysis='用于调试的策略分析'):
    return json.dumps(turn_payload([{'type': 'MOVE', 'move': move}] if move else [], analysis), ensure_ascii=False)


class DebugDetailTests(unittest.TestCase):
    def test_shared_log_order_survives_equal_timestamps_and_large_records(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), names=player_names('same', 'same')) as logs:
                with patch('model_rps.logs.datetime') as clock:
                    clock.now.return_value = datetime(2026, 10, 18, 12)
                    logs.lifecycle('match_start', players=logs.names)
                    logs.request(1, {'kind': 'http_request', 'player': 'A', 'payload': 'x' * 150000})
                    for index, player in enumerate(('B', 'A', 'B', 'A')):
                        logs.request(1, {'kind': 'usage', 'player': player, 'marker': f'ORDER-{index}'})
            rows = [json.loads(line) for seat in ('red', 'blue')
                    for line in (logs.path / seat / 'debug.log').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sorted(row['log_sequence'] for row in rows), list(range(1, len(rows) + 1)))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                watch(logs.path, once=True)
            text = output.getvalue()
            positions = [text.index(f'ORDER-{index}') for index in range(4)]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(text.count('run_closed'), 1)
            self.assertNotIn('x' * 100, text)

    def test_incomplete_log_still_drains_records_after_sequence_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            for seat, sequence in (('red', 1), ('blue', 3)):
                path = Path(directory) / seat
                path.mkdir()
                (path / 'debug.log').write_text(json.dumps({
                    'kind': 'usage', 'player': seat, 'log_sequence': sequence, 'marker': seat,
                }) + '\n' + '{"partial":', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                watch(Path(directory), once=True)
            self.assertIn('red：', output.getvalue())
            self.assertIn('blue：', output.getvalue())

    def test_analysis_and_commands_shown_thinking_and_history_stay_in_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), names=player_names('same', 'same'), secrets=('secret-key',)) as logs:
                logs.lifecycle('match_start', players=logs.names)
                for identity in ('A', 'B'):
                    base = {'player': identity, 'tick': 3, 'request_id': 8 if identity == 'A' else 9,
                            'attempt': 1, 'phase': 'game'}
                    logs.request(2, {**base, 'kind': 'request_start', 'state': {
                        'can_act': True, 'own_move': 'Rock', 'own_move_locked': True,
                        'opponent_committed': False, 'remaining_turns_including_current': 18,
                        'event_cursor': 2, 'request_reasons': ['opponent_say'], 'notifications': [2],
                        'history': [{'sequence': 1, 'text': 'OLD-HISTORY'},
                                    {'sequence': 2, 'player': identity, 'text': '新消息'}]}})
                    logs.request(2, {**base, 'kind': 'http_request', 'payload': 'WHOLE-HISTORY'})
                    logs.request(2, {**base, 'kind': 'http_response', 'status': 200,
                                    'body': json.dumps({'reasoning_content': 'NATIVE-THINKING'})})
                    logs.request(2, {**base, 'kind': 'reasoning', 'status': 'returned',
                                    'content': 'NATIVE-THINKING', 'reasoning_tokens': 12})
                    logs.request(2, {**base, 'kind': 'stream_delta', 'field': 'reasoning_content',
                                    'content': 'NATIVE-THINKING'})
                    logs.request(2, {**base, 'kind': 'stream_end', 'status': 'complete', 'chunks': 3,
                                    'message': {'reasoning_content': 'NATIVE-THINKING'}, 'finish_reason': 'stop'})
                    logs.request(2, {**base, 'kind': 'decision', 'summary': '策略第一段\n第二段 secret-key\x1b[2J',
                                    'actions': [{'type': 'SAY', 'text': '公开发言'}],
                                    'ignored_actions': [{'type': 'MOVE', 'move': 'Paper'}]})
                    logs.request(2, {**base, 'kind': 'action_resolved', 'applied_tick': 3,
                                    'outcomes': ['move_ignored'], 'own_move': 'Rock'})
                    logs.request(2, {**base, 'kind': 'http_error_body', 'body': json.dumps({
                        'error': {'type': 'invalid_request', 'message': 'bad secret-key', 'code': 'bad_input'},
                        'request': 'WHOLE-HISTORY', 'reasoning_content': 'NATIVE-THINKING'})})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                watch(logs.path, once=True)
            text = output.getvalue()
            for expected in ('Red · same', 'Blue · same', '第 2 局 Tick 3', '响应 #8', '响应 #9',
                             '策略第一段\n    第二段', 'SAY', 'MOVE：布', '已忽略', '石头',
                             '权限：可行动或聊天', '新消息 #2', 'bad_input', '[REDACTED]'):
                self.assertIn(expected, text)
            for hidden in ('OLD-HISTORY', 'WHOLE-HISTORY', 'NATIVE-THINKING', 'secret-key', '\x1b[2J'):
                self.assertNotIn(hidden, text)
            self.assertIn('NATIVE-THINKING', (logs.path / 'red' / 'thought.log').read_text(encoding='utf-8'))
            self.assertIn('\\u001b[2J', text)

    def test_bad_response_bounded_and_multiline_cannot_forge_event_headers(self):
        line = format_record({'kind': 'validation_error', 'player': 'Red · model\n伪造',
                              'error': '无效\n字段', 'content': 'x' * 4100 + '\n[伪造系统消息]密钥'})
        self.assertIn('model\\u000a伪造', line)
        self.assertIn('无效\n    字段', line)
        self.assertIn('窗口仅展示前段', line)
        self.assertNotIn('伪造系统消息', line)
        self.assertNotIn('密钥', line)
        self.assertIsNone(format_record({'kind': 'raw_response', 'content': 'raw'}))
        self.assertIn('完整内容见 debug.log', format_record({
            'kind': 'http_response', 'status': 400, 'body': '{"reasoning_content":"hidden"}'}))
        self.assertNotIn('hidden', format_record({
            'kind': 'http_response', 'status': 400, 'body': '{"reasoning_content":"hidden"}'}))

    def test_handoff_color_follows_log_seat_with_old_and_new_authors(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunLogs(Path(directory), names=player_names('人类', '对手')) as logs:
                logs.lifecycle('match_start', players=logs.names)
                logs.request(1, {'kind': 'handoff', 'player': 'A', 'actor': 'Red · 新模型', 'tick': 2,
                                'previous_player': 'Red · 人类', 'profile': 'new', 'completed_games': 0,
                                'public_events': 3, 'own_decisions': 1, 'own_move': 'Rock',
                                'own_move_locked': True, 'can_act': False,
                                'discarded_drafts': {'move': False, 'analysis_count': 1}})
                logs.request(1, {'kind': 'action_resolved', 'player': 'A', 'actor': 'Red · 人类',
                                'tick': 2, 'request_id': 1, 'applied_tick': 2, 'outcomes': ['commit'], 'own_move': 'Rock'})
                logs.request(1, {'kind': 'chat_validated', 'player': 'A', 'actor': 'Red · 新模型',
                                'tick': 2, 'request_id': 3, 'message_count': 0, 'messages': []})
            with patch('model_rps.debug_console.supports_color', return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                watch(logs.path, once=True)
            text = output.getvalue()
            self.assertIn('\x1b[91mRed · 新模型\x1b[0m', text)
            self.assertIn('\x1b[91mRed · 人类\x1b[0m', text)
            self.assertIn('3 条公共事件', text)
            self.assertIn('接管权限：仅聊天', text)
            self.assertIn('分析 1 段', text)


class GatedPlayer(ScriptedPlayer):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.reply = response('Scissors')

    async def respond(self, view, feedback=None):
        if not view.can_act:
            return '{"actions":[]}'
        await self.gate.wait()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class DebugDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_action_resolves_only_after_peer_and_keeps_request_identity(self):
        opponent = GatedPlayer()
        records = []
        session = GameSession(SeatPlayer(ScriptedPlayer([response('Rock')]), 'Red · 模型'), opponent,
                              state=GameState(), policy=FAST, on_request=records.append)
        task = asyncio.create_task(session.run())
        try:
            await until(lambda: any(r['kind'] == 'interaction_applied' and r.get('pending_action') for r in records))
            held = next(r for r in records if r['kind'] == 'interaction_applied' and r.get('pending_action'))
            self.assertIn('等待双方同步推进', format_record(held))
            self.assertFalse(any(r['kind'] == 'action_resolved' for r in records))
            opponent.gate.set()
            await task
            resolved = next(r for r in records if r['kind'] == 'action_resolved' and r['player'] == 'A')
            self.assertEqual(resolved['request_id'], held['request_id'])
            self.assertEqual(resolved['actor'], 'Red · 模型')
            self.assertEqual(resolved['outcomes'], ['commit'])
            self.assertEqual(resolved['own_move'], 'Rock')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_held_action_discarded_on_peer_failure_never_reported_executed(self):
        opponent = GatedPlayer()
        opponent.reply = PlayerError('服务错误', retryable=False)
        records = []
        session = GameSession(ScriptedPlayer([response('Rock')]), opponent,
                              state=GameState(), policy=FAST, on_request=records.append)
        task = asyncio.create_task(session.run())
        try:
            await until(lambda: any(r.get('pending_action') for r in records))
            opponent.gate.set()
            state = await task
            self.assertEqual(state.completed_ticks, 0)
            self.assertFalse(any(r['kind'] == 'action_resolved' for r in records))
            discarded, = [r for r in records if r['kind'] == 'action_discarded']
            self.assertEqual(discarded['player'], 'A')
            self.assertIn('行动未执行', format_record(discarded))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_revisable_reports_changes_and_locked_reports_ignored_moves(self):
        for policy in (MovePolicy.LOCKED, MovePolicy.REVISABLE):
            with self.subTest(policy=policy):
                records = []
                session = GameSession(ScriptedPlayer([response('Rock'), response('Paper')]),
                    ScriptedPlayer([response(), response('Scissors')]),
                    state=GameState(move_policy=policy), policy=FAST, on_request=records.append)
                await session.run()
                second, = [r for r in records if r['kind'] == 'action_resolved' and r['player'] == 'A' and r['tick'] == 2]
                self.assertEqual(second['outcomes'], ['move_ignored' if policy == MovePolicy.LOCKED else 'revise'])
                self.assertEqual(second['own_move'], 'Rock' if policy == MovePolicy.LOCKED else 'Paper')

    async def test_rejected_game_and_postmatch_responses_record_error_body_and_correction(self):
        records = []
        view = GameState().view_for('A')
        await request_interaction(ScriptedPlayer(['invalid', response('Rock')]), view, FAST,
                                  lambda event: None, records.append)
        rejected, = [r for r in records if r['kind'] == 'validation_error']
        self.assertEqual(rejected['content'], 'invalid')
        self.assertIn('未执行', format_record(rejected))
        failed = next(r for r in records if r['kind'] == 'request_end' and r['status'] == 'rejected')
        self.assertIn('纠错反馈', format_record(failed))
        self.assertIn('将重试', format_record(failed))

        class BadPostmatch(ScriptedPlayer):
            async def postmatch(self, context, feedback=None):
                return '{"actions":[{"type":"MOVE","move":"Rock"}]}'

        records.clear()
        with self.assertRaises(PostmatchFailed):
            await _request(BadPostmatch(), {'player': 'B', 'request_id': 1}, FAST, records.append)
        errors = [r for r in records if r['kind'] == 'validation_error']
        self.assertEqual(len(errors), 3)
        self.assertIn('赛后', errors[-1]['error'])
        self.assertIn('不再重试', format_record(records[-1]))

    async def test_http_failure_details_streaming_and_nonstreaming_are_redacted(self):
        for stream in (False, True):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as directory:
                def handler(request):
                    return httpx.Response(400, json={'error': {'message': '服务拒绝 test-api-secret',
                        'type': 'invalid_request', 'code': 'bad_input'}, 'reasoning_content': 'NEVER-SHOW-REASONING'})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with RunLogs(Path(directory), names=player_names('模型', '对手'), secrets=('test-api-secret',)) as logs:
                        player = ChatCompletionPlayer(client, OPENAI, 'test-api-secret', stream=stream)
                        state = await GameSession(player, ScriptedPlayer(), state=GameState(), policy=FAST,
                            on_request=lambda record: logs.request(1, record)).run()
                        self.assertEqual(state.result.reason, 'technical_error')
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    watch(logs.path, once=True)
                text = output.getvalue()
                self.assertIn('HTTP 400', text)
                self.assertIn('服务拒绝 [REDACTED]', text)
                self.assertIn('bad_input', text)
                self.assertNotIn('test-api-secret', text)
                self.assertNotIn('NEVER-SHOW-REASONING', text)
