import asyncio
import contextlib
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from fixtures import postmatch_payload, turn_payload
from model_rps.adapters import MODEL_ADAPTERS, THINKING_MODES, configure_thinking, endpoint, request_payload
from model_rps.cli import main, make_parser
from model_rps.config import RuntimeConfig, load_config
from model_rps.diagnostics import request_trace
from model_rps.game import GameState
from model_rps.player_factory import player_keys
from model_rps.players import ChatCompletionPlayer, PlayerError
from model_rps.requests import RequestPolicy, TurnFailed, request_interaction


def native_response(content, *, stop='end_turn'):
    return {'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'stop_reason': stop,
            'content': [{'type': 'thinking', 'thinking': 'NATIVE-PRIVATE', 'signature': 'OPAQUE'},
                        {'type': 'text', 'text': content}],
            'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 30, 'output_tokens': 40}}


def native_events(content, *, stop='end_turn'):
    return [
        {'type': 'message_start', 'message': {'type': 'message', 'role': 'assistant', 'content': [],
            'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 30, 'output_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'NATIVE-PRIVATE'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'OPAQUE'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'ping'},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': content}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': stop}, 'usage': {'output_tokens': 40}},
        {'type': 'message_stop'},
    ]


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, events):
        self.events, self.closed = events, False

    async def __aiter__(self):
        data = ''.join('event: ' + event['type'] + '\r\ndata: ' + json.dumps(event, ensure_ascii=False) + '\r\n\r\n'
                       for event in self.events).encode()
        for offset in range(0, len(data), 7):
            yield data[offset:offset+7]

    async def aclose(self):
        self.closed = True


class AdapterConfigTests(unittest.TestCase):
    def test_profile_precedence_cli_switch_and_independent_seats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[models.claude]\nadapter="anthropic"\napi_key="claude-key"\nthinking="medium"\n'
                            '[models.glm]\nadapter="glm"\napi_key="glm-key"\n'
                            '[players.red]\nprofile="claude"\nname="昵称"\n'
                            '[players.blue]\nprofile="claude"', encoding='utf-8')
            config = load_config(path)
            settings = RuntimeConfig.from_args(make_parser(config).parse_args(['--red-profile', 'glm', '--red-thinking', 'max']))
            self.assertEqual((settings.red.adapter, settings.red.model, settings.red.thinking, settings.red.api_key),
                             ('glm', 'glm-5.3', 'max', 'glm-key'))
            self.assertEqual(settings.red.display_name, '昵称')
            self.assertEqual((settings.blue.adapter, settings.blue.thinking, settings.blue.api_key), ('anthropic', 'medium', 'claude-key'))
            self.assertNotIn('glm-key', repr(settings))
            self.assertEqual(config['profiles']['claude']['thinking'], 'medium')
            human = RuntimeConfig.from_args(make_parser(config).parse_args(['--red-type', 'human']))
            self.assertEqual(human.red.analysis, 'optional')
            self.assertEqual(player_keys(human, Path('not-used')), (None, 'claude-key'))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                make_parser(config).print_help()
            self.assertNotIn('claude-key', out.getvalue())

    def test_profile_validation_and_legacy_credentials_reject_before_network(self):
        cases = ['[models.x]\nmodel="x"', '[models.x]\nadapter="glm"\nthinking="off"',
                 '[models.x]\nadapter="grok"\nthinking="max"', '[models.x]\nadapter="gemini"\nthinking="xhigh"',
                 '[models.x]\nadapter="anthropic"\ntype="human"', '[models.x]\nadapter="glm"\nprofile="y"',
                 '[players.red]\nprofile="missing"', '[models.x]\nadapter="glm"\napi_key=123',
                 '[models.x]\nadapter="glm"\nbase_url="https://private-key@service.test"',
                 '[models.x]\nadapter="glm"\n[models.x.nested]\napi_key="private-key"']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for content in cases:
                path.write_text(content, encoding='utf-8')
                with self.subTest(content=content), contextlib.redirect_stderr(io.StringIO()) as out, \
                        patch('model_rps.cli.httpx.AsyncClient') as client:
                    self.assertEqual(main(['--config', str(path)]), 2)
                    self.assertNotIn('private-key', out.getvalue())
                    client.assert_not_called()
        config = RuntimeConfig.from_args(make_parser().parse_args(['--red-adapter', 'glm']))
        self.assertEqual(config.red.thinking, 'high')
        with self.assertRaisesRegex(ValueError, 'TOML'):
            player_keys(config, Path('not-used'))

    def test_documented_thinking_parameters_and_protocols_do_not_mutate_defaults(self):
        for adapter, spec in MODEL_ADAPTERS.items():
            original = deepcopy(spec)
            self.assertEqual(spec, configure_thinking(spec, spec.thinking_mode))
            for mode in THINKING_MODES[adapter]:
                configured = configure_thinking(spec, mode)
                body = request_payload(configured, [{'role':'system','content':'rules'}, {'role':'user','content':'hello'}], 1234, True)
                self.assertEqual(body[spec.token_parameter], 1234)
                if adapter == 'anthropic':
                    self.assertEqual(body['output_config'], {'effort': mode})
                    self.assertEqual(body['thinking'], {'type': 'adaptive', 'display': 'summarized'})
                    self.assertEqual(body['system'], 'rules')
                    self.assertNotIn('response_format', body)
                    self.assertNotIn('stream_options', body)
                    self.assertEqual(body['messages'], [{'role':'user','content':'hello'}])
                else:
                    self.assertEqual(body.get('reasoning_effort'), ('none' if adapter=='openai' else None) if mode=='off' else mode)
                    self.assertEqual(body['stream_options'], {'include_usage': True})
            self.assertEqual(spec, original)
        for url in ('https://service.test', 'https://service.test/v1/'):
            self.assertEqual(endpoint(replace(MODEL_ADAPTERS['anthropic'], base_url=url)), 'https://service.test/v1/messages')


class NativeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_retry_budget_correction_and_nonretryable_http(self):
        for status in (429, 401):
            bodies, records = [], []

            def handler(request):
                bodies.append(json.loads(request.content))
                if len(bodies) == 1:
                    return httpx.Response(status, json={'error': {'message': 'SERVER-PRIVATE'}})
                content = 'invalid json' if len(bodies) == 2 else json.dumps(turn_payload([]))
                return httpx.Response(200, json=native_response(content))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'], 'high'), 'key')
                async def run():
                    return await request_interaction(player, GameState().view_for('A'),
                        RequestPolicy(timeout=1, retry_delay=0), lambda _: None, records.append)
                if status == 401:
                    with self.assertRaises(TurnFailed) as caught:
                        await run()
                    self.assertNotIn('SERVER-PRIVATE', str(caught.exception))
                    self.assertEqual(len(bodies), 1)
                else:
                    result = await run()
                    self.assertIsNotNone(result.interaction.decision)
                    self.assertEqual(len(bodies), 3)
                    self.assertEqual([r['status'] for r in records if r['kind'] == 'request_end'],
                                     ['rejected', 'rejected', 'valid'])
                    last = bodies[-1]['messages']
                    self.assertEqual(last[-2]['content'], 'invalid json')
                    self.assertTrue(json.loads(last[-1]['content'])['correction'])
                    self.assertNotIn('NATIVE-PRIVATE', json.dumps(bodies))

    async def test_redacted_thinking_and_failure_usage_are_not_fabricated(self):
        content = json.dumps(turn_payload([]))
        response = native_response(content)
        response['content'][0] = {'type': 'redacted_thinking', 'data': 'ENCRYPTED-OPAQUE'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
            player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'], 'high'), 'key')
            result = await player.respond(GameState().view_for('A'))
            self.assertFalse(result.has_reasoning)
            self.assertNotIn('ENCRYPTED-OPAQUE', json.dumps(player._messages))

        for stream in (False, True):
            records = []
            def handler(_):
                if stream:
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                        stream=BytesStream(native_events(content, stop='max_tokens')))
                return httpx.Response(200, json=native_response(content, stop='max_tokens'))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'], 'high'),
                                              'key', stream=stream)
                with request_trace(records.append, player='A', tick=1, attempt=1), self.assertRaises(PlayerError):
                    await player.respond(GameState().view_for('A'))
                self.assertEqual(len(player._messages), 1)
                self.assertEqual(player._usage['output_tokens'], 40)
                usage, = [r for r in records if r['kind'] == 'context_usage']
                self.assertEqual(usage['input_tokens'], 60)

    async def test_messages_and_sse_share_context_privacy_usage_and_auth(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                bodies, records, streams = [], [], []

                def handler(request):
                    body = json.loads(request.content)
                    bodies.append(body)
                    self.assertEqual(request.headers['x-api-key'], 'test-key')
                    self.assertEqual(request.headers['anthropic-version'], '2023-06-01')
                    self.assertNotIn('authorization', request.headers)
                    self.assertEqual(str(request.url), 'https://api.anthropic.com/v1/messages')
                    content = json.dumps(turn_payload([], '中文分析'), ensure_ascii=False)
                    if stream:
                        source = BytesStream(native_events(content))
                        streams.append(source)
                        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=source)
                    return httpx.Response(200, json=native_response(content))

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'], 'high'), 'test-key', stream=stream)
                    with request_trace(records.append, player='A',tick=1,attempt=1):
                        first = await player.respond(GameState().view_for('A'))
                        await player.respond(GameState().view_for('A'))
                    self.assertTrue(first.has_reasoning)
                    self.assertNotIn('NATIVE-PRIVATE', json.dumps(bodies))
                    self.assertNotIn('OPAQUE', json.dumps(player._messages))
                    self.assertIn('中文分析', json.dumps(bodies[-1],ensure_ascii=False))
                usage = [row for row in records if row['kind']=='context_usage']
                self.assertEqual([row['input_tokens'] for row in usage], [60, 60])
                self.assertEqual(usage[-1]['known_input_tokens_total'],120)
                self.assertTrue(all(s.closed for s in streams))
                self.assertNotIn('test-key', json.dumps(records))

    async def test_invalid_native_completion_never_enters_history(self):
        for stop in ('max_tokens', 'tool_use', 'pause_turn', 'refusal', None):
            response = native_response('{"actions":[]}', stop=stop)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=response))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'],'high'),'key')
                with self.subTest(stop=stop), self.assertRaises(PlayerError):
                    await player.respond(GameState().view_for('A'))
                self.assertEqual(len(player._messages),1)
        for content in ([{'type':'tool_use','id':'tool','input':{}}], [{'type':'text','text':123}]):
            response = {**native_response(''), 'content': content}
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=response))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'],'high'),'key')
                with self.assertRaises(PlayerError):
                    await player.respond(GameState().view_for('A'))
                self.assertEqual(len(player._messages),1)

    async def test_truncated_misordered_error_and_tool_streams_are_rejected_atomically(self):
        valid = native_events('{"actions":[]}')
        bad_index = deepcopy(valid)
        bad_index[7]['index'] = 9
        tool = deepcopy(valid)
        tool[6]['content_block']['type'] = 'tool_use'
        cases = [valid[:-1], valid[1:], bad_index, tool, native_events('{"actions":[]}',stop='max_tokens'),
                 valid[:7] + [{'type':'error','error':{'message':'private-server-text'}}],
                 valid[:8] + [{'type':'message_stop'}]]
        for events in cases:
            source = BytesStream(events)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                    200, headers={'content-type':'text/event-stream'},stream=source))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'],'high'),'key',stream=True)
                with self.assertRaises(PlayerError) as caught:
                    await player.respond(GameState().view_for('A'))
                self.assertNotIn('private-server-text',str(caught.exception))
                self.assertEqual(len(player._messages),1)
                self.assertTrue(source.closed)

    async def test_ping_timeout_and_cancellation_close_native_stream(self):
        class Pending(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                while True:
                    yield b'data: {"type":"ping"}\n\n'
                    await asyncio.sleep(.005)
            async def aclose(self):
                self.closed = True

        for cancel in (False, True):
            source = Pending()
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                    200, headers={'content-type':'text/event-stream'},stream=source))) as client:
                player = ChatCompletionPlayer(client, configure_thinking(MODEL_ADAPTERS['anthropic'],'high'),'key',stream=True,
                                              stream_first_timeout=1 if cancel else .025)
                task = asyncio.create_task(player.respond(GameState().view_for('A')))
                if cancel:
                    await asyncio.sleep(.02)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else PlayerError):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(source.closed)
                self.assertEqual(len(player._messages),1)


class MixedProtocolCliTests(unittest.TestCase):
    def test_native_claude_and_chat_glm_complete_bo_using_profiles_and_private_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.toml'
            path.write_text('[app]\nretry_delay_seconds=0\n[match]\nbest_of=3\n'
                            '[models.claude]\nadapter="anthropic"\napi_key="native-key"\n'
                            '[models.glm]\nadapter="glm"\napi_key="chat-key"\n'
                            '[players.red]\nprofile="claude"\n[players.blue]\nprofile="glm"', encoding='utf-8')
            phases = {'native': [], 'chat': []}

            def handler(request):
                body = json.loads(request.content)
                native = 'system' in body
                ctx = json.loads(body['messages'][-1]['content'])
                phases['native' if native else 'chat'].append(ctx['phase'])
                if ctx['phase'] == 'postmatch':
                    content = postmatch_payload('结束')
                else:
                    self.assertNotIn('PRIVATE-' + ('Blue' if native else 'Red'), json.dumps(body))
                    content = turn_payload([{'type':'MOVE','move':'Paper' if native else 'Rock'}],
                                           'PRIVATE-' + ('Red' if native else 'Blue'))
                response = native_response(json.dumps(content)) if native else {
                    'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content),'reasoning_content':'NATIVE-PRIVATE'}}]}
                return httpx.Response(200,json=response)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch('model_rps.cli.httpx.AsyncClient',return_value=client), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['--config',str(path),'--log-dir',str(root/'logs')]),0)
            self.assertEqual(phases,{'native':['game','game','postmatch'],'chat':['game','game','postmatch']})
            self.assertIn('赢得 BO3',out.getvalue())
            self.assertNotIn('NATIVE-PRIVATE',out.getvalue())
            run, = (root/'logs').iterdir()
            for file in run.rglob('*.log'):
                value = file.read_text(encoding='utf-8')
                self.assertNotIn('native-key',value)
                self.assertNotIn('chat-key',value)


if __name__ == '__main__':
    unittest.main()
